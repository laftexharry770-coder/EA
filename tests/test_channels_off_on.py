"""OFF skips exactly what a channel posted while it was OFF -- end to end
(fix round 2 of final review F28; the controller's ruling on the round-1
review).

The reviewer's sequence, on the real CLI and the real `watch` (listener,
poll, store; Telegram is the offline fake): watch runs and stops; while it
is stopped the channel, still ON, posts a 'close all' and a signal; it is
switched OFF; it posts while OFF; it is switched back ON; it posts again;
watch starts. Round 1 re-seeded at the switch-on, which skipped the posts
made while it was still ON too -- the 'close all' included, silently.
Now the next watch reads the channel in two ranges: up to the switch-off,
then from the switch-on.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import xaucopier.cli.channels as channels_module
import xaucopier.cli.main as cli_main
from fakes.telegram import FakeMessage
from watch_rig import (
    CHATTER,
    SIGNAL_TEXT,
    Rig,
    ScriptedWatchClient,
    _read,
    _until,
    _use_client,
    _write_config,
    rig,  # noqa: F401 -- the fixture
)
from xaucopier.cli import exit_codes

A, B = -1001111111111, -1002222222222
TWO_CHANNELS = f"""
[[channels]]
channel_id = {A}
name       = "First channel A"
parser     = "mmh"
ordinal    = 1
enabled    = true

[[channels]]
channel_id = {B}
name       = "Second channel B"
parser     = "mmh"
ordinal    = 2
enabled    = true
"""


def _signal(stop: int) -> str:
    """The fixture's signal with its own stop loss: each a different call."""
    assert "SL: 4470" in SIGNAL_TEXT
    return SIGNAL_TEXT.replace("SL: 4470", f"SL: {stop}")


def _watch(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, history: dict, script
) -> ScriptedWatchClient:
    client = ScriptedWatchClient(history={cid: list(posts) for cid, posts in history.items()},
                                 script=script)
    _use_client(rig, monkeypatch, client)
    assert cli_main.main(rig.argv()) == exit_codes.EXIT_LISTENER_STOPPED
    return client


def _channels(rig: Rig, monkeypatch: pytest.MonkeyPatch, *argv: str, at: datetime) -> int:
    monkeypatch.setattr(channels_module, "_utcnow", lambda: at)
    monkeypatch.setattr(channels_module, "session_in_use", lambda: False)
    return cli_main.main(
        ["channels", *argv, "--config-dir", str(rig.config_dir), "--db", str(rig.db_path)]
    )


def _stored(rig: Rig, channel_id: int) -> list[tuple[int, str]]:
    return _read(
        rig.db_path,
        "SELECT message_id, kind FROM messages WHERE channel_id = ? ORDER BY message_id",
        (channel_id,),
    )


@pytest.mark.parametrize("toggled", [True, False], ids=["switched off and on", "never off"])
def test_off_skips_exactly_what_was_posted_while_off(
    rig: Rig,  # noqa: F811 -- the fixture
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    toggled: bool,
) -> None:
    _write_config(rig.config_dir, TWO_CHANNELS)
    now = datetime.now(UTC)
    old = now - timedelta(hours=3)
    history = {A: [FakeMessage(id=10, message=CHATTER, date=old)],
               B: [FakeMessage(id=7000, message=CHATTER, date=old)]}

    # Run 1: B's signal 7001 is recorded; watch stops.
    async def run1(client: ScriptedWatchClient) -> None:
        await client.emit(B, FakeMessage(id=7001, message=_signal(4470), date=now - timedelta(minutes=50)))
        assert await _until(lambda: (7001, "signal") in _stored(rig, B))
        await asyncio.sleep(0.2)  # the poll moves B's pointer past it
        await client.disconnect()

    first = _watch(rig, monkeypatch, history, run1)
    history = {cid: list(posts) for cid, posts in first.history.items()}

    # watch is stopped; B is still ON: a 'close all' for 7001, and a signal.
    history[B] += [
        FakeMessage(id=7002, message="close all now", date=now - timedelta(minutes=40),
                    reply_to_msg_id=7001),
        FakeMessage(id=7003, message=_signal(4460), date=now - timedelta(minutes=39)),
    ]
    if toggled:
        assert _channels(rig, monkeypatch, "off", "2", at=now - timedelta(minutes=30)) == 0
    # Posted while OFF (or, in the control, while still ON).
    history[B] += [
        FakeMessage(id=7004, message=_signal(4450), date=now - timedelta(minutes=20)),
        FakeMessage(id=7005, message="close all now", date=now - timedelta(minutes=19),
                    reply_to_msg_id=7003),
    ]
    if toggled:
        assert _channels(rig, monkeypatch, "on", "2", at=now - timedelta(minutes=10)) == 0
    # Posted after it was switched back on, while watch was still stopped.
    history[B].append(FakeMessage(id=7006, message=_signal(4440), date=now - timedelta(minutes=5)))
    capsys.readouterr()

    # Run 2.
    async def run2(client: ScriptedWatchClient) -> None:
        await client.emit(B, FakeMessage(id=7007, message=_signal(4430), date=datetime.now(UTC)))
        assert await _until(lambda: (7007, "signal") in _stored(rig, B))
        await asyncio.sleep(0.2)
        await client.disconnect()

    _watch(rig, monkeypatch, history, run2)

    stored = _stored(rig, B)
    followups = _read(rig.db_path, "SELECT message_id, op FROM followups WHERE channel_id = ?", (B,))
    if toggled:
        # Up to the switch-off: read -- the 'close all' included. While
        # OFF: skipped. From the switch-on: read.
        assert stored == [(7001, "signal"), (7002, "followup"), (7003, "signal"),
                          (7006, "signal"), (7007, "signal")]
        assert followups == [(7002, "close_all")]
        window = capsys.readouterr().err
        assert "was switched off" in window and "not read" in window
    else:
        assert stored == [(7001, "signal"), (7002, "followup"), (7003, "signal"),
                          (7004, "signal"), (7005, "followup"), (7006, "signal"),
                          (7007, "signal")]
    assert _read(rig.db_path, "SELECT COUNT(*) FROM channel_off_periods") == [(0,)]

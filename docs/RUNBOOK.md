# xaucopier: how to run it

This version only **records** the gold signals posted in the Telegram channel
*MARKET MASTERY HUB / COMMUNITY*. It **does not place any trades**, and it
contains no code that could. Your MetaTrader accounts are not touched.

You do steps 1–3 once. After that, step 4 is how you start it, and step 5 is
how you check on it.

## Before you start

**Open PowerShell in the project folder.** In File Explorer, go to
`C:\Users\Herbert\Projects\xaucopier`, click the address bar, type
`powershell`, and press Enter. Every command below is typed in that window,
starting with `.\xaucopier`.

**Keep the Windows clock correct.** Open *Settings → Time & language → Date &
time* and turn on **Set time automatically**. If the PC's clock runs more
than a minute fast, the first time the recorder starts it can skip posts
from around that moment.

## 1. Get your own api_id and api_hash (once)

1. Go to <https://my.telegram.org> and log in with your phone number.
   Telegram sends the code to your Telegram app.
2. Click **API development tools**.
3. Fill in *App title* and *Short name* (for example `xaucopier`), choose
   *Desktop* as the platform, and click **Create application**.
4. Keep that page open. You need two things from it: **api_id** (a number)
   and **api_hash** (a long string of letters and numbers).

> **Never use an api_id or api_hash you found in a tutorial, a video, a
> forum or on GitHub.** Telegram bans those permanently, and the account
> using one can get locked out. Use only the pair from your own page, and
> never share your api_hash.

## 2. Log in: `.\xaucopier setup-login` (once)

Type `.\xaucopier setup-login` and press Enter. It asks for, in order:

1. **api_id** — type the number from step 1.
2. **api_hash** — paste it. Nothing appears while you type or paste; that is
   on purpose.
3. **Phone number** — your own, starting with `+` and the country code.
4. **The login code** — Telegram sends it **inside the Telegram app**, as a
   message from "Telegram". Type it in.
5. **Your 2FA password** — only if you have turned on two-step verification
   in Telegram. If you have not, just press Enter. Nothing appears while you
   type.

When it says *Login succeeded*, open Telegram on your phone and go to
**Settings → Devices**. You will see a new session called
**XAUCOPIER-TRADEBOX**. That is this program. **Do not terminate it** — if
you do, the recorder is logged out and stops, and you have to do this step
again. (If you ever want to cut the program off for good, terminating it is
exactly how.)

**What is kept, and what is not.** The program saves your api_id, your
api_hash and the login session, encrypted so that only your Windows user on
this PC can read them. It **never stores the login code, your 2FA password
or your phone number** — you type those once and they are forgotten.

> **The session file is a key to your whole Telegram account.** It is
> `config\secrets\session.dpapi`. It is encrypted so it only opens for your
> Windows user on this PC — but anyone who can use it can read your chats
> and send messages as you, without your code or password. Never copy it,
> email it, upload it, put it in a zip, or share it — not even with someone
> helping you. If you think it has leaked: in Telegram, **Settings → Devices
> → terminate XAUCOPIER-TRADEBOX**, then run step 2 again.

## 3. Find the channel: `.\xaucopier resolve-channels` (once)

Type `.\xaucopier resolve-channels`. It asks:

1. **Paste the channel's link or @username** — for example
   `https://t.me/somechannel`. If the channel is private and you have no
   link, **just press Enter**: it lists every channel you have joined.
2. If you pressed Enter: **type the number** next to *MARKET MASTERY HUB /
   COMMUNITY*.
3. It shows the channel it found. **Type `yes`** to save it.

It writes the channel's number into `config\channels.toml` and switches the
channel on. If `watch` is running, stop it first (step 4 says how).

## 4. Start recording: `.\xaucopier watch`

Type `.\xaucopier watch`. It prints which channel it is watching and then
keeps running. **Leave the window open** — closing it stops the recording.

- **To stop it:** click in the window and press **Ctrl+C**. Windows then
  asks `Terminate batch job (Y/N)?` — type **Y** and press Enter. (That
  question comes from Windows, not from xaucopier; answering Y is always
  safe.) Everything recorded so far is kept.
- **After a restart of the PC**, or whenever it has stopped: run
  `.\xaucopier watch` again. It catches up on the posts made while it was off.
- **Only one at a time.** Do not start a second `watch` in another window.

## 5. Check on it: `.\xaucopier status`

Open a **second** PowerShell window in the project folder (leave `watch`
running in the first) and type `.\xaucopier status`. It only reads; it
never changes anything, so you can run it as often as you like.

What to look at, from the top:

1. **ALERTS.** Ideally it says `ALERTS: none`. An alert is something the
   program could not safely decide by itself — for example a signal the
   poster deleted, or edited from BUY to SELL. Nothing is traded in this
   version, so an alert is never an emergency. `CRITICAL` ones are listed
   first. What to do is in **Clearing alerts** below.
2. **WATCH.** It should say `RUNNING`, with the last check of the channel a
   few seconds ago. `NOT HEALTHY` means watch is stuck, cannot read the
   channel, or its window was closed. If the watch window is still open,
   click in it, press **Ctrl+C** and answer **Y** first — a stuck watch
   still holds the login, so a second one would refuse to start (exit
   code 6). Then start it again with `.\xaucopier watch`. `NOT RUNNING` says
   when it stopped and why.
3. **MISSED BY TELEGRAM'S LIVE FEED** — the most important number. Telegram
   sometimes fails to deliver posts to programs like this one; watch checks
   the channel every 5 seconds and picks up anything the live feed dropped.
   *new posts* counts those rescues. A few a day is normal — it means the
   safety net is working. Dozens a day means Telegram is being unreliable;
   mention it.
4. **SHOULD STAY AT ZERO.** Every line there should be 0. A line marked
   `<-- look into this` means something went wrong — the first one means
   posts were **not** recorded. Send the whole status output.
5. **SIGNALS** and **CHANNELS** show what has been recorded, and when the
   newest post in the channel arrived.

### Clearing alerts: `.\xaucopier ack`

An alert stays at the top of `status` until you clear it, so none is
missed. Once you have dealt with one, clear it:

- `.\xaucopier ack` lists the alerts, each with a number after `#`.
- `.\xaucopier ack 7` clears alert number 7.
- `.\xaucopier ack --all` clears them all.

**Before you clear one, do what it needs:**

- **TELEGRAM_SESSION_DEAD** — Telegram has logged this program out. Run
  step 2 (`setup-login`) again, then `watch`. You do not need to clear this
  one: it clears itself as soon as `watch` reads the channel again.
- **TELEGRAM_API_ID_REJECTED** — Telegram will not accept your
  api_id/api_hash. Get a **new** pair (step 1), run step 2 again, then
  `watch`. This one also clears itself once `watch` reads the channel.
- **Any other alert** — copy its two lines and send them to whoever looks
  after xaucopier, then clear it.

## What the exit codes mean

When `watch` (or any command) stops, it prints an exit code:

| Code | What it means | What to do |
|---|---|---|
| 0 | Finished normally. | Nothing. |
| 1 | The command could not do what you asked. | Read the message above it. |
| 2 | The command was typed wrong. | Check the spelling; try `.\xaucopier --help`. |
| 3 | Telegram refuses this program's login: it was logged out (for example from Telegram → Settings → Devices), **a second copy of the session was used somewhere else at the same time**, or the account is deactivated, banned or frozen. | Do **not** just restart. Make sure nothing else uses this login, run step 2 again, then `watch`. A banned account cannot be fixed by any program. |
| 4 | `watch` stopped unexpectedly (for example the internet dropped). | Safe to start `watch` again. |
| 5 | Something in the setup is wrong (a config file, the channel number, the data folder, the session file) — or Telegram rejected your api_id/api_hash. | Read the message. Restarting will not help until it is fixed. For a rejected api_id: get a **new** pair (step 1), then run step 2 again. |
| 6 | `watch` is already running on this PC. | Find its window. Never run two. |
| 130 | You pressed Ctrl+C (and answered **Y**). | Nothing — start it again when you want. |

## Only if asked: `.\xaucopier export-fixtures`

This copies every recorded message into the project's test folder so a
developer can check the program reads real posts correctly. Run it only when
someone working on xaucopier asks you to. It never changes or deletes
anything.

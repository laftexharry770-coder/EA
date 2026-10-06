# xaucopier: how to run it

This version only **records** the gold signals posted in the Telegram channel
*MARKET MASTERY HUB / COMMUNITY*. It **does not place any trades**, and it
contains no code that could. Your MetaTrader accounts are not touched.
You can follow other channels as well, or instead: see **Choosing which
channels to follow** below.

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
5. **Your two-step verification password** — this step **only appears if
   two-step verification is turned on** for your Telegram account. If it
   does not appear, there is nothing to do. If it does appear, you need that
   password: it cannot be left blank. Nothing appears while you type. If you
   have forgotten it, reset it in Telegram → *Settings → Privacy and
   Security → Two-Step Verification*, then start step 2 again.

When it says *Login succeeded*, open Telegram on your phone and go to
**Settings → Devices**. You will see a new session called
**XAUCOPIER-TRADEBOX**. That is this program. **Do not terminate it** — if
you do, the recorder is logged out and stops, and you have to do this step
again. (If you ever want to cut the program off for good, terminating it is
exactly how.)

**If it fails after you typed the code.** If the message says *IMPORTANT:
Telegram had already accepted this login*, a XAUCOPIER-TRADEBOX session was
made that this program has no copy of. Open **Settings → Devices**, end that
extra XAUCOPIER-TRADEBOX session, and only then run step 2 again.

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
   link, **just press Enter**: it lists every channel and group you have
   joined, each marked *(channel)* or *(group)*.
2. If you pressed Enter: **type the number** next to *MARKET MASTERY HUB /
   COMMUNITY*.
3. It shows what it found — a channel or a group — and its Telegram id.
   **Type `yes`** to save it.

It writes the channel's **Telegram id** (a long number starting with -100)
into `config\channels.toml` and switches the channel on. It then says which
**channel number** it is in `.\xaucopier channels` — the short number you
use with `channels on` and `channels off`. If `watch` is running, stop it
first (step 4 says how).

**A group is asked about separately.** In a *channel* only its admins post.
In a *group* anyone in it can post, and **every member's message would be
read as a trading instruction**. So if what you picked is a group, it warns
you and asks once more; type `yes` only if that group really is where the
signals come from, and tell whoever looks after xaucopier that your source
is a group. (`--yes` does not answer this question; only `--allow-group`
does.)

## 4. Start recording: `.\xaucopier watch`

Type `.\xaucopier watch`. It prints which channel it is watching and then
keeps running. **Leave the window open** — closing it stops the recording.

- **To stop it:** click in the window and press **Ctrl+C**. Windows then
  asks `Terminate batch job (Y/N)?` — type **Y** and press Enter. (That
  question comes from Windows, not from xaucopier; answering Y is always
  safe.) Everything recorded so far is kept.
- **After a restart of the PC**, or whenever it has stopped: run
  `.\xaucopier watch` again. It catches up on the posts made while it was
  stopped (not on what a channel posted while it was switched off — see
  **Choosing which channels to follow**).
- **Only one at a time.** Do not start a second `watch` in another window.
- **If it says *Could not reach Telegram within 60 seconds*,** this PC is
  not online (or Telegram is not answering). It stops by itself (exit code
  4). Start it again once the internet works.
- **When it stops, it says why** in plain words, then
  `watch stopped (exit code N: ...)`. The technical details go to the file
  `data\watch.log`; send that file if someone asks for it.

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
   sometimes fails to deliver posts to programs like this one while `watch`
   is running; watch checks the channel every 5 seconds and picks up
   anything the live feed dropped. *new posts* counts those rescues. A few
   a day is normal — it means the safety net is working. Dozens a day means
   Telegram is being unreliable; mention it. Posts made while `watch` was
   **stopped** are not counted here: they are *caught up after a stop*, and
   counted under **FOR INFORMATION** instead.
4. **SHOULD STAY AT ZERO.** Every line there should be 0. A line marked
   `<-- look into this` means something went wrong — the first one means
   posts were **not** recorded. Send the whole status output.
5. **FOR INFORMATION -- NOTHING TO DO.** Posts made while watch was
   stopped and caught up after it started, and copies of a signal or an
   instruction the program had already recorded (the same call relayed by
   another channel, forwarded, or posted twice). A copy is recorded but
   never traded or acted on twice. These numbers are never a problem.
6. **SIGNALS** and **CHANNELS** show what has been recorded, and when the
   newest post in the channel arrived.

If `status` (or any command) says the database *was written by a newer
version of xaucopier*, use that newer version: this one does not read or
change it.

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
- **TEMPLATE_CHANGED_SUSPECTED** — 3 messages in a row looked like trade
  signals but could not be read. Either the channel has changed how it
  writes its signals, or it writes some calls in a layout this program does
  not read (a channel added although it never used the MARKET MASTERY HUB
  layout, or a group whose members post calls of their own). Signals
  written that way are **not traded**. Look at the channel, then send the
  alert and two or three of those messages to whoever looks after
  xaucopier.
- **ENTRY_SHAPED_INSTRUCTION_NOT_QUEUED** — a message looked like a trade
  signal the program cannot read, and also like an instruction for a trade
  already open (for example "move SL to 4470"). Neither was acted on. Look
  at the message: if it was meant as an instruction, **apply it by hand**.
- **EDIT_COMPLETED_SIGNAL** — a post that was not a readable signal was
  edited into one within 2 minutes of being posted. It is now a signal,
  recorded like any other. Nothing to do, unless the poster did not mean it
  as a call: then check the channel.
- **SIGNAL_REPOST_PROMOTED** — a signal's own post was deleted or withdrawn
  while the channel still showed the same call in another post. That other
  post is now the signal. Nothing to do, unless the poster meant to cancel
  the call altogether: then check the channel.
- **CHANNEL_OFF_ENTRIES_SKIPPED** — `watch` started with a channel switched
  off that still had signals waiting to be traded; they were cancelled, not
  traded. That is what switching a channel off does (see below). Nothing to
  do.
- **Any other alert** — copy its two lines and send them to whoever looks
  after xaucopier, then clear it.

## Choosing which channels to follow: `.\xaucopier channels`

Signals can come from more than one Telegram channel, and you choose which.

- **See them:** `.\xaucopier channels` lists every channel with its
  **number**, whether it is **ON** or **OFF**, when its last message was
  recorded, and how many signals it has produced. It only reads, so run it
  whenever you like.
- **Switch one off:** `.\xaucopier channels off 2` stops reading channel
  number 2 **completely**: its new posts, and also the edits, deletions and
  follow-ups (like "close all") of the signals it already posted. It tells
  you how many of its signals are still waiting to be traded, how many are
  too old to be traded (a signal posted more than 2 minutes ago never is),
  and how many were already traded. The waiting ones are cancelled the
  next time `watch` starts; a trade already open keeps its stop loss and
  take profit. **Whatever it posts while it is off is ignored for good** —
  it is not read later, not even after you switch it back on. Nothing
  already recorded is deleted.
- **Switch one on:** `.\xaucopier channels on 2`. The next `watch` then
  reads the channel in two parts. First, whatever it posted **before you
  switched it off** that `watch` had not read yet — for example a "close
  all" posted while `watch` was stopped — is read, as usual. Then the posts
  it made while it was off are skipped, and everything from the moment you
  switched it on is read. (Give or take a minute at either end: your PC's
  clock and Telegram's are never exactly the same, so a post made within a
  minute of a switch is read rather than lost.) Switching it off and on
  again several times before `watch` runs is fine: each time it was off is
  skipped, and only that.
- **If it says the time it was switched off could not be saved** (the
  database was busy or could not be used), the channel is still switched
  off. But when you switch it back on, everything it posted since `watch`
  last read it is skipped, not only what it posted while it was off.
- **Add a new channel:** first **join it in Telegram** on your phone. Then
  stop `watch` (click in its window, press **Ctrl+C** and answer **Y**) and
  type `.\xaucopier channels add`. Paste the channel's link, or just press
  Enter and type the number next to it in the list of channels and groups
  you have joined. Then type `yes`. It is added **switched on**, with the
  next free number. A channel that is already in the list is never added
  twice. A channel you removed and add again — with `channels add`, or by
  looking it up again with `resolve-channels` — is read from now on, not
  from where it stopped; it says so.
- **A group needs a second `yes`.** The list marks each entry *(channel)*
  or *(group)*. In a group anyone can post, and every member's message would
  be read as a trading instruction, so adding a group warns you and asks
  again. Say `yes` only if the signals really are posted there.

The number to type is the one `.\xaucopier channels` shows next to the
channel — a short number like 2, not the long Telegram id. It never
changes, even when you add channels or switch them on and off.

**A change takes effect the next time `watch` starts.** `watch` reads the
list only when it starts. After switching a channel on or off, stop `watch`
(**Ctrl+C**, then **Y**) and start it again with `.\xaucopier watch`. Adding a
channel needs `watch` stopped anyway: only one program may use your Telegram
login at a time.

**If every channel is OFF, `watch` will not start** (exit code 5). Switch one
back on first. A channel marked *NOT LOOKED UP YET* cannot be switched on
until you run `.\xaucopier resolve-channels` for it (step 3).

> **A new channel may be recorded but never traded.** So far the program can
> only read signals written the way *MARKET MASTERY HUB / COMMUNITY* writes
> them. From a channel that writes its signals differently, a signal is
> recorded but **not traded**, until someone builds a reader for that
> channel, which needs 5–10 of its real messages. A message that reads like
> an instruction for a trade already open — for example "close all" or
> "move SL to entry" — is still recorded as an instruction, whatever its
> layout. When 3 messages in a row look like signals but cannot be read,
> `status` shows a **TEMPLATE_CHANGED_SUSPECTED** alert. If you add such a
> channel, tell whoever looks after xaucopier.

## What the exit codes mean

When a command cannot do what you asked, its last line is its exit code,
for example `(exit code 6)`. When `watch` stops, it says
`watch stopped (exit code N: ...)`. A command that did what you asked
prints no code.

| Code | What it means | What to do |
|---|---|---|
| 0 | Finished normally. | Nothing. |
| 1 | The command could not do what you asked (for example Telegram asked us to wait, or another command changed `channels.toml` at the same moment). | Read the message above it. Where it says to run the command again, do. |
| 2 | The command was typed wrong. | Check the spelling; try `.\xaucopier --help`. |
| 3 | Telegram refuses this program's login: it was logged out (for example from Telegram → Settings → Devices), **a second copy of the session was used somewhere else at the same time**, or the account is deactivated, banned or frozen. The watch window may name the reason *AuthKeyNotFound* ("auth key not found"): that is a dead login too. | Do **not** just restart. Make sure nothing else uses this login, run step 2 again, then `watch`. A banned account cannot be fixed by any program. |
| 4 | `watch` stopped unexpectedly (for example the internet dropped, or it could not reach Telegram within 60 seconds of starting). | Safe to start `watch` again. |
| 5 | Something in the setup is wrong (a config file, the channel number, the data folder, the session file) — or Telegram rejected your api_id/api_hash. | Read the message. Restarting will not help until it is fixed. For a rejected api_id: get a **new** pair (step 1), then run step 2 again. |
| 6 | `watch` is already running on this PC — or `channels add` or `resolve-channels` is, which use the Telegram login too. | Find its window. Never run two. To add a channel, stop `watch` first. If it says it is probably running in a window opened as administrator, find that window and stop it there. |
| 130 | You pressed Ctrl+C (and answered **Y**). | Nothing — start it again when you want. |

## Only if asked: `.\xaucopier export-fixtures`

This copies every recorded message into the project's test folder so a
developer can check the program reads real posts correctly. Run it only when
someone working on xaucopier asks you to. It never changes or deletes
anything.

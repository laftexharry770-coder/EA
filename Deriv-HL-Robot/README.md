# Deriv HL Robot

A rebuild of the "HL robot" in the @bbfxtraders video "Making $2000 for iPhone Duo". It buys a $5 Higher and a $5 Lower on Volatility 100 Index on the same tick, 5 ticks each, and buys the next pair as soon as both settle.

## Files

| File | What it is |
| --- | --- |
| `HL_Robot_Web.html` | **Use this one.** A web app that places the Higher and the Lower together on the same tick, through Deriv's own trading API. |
| `HL_Robot.xml` | The bot as shown in the video, for bot.deriv.com. On the live site it only opens the Higher (see below). |
| `HL_Robot_Higher_tab.xml` + `HL_Robot_Lower_tab.xml` | The bot split in two for bot.deriv.com, one side per browser tab. |

## Web app

### What you need

- A Deriv **API token** with the **Read** and **Trade** scopes. Create it in Deriv under **Account settings → API token**. Don't give it the Payments or Admin scopes; the app doesn't need them.
- Chrome, Edge or Firefox on a computer. Open `HL_Robot_Web.html` by double-clicking it.

### Running it

1. In Deriv, switch to your **Demo** account, then create the token.
2. Paste the token into **API token** and press **Connect**. The top right shows your account, balance, and a **Demo** or **Real** badge.
3. The settings come filled in with the video's values. The two boxes under them show Deriv's live payout for each side. If Deriv refuses a setting, its reason shows there.
4. Press **Run**. On the next tick the app sends both buy orders to Deriv in the same instant, so both contracts start on the same entry tick.
5. Open **Transactions**. Each pair shows Lower above Higher, as in the video, with **Same entry tick** once Deriv reports both entry spots.
6. When both contracts in a pair settle, the next pair is bought on the next tick. **Stop** stops new pairs; contracts already open run to the end.

### Settings

| Setting | Default | Sent to Deriv as |
| --- | --- | --- |
| Market | Volatility 100 Index | `R_100` |
| Stake per trade | 5 | $5 on each side |
| Higher duration (ticks) | 5 | Higher: 5 ticks |
| Higher barrier below (offset) | 1 | Higher barrier `-1` |
| Lower duration (ticks) | 5 | Lower: 5 ticks |
| Lower barrier above (offset) | 1 | Lower barrier `+1` |
| When profit reaches / When loss reaches | Off | Stops the bot once total profit or loss for the session reaches that amount |

Each side here uses its own variables, as the names in the video say. The Deriv Bot file can only give both sides one barrier (see below).

### Good to know

- The token is only sent to Deriv. If you tick **Remember the token on this device**, it is saved in this browser's storage; otherwise it's gone when you close the page.
- The app uses Deriv's public app ID, 1089. If Deriv ever refuses it, register your own free app ID on Deriv's API site and enter it under **Connection settings**.
- If the connection drops, the bot stops, reconnects by itself, and follows any open contracts to the end. Press **Run** again to carry on.
- If Deriv refuses three pairs in a row, the bot stops. The **Journal** tab shows Deriv's exact message for every refused order.
- I tested the app in Chrome against a stand-in for Deriv's API that uses the same messages, since Deriv's servers couldn't be reached from where it was built. Run it on Demo first.

## Deriv Bot files (bot.deriv.com)

### Settings, as shown in the video

**1. Trade parameters**

| Setting | Value |
| --- | --- |
| Market | Derived → Continuous Indices → Volatility 100 Index |
| Trade type | Up/Down → Higher/Lower |
| Contract type | Both |
| Default candle interval | 1 minute |
| Restart buy/sell on error | Off |
| Restart last trade on error | Off |

**Run once at start**

| Variable | Value |
| --- | --- |
| Stake per trade | 5 (the saved bot had 1; it was changed to 5 before running) |
| Higher duration (ticks) | 5 |
| Higher barrier below (offset) | 1 |
| Lower duration (ticks) | 5 |
| Lower barrier above (offset) | 1 |

**Trade options:** Duration Ticks = `Higher duration (ticks)`, Stake USD = `Stake per trade`.

### Not shown in the video

The video never scrolls to these, so they are built from what its Transactions list shows: a Higher and then a Lower at $5 each, on the same entry tick, both held for 5 ticks, then the next pair.

| Block | Built as |
| --- | --- |
| Trade options barrier | `Offset -` `Higher barrier below (offset)`, so -1. One trade options block is shared by both purchases, so both sides get this barrier. |
| Purchase conditions | Purchase Higher, then Purchase Lower. Each sits in its own always-true `if`, because Deriv Bot rejects a file with two Purchase blocks stacked directly. |
| Sell conditions | Empty. Contracts run to expiry. |
| Restart trading conditions | Trade again. The bot runs until you press Stop. |

### Why `HL_Robot.xml` only opens the Higher

Deriv Bot lets one Purchase through per pass. Once the first is bought, any other Purchase in the same pass is skipped (`Purchase.js` in Deriv Bot's source: "Prevent calling purchase twice"). Tested on bot.deriv.com, only Higher rows appear. No block arrangement gets around this, which is why the web app exists.

The two-tab pair does put both sides on bot.deriv.com at the same time:

1. Open bot.deriv.com in two tabs of the same browser, on the same account.
2. Import `HL_Robot_Higher_tab.xml` in one tab and `HL_Robot_Lower_tab.xml` in the other, and press Run in both.
3. Both tabs buy only in the first 2 seconds of every 20-second slot of your device clock, so they buy on the same tick. That gives one pair every 20 seconds.

The Higher tab uses `Offset -` `Higher barrier below (offset)` and `Higher duration (ticks)`; the Lower tab uses `Offset +` `Lower barrier above (offset)` and `Lower duration (ticks)`.

## Before using real money

The results in the video don't fit how Deriv prices these contracts. Each winning side paid about 7.8× its stake. At that payout, Deriv is pricing each side at roughly a 1-in-8 chance. Yet one of the two sides won in 8 of the 9 pairs. With two 1-in-8 contracts, both sides should lose in about three pairs out of four. Each of the four winning pairs whose spots are readable in the video moved exactly 0.18 over its 5 ticks.

With the web app's barriers (Higher 1 point below the price, Lower 1 point above), both sides win whenever the price stays inside that 2-point band. Because that is the likely outcome, Deriv pays only a small profit on each side, and one move past the band loses a full $5 stake. Check the payouts the app shows, run it on Demo for a few hundred pairs, and look at **Total profit/loss** before trusting it with a real balance.

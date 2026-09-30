# Deriv HL Robot

A Deriv Bot (bot.deriv.com) rebuild of the "HL robot" in the @bbfxtraders video "Making $2000 for iPhone Duo". It buys a Higher and a Lower on Volatility 100 Index, 5 ticks each, and trades again as soon as they settle.

## Files

| File | What it is |
| --- | --- |
| `HL_Robot.xml` | The bot as shown in the video. One file, buys Higher then Lower in the same purchase pass. |
| `HL_Robot_Higher_tab.xml` + `HL_Robot_Lower_tab.xml` | The same bot split in two, one side per browser tab. Use these if `HL_Robot.xml` only opens Higher trades (see below). |

## Settings, as shown in the video

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

In the two-tab pair, each tab uses its own side's variables: the Higher tab uses `Offset -` `Higher barrier below (offset)` and `Higher duration (ticks)`, and the Lower tab uses `Offset +` `Lower barrier above (offset)` and `Lower duration (ticks)`.

## Loading it

1. Open bot.deriv.com and switch to your **Demo** account first.
2. Go to **Bot Builder**, press **Import**, and choose `HL_Robot.xml`.
3. Press **Run**, then open the **Transactions** tab.

## Does it open both sides at once?

Every pair should show as two rows, Lower on top of Higher, with the **same entry spot**.

Deriv Bot's published source only lets one Purchase through per pass. The engine blocks any purchase once one has been bought (`Purchase.js`: "Prevent calling purchase twice"). If your Deriv Bot works that way, you will see only Higher rows. The single file can't get around that, so use the two-tab pair:

1. Open bot.deriv.com in two tabs of the same browser, on the same account.
2. Import `HL_Robot_Higher_tab.xml` in one tab and `HL_Robot_Lower_tab.xml` in the other, and press Run in both.
3. Both tabs buy only in the first 2 seconds of every 20-second slot of your device clock, so they buy on the same tick. That gives one pair every 20 seconds. The contract lasts about 12 seconds, so it has settled before the next slot.

## Before using real money

The results in the video don't fit how Deriv prices these contracts. Each winning side paid about 7.8× its stake. At that payout, Deriv is pricing each side at roughly a 1-in-8 chance. Yet one of the two sides won in 8 of the 9 pairs. With two 1-in-8 contracts, both sides should lose in about three pairs out of four. Each of the four winning pairs whose spots are readable in the video moved exactly 0.18 over its 5 ticks.

A Higher and a Lower at the same barrier can't both win, and Deriv takes a margin on each contract. Run it on Demo for a few hundred pairs and look at **Total profit/loss** before trusting it with a real balance.

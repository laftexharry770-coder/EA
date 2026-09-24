# EA

MetaTrader 5 Expert Advisors, one folder per project.

| Project | Market | What it trades |
| --- | --- | --- |
| [XAUUSD-Fractal-EMA-Scalper](XAUUSD-Fractal-EMA-Scalper/) | XAUUSD, 1 minute | Williams Fractals (periods 2) with EMA 20/50/100 pullbacks. Stop past the 50 EMA, or the 100 EMA if the 50 was crossed. Target 1.5 × risk. |

## Installing an EA

1. In MetaTrader 5, open **File → Open Data Folder**, then go to `MQL5/Experts`.
2. Copy the project's `.mq5` file there.
3. Open it in MetaEditor and press **F7** to compile.
4. Drag the EA onto a chart, or test it in the Strategy Tester first.

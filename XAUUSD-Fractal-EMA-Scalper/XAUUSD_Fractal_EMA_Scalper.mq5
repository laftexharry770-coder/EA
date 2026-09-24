//+------------------------------------------------------------------+
//|                                  XAUUSD_Fractal_EMA_Scalper.mq5  |
//|        Williams Fractals + EMA 20/50/100 pullback scalper        |
//+------------------------------------------------------------------+
//
// The strategy from "EASY Scalping Strategy For Day Trading (High Win Rate
// Strategy)", rule for rule:
//
//  Chart  : 1 minute. Williams Fractals with Periods = 2 (TradingView's
//           algorithm), colours flipped so the triangle UNDER a candle is
//           GREEN and the triangle OVER a candle is RED. Three moving averages
//           on the close, lengths 20 / 50 / 100 ("Three Moving Averages" by
//           AdventTrading, which plots EMAs), coloured green, yellow and red.
//
//  Long   : only when the 20 EMA is above the 50 EMA and the 50 EMA is above
//           the 100 EMA. If they are crossing each other, no trade.
//           Price pulls back under the 20 EMA (a wick through it is enough),
//           then a green arrow appears -> buy.
//           Stop right below the 50 EMA. If price kept falling and crossed
//           the 50 EMA before the green arrow, stop right below the 100 EMA.
//           Target = 1.5 x the risk.
//           If price closes below the 100 EMA, disregard the next green arrow.
//
//  Short  : the complete opposite: 100 EMA on top, 50 in the middle, 20 at
//           the bottom; price pulls back above the 20 EMA; red arrow -> sell;
//           stop right above the 50 EMA (or the 100 EMA if price crossed the
//           50); target 1.5 x the risk; a close above the 100 EMA voids the
//           next red arrow.
//
//  Manage : trust the strategy and stay in the trade: stop and target only.
//           Each entry opens InpPositions positions with the same stop and
//           target; in the risk modes they share the entry's risk.
//
//  Costs  : the one-minute chart gives a lot of signals, so use a broker with
//           very low fees. (InpMaxSpread can skip wide-spread entries; it is
//           not in the video, so it is off by default.)
//
// The stop sits on the first price past the EMA, where the video drags it.
// Sell stops trigger on the Ask, so the spread is added to them: the chart
// (Bid) price then has to trade right above the EMA to stop the trade out.
//
// On the chart the fractals are drawn as in the screen recording: the video's
// green arrow (under the candle, the buy signal) is a cyan triangle pointing
// up, and the red arrow (over the candle, the sell signal) is a red triangle
// pointing down. Only the look changed; the signals are the same.
//
// How the arrow is traded: a Williams fractal only exists once the n candles
// after it have closed. The EA reads closed candles only, so when the green
// (or red) arrow appears on the chart it buys (or sells) at market on the
// next candle, the same moment the video places its long/short position.
//+------------------------------------------------------------------+
#property copyright   "Sentinal"
#property version     "1.00"
#property description "XAUUSD 1-minute scalper: Williams Fractals (2) + EMA 20/50/100 pullbacks."
#property description "Buy the cyan fractal after a pullback under the 20 EMA in a 20>50>100 stack,"
#property description "sell the red fractal after a pullback over the 20 EMA in a 100>50>20 stack."
#property description "Stop beyond the 50 EMA (100 EMA if the 50 was crossed), target 1.5 x risk."

#include <Trade\Trade.mqh>

//--- how each trade is sized
enum ENUM_LOT_MODE
  {
   LOT_MODE_FIXED        = 0,  // Fixed lots
   LOT_MODE_RISK_PERCENT = 1,  // Risk a % of the balance
   LOT_MODE_RISK_MONEY   = 2   // Risk a fixed amount of money
  };

//--- inputs
input group "Strategy (as in the video)"
input ENUM_TIMEFRAMES InpTimeframe      = PERIOD_M1;  // Signal timeframe (video: 1 minute)
input int             InpFractalPeriods = 2;          // Williams Fractals periods (video: 2)
input int             InpEmaFast        = 20;         // EMA 1 length (video: 20)
input int             InpEmaMid         = 50;         // EMA 2 length (video: 50)
input int             InpEmaSlow        = 100;        // EMA 3 length (video: 100)
input double          InpRewardRisk     = 1.5;        // Target as a multiple of the risk (video: 1.5)
input bool            InpTradeLongs     = true;       // Take buy trades
input bool            InpTradeShorts    = true;       // Take sell trades

input group "Stop loss"
input double          InpStopBuffer     = 0.0;        // Extra distance past the EMA for the stop, in price (video: 0, right at the line)
input bool            InpSpreadOnSellSL = true;       // Measure sell stops on the chart price like the video (adds the spread)

input group "Position size"
input int             InpPositions      = 3;          // Positions opened on each entry (1-100)
input ENUM_LOT_MODE   InpLotMode        = LOT_MODE_RISK_PERCENT; // Sizing mode
input double          InpFixedLots      = 0.01;       // Lots for each position (fixed mode)
input double          InpRiskPercent    = 1.0;        // Balance risked per entry, shared by its positions, %
input double          InpRiskMoney      = 50.0;       // Money risked per entry, shared by its positions
input double          InpCommissionLot  = 0.0;        // Round-turn commission per 1 lot, account currency
input double          InpMaxLots        = 5.0;        // Largest total size of one entry, lots
input bool            InpMinLotFallback = false;      // Trade the minimum lot when the risk is too small for it

input group "Execution and costs"
input ulong           InpMagic          = 20260924;   // Magic number
input double          InpSlippage       = 0.30;       // Maximum slippage, in price (0.30 = 30 cents)
input bool            InpOneEntry       = true;       // One entry at a time: stay in it until stop or target
input bool            InpRetryInBar     = true;       // If the entry is blocked, keep trying until the candle closes

input group "Optional filters (not in the video, off by default)"
input double          InpMaxSpread      = 0.0;        // Widest spread allowed at entry, in price (0 = off)
input double          InpMinStopDist    = 0.0;        // Skip if entry-to-stop is smaller than this, in price (0 = off)
input double          InpMaxStopDist    = 0.0;        // Skip if entry-to-stop is larger than this, in price (0 = off)
input bool            InpUseHours       = false;      // Only open trades between the hours below
input int             InpStartHour      = 1;          // First trading hour, server time (0-23)
input int             InpEndHour        = 22;         // Last trading hour, server time (0-23)

input group "Chart (as in the video)"
input bool            InpShowEMAs       = true;       // Draw the EMAs: 20 green, 50 yellow, 100 red
input bool            InpDrawArrows     = true;       // Draw the fractals: cyan triangle up under the candle, red triangle down over it
input bool            InpShowPanel      = true;       // Show the status panel
input int             InpLineBars       = 600;        // Candles of EMA line kept on the chart
input int             InpMaxArrows      = 500;        // Most arrows kept on the chart

#define EA_NAME      "XAUUSD Fractal EMA Scalper"
#define WARMUP_BARS  600      // closed candles replayed at start-up to rebuild the setup state
#define MAX_FAILS    5        // failed order sends allowed per entry before giving up
#define MAX_POSITIONS 100     // most positions one entry may open

//--- the colours picked in the video (TradingView palette)
const color CLR_GREEN  = C'76,175,80';    // 20 EMA
const color CLR_YELLOW = C'255,235,59';   // 50 EMA
const color CLR_RED    = C'244,67,54';    // 100 EMA
//--- fractal colours from the screen recording (TradingView palette)
const color CLR_FRACTAL_UNDER = C'0,188,212';  // cyan triangle under the candle (buy side)
const color CLR_FRACTAL_OVER  = C'242,54,69';  // red triangle over the candle (sell side)

//--- setup state for one side (long or short)
struct SideState
  {
   bool              pullback;   // a wick went through the 20 EMA while the EMAs were stacked
   bool              deep;       // it went through the 50 EMA too -> stop past the 100 EMA
   bool              skipNext;   // a candle closed beyond the 100 EMA -> disregard the next arrow
  };

//--- an arrow that qualified, waiting to be entered on the next candle
struct PendingSignal
  {
   bool              active;
   int               direction;  // +1 buy, -1 sell
   bool              deep;       // stop hangs off the 100 EMA instead of the 50 EMA
   double            stopEma;    // EMA value the stop is measured from
   datetime          arrowTime;  // candle the arrow sits under / over
   datetime          validBar;   // the entry is only taken inside this candle
   int               count;      // positions this entry opens (set on the first send)
   int               opened;     // positions opened so far
   double            lotsEach;   // size of each position
   double            sl;         // stop shared by every position of the entry
   int               fails;      // failed order sends for this entry
   string            holdKey;    // last reason the entry was held back (logged once)
  };

CTrade          g_trade;
ENUM_TIMEFRAMES g_tf         = PERIOD_M1;
int             g_hFast      = INVALID_HANDLE;
int             g_hMid       = INVALID_HANDLE;
int             g_hSlow      = INVALID_HANDLE;
bool            g_ready      = false;   // setup state rebuilt from history
datetime        g_barTime    = 0;       // open time of the forming candle already handled
datetime        g_lastClosed = 0;       // open time of the newest closed candle already processed
SideState       g_long;
SideState       g_short;
PendingSignal   g_signal;
bool            g_bullStack  = false;   // EMA order on the last closed candle, for the panel
bool            g_bearStack  = false;
bool            g_draw       = true;    // false in optimisation / non-visual tests
string          g_prefix     = "";
string          g_arrows[];             // ring of arrow object names
int             g_arrowNext  = 0;
string          g_lines[];              // ring of EMA line segment names
int             g_lineNext   = 0;
string          g_lastAction = "";
datetime        g_panelTime  = 0;

//+------------------------------------------------------------------+
//| Small helpers                                                    |
//+------------------------------------------------------------------+
int IMin(const int a, const int b) { return (a < b) ? a : b; }

double TickSize()
  {
   const double ts = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   return (ts > 0.0) ? ts : _Point;
  }

//--- prices on the symbol's tick grid: stops round away from the entry
double PriceDown(const double price)  { const double ts = TickSize(); return NormalizeDouble(MathFloor(price / ts + 1e-8) * ts, _Digits); }
double PriceUp(const double price)    { const double ts = TickSize(); return NormalizeDouble(MathCeil(price / ts - 1e-8) * ts, _Digits); }
double PriceRound(const double price) { const double ts = TickSize(); return NormalizeDouble(MathRound(price / ts) * ts, _Digits); }

string Px(const double price) { return DoubleToString(price, _Digits); }
string YesNo(const bool v)    { return v ? "yes" : "no"; }

string TfName(const ENUM_TIMEFRAMES tf)
  {
   const string s = EnumToString(tf);
   return (StringFind(s, "PERIOD_") == 0) ? StringSubstr(s, 7) : s;
  }

int VolumeDigits(const double step)
  {
   int    d = 0;
   double v = step;
   while(d < 8 && MathAbs(v - MathRound(v)) > 1e-8)
     {
      v *= 10.0;
      d++;
     }
   return d;
  }

void ResetSide(SideState &side)
  {
   side.pullback = false;
   side.deep     = false;
   side.skipNext = false;
  }

void ClearSignal()
  {
   g_signal.active    = false;
   g_signal.direction = 0;
   g_signal.deep      = false;
   g_signal.stopEma   = 0.0;
   g_signal.arrowTime = 0;
   g_signal.validBar  = 0;
   g_signal.count     = 0;
   g_signal.opened    = 0;
   g_signal.lotsEach  = 0.0;
   g_signal.sl        = 0.0;
   g_signal.fails     = 0;
   g_signal.holdKey   = "";
  }

void Note(const string text)
  {
   g_lastAction = TimeToString(TimeCurrent(), TIME_MINUTES) + "  " + text;
   Print(EA_NAME, ": ", text);
   UpdatePanel(true);
  }

//+------------------------------------------------------------------+
//| TradingView "Williams Fractals", ported line for line.           |
//| r[] is a series array (r[0] = forming candle). c is the candle   |
//| the arrow belongs to; it is confirmed once the n candles after   |
//| it have closed, so r[c-1]..r[c-n] are the candles after it and   |
//| r[c+1]..r[c+n+4] the candles before it. Like TradingView, up to  |
//| four equal highs / lows are allowed on the left side.            |
//+------------------------------------------------------------------+
//--- green arrow (TradingView "downFractal"): a swing low, drawn under the candle
bool IsGreenFractal(const MqlRates &r[], const int c, const int n)
  {
   const double v = r[c].low;
   bool after = true;
   bool b0 = true, b1 = true, b2 = true, b3 = true, b4 = true;
   for(int i = 1; i <= n; i++)
     {
      after = after && (r[c - i].low > v);
      b0 = b0 && (r[c + i].low > v);
      b1 = b1 && (r[c + 1].low >= v && r[c + i + 1].low > v);
      b2 = b2 && (r[c + 1].low >= v && r[c + 2].low >= v && r[c + i + 2].low > v);
      b3 = b3 && (r[c + 1].low >= v && r[c + 2].low >= v && r[c + 3].low >= v && r[c + i + 3].low > v);
      b4 = b4 && (r[c + 1].low >= v && r[c + 2].low >= v && r[c + 3].low >= v && r[c + 4].low >= v && r[c + i + 4].low > v);
     }
   return after && (b0 || b1 || b2 || b3 || b4);
  }

//--- red arrow (TradingView "upFractal"): a swing high, drawn over the candle
bool IsRedFractal(const MqlRates &r[], const int c, const int n)
  {
   const double v = r[c].high;
   bool after = true;
   bool b0 = true, b1 = true, b2 = true, b3 = true, b4 = true;
   for(int i = 1; i <= n; i++)
     {
      after = after && (r[c - i].high < v);
      b0 = b0 && (r[c + i].high < v);
      b1 = b1 && (r[c + 1].high <= v && r[c + i + 1].high < v);
      b2 = b2 && (r[c + 1].high <= v && r[c + 2].high <= v && r[c + i + 2].high < v);
      b3 = b3 && (r[c + 1].high <= v && r[c + 2].high <= v && r[c + 3].high <= v && r[c + i + 3].high < v);
      b4 = b4 && (r[c + 1].high <= v && r[c + 2].high <= v && r[c + 3].high <= v && r[c + 4].high <= v && r[c + i + 4].high < v);
     }
   return after && (b0 || b1 || b2 || b3 || b4);
  }

//+------------------------------------------------------------------+
//| Chart drawing                                                    |
//+------------------------------------------------------------------+
//--- keep a fixed number of objects: the newest name takes the oldest slot
void Remember(string &ring[], int &next, const string name)
  {
   const int size = ArraySize(ring);
   if(size <= 0)
      return;
   const int slot = next % size;
   if(ring[slot] != "")
      ObjectDelete(0, ring[slot]);
   ring[slot] = name;
   next       = (slot + 1) % size;
  }

void DrawArrow(const bool under, const datetime t, const double price)
  {
   if(!g_draw || !InpDrawArrows || ArraySize(g_arrows) <= 0)
      return;
   const string name = g_prefix + (under ? "G" : "R") + IntegerToString((long)t);
   if(ObjectFind(0, name) >= 0)
      return;
   if(!ObjectCreate(0, name, OBJ_TEXT, 0, t, price))
      return;
   //--- as in the screen recording: a solid cyan triangle pointing up under the
   //--- candle, a solid red triangle pointing down over it (Unicode U+25B2 / U+25BC)
   ObjectSetString(0, name, OBJPROP_TEXT, ShortToString((ushort)(under ? 0x25B2 : 0x25BC)));
   ObjectSetString(0, name, OBJPROP_FONT, "Arial");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 11);
   ObjectSetInteger(0, name, OBJPROP_COLOR, under ? CLR_FRACTAL_UNDER : CLR_FRACTAL_OVER);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, under ? ANCHOR_UPPER : ANCHOR_LOWER);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   ObjectSetInteger(0, name, OBJPROP_BACK, false);
   Remember(g_arrows, g_arrowNext, name);
  }

//--- one candle of an EMA line, from the previous candle to this one
void DrawEmaSegment(const string tag, const datetime t1, const double p1, const datetime t2, const double p2,
                    const color clr)
  {
   const string name = g_prefix + "E" + tag + IntegerToString((long)t2);
   if(ObjectFind(0, name) >= 0)
      return;
   if(!ObjectCreate(0, name, OBJ_TREND, 0, t1, p1, t2, p2))
      return;
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 1);
   ObjectSetInteger(0, name, OBJPROP_STYLE, STYLE_SOLID);
   ObjectSetInteger(0, name, OBJPROP_RAY_LEFT, false);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
   Remember(g_lines, g_lineNext, name);
  }

//--- the three EMAs in the video's colours: 20 green, 50 yellow, 100 red
void DrawEmas(const int s, const MqlRates &r[], const double &emaF[], const double &emaM[], const double &emaS[])
  {
   if(!g_draw || !InpShowEMAs || ArraySize(g_lines) <= 0)
      return;
   DrawEmaSegment("F", r[s + 1].time, emaF[s + 1], r[s].time, emaF[s], CLR_GREEN);
   DrawEmaSegment("M", r[s + 1].time, emaM[s + 1], r[s].time, emaM[s], CLR_YELLOW);
   DrawEmaSegment("S", r[s + 1].time, emaS[s + 1], r[s].time, emaS[s], CLR_RED);
  }

void UpdatePanel(const bool force)
  {
   if(!g_draw || !InpShowPanel)
      return;
   const datetime now = TimeCurrent();
   if(!force && now == g_panelTime)
      return;
   g_panelTime = now;

   MqlTick tick;
   double  spread = 0.0;
   if(SymbolInfoTick(_Symbol, tick))
      spread = tick.ask - tick.bid;

   const string f = IntegerToString(InpEmaFast);
   const string m = IntegerToString(InpEmaMid);
   const string s = IntegerToString(InpEmaSlow);
   string stack = "crossing - no trades";
   if(g_bullStack)
      stack = f + " > " + m + " > " + s + "  (longs only)";
   if(g_bearStack)
      stack = s + " > " + m + " > " + f + "  (shorts only)";

   string text = EA_NAME + "   " + _Symbol + " " + TfName(g_tf) + "   magic " + IntegerToString((long)InpMagic) + "\n";
   text += "EMAs " + f + "/" + m + "/" + s + ": " + stack + "\n";
   text += "Long : under " + f + " EMA " + YesNo(g_long.pullback) + " | crossed " + m + " EMA " + YesNo(g_long.deep)
           + " | skip next cyan " + YesNo(g_long.skipNext) + "\n";
   text += "Short: over " + f + " EMA " + YesNo(g_short.pullback) + " | crossed " + m + " EMA " + YesNo(g_short.deep)
           + " | skip next red " + YesNo(g_short.skipNext) + "\n";
   text += "Spread " + Px(spread) + (InpMaxSpread > 0.0 ? " (max " + Px(InpMaxSpread) + ")" : "")
           + "   stop " + (InpStopBuffer > 0.0 ? Px(InpStopBuffer) + " past the EMA" : "right past the EMA")
           + "   target " + DoubleToString(InpRewardRisk, 2) + "R\n";
   text += "Positions per entry " + IntegerToString(InpPositions) + "   open now "
           + IntegerToString(CountMyPositions()) + "\n";
   text += "Last: " + g_lastAction;
   Comment(text);
  }

//+------------------------------------------------------------------+
//| The rules                                                        |
//+------------------------------------------------------------------+
void QueueSignal(const int dir, const bool deep, const double stopEma, const datetime arrowTime, const datetime entryBar)
  {
   ClearSignal();
   g_signal.active    = true;
   g_signal.direction = dir;
   g_signal.deep      = deep;
   g_signal.stopEma   = stopEma;
   g_signal.arrowTime = arrowTime;
   g_signal.validBar  = entryBar;
   Note(StringFormat("%s arrow at %s -> %s, stop %s the %d EMA (%s)",
                     dir > 0 ? "cyan" : "red",
                     TimeToString(arrowTime, TIME_DATE | TIME_MINUTES),
                     dir > 0 ? "BUY" : "SELL",
                     dir > 0 ? "below" : "above",
                     deep ? InpEmaSlow : InpEmaMid,
                     Px(stopEma)));
  }

//--- a green arrow was just confirmed
void OnGreenArrow(const bool bull, const double emaMid, const double emaSlow,
                  const datetime arrowTime, const datetime entryBar, const bool live)
  {
   if(g_long.skipNext)
     {
      //--- "if the price ever closes below the 100 day, just disregard the next green arrow"
      if(live && bull && g_long.pullback)
         Note("cyan arrow at " + TimeToString(arrowTime, TIME_MINUTES) + " disregarded: price closed below the "
              + IntegerToString(InpEmaSlow) + " EMA");
      g_long.skipNext = false;
      g_long.pullback = false;
      g_long.deep     = false;
      return;
     }
   if(!bull || !g_long.pullback)
      return;                           // EMAs not stacked or no pullback under the 20 EMA: no setup
   const bool deep = g_long.deep;
   g_long.pullback = false;             // this arrow uses up the pullback
   g_long.deep     = false;
   if(live && InpTradeLongs)
      QueueSignal(+1, deep, deep ? emaSlow : emaMid, arrowTime, entryBar);
  }

//--- a red arrow was just confirmed (the complete opposite of the green one)
void OnRedArrow(const bool bear, const double emaMid, const double emaSlow,
                const datetime arrowTime, const datetime entryBar, const bool live)
  {
   if(g_short.skipNext)
     {
      if(live && bear && g_short.pullback)
         Note("red arrow at " + TimeToString(arrowTime, TIME_MINUTES) + " disregarded: price closed above the "
              + IntegerToString(InpEmaSlow) + " EMA");
      g_short.skipNext = false;
      g_short.pullback = false;
      g_short.deep     = false;
      return;
     }
   if(!bear || !g_short.pullback)
      return;
   const bool deep = g_short.deep;
   g_short.pullback = false;
   g_short.deep     = false;
   if(live && InpTradeShorts)
      QueueSignal(-1, deep, deep ? emaSlow : emaMid, arrowTime, entryBar);
  }

//+------------------------------------------------------------------+
//| Run one closed candle (series shift s >= 1) through the rules.   |
//| live = false while replaying history: state only, no signals.    |
//+------------------------------------------------------------------+
void ProcessClosedBar(const int s, const MqlRates &r[], const double &emaF[], const double &emaM[],
                      const double &emaS[], const bool live)
  {
   const int    n    = InpFractalPeriods;
   const double e1   = emaF[s];
   const double e2   = emaM[s];
   const double e3   = emaS[s];
   const bool   bull = (e1 > e2 && e2 > e3);   // 20 above 50 above 100
   const bool   bear = (e1 < e2 && e2 < e3);   // 100 on top, 50 middle, 20 bottom

   //--- long side
   if(r[s].close < e3)
      g_long.skipNext = true;                  // closed below the 100 EMA
   if(bull)
     {
      if(r[s].low < e1)
         g_long.pullback = true;               // pulled back under the 20 EMA
      if(r[s].low < e2)
         g_long.deep = true;                   // ...and crossed the 50 EMA
     }
   else
     {
      g_long.pullback = false;                 // crossing EMAs: no long setup
      g_long.deep     = false;
     }

   //--- short side
   if(r[s].close > e3)
      g_short.skipNext = true;                 // closed above the 100 EMA
   if(bear)
     {
      if(r[s].high > e1)
         g_short.pullback = true;              // pulled back over the 20 EMA
      if(r[s].high > e2)
         g_short.deep = true;                  // ...and crossed the 50 EMA
     }
   else
     {
      g_short.pullback = false;
      g_short.deep     = false;
     }

   if(s == 1)
     {
      g_bullStack = bull;
      g_bearStack = bear;
     }
   DrawEmas(s, r, emaF, emaM, emaS);

   //--- the arrow confirmed by this candle sits n candles back
   const int c = s + n;
   if(IsGreenFractal(r, c, n))
     {
      DrawArrow(true, r[c].time, r[c].low);
      OnGreenArrow(bull, e2, e3, r[c].time, r[s - 1].time, live);
     }
   if(IsRedFractal(r, c, n))
     {
      DrawArrow(false, r[c].time, r[c].high);
      OnRedArrow(bear, e2, e3, r[c].time, r[s - 1].time, live);
     }
  }

bool CopySeries(const int count, MqlRates &r[], double &emaF[], double &emaM[], double &emaS[])
  {
   ArraySetAsSeries(r, true);
   ArraySetAsSeries(emaF, true);
   ArraySetAsSeries(emaM, true);
   ArraySetAsSeries(emaS, true);
   if(CopyRates(_Symbol, g_tf, 0, count, r) != count)
      return false;
   if(CopyBuffer(g_hFast, 0, 0, count, emaF) != count)
      return false;
   if(CopyBuffer(g_hMid, 0, 0, count, emaM) != count)
      return false;
   if(CopyBuffer(g_hSlow, 0, 0, count, emaS) != count)
      return false;
   return true;
  }

//+------------------------------------------------------------------+
//| Feed every candle that closed since the last call through the    |
//| rules. The first call (or a gap in history) replays the last     |
//| WARMUP_BARS candles without trading, so the setup state is right |
//| even when the EA is attached in the middle of a pullback.        |
//+------------------------------------------------------------------+
bool SyncClosedBars()
  {
   const int extra = 2 * InpFractalPeriods + 6;
   const int calc  = IMin(BarsCalculated(g_hFast), IMin(BarsCalculated(g_hMid), BarsCalculated(g_hSlow)));
   const int avail = IMin(Bars(_Symbol, g_tf), calc);
   if(avail < extra + 2)
      return false;                            // history or EMAs not ready yet

   bool rebuild = !g_ready;
   int  from    = 1;                           // oldest unprocessed closed candle (series shift)
   if(!rebuild)
     {
      const int shift = iBarShift(_Symbol, g_tf, g_lastClosed, true);
      if(shift < 1 || shift - 1 > WARMUP_BARS)
         rebuild = true;                       // history changed or a long gap: start over
      else
         from = shift - 1;
     }
   if(rebuild)
      from = WARMUP_BARS;
   from = IMin(from, avail - extra - 1);
   if(from < 1)
      return !rebuild;                         // nothing new has closed

   const int count = from + extra + 1;
   MqlRates  rates[];
   double    emaF[];
   double    emaM[];
   double    emaS[];
   if(!CopySeries(count, rates, emaF, emaM, emaS))
      return false;

   if(rebuild)
     {
      ResetSide(g_long);
      ResetSide(g_short);
      ClearSignal();
     }
   for(int s = from; s >= 1; s--)
      ProcessClosedBar(s, rates, emaF, emaM, emaS, !rebuild && s == 1);
   g_lastClosed = rates[1].time;
   if(rebuild && !g_ready)
      Print(EA_NAME, ": ready on ", _Symbol, " ", TfName(g_tf), ", history replayed from ",
            TimeToString(rates[from].time, TIME_DATE | TIME_MINUTES));
   g_ready = true;
   return true;
  }

//+------------------------------------------------------------------+
//| Entry                                                            |
//+------------------------------------------------------------------+
bool TradingAllowed(const bool isBuy, string &reason)
  {
   if(TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) == 0)
     {
      reason = "Algo Trading is switched off in the terminal";
      return false;
     }
   if(MQLInfoInteger(MQL_TRADE_ALLOWED) == 0)
     {
      reason = "live trading is not allowed for this EA";
      return false;
     }
   const long mode = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE);
   if(mode == SYMBOL_TRADE_MODE_DISABLED || mode == SYMBOL_TRADE_MODE_CLOSEONLY)
     {
      reason = "the symbol is not open for new trades";
      return false;
     }
   if(isBuy && mode == SYMBOL_TRADE_MODE_SHORTONLY)
     {
      reason = "the broker only allows sells on this symbol";
      return false;
     }
   if(!isBuy && mode == SYMBOL_TRADE_MODE_LONGONLY)
     {
      reason = "the broker only allows buys on this symbol";
      return false;
     }
   return true;
  }

bool InsideHours()
  {
   MqlDateTime t;
   TimeToStruct(TimeCurrent(), t);
   if(InpStartHour <= InpEndHour)
      return (t.hour >= InpStartHour && t.hour <= InpEndHour);
   return (t.hour >= InpStartHour || t.hour <= InpEndHour);
  }

int CountMyPositions()
  {
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      const ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC) != InpMagic)
         continue;
      count++;
     }
   return count;
  }

bool IsHedging()
  {
   return AccountInfoInteger(ACCOUNT_MARGIN_MODE) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING;
  }

bool BlockedByOpenTrade()
  {
   //--- netting accounts hold one position per symbol: never add to or flip someone else's
   if(!IsHedging())
      return PositionSelect(_Symbol);
   return InpOneEntry && CountMyPositions() > 0;
  }

//--- money lost by 1 lot going from entry to the stop
double LossPerLot(const bool isBuy, const double entry, const double sl)
  {
   double profit = 0.0;
   if(OrderCalcProfit(isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, 1.0, entry, sl, profit) && profit < 0.0)
      return -profit;
   double tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE_LOSS);
   if(tv <= 0.0)
      tv = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   if(tv <= 0.0)
      return 0.0;
   return MathAbs(entry - sl) / TickSize() * tv;
  }

//+------------------------------------------------------------------+
//| Size of each position of an entry, 0 if it cannot be traded.     |
//| Risk modes: the entry's risk is shared by all its positions. If  |
//| the share is below the minimum lot, fewer minimum-lot positions  |
//| are opened so the entry never risks more than it should (unless  |
//| InpMinLotFallback). Fixed mode: InpFixedLots for each position.  |
//| InpMaxLots caps the whole entry. count may be lowered.           |
//+------------------------------------------------------------------+
double LotsPerPosition(const bool isBuy, const double entry, const double sl, int &count)
  {
   double       minLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   const double maxLot = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   double       step   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      step = (minLot > 0.0) ? minLot : 0.01;
   if(minLot <= 0.0)
      minLot = step;
   if(count < 1)
      return 0.0;

   double each = 0.0;
   if(InpLotMode == LOT_MODE_FIXED)
     {
      each = MathFloor(InpFixedLots / step + 1e-8) * step;
      if(each < minLot)
         each = minLot;
     }
   else
     {
      const double money  = (InpLotMode == LOT_MODE_RISK_PERCENT)
                            ? AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPercent / 100.0
                            : InpRiskMoney;
      const double perLot = LossPerLot(isBuy, entry, sl) + MathMax(InpCommissionLot, 0.0);
      if(money <= 0.0 || perLot <= 0.0)
         return 0.0;
      const double total = money / perLot;                  // lots for the whole entry
      each = MathFloor(total / count / step + 1e-8) * step;
      if(each < minLot)
        {
         if(InpMinLotFallback)
            each = minLot;
         else
           {
            //--- too small to split that many ways: fewer positions of the minimum lot
            count = IMin(count, (int)MathFloor(total / minLot + 1e-8));
            if(count < 1)
               return 0.0;
            each = minLot;
           }
        }
     }

   //--- never more than InpMaxLots for the whole entry
   if(each * count > InpMaxLots + 1e-8)
     {
      each = MathFloor(InpMaxLots / count / step + 1e-8) * step;
      if(each < minLot)
        {
         count = (int)MathFloor(InpMaxLots / minLot + 1e-8);
         if(count < 1)
            return 0.0;
         each = minLot;
        }
     }
   //--- nor more than the broker allows in one order
   if(maxLot > 0.0 && each > maxLot)
      each = MathFloor(maxLot / step + 1e-8) * step;
   if(each < minLot)
      return 0.0;
   return NormalizeDouble(each, VolumeDigits(step));
  }

string Lots(const double lots)
  {
   return DoubleToString(lots, VolumeDigits(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP)));
  }

//--- give up on the current signal (positions already opened stay open)
void Drop(const string reason)
  {
   const string side = (g_signal.direction > 0) ? "buy" : "sell";
   if(g_signal.opened > 0)
      Note(StringFormat("%s entry stopped at %d of %d positions: %s", side, g_signal.opened, g_signal.count, reason));
   else
      Note(StringFormat("%s signal skipped: %s", side, reason));
   ClearSignal();
  }

//--- hold the signal and try again on the next tick of the same candle
void Hold(const string key, const string message)
  {
   if(!InpRetryInBar)
     {
      Drop(message);
      return;
     }
   if(key != g_signal.holdKey)
     {
      Print(EA_NAME, ": entry on hold - ", message);
      g_signal.holdKey = key;
     }
  }

void TryEntry(const datetime barTime)
  {
   if(g_signal.validBar != barTime)
     {
      Drop("the candle after the arrow closed before an entry was possible");
      return;
     }
   const bool isBuy  = (g_signal.direction > 0);
   const bool first  = (g_signal.opened == 0);     // nothing of this entry is open yet
   string     reason = "";
   if(!TradingAllowed(isBuy, reason))
     {
      Drop(reason);
      return;
     }
   if(first && InpUseHours && !InsideHours())
     {
      Drop("outside the trading hours");
      return;
     }
   if(first && BlockedByOpenTrade())
     {
      Drop("a trade is already open - staying in it");
      return;
     }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick) || tick.bid <= 0.0 || tick.ask <= 0.0)
      return;
   const double spread = tick.ask - tick.bid;
   if(InpMaxSpread > 0.0 && spread > InpMaxSpread + _Point * 0.5)
     {
      Hold("spread", "spread " + Px(spread) + " is wider than " + Px(InpMaxSpread));
      return;
     }

   //--- stop on the first price right below / above the EMA, shared by every position
   const double entry = isBuy ? tick.ask : tick.bid;
   if(first)
     {
      const double half = TickSize() * 0.5;      // keeps the stop strictly past the EMA
      if(isBuy)
         g_signal.sl = PriceDown(g_signal.stopEma - InpStopBuffer - half);
      else
         g_signal.sl = PriceUp(g_signal.stopEma + InpStopBuffer + (InpSpreadOnSellSL ? spread : 0.0) + half);
     }
   const double sl   = g_signal.sl;
   const double risk = isBuy ? entry - sl : sl - entry;
   if(risk <= 0.0)
     {
      Drop("price is already through the stop level " + Px(sl));
      return;
     }
   //--- target 1.5 x the risk
   const double tp = PriceRound(isBuy ? entry + InpRewardRisk * risk : entry - InpRewardRisk * risk);

   if(first && InpMinStopDist > 0.0 && risk < InpMinStopDist)
     {
      Drop("stop distance " + Px(risk) + " is below the minimum " + Px(InpMinStopDist));
      return;
     }
   if(first && InpMaxStopDist > 0.0 && risk > InpMaxStopDist)
     {
      Drop("stop distance " + Px(risk) + " is above the maximum " + Px(InpMaxStopDist));
      return;
     }

   //--- the broker's minimum distance between the market and the stop / target
   const double minDist = (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL) * _Point;
   const bool   stopsOk = isBuy ? (tick.bid - sl >= minDist && tp - tick.bid >= minDist)
                                : (sl - tick.ask >= minDist && tick.ask - tp >= minDist);
   if(!stopsOk)
     {
      Hold("stops", "stop " + Px(sl) + " or target " + Px(tp) + " is too close to the market for the broker");
      return;
     }

   //--- how many positions and how big: decided once, on the first send
   if(first)
     {
      //--- a netting account merges everything into one position: send it as one order
      int count = IsHedging() ? InpPositions : 1;
      const double each = LotsPerPosition(isBuy, entry, sl, count);
      if(each <= 0.0)
        {
         Drop("the risk is too small for the minimum lot size");
         return;
        }
      if(IsHedging() && count < InpPositions)
         Print(EA_NAME, ": the risk only covers ", IntegerToString(count), " of ", IntegerToString(InpPositions),
               " positions at the minimum lot size - opening ", IntegerToString(count));
      double margin = 0.0;
      if(OrderCalcMargin(isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL, _Symbol, each * count, entry, margin)
         && margin > AccountInfoDouble(ACCOUNT_MARGIN_FREE))
        {
         Drop("not enough free margin for " + IntegerToString(count) + " x " + Lots(each) + " lots");
         return;
        }
      g_signal.count    = count;
      g_signal.lotsEach = each;
     }

   //--- open the positions, all with the same stop and target
   const int emaLen = g_signal.deep ? InpEmaSlow : InpEmaMid;
   while(g_signal.opened < g_signal.count)
     {
      const string comment = StringFormat("FES %s %d %d/%d", isBuy ? "buy" : "sell", emaLen,
                                          g_signal.opened + 1, g_signal.count);
      const bool sent = isBuy ? g_trade.Buy(g_signal.lotsEach, _Symbol, entry, sl, tp, comment)
                              : g_trade.Sell(g_signal.lotsEach, _Symbol, entry, sl, tp, comment);
      const uint rc   = g_trade.ResultRetcode();
      if(sent && (rc == TRADE_RETCODE_DONE || rc == TRADE_RETCODE_DONE_PARTIAL || rc == TRADE_RETCODE_PLACED))
        {
         g_signal.opened++;
         continue;
        }
      g_signal.fails++;
      const bool transient = (rc == TRADE_RETCODE_REQUOTE || rc == TRADE_RETCODE_PRICE_CHANGED
                              || rc == TRADE_RETCODE_PRICE_OFF || rc == TRADE_RETCODE_TIMEOUT
                              || rc == TRADE_RETCODE_CONNECTION || rc == TRADE_RETCODE_TOO_MANY_REQUESTS);
      if(transient && InpRetryInBar && g_signal.fails < MAX_FAILS)
        {
         Print(EA_NAME, ": order not filled (", g_trade.ResultRetcodeDescription(), "), retrying on the next tick");
         return;
        }
      Drop("order rejected: " + IntegerToString(rc) + " " + g_trade.ResultRetcodeDescription());
      return;
     }

   Note(StringFormat("%s %d x %s lots at %s, stop %s (%s the %d EMA), target %s (%.2fR)",
                     isBuy ? "BUY" : "SELL",
                     g_signal.count,
                     Lots(g_signal.lotsEach),
                     Px(entry),
                     Px(sl),
                     isBuy ? "below" : "above",
                     emaLen,
                     Px(tp),
                     InpRewardRisk));
   ClearSignal();
  }

//+------------------------------------------------------------------+
//| Expert events                                                    |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpFractalPeriods < 2)
     {
      Print(EA_NAME, ": Williams Fractals periods must be 2 or more (video: 2)");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpEmaFast < 1 || InpEmaMid <= InpEmaFast || InpEmaSlow <= InpEmaMid)
     {
      Print(EA_NAME, ": EMA lengths must rise from EMA 1 to EMA 3 (video: 20 / 50 / 100)");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpRewardRisk <= 0.0 || InpStopBuffer < 0.0 || InpMaxLots <= 0.0 || InpMaxSpread < 0.0)
     {
      Print(EA_NAME, ": target multiple and max lots must be above 0, stop buffer and max spread 0 or more");
      return INIT_PARAMETERS_INCORRECT;
     }
   if((InpLotMode == LOT_MODE_FIXED && InpFixedLots <= 0.0)
      || (InpLotMode == LOT_MODE_RISK_PERCENT && (InpRiskPercent <= 0.0 || InpRiskPercent > 100.0))
      || (InpLotMode == LOT_MODE_RISK_MONEY && InpRiskMoney <= 0.0))
     {
      Print(EA_NAME, ": the position size for the chosen sizing mode must be above 0");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpStartHour < 0 || InpStartHour > 23 || InpEndHour < 0 || InpEndHour > 23)
     {
      Print(EA_NAME, ": trading hours must be between 0 and 23");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpMinStopDist < 0.0 || InpMaxStopDist < 0.0 || InpSlippage < 0.0 || InpLineBars < 0 || InpMaxArrows < 0)
     {
      Print(EA_NAME, ": distances, slippage and chart history cannot be negative");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpPositions < 1 || InpPositions > MAX_POSITIONS)
     {
      Print(EA_NAME, ": positions per entry must be between 1 and ", IntegerToString(MAX_POSITIONS));
      return INIT_PARAMETERS_INCORRECT;
     }
   if(InpPositions > 1 && !IsHedging())
      Print(EA_NAME, ": this is a netting account, which holds one position per symbol - each entry opens",
            " one position with the combined size of ", IntegerToString(InpPositions), " positions");

   g_tf = (InpTimeframe == PERIOD_CURRENT) ? (ENUM_TIMEFRAMES)_Period : InpTimeframe;

   string symbol = _Symbol;
   StringToUpper(symbol);
   if(StringFind(symbol, "XAU") < 0 && StringFind(symbol, "GOLD") < 0)
      Print(EA_NAME, ": warning - built for XAUUSD but attached to ", _Symbol,
            ". The stop buffer and spread limit are in price units, check them for this symbol.");

   //--- the Strategy Tester would plot the EMAs in its default colour on top of
   //--- the green / yellow / red lines the EA draws, so keep them hidden
   TesterHideIndicators(true);
   g_hFast = iMA(_Symbol, g_tf, InpEmaFast, 0, MODE_EMA, PRICE_CLOSE);
   g_hMid  = iMA(_Symbol, g_tf, InpEmaMid, 0, MODE_EMA, PRICE_CLOSE);
   g_hSlow = iMA(_Symbol, g_tf, InpEmaSlow, 0, MODE_EMA, PRICE_CLOSE);
   if(g_hFast == INVALID_HANDLE || g_hMid == INVALID_HANDLE || g_hSlow == INVALID_HANDLE)
     {
      Print(EA_NAME, ": could not create the moving averages, error ", IntegerToString(GetLastError()));
      return INIT_FAILED;
     }

   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints((ulong)MathRound(MathMax(InpSlippage, 0.0) / _Point));
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.SetMarginMode();
   g_trade.LogLevel(LOG_LEVEL_ERRORS);

   g_draw   = !(MQLInfoInteger(MQL_OPTIMIZATION) != 0
                || (MQLInfoInteger(MQL_TESTER) != 0 && MQLInfoInteger(MQL_VISUAL_MODE) == 0));
   g_prefix = "FES_" + IntegerToString((long)InpMagic) + "_";
   ArrayResize(g_arrows, (g_draw && InpDrawArrows) ? InpMaxArrows : 0);
   for(int i = 0; i < ArraySize(g_arrows); i++)
      g_arrows[i] = "";
   g_arrowNext = 0;
   ArrayResize(g_lines, (g_draw && InpShowEMAs) ? 3 * InpLineBars : 0);
   for(int i = 0; i < ArraySize(g_lines); i++)
      g_lines[i] = "";
   g_lineNext = 0;

   ResetSide(g_long);
   ResetSide(g_short);
   ClearSignal();
   g_ready      = false;
   g_barTime    = 0;
   g_lastClosed = 0;
   g_bullStack  = false;
   g_bearStack  = false;
   g_lastAction = "waiting for the first signal";
   UpdatePanel(true);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   if(g_hFast != INVALID_HANDLE)
      IndicatorRelease(g_hFast);
   if(g_hMid != INVALID_HANDLE)
      IndicatorRelease(g_hMid);
   if(g_hSlow != INVALID_HANDLE)
      IndicatorRelease(g_hSlow);
   g_hFast = INVALID_HANDLE;
   g_hMid  = INVALID_HANDLE;
   g_hSlow = INVALID_HANDLE;
   ObjectsDeleteAll(0, g_prefix);
   Comment("");
  }

void OnTick()
  {
   const datetime barTime = iTime(_Symbol, g_tf, 0);
   if(barTime == 0)
      return;
   if(barTime != g_barTime)
     {
      if(!SyncClosedBars())
         return;                               // data not ready yet: try again next tick
      g_barTime = barTime;
     }
   if(g_signal.active)
      TryEntry(barTime);
   UpdatePanel(false);
  }

//--- report how each trade ended
void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
  {
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || !HistoryDealSelect(trans.deal))
      return;
   if(HistoryDealGetString(trans.deal, DEAL_SYMBOL) != _Symbol)
      return;
   if((ulong)HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != InpMagic)
      return;
   if(HistoryDealGetInteger(trans.deal, DEAL_ENTRY) != DEAL_ENTRY_OUT)
      return;
   const long   why = HistoryDealGetInteger(trans.deal, DEAL_REASON);
   const double pnl = HistoryDealGetDouble(trans.deal, DEAL_PROFIT) + HistoryDealGetDouble(trans.deal, DEAL_SWAP)
                      + HistoryDealGetDouble(trans.deal, DEAL_COMMISSION);
   string how = "closed";
   if(why == DEAL_REASON_TP)
      how = "hit the target";
   if(why == DEAL_REASON_SL)
      how = "hit the stop";
   Note(StringFormat("trade %s at %s, result %.2f %s", how, Px(HistoryDealGetDouble(trans.deal, DEAL_PRICE)),
                     pnl, AccountInfoString(ACCOUNT_CURRENCY)));
  }
//+------------------------------------------------------------------+

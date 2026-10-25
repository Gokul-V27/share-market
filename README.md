# share-market

Share market analysis and tracking repository.

**Laya** (`laya_market/`) is a rule-based chart-analysis and decision engine for **study and
paper trading only**. It never gives guaranteed calls or "buy/sell now" orders; it produces a
scored, rule-based read you can paper trade and journal.

Three ways in:

| Command | Input | What it does |
|---|---|---|
| `analyze` | a JSON chart read (scores, levels, setups) | recomputes scores, R:R, ranking and verdict; flags any wrong numbers |
| `btc` | live Binance/Coinbase candles, or a CSV | the **BTC watcher**: scores 1h/15m/5m/1m, builds setups from *your* levels, alerts on close triggers, replays a CSV as paper trades |
| `screenshot` | TradingView screenshots | Claude reads the charts with the Laya prompt, then the engine re-checks its numbers |

Core engine, BTC watcher and tests use the Python standard library only (Python 3.10+).

## Quick start

```bash
# 1. Re-check a written analysis
python -m laya_market analyze examples/sample_analysis.json

# 2. BTC watcher, live (levels = the horizontal lines you drew)
python -m laya_market btc --level 80000 --level 80500 --level 81000
python -m laya_market btc --levels-file my_levels.json --watch --every 30

# 3. BTC watcher on recorded data (trades CSV or OHLCV CSV)
python -m laya_market btc --symbol BTCUSD --csv tests/fixtures/btcusd_2026-09-01_1m.csv \
    --levels-file examples/btc_levels.json --at 2026-09-01T18:32
python -m laya_market btc --symbol BTCUSD --csv tests/fixtures/btcusd_2026-09-01_1m.csv \
    --levels-file examples/btc_levels.json --replay

# 4. Screenshots (needs `pip install anthropic` and ANTHROPIC_API_KEY)
python -m laya_market screenshot 1h.png 15m.png 5m.png 1m.png --save read.json

# Tests
python -m unittest discover -s tests -t .
```

## The method

Top-down, 1h -> 15m -> 5m -> 1m. Each timeframe gets four component scores (-2..+2):

| Component | Rule (BTC watcher, from candles) |
|---|---|
| Trend | price above both MA9/MA26: +1, and MA9 > MA26: +2 (mirror for bearish); between the MAs: 0 |
| Structure | last two swing highs and lows: +1 per higher high / higher low, -1 per lower high / lower low |
| Momentum | net body of the last 3 closed candles in ATR units: >= 1.5 -> +2, >= 0.5 -> +1 (mirror) |
| Location | within 1 ATR above drawn support with room overhead: +1; pressed under drawn resistance: -1 |

* The timeframe score is the component sum **clamped to -2..+2**. The total over the four
  timeframes is **bullish at >= +3, bearish at <= -3, otherwise mixed**.
* A lower timeframe whose lean opposes the combined higher timeframes is a
  **counter-trend bounce**; counter-trend setups always rank last.
* Only **closed** candles are scored. The forming candle supplies the last price and the
  countdown, and no close-based rule counts before that candle closes.

**Setups** trigger on a candle **close** only. R:R = |target - entry| / |entry - stop|, and
anything under 1:1.5 to target 1 is flagged weak. A stop must sit beyond the trigger level that
invalidates the idea, or the setup is marked INVALID. Setups are ranked on points: alignment
(with-trend 3 / neutral 1 / counter 0), confluence with drawn levels (up to 2), stop size
(tightest = 2), and target-1 R:R (1:3 = 2).

**Verdict**: ENTER-ON-TRIGGER only when the bias is not mixed and the top setup is valid,
with-trend and not weak; otherwise WAIT. Confidence is high when all four timeframes agree,
moderate when three do, and low otherwise.

The report always has the same 7 parts (verdict, alignment table, setups, ranking,
invalidation, timing and costs, blind spots). It adds a **"Corrections to the supplied
numbers"** section when the input claimed something the arithmetic doesn't support. It asks at
most one question, ends with one disclaimer, and **never invents a level**. In the BTC
watcher, every trigger and target is one of your levels. Entry and stop sit half an
exec-timeframe ATR either side of the level.

### Example: the supplied hypothetical read

`examples/sample_analysis.json` holds the hypothetical example (1h +3, 15m -2, 5m +1, 1m +3;
three setups). Recomputed:

* Total **+3 -> bullish** under the >= +3 rule (it was labelled mixed); 15m is a counter-trend bounce.
* The R:R claims were off: Setup 1 is **1:1.14 / 1:2.57** (claimed 5.0), Setup 2 **1:1.00 / 1:2.25**
  (claimed 5.0), Setup 3 **1:1.00 / 1:2.67** (claimed 20.0). All three are weak to target 1.
* The verdict is still **WAIT, moderate**, but because every target 1 is under 1:1.5, not
  because the bias is mixed. Setup 2 (short) is counter-trend and ranks last.

## BTC watcher

```bash
python -m laya_market btc [--symbol BTCUSDT] [--level P ...] [--levels-file F] [--exec-tf 5m]
                          [--cost-pct 0.10] [--watch --every 30] [--source auto|binance|coinbase]
                          [--csv FILE [--at TIME | --replay] [--save-1m OUT]] [--json] [--note TEXT]
```

* **Live**: Binance spot klines via `data-api.binance.vision` (falls back to `api.binance.com`,
  then Coinbase `BTC-USD`). With `--watch`, the screen refreshes, and a terminal bell plus a line
  are printed when a 5m close crosses, or wicks and rejects, one of your levels, or fires a setup trigger.
* **Levels file**: `{"levels": [{"price": 80000, "label": "weekly high"}, ...]}`. With no
  levels, the watcher still scores trend, structure and momentum, but proposes no trades and
  asks for your levels.
* **CSV**: either a trades file (`price,size,timestamp`, e.g. the BTCUSD tick export) aggregated
  into 1m candles, or an OHLCV file (`timestamp,open,high,low,close,volume`). `--at` shows the
  chart as it looked at that moment, with the countdown included.
* **Replay** paper-trades the engine's own rules. A trade opens only when the read would say
  ENTER-ON-TRIGGER (valid, with-trend, not-weak setup whose close trigger fires) and fills at the
  trigger candle's close. It exits at the stop, target 1, or after 8 exec candles (levels stale).
  If the stop and target fall in the same 1m candle, the stop counts as hit first. Results are
  shown gross and net of `--cost-pct`.

Add `--balance 10` to run the replay through a $10 paper account. Each trade uses the whole
balance (a $10 account is too small to size by risk), with no leverage unless `--leverage` is
set, and profits compound. Shorts assume futures or margin. On 2026-09-01: $10 -> **$9.94**
on 5m at 0.10% cost, $10.03 at 0.04%; $9.84 on 15m.

The daily table lists each UTC day's trend (share of 5m reads that were bullish / bearish /
mixed), trades won and lost, $ P&L and balance. A day with no trade says why: no trend all day,
no with-trend setup worth 1:1.5, or setups ready but no close trigger.

### 30 days (September 2026)

```bash
python -m laya_market download --symbol BTCUSDT --start 2026-09-01 --days 30 --out data/btcusdt_2026-09_1m.csv.gz
python -m laya_market btc --csv data/btcusdt_2026-09_1m.csv.gz --levels-file examples/btc_levels_2026-09.json \
    --replay --balance 10
```

`data/btcusdt_2026-09_1m.csv.gz` is Binance BTCUSDT 1m candles for 2026-09-01 to 09-30 (43,200).
`examples/btc_levels_2026-09.json` holds **test levels** every 500 USD across the month's range.
With $10, full balance per trade and no leverage:

| Exec TF | Trades | No cost | 0.04% cost | 0.10% cost |
|---|---|---|---|---|
| 5m | 338 | $10.09 | $8.81 | $7.19 |
| 15m | 164 | $10.00 | $9.36 | $8.49 |

Before costs the rules roughly break even. After costs every variant loses, because round-number
levels and half-ATR stops get stopped out by noise far more often than they reach target 1.

`tests/fixtures/btcusd_2026-09-01_1m.csv` is the 2026-09-01 BTCUSD tick file (314,107 trades)
aggregated to 1,440 one-minute candles. `examples/btc_levels.json` uses round numbers as
**test levels**; replace them with the lines you actually draw. On that day, with those levels,
the 5m replay takes 14 trades: +2.06R gross but -8.52R net at a 0.10% round trip. Tight 5m
stops are mostly eaten by costs, which is the point the timing-and-costs section keeps making.

## Text guard and NandhaKishorM/laya

[`laya`](https://github.com/NandhaKishorM/laya) (`pip install laya`, needs torch) is a
non-autoregressive decision model that answers typed questions about text. It was tested here as
a decision layer, with these results:

| Question asked of the model | Result | Used? |
|---|---|---|
| "Is the user asking for a guaranteed buy/sell call?" | 0.82-0.95 on "sure-shot / should I buy now", <= 0.50 otherwise | yes: refuse calls |
| "Does the text mention scheduled high-impact news (CPI/FOMC/NFP)?" | 0.80-0.89 on CPI/FOMC notes, <= 0.10 otherwise | yes: adds a news blind spot |
| "What is the bias?" from descriptive text | right on clear text, called a mixed description bullish | no: the rules compute it |
| "Wait or enter?" | about 0.51 either way, even "enter" on a bearish description | no |

So `laya` is used only as an optional **text guard** on `--note` text (`laya_market/text_guard.py`).
When it isn't installed, a keyword fallback covers both checks. Trade decisions stay with the rules.
`python -m laya_market guard "should I buy BTC now?"` runs the guard on its own.

## Screenshots

`laya_market/prompts/laya_system.md` is the full Laya role prompt, plus a request for a JSON
appendix of exactly what was read. `screenshot` sends the images to Claude (`claude-opus-5-5`,
streaming, adaptive thinking, server-side refusal fallback) and prints the report. It then
feeds the JSON appendix to the engine and prints any arithmetic it disagrees with. `--save`
keeps the read for `analyze` later.

## Layout

```
laya_market/
  models.py        timeframe reads, levels, setups, input parsing
  engine.py        scoring, alignment, setup checks, ranking, verdict, invalidation, audit
  report.py        the 7-part text report and JSON output
  indicators.py    MA, ATR, swing pivots, component scores from candles
  market_data.py   Binance/Coinbase fetchers, trades/OHLCV CSV loaders, resampling
  btc_watcher.py   setups from your levels, trigger alerts, live loop, paper-trade replay
  text_guard.py    optional NandhaKishorM/laya (or keyword) guard on free text
  vision.py        screenshot analysis with Claude + engine re-check
  prompts/laya_system.md
examples/          sample_analysis.json, btc_levels.json
tests/             unittest suite + 2026-09-01 BTCUSD 1m fixture
```

Study example, not financial advice.

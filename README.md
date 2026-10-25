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

### Fixed 1:4 / 1:6 exits

`--target-r 4` (or `6`) exits at 4R (6R) from the fill instead of at the next drawn level, and
the entry filter then uses that planned R:R. `--stop-atr` sets how far beyond the level the
stop sits (in exec-timeframe ATRs, default 0.5), and `--max-candles` sets the time stop
(default 8 exec candles).

```bash
python -m laya_market btc --csv data/btcusdt_2026-09_1m.csv.gz --levels-file examples/btc_levels_2026-09.json \
    --replay --balance 10 --exec-tf 15m --target-r 6 --stop-atr 1.5 --max-candles 96
```

$10 per month, levels every 500 USD, full balance per trade, no leverage. The settings were
picked on September from 54 combinations; August and October were not used to pick them.

| Settings | Aug 2026 | Sep 2026 | Oct 1-24 2026 |
|---|---|---|---|
| original: 5m, exit at next level, 0.5 ATR stop, 8-candle time stop | $7.38 | $7.19 | $7.73 |
| 5m, 1:4, 1 ATR stop, 1-day time stop | $9.49 | $8.93 | $8.71 |
| 5m, 1:6, 1 ATR stop, 1-day time stop | $9.47 | $9.69 | $9.12 |
| 15m, 1:4, 1.5 ATR stop, 1-day time stop | $10.34 | $10.27 | $9.43 |
| 15m, 1:6, 1.5 ATR stop, 1-day time stop | **$10.97** | **$11.24** | $9.90 |

All after a 0.10% round-trip cost. Wider stops, a far target and fewer trades did better than
tight 5m scalps. Only about 1 trade in 7-9 reached 6R, so long losing streaks are normal.

### R:R sweep vs the win-rate chart

Break-even win rate is `(1 + cost in R) / (1 + R)`: 50% at 1:1, 33% at 1:2, 25% at 1:3, 20% at
1:4, 17% at 1:5, before costs. `rr` replays files at several R:R exits and compares the real win
rate with that line:

```bash
python -m laya_market rr --csv aug.csv.gz --csv sep.csv.gz --csv oct.csv.gz \
    --exec-tf 15m --stop-atr 1.5 --max-candles 96 --rr 1,1.5,2,3,4,5,6,auto [--trail]
```

Aug + Sep + Oct 1-24 2026, 15m, $10, 0.10% cost:

| R:R | Win rate | Break-even (no cost) | Break-even (with cost) | $10 -> |
|---|---|---|---|---|
| 1:1 | skipped (rules need >= 1:1.5) | 50% | 50% | $10.00 |
| 1:1.5 | 40.1% | 40.0% | 47.4% | $8.57 |
| 1:2 | 33.9% | 33.3% | 39.6% | $9.44 |
| 1:3 | 22.3% | 25.0% | 29.7% | $9.32 |
| 1:4 | 17.4% | 20.0% | 23.8% | $10.01 |
| 1:5 | 14.9% | 16.7% | 20.0% | $10.64 |
| 1:6 | 13.2% | 14.3% | 17.2% | $12.21 |
| auto 1:3-1:6 by trend strength | 14.9% | 17.5% | 20.9% | $9.14 |
| 1:6 + trailing stop | 0.7% | 14.3% | 17.1% | $9.02 |

The win rate sits almost exactly on the no-cost break-even line at every R:R, so the entries
have no edge of their own; costs then push every short target into a loss. 1:5 and 1:6 only
came out ahead because trades closed by the time stop were net positive. The two
market-adaptive options, `--target-r auto` (bigger target in a stronger trend) and `--trail`
(stop to break-even at +1R, then 1R behind the best price), both did worse than a plain 1:6.

`tests/fixtures/btcusd_2026-09-01_1m.csv` is the 2026-09-01 BTCUSD tick file (314,107 trades)
aggregated to 1,440 one-minute candles. `examples/btc_levels.json` uses round numbers as
**test levels**; replace them with the lines you actually draw. On that day, with those levels,
the 5m replay takes 14 trades: +2.06R gross but -8.52R net at a 0.10% round trip. Tight 5m
stops are mostly eaten by costs, which is the point the timing-and-costs section keeps making.

## Three bots and the decision funnel

`laya_market/bots.py` adapts two day-trading bots to 24/7 BTC, and `laya_market/funnel.py` runs
every candidate trade through one funnel: market data -> bot rules -> Laya check -> risk gate ->
paper broker. The check can suggest; the risk gate can still reject.

* **ORB**: opening range = first 30 min after 00:00 UTC; a 5m close beyond it enters; stop at the
  other side of the range; exit at the day's end.
* **LVL sweep**: range = Asia session 00:00-08:00 UTC. From 08:00 to 20:00, after price trades
  beyond one side and a 5m candle closes back inside, enter at 25% of the range, stop at 12.5%,
  target at 50% (1:2).
* **News/insider bot**: not included; BTC has no insider filings and no historical news feed is
  available here.
* **Laya check**: `rules` (the engine's 4-timeframe read must agree with the trade's direction)
  or `model` (NandhaKishorM `laya` asked TAKE/SKIP from a text summary of that read).
* **Risk gate**: skip if fees exceed 0.35R; stop a bot for the day after 2 losses.

```bash
python -m laya_market bots --csv aug.csv.gz --csv sep.csv.gz --csv oct.csv.gz --check off,none,rules
```

Aug + Sep + Oct 1-24 2026, $10 each, 0.10% cost:

| Bot | Bot alone | + risk gate | + rules check + risk gate | + model check + risk gate |
|---|---|---|---|---|
| ORB (30m) | $9.04 | $9.79 | $9.47 | $9.79 |
| LVL sweep (1:2) | $9.44 | $9.77 | $9.85 | $9.85 |
| Laya 15m 1:6 | **$12.21** | $10.76 | $10.47 | $10.36 |

The risk gate cut losses for ORB and LVL by skipping trades whose stops were too tight for the
fees, but it also cut the Laya bot's winners. Neither check beat the Laya bot running alone.

## Delta Exchange

`--source delta --symbol BTCUSD` feeds the watcher live candles from Delta Exchange India
(public, no key needed). `delta-account` shows your balances and open positions **read-only**.
Nothing in this repo places orders.

Put credentials in a local `.env` (git-ignored, never commit it):

```
DELTA_API_KEY=...
DELTA_API_SECRET=...
DELTA_BASE_URL=https://api.india.delta.exchange
```

Delta keys are locked to whitelisted IPs, so run `delta-account` from a machine whose IP is on
the key's whitelist. For this, create the key with read-only permission (no trading).

```bash
python -m laya_market btc --source delta --symbol BTCUSD --levels-file my_levels.json --watch
python -m laya_market delta-account
```

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

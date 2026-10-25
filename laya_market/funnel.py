"""One setup, one decision: market data -> bot rules -> Laya check -> risk gate -> paper broker.

Each candidate trade from a bot (ORB, LVL sweep, Laya) is checked before the paper broker sees it:

* Laya check  - "rules": the engine's top-down read at entry time must agree with the trade's
                direction (bias not mixed, same side). "model": the NandhaKishorM `laya` decision
                model is asked TAKE or SKIP from a text description of that read (needs
                `pip install laya`). "none": pass everything through.
* Risk gate   - skip when the round-trip cost eats more than MAX_COST_R of the stop distance, and
                stop a bot for the rest of the UTC day after MAX_DAILY_LOSSES losing trades.
* Broker      - paper only: the trade is recorded, or SKIP with the reason.

The check can suggest; the risk gate can still reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .btc_watcher import Snapshotter, Trade, build_analysis
from .engine import Result, analyse
from .market_data import Candle
from .models import Level, fmt_price

MAX_COST_R = 0.35
MAX_DAILY_LOSSES = 2
MODEL_MIN_CONFIDENCE = 0.6

MODEL_QUESTIONS = {
    "action": {"type": "choice", "instructions": "Should a disciplined trader take this candidate trade?",
               "criteria": {"take": "the trade goes with the higher-timeframe trend and the context supports it",
                            "skip": "the context is against the trade, mixed, or unclear"}},
}


@dataclass
class Decision:
    trade: Trade
    taken: bool
    stage: str  # "laya" or "risk" when skipped, "broker" when taken
    reason: str
    confidence: Optional[float] = None


def read_at(snaps: Snapshotter, levels: list[Level], when: int, exec_tf: str = "15m") -> Result:
    return analyse(build_analysis("BTC", snaps.at(when), levels, when, exec_tf, 0.10, "funnel"))


def describe(t: Trade, read: Result, cost_pct: float) -> str:
    tf = ", ".join(f"{x.read.timeframe} {x.read.lean} {x.read.score:+d}" for x in read.timeframes)
    v = read.verdict
    stop_pct = t.risk / t.fill * 100
    rr = f"1:{t.planned_r:.1f}" if t.planned_r else "exit at session end"
    return (f"Candidate {t.setup.direction.upper()} {t.setup.kind} trade on BTC at {fmt_price(t.fill)}. "
            f"Market read: {tf}; total {v.total:+d}, overall {v.bias}, confidence {v.confidence}. "
            f"Stop {stop_pct:.2f}% away, target {rr}, fees {cost_pct:.2f}% round trip.")


_router = None


def model_check(text: str) -> tuple[bool, float]:
    global _router
    if _router is None:
        from laya import Router  # optional dependency (NandhaKishorM/laya)

        _router = Router()
    ans = _router.predict(text, MODEL_QUESTIONS)["answers"]["action"]
    conf = float(ans.get("answer_confidence", ans.get("confidence", 0.0)))
    return ans["choice"] == "take" and conf >= MODEL_MIN_CONFIDENCE, conf


def run(trades: list[Trade], candles_1m: list[Candle], levels: list[Level], check: str = "rules",
        cost_pct: float = 0.10, snaps: Optional[Snapshotter] = None) -> list[Decision]:
    snaps = snaps or Snapshotter(candles_1m)
    losses_by_day: dict[int, int] = {}
    out = []
    for t in sorted(trades, key=lambda x: x.opened):
        day = t.opened - t.opened % 86_400_000
        conf = None
        # Laya check
        if check != "none":
            read = read_at(snaps, levels, t.opened)
            if check == "rules":
                v = read.verdict
                ok = v.bias == ("bullish" if t.setup.direction == "long" else "bearish")
                conf = {"low": 0.4, "moderate": 0.7, "high": 0.9}[v.confidence]
                if not ok:
                    out.append(Decision(t, False, "laya", f"read is {v.bias} {v.total:+d}", conf))
                    continue
            elif check == "model":
                ok, conf = model_check(describe(t, read, cost_pct))
                if not ok:
                    out.append(Decision(t, False, "laya", f"model says skip ({conf:.2f})", conf))
                    continue
        # Risk gate
        cost_r = cost_pct / (t.risk / t.fill * 100) if t.risk else 9.9
        if cost_r > MAX_COST_R:
            out.append(Decision(t, False, "risk", f"fees {cost_r:.2f}R of the stop", conf))
            continue
        if losses_by_day.get(day, 0) >= MAX_DAILY_LOSSES:
            out.append(Decision(t, False, "risk", f"daily loss limit ({MAX_DAILY_LOSSES})", conf))
            continue
        # Paper broker
        out.append(Decision(t, True, "broker", "paper order", conf))
        if t.r_multiple(cost_pct) < 0:
            losses_by_day[day] = losses_by_day.get(day, 0) + 1
    return out

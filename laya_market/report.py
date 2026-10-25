"""Render an engine Result in Laya's fixed 7-part output format."""

from __future__ import annotations

import json

from .engine import WEAK_RR, Result, SetupResult
from .models import fmt_duration, fmt_price

DISCLAIMER = "Study example, not financial advice."


def _signed(n: int) -> str:
    return f"{n:+d}" if n else "0"


def _rr(x) -> str:
    return "-" if x is None else f"1:{x:.2f}"


def _target(price, rr) -> str:
    if price is None:
        return "-"
    return f"{fmt_price(price)} ({rr:.2f}R)" if rr is not None else fmt_price(price)


def _flags(r: SetupResult) -> str:
    flags = []
    if not r.valid:
        flags.append("INVALID")
    if r.weak:
        flags.append(f"weak (<1:{WEAK_RR})")
    if r.alignment == "counter-trend":
        flags.append("counter-trend")
    return ", ".join(flags) or "ok"


def render(res: Result) -> str:
    inp, v = res.inp, res.verdict
    out = []
    head = f"LAYA | {inp.symbol} | study read"
    if inp.analysed_at:
        head += f" | {inp.analysed_at}"
    if inp.source:
        head += f" | source: {inp.source}"
    out.append(head)
    out.append("")

    out.append("1. Verdict")
    out.append(f"   {v.bias.capitalize()} (total {_signed(v.total)}) | {v.action} | confidence: {v.confidence}")
    for reason in v.reasons:
        out.append(f"   - {reason}")
    for t in res.timeframes:
        if t.counter_trend:
            out.append(
                f"   - {t.read.timeframe} ({_signed(t.read.score)}) opposes the {t.higher_lean} higher "
                f"timeframes: treat moves in its direction as a counter-trend bounce"
            )
    out.append("")

    out.append("2. Timeframe alignment")
    out.append("| TF | Trend | Structure | Momentum | Location | Raw | Score | Lean |")
    out.append("|----|-------|-----------|----------|----------|-----|-------|------|")
    for t in res.timeframes:
        r = t.read
        lean = r.lean + (" (counter-trend bounce)" if t.counter_trend else "")
        out.append(
            f"| {r.timeframe} | {_signed(r.trend)} | {_signed(r.structure)} | {_signed(r.momentum)} | "
            f"{_signed(r.location)} | {_signed(r.raw)} | {_signed(r.score)} | {lean} |"
        )
    out.append(f"| Total | | | | | | {_signed(v.total)} | {v.bias} (<= -3 bearish, >= +3 bullish) |")
    for t in res.timeframes:
        r = t.read
        bits = []
        if r.last_price is not None:
            bits.append(f"last {fmt_price(r.last_price)}")
        if r.ma_fast is not None and r.ma_slow is not None:
            bits.append(f"MA9 {fmt_price(r.ma_fast)} / MA26 {fmt_price(r.ma_slow)}")
        if r.seconds_to_close is not None:
            bits.append(f"closes in {fmt_duration(r.seconds_to_close)}")
        bits.extend(r.notes)
        if bits:
            out.append(f"   {r.timeframe}: " + " | ".join(bits))
    out.append("")

    out.append("3. Setups")
    if not res.setups:
        out.append("   No setups: none supplied, or no drawn levels to build them from.")
    else:
        out.append("| # | Dir | Trigger (close only) | Entry | Stop | Target 1 | Target 2 | R:R (T1/T2) | Flags | Why |")
        out.append("|---|-----|----------------------|-------|------|----------|----------|-------------|-------|-----|")
        for r in res.setups:
            s = r.setup
            out.append(
                f"| {s.id} | {r.direction} | {s.trigger_text} | {fmt_price(s.entry)} | "
                f"{fmt_price(s.stop)} ({r.stop_pct:.2f}%) | {_target(s.target1, r.rr1)} | "
                f"{_target(s.target2, r.rr2)} | {_rr(r.rr1)} / {_rr(r.rr2)} | {_flags(r)} | {s.rationale} |"
            )
        out.append("   R:R = |target - entry| / |entry - stop|; weak below 1:1.5 to target 1.")
    out.append("")

    out.append("4. Ranking")
    ranked = [r for r in res.setups if r.valid]
    if ranked:
        order = " > ".join(f"#{r.setup.id} ({r.rank_score:g})" for r in ranked)
        out.append(f"   {order}  [alignment, confluence, stop size, R:R]")
        top = ranked[0]
        conf = f"{top.confluence} drawn level{'s' if top.confluence != 1 else ''} at the trigger"
        out.append(
            f"   #{top.setup.id} first: {top.alignment}, {conf}, "
            f"{top.stop_pct:.2f}% stop, {_rr(top.rr1)} to T1."
        )
    bad = [r for r in res.setups if not r.valid]
    for r in bad:
        out.append(f"   #{r.setup.id} not ranked: {'; '.join(r.errors)}")
    if not ranked and not bad:
        out.append("   Nothing to rank.")
    out.append("")

    out.append("5. Invalidation")
    out.extend(f"   - {line}" for line in res.invalidation)
    out.append("")

    out.append("6. Timing and costs")
    out.extend(f"   - {line}" for line in res.timing)
    out.append("")

    out.append("7. Blind spots")
    out.extend(f"   - {line}" for line in res.blind_spots)
    out.append("")

    if res.audit:
        out.append("Corrections to the supplied numbers")
        out.extend(f"   - {line}" for line in res.audit)
        out.append("")

    if res.question:
        out.append(f"Question: {res.question}")
        out.append("")

    out.append(DISCLAIMER)
    return "\n".join(out)


def to_dict(res: Result) -> dict:
    v = res.verdict
    return {
        "symbol": res.inp.symbol,
        "verdict": {"bias": v.bias, "total": v.total, "action": v.action, "confidence": v.confidence,
                    "reasons": v.reasons},
        "timeframes": [
            {"timeframe": t.read.timeframe, "trend": t.read.trend, "structure": t.read.structure,
             "momentum": t.read.momentum, "location": t.read.location, "raw": t.read.raw,
             "score": t.read.score, "lean": t.read.lean, "counter_trend": t.counter_trend,
             "last_price": t.read.last_price, "seconds_to_close": t.read.seconds_to_close}
            for t in res.timeframes
        ],
        "setups": [
            {"id": r.setup.id, "direction": r.direction, "trigger": r.setup.trigger_text,
             "entry": r.setup.entry, "stop": r.setup.stop, "stop_pct": round(r.stop_pct, 4),
             "target1": r.setup.target1, "target2": r.setup.target2,
             "rr1": None if r.rr1 is None else round(r.rr1, 3),
             "rr2": None if r.rr2 is None else round(r.rr2, 3),
             "net_rr1": None if r.net_rr1 is None else round(r.net_rr1, 3),
             "alignment": r.alignment, "confluence": r.confluence, "weak": r.weak,
             "errors": r.errors, "rank_score": r.rank_score, "rationale": r.setup.rationale}
            for r in res.setups
        ],
        "invalidation": res.invalidation,
        "timing": res.timing,
        "blind_spots": res.blind_spots,
        "audit": res.audit,
        "question": res.question,
        "disclaimer": DISCLAIMER,
    }


def render_json(res: Result) -> str:
    return json.dumps(to_dict(res), indent=2)

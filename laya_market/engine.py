"""Laya's rule engine: score timeframes, check setups, rank them, decide a verdict.

Everything here is deterministic arithmetic on the numbers it is given. It never
creates a price level: levels and setups come from the user (or, in the BTC
watcher, are built only from levels the user supplied).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import (
    REQUIRED_TFS,
    TF_MINUTES,
    AnalysisInput,
    Level,
    Setup,
    TimeframeRead,
    fmt_duration,
    fmt_price,
)

BIAS_THRESHOLD = 3  # total <= -3 bearish, >= +3 bullish, otherwise mixed
WEAK_RR = 1.5  # anything below 1:1.5 is flagged weak
CONFLUENCE_TOL_PCT = 0.15  # a drawn level within 0.15% of the trigger counts as confluence
STALE_CANDLES = (6, 8)  # levels go stale after about 6-8 candles of their timeframe
SHORT_TFS = ("1m", "5m")


@dataclass
class TimeframeResult:
    read: TimeframeRead
    higher_lean: str  # combined lean of all higher timeframes analysed
    counter_trend: bool


@dataclass
class SetupResult:
    setup: Setup
    direction: str
    risk: float
    stop_pct: float
    rr1: Optional[float]
    rr2: Optional[float]
    net_rr1: Optional[float]
    confluence: int
    alignment: str = "neutral"  # with-trend / counter-trend / neutral
    errors: list[str] = field(default_factory=list)
    rank_score: float = 0.0

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def weak(self) -> bool:
        return self.rr1 is not None and self.rr1 < WEAK_RR


@dataclass
class Verdict:
    bias: str
    total: int
    action: str
    confidence: str
    reasons: list[str]


@dataclass
class Result:
    inp: AnalysisInput
    timeframes: list[TimeframeResult]
    setups: list[SetupResult]  # ranked, best first; invalid ones at the end
    verdict: Verdict
    invalidation: list[str]
    timing: list[str]
    blind_spots: list[str]
    audit: list[str]
    question: Optional[str]


def bias_from_total(total: int) -> str:
    if total >= BIAS_THRESHOLD:
        return "bullish"
    if total <= -BIAS_THRESHOLD:
        return "bearish"
    return "mixed"


def _lean_of(value: int) -> str:
    return "bullish" if value > 0 else "bearish" if value < 0 else "neutral"


def align_timeframes(reads: list[TimeframeRead]) -> list[TimeframeResult]:
    """Top-down: a lower timeframe that opposes the higher ones is a counter-trend bounce."""
    ordered = sorted(reads, key=lambda r: r.rank)
    out = []
    running = 0
    for i, r in enumerate(ordered):
        higher = _lean_of(running) if i else "neutral"
        counter = higher != "neutral" and r.lean != "neutral" and r.lean != higher
        out.append(TimeframeResult(read=r, higher_lean=higher, counter_trend=counter))
        running += r.score
    return out


def evaluate_setup(s: Setup, levels: list[Level], cost_pct: float) -> SetupResult:
    long = s.direction == "long"
    risk = abs(s.entry - s.stop)
    errors = []
    if risk == 0:
        errors.append("stop equals entry")
    if long:
        if s.target1 <= s.entry:
            errors.append("long setup but target 1 is not above entry")
        if s.target2 is not None and s.target2 <= s.target1:
            errors.append("target 2 should sit beyond target 1")
        if s.stop >= s.trigger_level:
            errors.append("stop is not beyond (below) the trigger level that invalidates the idea")
    else:
        if s.target1 >= s.entry:
            errors.append("short setup but target 1 is not below entry")
        if s.target2 is not None and s.target2 >= s.target1:
            errors.append("target 2 should sit beyond target 1")
        if s.stop <= s.trigger_level:
            errors.append("stop is not beyond (above) the trigger level that invalidates the idea")

    def rr(target: Optional[float]) -> Optional[float]:
        if target is None or risk == 0:
            return None
        return abs(target - s.entry) / risk

    cost = s.entry * cost_pct / 100
    net = None
    if risk:
        net = (abs(s.target1 - s.entry) - cost) / (risk + cost)
    tol = s.trigger_level * CONFLUENCE_TOL_PCT / 100
    confluence = sum(1 for lv in levels if abs(lv.price - s.trigger_level) <= tol)
    return SetupResult(
        setup=s,
        direction=s.direction,
        risk=risk,
        stop_pct=risk / s.entry * 100 if s.entry else 0.0,
        rr1=rr(s.target1),
        rr2=rr(s.target2),
        net_rr1=net,
        confluence=confluence,
        errors=errors,
    )


def rank_setups(results: list[SetupResult], trend_lean: str) -> list[SetupResult]:
    """Rank by trend alignment, level confluence, stop size, then R:R.

    Points: alignment with-trend 3 / neutral 1 / counter 0; confluence up to 2;
    stop size up to 2 (tightest stop in the set scores 2); target-1 R:R up to 2
    (1:3 or better). Counter-trend setups always rank after the rest, and setups
    with errors are listed last and not ranked.
    """
    valid = [r for r in results if r.valid]
    tightest = min((r.stop_pct for r in valid if r.stop_pct > 0), default=0)
    for r in results:
        want = "long" if trend_lean == "bullish" else "short" if trend_lean == "bearish" else None
        r.alignment = "neutral" if want is None else "with-trend" if r.direction == want else "counter-trend"
        if not r.valid:
            r.rank_score = 0.0
            continue
        align_pts = {"with-trend": 3, "neutral": 1, "counter-trend": 0}[r.alignment]
        stop_pts = 2 * tightest / r.stop_pct if r.stop_pct > 0 else 0
        rr_pts = min(r.rr1 or 0, 3) * 2 / 3
        r.rank_score = round(align_pts + min(r.confluence, 2) + stop_pts + rr_pts, 2)
    return sorted(
        results,
        key=lambda r: (not r.valid, r.alignment == "counter-trend", -r.rank_score, r.setup.id),
    )


def _verdict(tfs: list[TimeframeResult], ranked: list[SetupResult]) -> Verdict:
    total = sum(t.read.score for t in tfs)
    bias = bias_from_total(total)
    reasons = []
    if bias == "mixed":
        confidence = "low"
        reasons.append(f"total {total:+d} is inside the -{BIAS_THRESHOLD}..+{BIAS_THRESHOLD} mixed band")
    else:
        agree = sum(1 for t in tfs if t.read.lean == bias)
        confidence = "high" if agree == len(tfs) and len(tfs) >= 4 else "moderate" if agree >= 3 else "low"
        if agree < len(tfs):
            against = [t.read.timeframe for t in tfs if t.read.lean != bias]
            reasons.append(f"{', '.join(against)} not aligned with the {bias} total")

    action = "WAIT"
    top = next((r for r in ranked if r.valid), None)
    if bias == "mixed":
        reasons.append("no directional edge")
    elif top is None:
        reasons.append("no valid setup to trigger")
    elif top.alignment != "with-trend":
        reasons.append(f"best valid setup (#{top.setup.id}) is not with the {bias} bias")
    elif top.weak:
        reasons.append(f"top setup #{top.setup.id} pays only 1:{top.rr1:.2f} to target 1 (< 1:{WEAK_RR})")
    else:
        action = "ENTER-ON-TRIGGER"
        reasons.append(f"top setup #{top.setup.id} is with-trend at 1:{top.rr1:.2f} to target 1")
    return Verdict(bias=bias, total=total, action=action, confidence=confidence, reasons=reasons)


def _invalidation(tfs: list[TimeframeResult], ranked: list[SetupResult], levels: list[Level]) -> list[str]:
    lines = []
    longs = [r for r in ranked if r.valid and r.direction == "long"]
    shorts = [r for r in ranked if r.valid and r.direction == "short"]
    for t in tfs:
        r = t.read
        if r.lean == "neutral":
            lines.append(f"{r.timeframe}: neutral, nothing to cancel; a close through a drawn level sets the lean")
            continue
        level = None
        if r.last_price is not None and levels:
            if r.lean == "bullish":
                below = [lv.price for lv in levels if lv.price < r.last_price]
                level = max(below) if below else None
            else:
                above = [lv.price for lv in levels if lv.price > r.last_price]
                level = min(above) if above else None
        if level is None:
            # Fall back to the trigger of the best opposite setup: if it fires, the lean is wrong.
            opposite = shorts if r.lean == "bullish" else longs
            if opposite:
                level = (max if r.lean == "bullish" else min)(o.setup.trigger_level for o in opposite)
        if level is None:
            lines.append(f"{r.timeframe}: {r.lean} lean, but no drawn level on the other side to define a cancel")
            continue
        side = "below" if r.lean == "bullish" else "above"
        lines.append(f"{r.timeframe}: a close {side} {fmt_price(level)} cancels the {r.lean} lean")
    for res in ranked:
        if not res.valid:
            continue
        s = res.setup
        back = "below" if res.direction == "long" else "above"
        lines.append(
            f"Setup {s.id}: after entry, a {s.timeframe} close back {back} {fmt_price(s.trigger_level)} "
            f"cancels it; {fmt_price(s.stop)} is the hard stop"
        )
    return lines


def _timing(inp: AnalysisInput, tfs: list[TimeframeResult], ranked: list[SetupResult]) -> list[str]:
    lines = []
    for t in tfs:
        r = t.read
        if r.seconds_to_close is not None:
            lines.append(
                f"{r.timeframe} candle closes in {fmt_duration(r.seconds_to_close)}; "
                f"no {r.timeframe} close-based rule counts before then"
            )
    exec_read = next((t.read for t in tfs if t.read.timeframe == inp.exec_timeframe), None)
    if exec_read is None or exec_read.seconds_to_close is None:
        lines.append(f"{inp.exec_timeframe} countdown not readable; wait for a confirmed close before acting")
    stale = []
    for t in tfs:
        lo, hi = (n * TF_MINUTES[t.read.timeframe] for n in STALE_CANDLES)
        stale.append(f"{t.read.timeframe} {_minutes(lo)}-{_minutes(hi)}")
    lines.append("Levels go stale after ~6-8 candles: " + ", ".join(stale))
    short = [r for r in ranked if r.valid and r.setup.timeframe in SHORT_TFS]
    if short:
        parts = []
        for r in short:
            net = f"net 1:{r.net_rr1:.2f}" if r.net_rr1 is not None else "net n/a"
            parts.append(f"#{r.setup.id} stop {r.stop_pct:.2f}% ({net})")
        lines.append(
            f"Stops as % of price, T1 R:R after an assumed {inp.round_trip_cost_pct:.2f}% round-trip cost: "
            + "; ".join(parts)
        )
        lines.append("Fees, spread and slippage can eat most of a tight 1m/5m stop's R:R")
    return lines


def _minutes(m: int) -> str:
    if m >= 60:
        h = m / 60
        return f"{h:g}h"
    return f"{m}m"


def _blind_spots(inp: AnalysisInput) -> list[str]:
    have = {t.timeframe for t in inp.timeframes}
    spots = []
    missing = [tf for tf in REQUIRED_TFS if tf not in have]
    if missing:
        spots.append(f"Missing timeframes: {', '.join(missing)}")
    if not inp.levels:
        spots.append("No drawn levels supplied, so location and setups can't be anchored")
    if not inp.has_volume:
        spots.append("No volume data: can't judge participation behind a move")
    spots.extend(s for s in inp.blind_spots if s not in spots)
    if not any("news" in s.lower() or "calendar" in s.lower() for s in spots):
        spots.append("Scheduled news/economic calendar not checked; news can blow through any level")
    return spots


def _audit(inp: AnalysisInput, tfs: list[TimeframeResult], ranked: list[SetupResult], v: Verdict) -> list[str]:
    """Compare what the supplied write-up claimed against the recomputed numbers."""
    notes = []
    for t in tfs:
        r = t.read
        if r.claimed_total is not None and int(r.claimed_total) != r.raw:
            notes.append(f"{r.timeframe}: claimed total {int(r.claimed_total):+d}, components sum to {r.raw:+d}")
        accepted = {r.lean} | ({"mixed", "neutral"} if r.lean == "neutral" else set())
        if r.claimed_bias and r.claimed_bias.lower() not in accepted:
            notes.append(f"{r.timeframe}: labelled {r.claimed_bias}, but its score {r.score:+d} leans {r.lean}")
        if r.claimed_total is not None and r.raw != r.score:
            notes.append(f"{r.timeframe}: raw {r.raw:+d} clamped to {r.score:+d} (each timeframe scores -2..+2)")
    for res in ranked:
        s = res.setup
        if s.claimed_rr is not None and res.rr1 is not None:
            best = max(x for x in (res.rr1, res.rr2) if x is not None)
            if abs(s.claimed_rr - best) > 0.05:
                t2 = f", T2 1:{res.rr2:.2f}" if res.rr2 is not None else ""
                notes.append(
                    f"Setup {s.id}: claimed R:R {s.claimed_rr:g}, actual is T1 1:{res.rr1:.2f}{t2} "
                    f"(risk {fmt_price(res.risk)} from entry {fmt_price(s.entry)} to stop {fmt_price(s.stop)})"
                )
        for e in res.errors:
            notes.append(f"Setup {s.id}: {e}")
    cv = inp.claimed_verdict
    if cv.get("bias") and cv["bias"].lower() != v.bias:
        notes.append(
            f"Verdict: claimed {cv['bias']}, but total {v.total:+d} is {v.bias} under the "
            f"<= -{BIAS_THRESHOLD} / >= +{BIAS_THRESHOLD} rule"
        )
    if cv.get("action") and cv["action"].upper() != v.action:
        notes.append(f"Verdict: claimed {cv['action']}, rules give {v.action}")
    return notes


def _question(inp: AnalysisInput) -> Optional[str]:
    have = {t.timeframe for t in inp.timeframes}
    missing = [tf for tf in REQUIRED_TFS if tf not in have]
    if missing:
        return f"Can you share the {missing[0]} chart? The top-down read is incomplete without it."
    if not inp.levels:
        return f"Which horizontal levels have you drawn on {inp.symbol}?"
    return None


def analyse(inp: AnalysisInput) -> Result:
    tfs = align_timeframes(inp.timeframes)
    total = sum(t.read.score for t in tfs)
    trend_lean = bias_from_total(total)
    if trend_lean == "mixed":
        # Fall back to the highest timeframe's lean so counter-trend ideas still rank last.
        trend_lean = tfs[0].read.lean if tfs else "neutral"
    results = [evaluate_setup(s, inp.levels, inp.round_trip_cost_pct) for s in inp.setups]
    ranked = rank_setups(results, trend_lean)
    verdict = _verdict(tfs, ranked)
    return Result(
        inp=inp,
        timeframes=tfs,
        setups=ranked,
        verdict=verdict,
        invalidation=_invalidation(tfs, ranked, inp.levels),
        timing=_timing(inp, tfs, ranked),
        blind_spots=_blind_spots(inp),
        audit=_audit(inp, tfs, ranked, verdict),
        question=_question(inp),
    )

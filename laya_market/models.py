"""Plain data types shared by the engine, the report and the BTC watcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Highest timeframe first. Top-down analysis walks this order.
TF_ORDER = ("1d", "4h", "1h", "15m", "5m", "1m")
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
REQUIRED_TFS = ("1h", "15m", "5m", "1m")


class InputError(ValueError):
    """Raised when an analysis input is malformed."""


def _component(value, name: str, tf: str) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise InputError(f"{tf}: {name} must be an integer from -2 to +2, got {value!r}")
    if not -2 <= v <= 2:
        raise InputError(f"{tf}: {name} must be from -2 to +2, got {v}")
    return v


@dataclass
class TimeframeRead:
    """One timeframe's read: four component scores plus what was visible on the chart."""

    timeframe: str
    trend: int = 0
    structure: int = 0
    momentum: int = 0
    location: int = 0
    last_price: Optional[float] = None
    ma_fast: Optional[float] = None
    ma_slow: Optional[float] = None
    seconds_to_close: Optional[float] = None
    notes: list[str] = field(default_factory=list)
    # What the supplied write-up claimed, so the engine can audit it.
    claimed_total: Optional[int] = None
    claimed_bias: Optional[str] = None

    @property
    def raw(self) -> int:
        return self.trend + self.structure + self.momentum + self.location

    @property
    def score(self) -> int:
        """Timeframe score, clamped to the -2..+2 range the method uses."""
        return max(-2, min(2, self.raw))

    @property
    def lean(self) -> str:
        return "bullish" if self.score > 0 else "bearish" if self.score < 0 else "neutral"

    @property
    def rank(self) -> int:
        return TF_ORDER.index(self.timeframe) if self.timeframe in TF_ORDER else len(TF_ORDER)

    @classmethod
    def from_dict(cls, d: dict) -> "TimeframeRead":
        tf = str(d.get("timeframe", "")).strip()
        if tf not in TF_MINUTES:
            raise InputError(f"unknown timeframe {tf!r}; use one of {', '.join(TF_MINUTES)}")
        return cls(
            timeframe=tf,
            trend=_component(d.get("trend", 0), "trend", tf),
            structure=_component(d.get("structure", 0), "structure", tf),
            momentum=_component(d.get("momentum", 0), "momentum", tf),
            location=_component(d.get("location", 0), "location", tf),
            last_price=_opt_float(d.get("last_price")),
            ma_fast=_opt_float(d.get("ma_fast")),
            ma_slow=_opt_float(d.get("ma_slow")),
            seconds_to_close=_opt_float(d.get("seconds_to_close")),
            notes=list(d.get("notes", [])),
            claimed_total=d.get("claimed_total"),
            claimed_bias=d.get("claimed_bias"),
        )


@dataclass
class Level:
    """A key level. Only levels the user drew (or typed in) belong here."""

    price: float
    label: str = ""

    @classmethod
    def from_dict(cls, d) -> "Level":
        if isinstance(d, (int, float)):
            return cls(price=float(d))
        return cls(price=float(d["price"]), label=str(d.get("label", "")))


@dataclass
class Setup:
    """A candidate trade idea. Direction is inferred from where the stop sits."""

    id: int
    trigger_side: str  # "above" or "below": the candle must CLOSE above/below trigger_level
    trigger_level: float
    entry: float
    stop: float
    target1: float
    target2: Optional[float] = None
    timeframe: str = "5m"
    rationale: str = ""
    trigger_note: str = ""
    claimed_rr: Optional[float] = None
    kind: str = ""  # breakout / breakdown / bounce / rejection (set by the BTC watcher)

    @property
    def direction(self) -> str:
        return "long" if self.stop < self.entry else "short"

    @property
    def trigger_text(self) -> str:
        text = f"{self.timeframe} close {self.trigger_side} {fmt_price(self.trigger_level)}"
        return f"{text} {self.trigger_note}".strip()

    @classmethod
    def from_dict(cls, d: dict, default_id: int, default_tf: str) -> "Setup":
        side = str(d.get("trigger_side", "")).lower()
        if side not in ("above", "below"):
            raise InputError(f"setup {default_id}: trigger_side must be 'above' or 'below'")
        t2 = d.get("target2")
        return cls(
            id=int(d.get("id", default_id)),
            trigger_side=side,
            trigger_level=float(d["trigger_level"]),
            entry=float(d["entry"]),
            stop=float(d["stop"]),
            target1=float(d["target1"]),
            target2=None if t2 is None else float(t2),
            timeframe=str(d.get("timeframe", default_tf)),
            rationale=str(d.get("rationale", "")),
            trigger_note=str(d.get("trigger_note", "")),
            claimed_rr=_opt_float(d.get("claimed_rr")),
        )


@dataclass
class AnalysisInput:
    symbol: str
    timeframes: list[TimeframeRead]
    levels: list[Level] = field(default_factory=list)
    setups: list[Setup] = field(default_factory=list)
    exec_timeframe: str = "5m"
    round_trip_cost_pct: float = 0.10
    has_volume: bool = False
    blind_spots: list[str] = field(default_factory=list)
    source: str = ""
    analysed_at: str = ""
    claimed_verdict: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "AnalysisInput":
        if not d.get("timeframes"):
            raise InputError("input needs at least one entry under 'timeframes'")
        exec_tf = str(d.get("exec_timeframe", "5m"))
        tfs = [TimeframeRead.from_dict(t) for t in d["timeframes"]]
        seen = [t.timeframe for t in tfs]
        if len(seen) != len(set(seen)):
            raise InputError("each timeframe may appear only once")
        return cls(
            symbol=str(d.get("symbol", "UNKNOWN")),
            timeframes=tfs,
            levels=[Level.from_dict(x) for x in d.get("levels", [])],
            setups=[Setup.from_dict(s, i + 1, exec_tf) for i, s in enumerate(d.get("setups", []))],
            exec_timeframe=exec_tf,
            round_trip_cost_pct=float(d.get("round_trip_cost_pct", 0.10)),
            has_volume=bool(d.get("has_volume", False)),
            blind_spots=list(d.get("blind_spots", [])),
            source=str(d.get("source", "")),
            analysed_at=str(d.get("analysed_at", "")),
            claimed_verdict=dict(d.get("claimed_verdict", {})),
        )


def _opt_float(v) -> Optional[float]:
    return None if v is None else float(v)


def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "-"
    if abs(p) >= 1000:
        return f"{p:,.2f}"
    if abs(p) >= 10:
        return f"{p:.2f}"
    return f"{p:.4f}"


def fmt_duration(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"

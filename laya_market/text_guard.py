"""Optional text guard on free text (notes, questions, pasted headlines).

Uses the `laya` decision model from https://github.com/NandhaKishorM/laya when it is
installed (`pip install laya`, needs torch), else a keyword fallback. It answers two
yes/no questions only:

  * is the text asking for a guaranteed buy/sell call?  -> refuse, the engine never gives one
  * does it mention a scheduled high-impact event?      -> add a news blind spot

It never sets bias or action. In testing, the model's yes/no probabilities separated
these two cases cleanly, but its "wait or enter" answers were near 50/50, so trade
decisions stay with the rule engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

THRESHOLD = 0.7

QUESTIONS = {
    "guarantee": {"type": "noul", "instructions": "Is the user asking for a guaranteed buy or sell call?"},
    "news": {"type": "noul",
             "instructions": "Does this mention a scheduled high-impact economic event such as CPI, FOMC or NFP?"},
}

_GUARANTEE = re.compile(
    r"\b(guarantee[d]?|sure[- ]?shot|100 ?%|can'?t lose|risk[- ]?free|should i (buy|sell)|"
    r"(buy|sell) (it )?now\??|tell me (to|what to) (buy|sell))\b", re.I)
_NEWS = re.compile(
    r"\b(cpi|ppi|fomc|fed (rate|decision|minutes|chair)|nfp|non[- ]?farm|payrolls|gdp|pce|"
    r"rate decision|interest rate|jobless claims|ecb|boe|boj|powell|earnings|etf (approval|decision))\b", re.I)

_router = None


@dataclass
class GuardResult:
    guarantee: float
    news: float
    backend: str

    @property
    def asks_for_call(self) -> bool:
        return self.guarantee >= THRESHOLD

    @property
    def mentions_news(self) -> bool:
        return self.news >= THRESHOLD


def _laya_router():
    global _router
    if _router is None:
        from laya import Router  # optional dependency

        _router = Router()
    return _router


def check(text: str, backend: str = "auto") -> GuardResult:
    """backend: 'auto' (laya if importable, else keywords), 'laya', or 'keywords'."""
    if backend in ("auto", "laya"):
        try:
            answers = _laya_router().predict(text, QUESTIONS)["answers"]
            return GuardResult(float(answers["guarantee"]["noul"]), float(answers["news"]["noul"]), "laya")
        except ImportError:
            if backend == "laya":
                raise
    return GuardResult(
        guarantee=1.0 if _GUARANTEE.search(text) else 0.0,
        news=1.0 if _NEWS.search(text) else 0.0,
        backend="keywords",
    )


REFUSAL = ("Laya doesn't give guaranteed calls or 'buy/sell now' orders. "
           "Below is the scored, rule-based read to paper trade and journal.")

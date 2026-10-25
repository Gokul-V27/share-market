"""Laya: a rule-based chart-analysis and decision engine for study and paper trading."""

from .engine import analyse
from .models import AnalysisInput

__all__ = ["analyse", "AnalysisInput"]
__version__ = "0.1.0"

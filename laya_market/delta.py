"""Delta Exchange (India) client: public candles and READ-ONLY account views.

Credentials come from the environment or a local `.env` file (DELTA_API_KEY, DELTA_API_SECRET,
DELTA_BASE_URL). `.env` is git-ignored; never commit it. This module has no order placement:
Laya is for study and paper trading.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .market_data import TF_MS, Candle, DataError

DEFAULT_BASE = "https://api.india.delta.exchange"
USER_AGENT = "laya-market/0.1"
RESOLUTIONS = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}


def load_env(path: Optional[str] = None) -> None:
    """Read KEY=VALUE lines from .env into os.environ (existing variables win)."""
    p = Path(path) if path else Path.cwd() / ".env"
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def base_url() -> str:
    load_env()
    return os.environ.get("DELTA_BASE_URL", DEFAULT_BASE).rstrip("/")


def sign(secret: str, method: str, timestamp: str, path: str, query: str = "", body: str = "") -> str:
    """Delta signature: HMAC-SHA256(secret, method + timestamp + path + query_string + body), hex."""
    msg = method + timestamp + path + query + body
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def _request(method: str, path: str, params: Optional[dict] = None, auth: bool = False, timeout: float = 15):
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if auth:
        load_env()
        key, secret = os.environ.get("DELTA_API_KEY"), os.environ.get("DELTA_API_SECRET")
        if not key or not secret:
            raise DataError("DELTA_API_KEY / DELTA_API_SECRET not set (put them in a local .env)")
        ts = str(int(time.time()))
        headers.update({"api-key": key, "timestamp": ts, "signature": sign(secret, method, ts, path, query)})
    req = urllib.request.Request(base_url() + path + query, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e)
        except Exception:
            detail = {}
        err = detail.get("error", detail) if isinstance(detail, dict) else detail
        raise DataError(f"Delta {method} {path}: HTTP {e.code} {err}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise DataError(f"Delta {method} {path}: {e}") from e
    if not data.get("success", False):
        raise DataError(f"Delta {method} {path}: {data.get('error', data)}")
    return data.get("result")


def parse_candles(rows: list, tf: str) -> list[Candle]:
    out = [Candle(int(r["time"]) * 1000, float(r["open"]), float(r["high"]), float(r["low"]),
                  float(r["close"]), float(r.get("volume") or 0), TF_MS[tf]) for r in rows]
    return sorted(out, key=lambda c: c.open_time)


def fetch_candles(symbol: str = "BTCUSD", tf: str = "5m", limit: int = 150) -> list[Candle]:
    """Recent candles, oldest first; the last one is usually still forming."""
    end = int(time.time())
    start = end - (limit + 1) * TF_MS[tf] // 1000
    rows = _request("GET", "/v2/history/candles",
                    {"resolution": RESOLUTIONS[tf], "symbol": symbol, "start": start, "end": end})
    return parse_candles(rows or [], tf)[-limit:]


def wallet_balances() -> list[dict]:
    """READ-ONLY: wallet balances for the API key's account."""
    return _request("GET", "/v2/wallet/balances", auth=True) or []


def positions() -> list[dict]:
    """READ-ONLY: open margined positions."""
    return _request("GET", "/v2/positions/margined", auth=True) or []

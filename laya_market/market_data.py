"""Candles from public exchange APIs or local CSV files (stdlib only)."""

from __future__ import annotations

import csv
import gzip
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
BINANCE_BASES = ("https://data-api.binance.vision", "https://api.binance.com")
COINBASE_BASE = "https://api.exchange.coinbase.com"
COINBASE_GRANULARITY = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "1d": 86400}
USER_AGENT = "laya-btc-watcher/0.1"


class DataError(RuntimeError):
    pass


@dataclass
class Candle:
    open_time: int  # ms since epoch, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    interval_ms: int

    @property
    def close_time(self) -> int:
        """First millisecond after this candle (when it closes)."""
        return self.open_time + self.interval_ms

    def is_closed(self, now_ms: int) -> bool:
        return now_ms >= self.close_time


def _get_json(url: str, timeout: float = 10):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise DataError(f"{url}: {e}") from e


def parse_binance(rows: list, tf: str) -> list[Candle]:
    if isinstance(rows, dict):
        raise DataError(f"binance error: {rows.get('msg', rows)}")
    return [
        Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]), TF_MS[tf])
        for r in rows
    ]


def parse_coinbase(rows: list, tf: str) -> list[Candle]:
    if isinstance(rows, dict):
        raise DataError(f"coinbase error: {rows.get('message', rows)}")
    # Coinbase rows are [time_s, low, high, open, close, volume], newest first.
    out = [
        Candle(int(r[0]) * 1000, float(r[3]), float(r[2]), float(r[1]), float(r[4]), float(r[5]), TF_MS[tf])
        for r in rows
    ]
    return sorted(out, key=lambda c: c.open_time)


def coinbase_product(symbol: str) -> str:
    s = symbol.upper().replace("-", "")
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote):
            return f"{s[: -len(quote)]}-USD"
    return symbol


def fetch_candles(symbol: str, tf: str, limit: int = 120, source: str = "auto") -> tuple[list[Candle], str]:
    """Return (candles oldest-first, source name). The last candle is usually still forming."""
    errors = []
    if source == "delta":
        from . import delta  # Delta Exchange India, same BTCUSD contract as the trades CSV

        try:
            return delta.fetch_candles(symbol, tf, limit), f"Delta Exchange {symbol}"
        except DataError as e:
            raise DataError(f"no data source worked:\n  {e}") from e
    if source in ("auto", "binance"):
        for base in BINANCE_BASES:
            url = f"{base}/api/v3/klines?symbol={symbol.upper()}&interval={tf}&limit={limit}"
            try:
                return parse_binance(_get_json(url), tf), f"Binance spot {symbol.upper()}"
            except DataError as e:
                errors.append(str(e))
    if source in ("auto", "coinbase"):
        if tf not in COINBASE_GRANULARITY:
            errors.append(f"coinbase has no {tf} candles")
        else:
            product = coinbase_product(symbol)
            url = f"{COINBASE_BASE}/products/{product}/candles?granularity={COINBASE_GRANULARITY[tf]}"
            try:
                return parse_coinbase(_get_json(url), tf)[-limit:], f"Coinbase {product}"
            except DataError as e:
                errors.append(str(e))
    raise DataError("no data source worked:\n  " + "\n  ".join(errors))


def _parse_ts(value: str) -> int:
    value = value.strip()
    if value.replace(".", "", 1).isdigit():
        n = float(value)
        return int(n if n > 1e11 else n * 1000)  # ms or s epoch
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def trades_to_candles(trades: Iterable[tuple[int, float, float]], tf: str = "1m") -> list[Candle]:
    """Aggregate (ts_ms, price, size) trades into OHLCV candles. Empty intervals are skipped."""
    step = TF_MS[tf]
    out: list[Candle] = []
    cur: Optional[Candle] = None
    for ts, price, size in trades:
        start = ts - ts % step
        if cur is None or start != cur.open_time:
            if cur is not None and start < cur.open_time:
                raise DataError("trades must be in time order")
            cur = Candle(start, price, price, price, price, 0.0, step)
            out.append(cur)
        cur.high = max(cur.high, price)
        cur.low = min(cur.low, price)
        cur.close = price
        cur.volume += size
    return out


def load_csv(path: str, tf: str = "1m") -> list[Candle]:
    """Load either a trades CSV (price,size,timestamp) or an OHLCV CSV (timestamp,open,high,low,close,volume)."""
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", newline="") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
        ts_col = next((cols[c] for c in ("timestamp", "time", "open_time", "date", "datetime") if c in cols), None)
        if ts_col is None:
            raise DataError(f"{path}: no timestamp column (have {', '.join(cols)})")
        if {"open", "high", "low", "close"} <= cols.keys():
            vol = cols.get("volume")
            candles = [
                Candle(_parse_ts(r[ts_col]), float(r[cols["open"]]), float(r[cols["high"]]),
                       float(r[cols["low"]]), float(r[cols["close"]]),
                       float(r[vol]) if vol and r[vol] else 0.0, TF_MS[tf])
                for r in reader
            ]
            return sorted(candles, key=lambda c: c.open_time)
        if "price" in cols:
            size = cols.get("size") or cols.get("qty") or cols.get("quantity")
            trades = (
                (_parse_ts(r[ts_col]), float(r[cols["price"]]), float(r[size]) if size else 0.0)
                for r in reader
            )
            return trades_to_candles(trades, tf)
    raise DataError(f"{path}: expected OHLC columns or a price column")


def write_candles_csv(candles: list[Candle], path: str) -> None:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            ts = datetime.fromtimestamp(c.open_time / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            w.writerow([ts, f"{c.open:g}", f"{c.high:g}", f"{c.low:g}", f"{c.close:g}", f"{c.volume:g}"])


def resample(candles: list[Candle], tf: str, now_ms: Optional[int] = None) -> list[Candle]:
    """Build higher-timeframe candles from lower ones, using only candles closed by now_ms.

    The last returned candle is the one still forming at now_ms (if any of its
    sub-candles have closed), matching what a live chart shows.
    """
    step = TF_MS[tf]
    out: list[Candle] = []
    for c in candles:
        if now_ms is not None and not c.is_closed(now_ms):
            break
        start = c.open_time - c.open_time % step
        if out and out[-1].open_time == start:
            b = out[-1]
            b.high = max(b.high, c.high)
            b.low = min(b.low, c.low)
            b.close = c.close
            b.volume += c.volume
        else:
            out.append(Candle(start, c.open, c.high, c.low, c.close, c.volume, step))
    return out


def fetch_history(symbol: str, start_ms: int, end_ms: int, tf: str = "1m", progress=None) -> list[Candle]:
    """Closed candles in [start_ms, end_ms) from Binance's public market-data API, 1000 per request."""
    step = TF_MS[tf]
    out: list[Candle] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (f"{BINANCE_BASES[0]}/api/v3/klines?symbol={symbol.upper()}&interval={tf}"
               f"&startTime={cursor}&endTime={end_ms - 1}&limit=1000")
        batch = parse_binance(_get_json(url, timeout=30), tf)
        if not batch:
            break
        out.extend(c for c in batch if c.open_time < end_ms)
        cursor = batch[-1].open_time + step
        if progress:
            progress(len(out))
    return out

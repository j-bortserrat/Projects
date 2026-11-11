"""Histórico de velas con caché en disco, para no volver a descargar lo ya bajado."""
from __future__ import annotations

import json
import os

from .market import Candle


def _path(cache_dir: str, symbol: str, timeframe: str) -> str:
    return os.path.join(cache_dir, f"{symbol.replace('/', '-')}_{timeframe}.json")


def load_cached(cache_dir: str, symbol: str, timeframe: str) -> list[Candle]:
    path = _path(cache_dir, symbol, timeframe)
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [Candle(*row) for row in json.load(fh)]


def save_cached(cache_dir: str, symbol: str, timeframe: str, candles: list[Candle]) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    with open(_path(cache_dir, symbol, timeframe), "w") as fh:
        json.dump([[c.ts, c.open, c.high, c.low, c.close, c.volume] for c in candles], fh)


def load_or_fetch(market, cache_dir: str, symbol: str, timeframe: str, tf_ms: int,
                  start_ms: int, end_ms: int) -> list[Candle]:
    """Devuelve velas cerradas en [start_ms, end_ms), descargando solo lo que falte."""
    have = {c.ts: c for c in load_cached(cache_dir, symbol, timeframe)}
    first, last = (min(have), max(have)) if have else (None, None)
    spans = []
    if first is None:
        spans.append((start_ms, end_ms))
    else:
        if start_ms < first:
            spans.append((start_ms, first))
        if end_ms > last + tf_ms:
            spans.append((last + tf_ms, end_ms))
    fetched = False
    for a, b in spans:
        for c in market.history(symbol, timeframe, a, b, tf_ms):
            have[c.ts] = c
            fetched = True
    if fetched:
        save_cached(cache_dir, symbol, timeframe, [have[k] for k in sorted(have)])
    return [have[k] for k in sorted(have) if start_ms <= k < end_ms]

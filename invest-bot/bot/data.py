"""Proveedores de datos de mercado.

* ``YahooProvider``     -> datos reales (yfinance). Es el que debes usar.
* ``SyntheticProvider`` -> precios inventados pero deterministas. Solo sirve para probar
  el programa en entornos sin acceso a Internet. El panel avisa cuando se usa.
"""
from __future__ import annotations

import time
import zlib
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


class DataError(RuntimeError):
    pass


class YahooProvider:
    name = "yahoo"

    def __init__(self, ttl_seconds: int = 900):
        self.ttl = ttl_seconds
        self._cache: dict[tuple[str, int], tuple[float, pd.DataFrame]] = {}

    def history(self, symbol: str, days: int) -> pd.DataFrame:
        key = (symbol, days)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        import yfinance as yf  # import perezoso: no hace falta para el modo sintético

        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        try:
            df = yf.Ticker(symbol).history(start=start, interval="1d", auto_adjust=True)
        except Exception as exc:  # red, límites de Yahoo, ticker inexistente...
            raise DataError(f"{symbol}: {exc}") from exc
        if df is None or df.empty:
            raise DataError(f"{symbol}: Yahoo no devolvió datos")
        df = df[COLUMNS].dropna(subset=["Close"]).copy()
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        df = df[~df.index.duplicated(keep="last")]
        self._cache[key] = (time.time(), df)
        return df


class SyntheticProvider:
    """Genera series deterministas por ticker, ancladas a una fecha fija para que
    los precios de ayer sean iguales mañana y solo se añadan días nuevos."""

    name = "synthetic"
    _ANCHOR = "2012-01-02"

    def history(self, symbol: str, days: int) -> pd.DataFrame:
        rng = np.random.default_rng(zlib.crc32(symbol.encode()))
        mu = rng.uniform(-0.05, 0.30)
        sigma = rng.uniform(0.14, 0.45)
        period = rng.uniform(350, 900)
        phase = rng.uniform(0, 2 * np.pi)

        idx = pd.bdate_range(self._ANCHOR, pd.Timestamp.today().normalize())
        n = len(idx)
        i = np.arange(n)
        # Un generador por día haría que añadir días cambiase el pasado; en su lugar
        # se generan todos los valores en bloque con una semilla fija por ticker y
        # por tipo de dato, de modo que el prefijo es estable al crecer `n`.
        seed = zlib.crc32(symbol.encode())
        shocks = np.random.default_rng([seed, 1]).normal(0, 1, n)
        open_noise = np.random.default_rng([seed, 2]).normal(0, 0.003, n)
        spread = np.abs(np.random.default_rng([seed, 3]).normal(0, 0.006, n))
        volume = np.random.default_rng([seed, 4]).lognormal(mean=15, sigma=0.3, size=n)

        regime = 0.25 * np.sin(2 * np.pi * i / period + phase)
        daily = (mu + regime) / 252 - 0.5 * sigma**2 / 252 + sigma / np.sqrt(252) * shocks
        close = 100 * np.exp(np.cumsum(daily))
        prev = np.concatenate([[close[0]], close[:-1]])
        open_ = prev * (1 + open_noise)
        high = np.maximum(open_, close) * (1 + spread)
        low = np.minimum(open_, close) * (1 - spread)

        df = pd.DataFrame(
            {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume}, index=idx
        )
        keep = int(days * 252 / 365) + 1
        return df.iloc[-keep:]


def make_provider(kind: str | None):
    kind = (kind or "yahoo").lower()
    if kind == "synthetic":
        return SyntheticProvider()
    if kind == "yahoo":
        return YahooProvider()
    raise ValueError(f"DATA_PROVIDER desconocido: {kind!r} (usa 'yahoo' o 'synthetic')")

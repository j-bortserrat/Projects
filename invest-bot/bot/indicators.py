"""Indicadores técnicos calculados sobre velas diarias."""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_BARS = 252  # hace falta ~1 año de historia para SMA200 y máximo de 52 semanas


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    # sin pérdidas en la ventana -> RSI 100
    return out.where(loss != 0, 100.0).where(gain.notna())


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close, volume = df["Close"], df["Volume"]
    out = pd.DataFrame(index=df.index)
    out["open"] = df["Open"]
    out["close"] = close
    out["sma50"] = close.rolling(50).mean()
    out["sma200"] = close.rolling(200).mean()
    out["rsi"] = rsi(close)
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    out["macd"] = macd
    out["macd_signal"] = signal
    out["macd_hist"] = macd - signal
    out["macd_hist_prev"] = out["macd_hist"].shift(3)
    out["vol_ratio"] = volume.rolling(20).mean() / volume.rolling(60).mean().replace(0, np.nan)
    out["high52"] = df["High"].rolling(MIN_BARS).max()
    out["ret63"] = close.pct_change(63)
    out["vol30"] = close.pct_change().rolling(30).std() * np.sqrt(252)
    return out

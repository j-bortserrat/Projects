"""Horarios de las bolsas, deducidos del sufijo del ticker de Yahoo (SPY -> Nueva York,
VWCE.DE -> Xetra). No conoce los festivos: en un festivo el precio simplemente no cambia.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd


@dataclass(frozen=True)
class Exchange:
    name: str
    tz: str
    open: time
    close: time

    def local(self, now: datetime) -> datetime:
        return now.astimezone(ZoneInfo(self.tz))

    def is_open(self, now: datetime) -> bool:
        local = self.local(now)
        return local.weekday() < 5 and self.open <= local.time() < self.close


US = Exchange("Nueva York", "America/New_York", time(9, 30), time(16, 0))
_EUROPE = {
    "DE": ("Xetra", "Europe/Berlin"), "F": ("Fráncfort", "Europe/Berlin"),
    "AS": ("Ámsterdam", "Europe/Amsterdam"), "PA": ("París", "Europe/Paris"),
    "MI": ("Milán", "Europe/Rome"), "MC": ("Madrid", "Europe/Madrid"),
    "BR": ("Bruselas", "Europe/Brussels"), "LS": ("Lisboa", "Europe/Lisbon"),
    "SW": ("Zúrich", "Europe/Zurich"),
}
_BY_SUFFIX = {k: Exchange(name, tz, time(9, 0), time(17, 30)) for k, (name, tz) in _EUROPE.items()}
_BY_SUFFIX["L"] = Exchange("Londres", "Europe/London", time(8, 0), time(16, 30))


def exchange_of(symbol: str) -> Exchange:
    """Bolsa del ticker. Sin sufijo conocido (SPY, BRK.B...) se asume Nueva York."""
    suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else ""
    return _BY_SUFFIX.get(suffix, US)


def closed_exchanges(symbols, now: datetime) -> list[str]:
    """Nombres de las bolsas cerradas ahora entre las de estos tickers (lista vacía = todas abiertas)."""
    seen: dict[str, Exchange] = {}
    for sym in symbols:
        ex = exchange_of(sym)
        seen[ex.name] = ex
    return sorted(name for name, ex in seen.items() if not ex.is_open(now))


def split_live_bars(df: pd.DataFrame, now: datetime, exchange: Exchange = US) -> tuple[pd.DataFrame, float]:
    """Separa las velas completas del precio actual.

    Durante la sesión, la última vela diaria está a medias (su volumen y su cierre no son
    definitivos). Las condiciones se calculan solo con velas completas, igual que en el
    backtest; la vela a medias solo aporta el precio actual.
    """
    price = float(df["Close"].iloc[-1])
    local = exchange.local(now)
    settled = (datetime.combine(local.date(), exchange.close) + timedelta(minutes=10)).time()
    if len(df) > 1 and df.index[-1].date() == local.date() and local.time() < settled:
        return df.iloc[:-1], price
    return df, price

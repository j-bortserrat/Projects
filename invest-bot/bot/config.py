"""Ajustes por defecto y validación de los ajustes editables desde el panel."""
from __future__ import annotations

import re

DEFAULT_UNIVERSE = [
    # ETFs
    "SPY", "QQQ", "VTI", "IWM", "DIA", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV", "GLD", "TLT",
    # Acciones grandes
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "V", "KO", "JNJ", "XOM",
]

# Todos los porcentajes se expresan como números "humanos": 8.0 significa 8 %.
DEFAULT_SETTINGS: dict = {
    "universe": DEFAULT_UNIVERSE,
    "initial_cash": 1000.0,      # dinero ficticio inicial
    "position_size": 100.0,      # cuánto se invierte en cada compra
    "max_positions": 10,         # posiciones abiertas a la vez
    "buy_threshold_pct": 90.0,   # % de condiciones que deben cumplirse para comprar
    "exit_score_pct": 50.0,      # vende si las condiciones caen por debajo de este %
    "stop_loss_pct": 8.0,        # pérdida máxima desde el precio de compra
    "trailing_stop_pct": 12.0,   # caída máxima desde el máximo alcanzado (0 = desactivado)
    "take_profit_pct": 0.0,      # beneficio objetivo (0 = desactivado, deja correr ganancias)
    "fee_pct": 0.1,              # comisión simulada por operación
}

_TICKER_RE = re.compile(r"^[A-Z0-9.\-=^]{1,15}$")

# (mínimo, máximo) para cada ajuste numérico
_RANGES = {
    "initial_cash": (10, 10_000_000),
    "position_size": (1, 1_000_000),
    "max_positions": (1, 100),
    "buy_threshold_pct": (50, 100),
    "exit_score_pct": (0, 100),
    "stop_loss_pct": (0.5, 90),
    "trailing_stop_pct": (0, 90),
    "take_profit_pct": (0, 1000),
    "fee_pct": (0, 5),
}


_LABELS = {
    "initial_cash": "Capital inicial",
    "position_size": "Importe de cada compra",
    "max_positions": "Máximo de posiciones",
    "buy_threshold_pct": "Umbral de compra",
    "exit_score_pct": "Umbral de venta",
    "stop_loss_pct": "Stop-loss",
    "trailing_stop_pct": "Trailing stop",
    "take_profit_pct": "Objetivo de beneficio",
    "fee_pct": "Comisión",
}


class SettingsError(ValueError):
    pass


def parse_universe(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\s,;]+", value)
    seen: list[str] = []
    for raw in value:
        sym = str(raw).strip().upper()
        if not sym:
            continue
        if not _TICKER_RE.match(sym):
            raise SettingsError(f"Ticker no válido: {sym!r}")
        if sym not in seen:
            seen.append(sym)
    if not seen:
        raise SettingsError("La lista de tickers no puede estar vacía")
    if len(seen) > 60:
        raise SettingsError("Máximo 60 tickers")
    return seen


def validate_settings(data: dict, base: dict | None = None) -> dict:
    """Devuelve unos ajustes completos y válidos a partir de `data` aplicado sobre `base`."""
    out = dict(DEFAULT_SETTINGS if base is None else base)
    for key, value in data.items():
        if key == "universe":
            out["universe"] = parse_universe(value)
        elif key in _RANGES:
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise SettingsError(f"{_LABELS[key]}: debe ser un número") from None
            lo, hi = _RANGES[key]
            if num != num or not (lo <= num <= hi):
                raise SettingsError(f"{_LABELS[key]}: debe estar entre {lo} y {hi}")
            out[key] = int(num) if key == "max_positions" else num
        # las claves desconocidas se ignoran
    return out

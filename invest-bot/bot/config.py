"""Ajustes por defecto y validación de los ajustes editables desde el panel."""
from __future__ import annotations

import re

# ETFs cotizados en EE. UU.: índices amplios, sectores, bonos y activos reales.
US_ETFS = [
    "SPY", "QQQ", "IWM", "EFA", "EEM",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB",
    "TLT", "IEF", "LQD", "HYG", "TIP", "SHY",
    "GLD", "DBC", "VNQ",
]
# ETFs UCITS cotizados en Xetra (en euros): los que un particular puede comprar desde la UE.
UCITS_ETFS = [
    "EUNL.DE", "SXR8.DE", "SXRV.DE", "EXSA.DE", "SXRT.DE", "EXS1.DE", "IS3N.DE", "IUSN.DE",
    "EUNA.DE", "VGEA.DE", "EUN5.DE", "IBCI.DE", "4GLD.DE", "XEON.DE",
]
DEFAULT_UNIVERSE = US_ETFS

# Todos los porcentajes se expresan como números "humanos": 8.0 significa 8 %.
#
# La estrategia por defecto se eligió comparando alternativas con datos reales de 2008 a
# 2026 (incluida la crisis de 2008): la cartera de reparto fijo fue la más estable en las
# dos mitades del periodo y la que menos opera. Aun así está elegida mirando el pasado.
DEFAULT_SETTINGS: dict = {
    "strategy": "allocation",    # "allocation": cartera de pesos fijos | "rotation" | "checklist"
    "benchmark": "SPY",          # índice de referencia para comparar y para el filtro de mercado
    "currency": "USD",           # solo cambia el símbolo que se muestra: USD ($) o EUR (€)
    # --- cartera
    "initial_cash": 1000.0,      # dinero ficticio inicial
    "monthly_contribution": 0.0, # aportación ficticia el primer día de bolsa de cada mes
    "fee_pct": 0.1,              # comisión simulada por operación
    "monthly_cost": 0.0,         # gastos mensuales (servidor, datos) para calcular el neto
    # --- estrategia de cartera fija (como una cartera de fondos)
    "allocation": {"VT": 60.0, "BND": 40.0},  # ETF -> % objetivo; debe sumar 100
    "rebalance_band_pct": 5.0,   # reequilibra si algún peso se desvía más de estos puntos
    # --- estrategias de rotación y de lista de condiciones
    "universe": DEFAULT_UNIVERSE,
    "max_positions": 5,          # posiciones abiertas a la vez
    "sizing_mode": "percent",    # "percent": % del valor de la cartera | "fixed": importe fijo
    "position_pct": 20.0,        # % de la cartera en cada compra (modo percent)
    "position_size": 100.0,      # importe de cada compra (modo fixed)
    "rotation_min_score_pct": 60.0,  # rotación: % mínimo de condiciones para estar en cartera
    "trend_filter": True,        # rotación: solo tickers por encima de su media de 200 días
    "buy_threshold_pct": 90.0,   # condiciones: % que debe cumplirse para comprar
    "exit_score_pct": 30.0,      # condiciones: vende si baja de este %
    # --- protección (rotación y condiciones; la cartera fija no usa stops)
    "stop_loss_pct": 25.0,       # pérdida máxima desde el precio de compra
    "trailing_stop_pct": 0.0,    # caída máxima desde el máximo alcanzado (0 = desactivado)
    "take_profit_pct": 0.0,      # beneficio objetivo (0 = desactivado, deja correr ganancias)
    "market_filter": False,      # no abrir compras si el índice está bajo su media de 200 días
}

# Ejemplos que el panel puede cargar con un clic. No se aplican hasta pulsar "Guardar".
PRESETS = [
    {"id": "us-global", "name": "60/40 global · ETFs de EE. UU. en dólares",
     "note": "Acciones de todo el mundo (VT) y bonos (BND). Historia desde 2008.",
     "settings": {"strategy": "allocation", "allocation": {"VT": 60.0, "BND": 40.0},
                  "universe": US_ETFS, "benchmark": "SPY", "currency": "USD"}},
    {"id": "us-classic", "name": "60/40 clásica · S&P 500 y bonos del Tesoro",
     "note": "SPY e IEF. La referencia más estudiada; solo EE. UU.",
     "settings": {"strategy": "allocation", "allocation": {"SPY": 60.0, "IEF": 40.0},
                  "universe": US_ETFS, "benchmark": "SPY", "currency": "USD"}},
    {"id": "eu-global", "name": "60/40 global · ETFs UCITS en euros (Xetra)",
     "note": "IUSQ.DE (acciones mundiales) y EUNA.DE (bonos globales cubiertos a euro). "
             "Son los comprables desde España; historia conjunta solo desde 2017.",
     "settings": {"strategy": "allocation", "allocation": {"IUSQ.DE": 60.0, "EUNA.DE": 40.0},
                  "universe": UCITS_ETFS, "benchmark": "EUNL.DE", "currency": "EUR"}},
]

_TICKER_RE = re.compile(r"^[A-Z0-9.\-=^]{1,15}$")

# (mínimo, máximo) para cada ajuste numérico
_RANGES = {
    "initial_cash": (10, 10_000_000),
    "position_size": (1, 1_000_000),
    "position_pct": (0.5, 100),
    "rebalance_band_pct": (0.5, 50),
    "monthly_contribution": (0, 1_000_000),
    "rotation_min_score_pct": (0, 100),
    "monthly_cost": (0, 100_000),
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
    "position_pct": "Porcentaje por compra",
    "rebalance_band_pct": "Banda de reequilibrio",
    "monthly_contribution": "Aportación mensual",
    "rotation_min_score_pct": "Condiciones mínimas en rotación",
    "monthly_cost": "Gastos mensuales",
    "max_positions": "Máximo de posiciones",
    "buy_threshold_pct": "Umbral de compra",
    "exit_score_pct": "Umbral de venta",
    "stop_loss_pct": "Stop-loss",
    "trailing_stop_pct": "Trailing stop",
    "take_profit_pct": "Objetivo de beneficio",
    "fee_pct": "Comisión",
}


STRATEGIES = ("allocation", "rotation", "checklist")


class SettingsError(ValueError):
    pass


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "on", "yes", "si", "sí"):
        return True
    if text in ("0", "false", "off", "no", ""):
        return False
    raise SettingsError(f"Valor no válido: {value!r} (usa sí o no)")


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


def parse_allocation(value) -> dict[str, float]:
    """Acepta un diccionario o un texto como "VT 60, BND 40". Los pesos deben sumar 100."""
    if isinstance(value, dict):
        pairs = list(value.items())
    else:
        tokens = [t.rstrip("%") for t in re.split(r"[,;:=\s]+", str(value).strip()) if t.rstrip("%")]
        if not tokens or len(tokens) % 2:
            raise SettingsError('Cartera: escribe cada ETF con su porcentaje, por ejemplo "VT 60, BND 40"')
        pairs = list(zip(tokens[0::2], tokens[1::2]))
    out: dict[str, float] = {}
    for raw_sym, raw_pct in pairs:
        sym = str(raw_sym).strip().upper()
        if not _TICKER_RE.match(sym):
            raise SettingsError(f"Ticker no válido en la cartera: {sym!r}")
        try:
            pct = float(str(raw_pct).replace(",", "."))
        except ValueError:
            raise SettingsError(f"Cartera: el peso de {sym} debe ser un número") from None
        if pct != pct or pct <= 0:
            raise SettingsError(f"Cartera: el peso de {sym} debe ser mayor que cero")
        if sym in out:
            raise SettingsError(f"Cartera: {sym} aparece dos veces")
        out[sym] = pct
    if not out or len(out) > 20:
        raise SettingsError("Cartera: indica entre 1 y 20 ETFs")
    total = sum(out.values())
    if abs(total - 100.0) > 0.5:
        raise SettingsError(f"Cartera: los pesos deben sumar 100 y suman {total:g}")
    return out


def validate_settings(data: dict, base: dict | None = None) -> dict:
    """Devuelve unos ajustes completos y válidos a partir de `data` aplicado sobre `base`."""
    out = dict(DEFAULT_SETTINGS if base is None else base)
    for key, value in data.items():
        if key == "universe":
            out["universe"] = parse_universe(value)
        elif key == "sizing_mode":
            if value not in ("percent", "fixed"):
                raise SettingsError("Tamaño de compra: debe ser 'percent' o 'fixed'")
            out[key] = value
        elif key == "strategy":
            if value not in STRATEGIES:
                raise SettingsError("Estrategia: debe ser 'allocation', 'rotation' o 'checklist'")
            out[key] = value
        elif key == "allocation":
            out[key] = parse_allocation(value)
        elif key == "currency":
            if value not in ("USD", "EUR"):
                raise SettingsError("Moneda: debe ser USD o EUR")
            out[key] = value
        elif key in ("market_filter", "trend_filter"):
            out[key] = _parse_bool(value)
        elif key == "benchmark":
            sym = str(value).strip().upper()
            if not _TICKER_RE.match(sym):
                raise SettingsError(f"Índice de referencia no válido: {sym!r}")
            out[key] = sym
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

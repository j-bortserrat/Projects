"""Motor de estado: convierte velas, libro de órdenes y cuenta en UNA instantánea numérica
y compacta. Es lo único que ve el REFLEX.

Reglas:
* Causalidad estricta: solo entran velas ya cerradas en `as_of`. Nada posterior.
* Todos los cálculos (precio medio, spread, desequilibrio, volatilidad, inventario,
  exposición, caída) se hacen aquí, en código, nunca en el modelo.
* Si el feed tiene huecos, está caducado o el reloj está desfasado, `validate_feed` lo
  dice y no se envía ninguna orden para ese ticker.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

from .market import Candle, OrderBook

LOOKBACK = 400      # velas que se usan para los indicadores
MIN_CANDLES = 260   # mínimo para que la media de 200 esté asentada
MAX_PROMPT_TOKENS = 400


@dataclass(frozen=True)
class AccountView:
    equity: float
    cash: float
    peak_equity: float
    day_start_equity: float
    position_values: dict[str, float] = field(default_factory=dict)

    @property
    def exposure(self) -> float:
        return sum(self.position_values.values()) / self.equity if self.equity > 0 else 0.0

    @property
    def drawdown(self) -> float:
        return max(0.0, 1 - self.equity / self.peak_equity) if self.peak_equity > 0 else 0.0

    @property
    def day_return(self) -> float:
        return self.equity / self.day_start_equity - 1 if self.day_start_equity > 0 else 0.0


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    as_of: int                 # cierre de la última vela usada, en ms UTC
    price: float
    atr: float                 # en unidades de precio; lo usa el código para stops
    features: dict[str, float | None]

    def to_prompt(self) -> str:
        return json.dumps({"s": self.symbol, "t": self.as_of, **self.features}, separators=(",", ":"))


def estimate_tokens(text: str) -> int:
    """Estimación conservadora: el JSON numérico ronda 1 token cada 2,5 caracteres."""
    return math.ceil(len(text) / 2.5)


def to_array(candles: list[Candle]) -> np.ndarray:
    """Últimas LOOKBACK velas como matriz [apertura, máximo, mínimo, cierre, volumen]."""
    return np.array([(c.open, c.high, c.low, c.close, c.volume) for c in candles[-LOOKBACK:]], dtype=float)


def validate_feed(candles: list[Candle], tf_ms: int, now_ms: int, server_ms: int | None = None,
                  max_skew_ms: int = 5_000, arr: np.ndarray | None = None) -> list[str]:
    """Devuelve los problemas del feed (lista vacía = se puede operar)."""
    if not candles:
        return ["sin_datos"]
    issues: list[str] = []
    if server_ms is not None and abs(server_ms - now_ms) > max_skew_ms:
        issues.append("reloj_desfasado")
    window = candles[-LOOKBACK:]
    if candles[-1].ts + tf_ms > now_ms or max(c.ts for c in window) + tf_ms > now_ms:
        issues.append("vela_en_curso")
    if len(candles) < MIN_CANDLES:
        issues.append("historia_insuficiente")
    ts = [c.ts for c in window]
    if ts != list(range(ts[0], ts[0] + len(ts) * tf_ms, tf_ms)):
        issues.append("huecos_o_desorden")
    expected_last = (now_ms // tf_ms) * tf_ms - tf_ms  # apertura de la última vela que debería estar cerrada
    if window[-1].ts < expected_last:
        issues.append("feed_caducado")
    a = to_array(candles) if arr is None else arr
    o, h, l, c, v = a[:, 0], a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    if (not np.isfinite(a).all() or a[:, :4].min() <= 0 or v.min() < 0
            or (h < np.maximum(o, c)).any() or (l > np.minimum(o, c)).any()):
        issues.append("datos_invalidos")
    return issues


# --- indicadores -----------------------------------------------------------------
_WEIGHTS: dict[tuple[float, int], np.ndarray] = {}


def _ewm_last(values: np.ndarray, alpha: float, seed: float) -> float:
    """Último valor de la recurrencia e = e + alpha * (x - e), partiendo de `seed`.
    Forma cerrada: (1-alpha)^n * seed + alpha * sum((1-alpha)^k * x[n-1-k])."""
    n = len(values)
    w = _WEIGHTS.get((alpha, n))
    if w is None:
        w = _WEIGHTS[(alpha, n)] = alpha * (1 - alpha) ** np.arange(n - 1, -1, -1)
    return float((1 - alpha) ** n * seed + w @ values)


def _ema(values: np.ndarray, span: int) -> float:
    return _ewm_last(values[1:], 2.0 / (span + 1), float(values[0]))


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float:
    prev = close[:-1]
    tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - prev), np.abs(low[1:] - prev)))
    return float(tr[-period:].mean())


def _rsi(close: np.ndarray, period: int = 14) -> float:
    """RSI de Wilder: media inicial simple y después suavizado exponencial."""
    delta = np.diff(close[-(period * 8):])
    gain, loss = np.clip(delta, 0, None), np.clip(-delta, 0, None)
    avg_gain = _ewm_last(gain[period:], 1.0 / period, float(gain[:period].mean()))
    avg_loss = _ewm_last(loss[period:], 1.0 / period, float(loss[:period].mean()))
    if avg_loss <= 0:
        return 100.0 if avg_gain > 0 else 50.0   # sin movimiento alguno, el RSI es neutral
    return 100.0 - 100.0 / (1 + avg_gain / avg_loss)


def _r(x: float | None, nd: int = 3) -> float | None:
    if x is None or not math.isfinite(x):
        return None
    return round(float(x), nd)


def trend_features(candles: list[Candle], arr: np.ndarray | None = None) -> tuple[dict[str, float | None], float]:
    """Indicadores de tendencia y volatilidad. Devuelve (indicadores, ATR en precio)."""
    a = to_array(candles) if arr is None else arr
    high, low, close, vol = a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    px = float(close[-1])
    atr = _atr(high, low, close)
    logret = np.diff(np.log(close))
    sd24, sd168 = float(logret[-24:].std()), float(logret[-168:].std())
    hi24, lo24 = float(high[-24:].max()), float(low[-24:].min())
    safe_atr = atr if atr > 0 else float("nan")
    vol168 = float(vol[-168:].mean())
    feats = {
        "r1": _r((px / close[-2] - 1) * 100),
        "r4": _r((px / close[-5] - 1) * 100),
        "r24": _r((px / close[-25] - 1) * 100),
        "r168": _r((px / close[-169] - 1) * 100),
        "atr_pct": _r(atr / px * 100),
        "vol24": _r(sd24 * math.sqrt(24) * 100),
        "vol_ratio": _r(sd24 / sd168 if sd168 > 0 else float("nan"), 2),
        "rsi": _r(_rsi(close), 1),
        "ema50_gap": _r((px - _ema(close, 50)) / safe_atr, 2),
        "ema200_gap": _r((px - _ema(close, 200)) / safe_atr, 2),
        "range_pos": _r((px - lo24) / (hi24 - lo24) if hi24 > lo24 else 0.5, 2),
        "volu_ratio": _r(float(vol[-24:].mean()) / vol168 if vol168 > 0 else float("nan"), 2),
        "dd168": _r((px / float(high[-168:].max()) - 1) * 100),
    }
    return feats, atr


def build_snapshot(symbol: str, candles: list[Candle], tf_ms: int, account: AccountView,
                   book: OrderBook | None = None, context: list[Candle] | None = None,
                   arr: np.ndarray | None = None, context_features: dict | None = None) -> Snapshot:
    """Construye la instantánea a partir de velas YA validadas y cerradas.

    El contexto de mercado (BTC) puede llegar como velas (`context`) o ya calculado
    (`context_features`); en ambos casos solo con datos hasta el mismo instante.
    """
    feats, atr = trend_features(candles, arr)
    last = candles[-1]
    features: dict[str, float | None] = {"px": float(f"{last.close:.6g}"), **feats}

    if context_features is None and context:
        ctx = [c for c in context if c.ts <= last.ts]
        if len(ctx) >= MIN_CANDLES:
            context_features, _ = trend_features(ctx)
    features["ctx_r24"] = context_features["r24"] if context_features else None
    features["ctx_ema200_gap"] = context_features["ema200_gap"] if context_features else None

    # Libro de órdenes (solo en vivo; en backtest no hay histórico y queda en null).
    spread_bps = imbalance = None
    if book and book.bids and book.asks:
        bid, ask = book.bids[0][0], book.asks[0][0]
        mid = (bid + ask) / 2
        bq, aq = sum(q for _, q in book.bids[:10]), sum(q for _, q in book.asks[:10])
        if mid > 0 and ask >= bid and bq + aq > 0:
            spread_bps, imbalance = _r((ask - bid) / mid * 1e4, 2), _r((bq - aq) / (bq + aq), 2)
    features["spread_bps"], features["imbalance"] = spread_bps, imbalance

    # Cuenta: inventario de este ticker, exposición total, caída y resultado del día.
    eq = account.equity if account.equity > 0 else float("nan")
    features["inv"] = _r(account.position_values.get(symbol, 0.0) / eq, 3)
    features["expo"] = _r(account.exposure, 3)
    features["dd"] = _r(account.drawdown * 100, 2)
    features["day"] = _r(account.day_return * 100, 2)
    features["npos"] = len(account.position_values)

    return Snapshot(symbol=symbol, as_of=last.ts + tf_ms, price=last.close, atr=atr, features=features)

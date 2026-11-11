"""Datos de mercado. `CcxtMarket` lee datos públicos reales; `MemoryMarket` sirve velas
guardadas para tests y backtests. Ninguno de los dos envía órdenes."""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    ts: int          # apertura de la vela, en ms UTC
    open: float
    high: float
    low: float
    close: float
    volume: float    # en la moneda base

    @property
    def quote_volume(self) -> float:
        return self.volume * self.close


@dataclass(frozen=True)
class OrderBook:
    ts: int
    bids: list[tuple[float, float]]  # (precio, cantidad), mejor primero
    asks: list[tuple[float, float]]


def closed_only(candles: list[Candle], tf_ms: int, now_ms: int) -> list[Candle]:
    """Descarta la vela en curso: solo cuentan las que ya han cerrado."""
    return [c for c in candles if c.ts + tf_ms <= now_ms]


class CcxtMarket:
    """Datos públicos de un exchange vía ccxt. No necesita claves."""

    def __init__(self, exchange_id: str = "binance", public_api_url: str = ""):
        import ccxt  # import perezoso: los tests no lo necesitan

        self.ex = getattr(ccxt, exchange_id)({
            "enableRateLimit": True,
            "options": {"defaultType": "spot", "fetchMarkets": ["spot"]},
        })
        # Respeta el proxy y los certificados del sistema (HTTPS_PROXY, REQUESTS_CA_BUNDLE):
        # por defecto ccxt los ignora y falla en redes corporativas o con proxy.
        self.ex.session.trust_env = True
        if public_api_url:
            self.ex.urls["api"]["public"] = public_api_url.rstrip("/")

    def server_time_ms(self) -> int:
        return int(self.ex.fetch_time())

    def candles(self, symbol: str, timeframe: str, limit: int = 500, since: int | None = None) -> list[Candle]:
        rows = self.ex.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]

    def order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        ob = self.ex.fetch_order_book(symbol, depth)
        ts = int(ob.get("timestamp") or time.time() * 1000)
        return OrderBook(ts, [(float(p), float(q)) for p, q in ob["bids"]],
                         [(float(p), float(q)) for p, q in ob["asks"]])

    def last_price(self, symbol: str) -> float:
        return float(self.ex.fetch_ticker(symbol)["last"])

    def quote_volume_24h(self, symbol: str) -> float:
        return float(self.ex.fetch_ticker(symbol).get("quoteVolume") or 0.0)

    def history(self, symbol: str, timeframe: str, start_ms: int, end_ms: int, tf_ms: int) -> list[Candle]:
        """Descarga paginada de velas cerradas entre dos fechas."""
        out: list[Candle] = []
        since = start_ms
        while since < end_ms:
            batch = self.candles(symbol, timeframe, limit=1000, since=since)
            batch = [c for c in batch if c.ts < end_ms]
            if not batch:
                break
            out.extend(batch)
            since = batch[-1].ts + tf_ms
            if len(batch) < 500:
                break
        seen: dict[int, Candle] = {c.ts: c for c in out}
        return [seen[k] for k in sorted(seen)]


class MemoryMarket:
    """Mercado en memoria con reloj controlable, para tests."""

    def __init__(self, data: dict[str, list[Candle]], tf_ms: int, now_ms: int = 0):
        self.data, self.tf_ms, self.now_ms = data, tf_ms, now_ms
        self.fail: set[str] = set()

    def server_time_ms(self) -> int:
        return self.now_ms

    def candles(self, symbol: str, timeframe: str, limit: int = 500, since: int | None = None) -> list[Candle]:
        if symbol in self.fail:
            raise RuntimeError(f"error simulado del exchange para {symbol}")
        rows = [c for c in self.data.get(symbol, []) if c.ts <= self.now_ms]
        return rows[-limit:]

    def order_book(self, symbol: str, depth: int = 20) -> OrderBook:
        px = self.last_price(symbol)
        return OrderBook(self.now_ms, [(px * 0.9999, 5.0)], [(px * 1.0001, 5.0)])

    def last_price(self, symbol: str) -> float:
        return closed_only(self.data[symbol], self.tf_ms, self.now_ms)[-1].close

    def quote_volume_24h(self, symbol: str) -> float:
        return 1e9

"""Bróker simulado. Aplica comisiones, deslizamiento, liquidez limitada y órdenes fallidas.
No se conecta a ningún exchange ni mueve dinero."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Position:
    symbol: str
    qty: float
    entry: float          # precio medio de entrada, con deslizamiento
    stop: float
    target: float
    opened_ts: int        # cierre de la vela en que se decidió
    cost: float           # dinero gastado, comisión incluida
    confidence: float
    p_win: float


@dataclass
class Fill:
    ts: int
    symbol: str
    side: str             # BUY | SELL
    qty: float
    price: float
    fee: float
    status: str           # filled | partial | rejected
    reason: str
    pnl: float | None = None
    pnl_r: float | None = None


class OrderRejected(Exception):
    pass


@dataclass
class PaperBroker:
    cash: float
    fee_bps: float = 10.0
    slippage_bps: float = 5.0
    max_participation: float = 0.01
    positions: dict[str, Position] = field(default_factory=dict)
    fail_next: int = 0    # para pruebas: rechaza las próximas N órdenes

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(p.qty * prices.get(s, p.entry) for s, p in self.positions.items())

    def position_values(self, prices: dict[str, float]) -> dict[str, float]:
        return {s: p.qty * prices.get(s, p.entry) for s, p in self.positions.items()}

    def _maybe_fail(self, ts: int, symbol: str, side: str, reason: str) -> Fill | None:
        if self.fail_next > 0:
            self.fail_next -= 1
            return Fill(ts, symbol, side, 0.0, 0.0, 0.0, "rejected", f"{reason} · orden rechazada (simulado)")
        return None

    def buy(self, symbol: str, qty: float, ref_price: float, ts: int, reason: str, stop: float, target: float,
            confidence: float, p_win: float, liquidity_quote: float | None = None) -> Fill:
        failed = self._maybe_fail(ts, symbol, "BUY", reason)
        if failed:
            return failed
        if symbol in self.positions or qty <= 0 or ref_price <= 0:
            return Fill(ts, symbol, "BUY", 0.0, 0.0, 0.0, "rejected", f"{reason} · orden no válida")
        price = ref_price * (1 + self.slippage_bps / 1e4)
        status = "filled"
        if liquidity_quote is not None and qty * price > self.max_participation * liquidity_quote:
            qty, status = self.max_participation * liquidity_quote / price, "partial"
        fee_rate = self.fee_bps / 1e4
        if qty * price * (1 + fee_rate) > self.cash:        # nunca gasta más de lo que hay
            qty, status = self.cash / (price * (1 + fee_rate)), "partial"
        if qty <= 0:
            return Fill(ts, symbol, "BUY", 0.0, 0.0, 0.0, "rejected", f"{reason} · sin liquidez o efectivo")
        fee = qty * price * fee_rate
        self.cash -= qty * price + fee
        self.positions[symbol] = Position(symbol, qty, price, stop, target, ts, qty * price + fee, confidence, p_win)
        return Fill(ts, symbol, "BUY", qty, price, fee, status, reason)

    def sell(self, symbol: str, ref_price: float, ts: int, reason: str) -> Fill:
        """Cierra la posición entera. Si la orden falla, la posición sigue abierta."""
        failed = self._maybe_fail(ts, symbol, "SELL", reason)
        if failed:
            return failed
        pos = self.positions.get(symbol)
        if pos is None:
            return Fill(ts, symbol, "SELL", 0.0, 0.0, 0.0, "rejected", f"{reason} · no hay posición")
        price = ref_price * (1 - self.slippage_bps / 1e4)
        fee = pos.qty * price * self.fee_bps / 1e4
        net = pos.qty * price - fee
        self.cash += net
        del self.positions[symbol]
        risk = pos.qty * (pos.entry - pos.stop)
        pnl = net - pos.cost
        return Fill(ts, symbol, "SELL", pos.qty, price, fee, "filled", reason, pnl=pnl,
                    pnl_r=pnl / risk if risk > 0 else None)

    # --- conciliación ---------------------------------------------------------------
    def reconcile(self, initial_cash: float, fills: list[Fill], tol: float = 1e-6) -> list[str]:
        """Reconstruye efectivo y posiciones a partir del diario de órdenes y los compara
        con el estado del bróker. Cualquier diferencia detiene las compras."""
        cash, qty = initial_cash, {}
        for f in fills:
            if f.status == "rejected":
                continue
            if f.side == "BUY":
                cash -= f.qty * f.price + f.fee
                qty[f.symbol] = qty.get(f.symbol, 0.0) + f.qty
            else:
                cash += f.qty * f.price - f.fee
                qty[f.symbol] = qty.get(f.symbol, 0.0) - f.qty
        issues = []
        if abs(cash - self.cash) > tol * max(1.0, abs(initial_cash)):
            issues.append(f"efectivo: diario {cash:.6f} vs bróker {self.cash:.6f}")
        for sym in set(qty) | set(self.positions):
            expected = qty.get(sym, 0.0)
            actual = self.positions[sym].qty if sym in self.positions else 0.0
            if abs(expected - actual) > tol * max(1.0, abs(expected)):
                issues.append(f"{sym}: diario {expected:.8f} vs bróker {actual:.8f}")
        return issues

    def to_dict(self) -> dict:
        return {"cash": self.cash, "positions": {s: asdict(p) for s, p in self.positions.items()}}

    def load(self, d: dict) -> None:
        self.cash = d["cash"]
        self.positions = {s: Position(**p) for s, p in d["positions"].items()}

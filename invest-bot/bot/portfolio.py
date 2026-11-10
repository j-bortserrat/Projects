"""Cartera ficticia en memoria. La usan tanto el bot en vivo como el backtest."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Position:
    symbol: str
    qty: float
    cost: float          # dinero total gastado en la compra, comisión incluida
    opened_at: str
    entry_score: float
    high_water: float    # precio máximo visto desde la compra (para el trailing stop)
    last_price: float

    @property
    def avg_price(self) -> float:
        return self.cost / self.qty

    @property
    def value(self) -> float:
        return self.qty * self.last_price

    def mark(self, price: float) -> None:
        self.last_price = price
        self.high_water = max(self.high_water, price)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(
            avg_price=self.avg_price,
            value=self.value,
            pnl=self.value - self.cost,
            pnl_pct=100.0 * (self.value - self.cost) / self.cost,
        )
        return d


@dataclass
class Account:
    cash: float
    fee_pct: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[dict] = field(default_factory=list)  # operaciones nuevas aún sin guardar

    @property
    def equity(self) -> float:
        return self.cash + sum(p.value for p in self.positions.values())

    def buy(self, symbol: str, price: float, amount: float, ts: str, score: float, reason: str) -> dict | None:
        """Invierte `amount` (comisión incluida). Devuelve la operación o None si no se puede."""
        if symbol in self.positions or price <= 0 or amount <= 0 or self.cash + 1e-9 < amount:
            return None
        fee = amount * self.fee_pct / 100.0
        qty = (amount - fee) / price
        self.cash -= amount
        self.positions[symbol] = Position(symbol, qty, amount, ts, score, price, price)
        trade = {
            "ts": ts, "symbol": symbol, "side": "BUY", "qty": qty, "price": price, "fee": fee,
            "amount": amount, "pnl": None, "pnl_pct": None, "reason": reason, "score": score,
        }
        self.trades.append(trade)
        return trade

    def sell(self, symbol: str, price: float, ts: str, score: float | None, reason: str) -> dict | None:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None
        gross = pos.qty * price
        fee = gross * self.fee_pct / 100.0
        net = gross - fee
        self.cash += net
        trade = {
            "ts": ts, "symbol": symbol, "side": "SELL", "qty": pos.qty, "price": price, "fee": fee,
            "amount": net, "pnl": net - pos.cost, "pnl_pct": 100.0 * (net - pos.cost) / pos.cost,
            "reason": reason, "score": score,
        }
        self.trades.append(trade)
        return trade

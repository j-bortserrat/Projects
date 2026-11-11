"""Capa de riesgo dura. Se comprueba antes de CADA orden y ningún modelo puede saltársela.

El REFLEX opina; aquí se decide. Las salidas (reducir riesgo) siempre están permitidas
y nunca dependen de una llamada al modelo.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import RiskCfg
from .schema import ReflexDecision, RiskState
from .state import AccountView


@dataclass
class Verdict:
    allowed: bool
    qty: float
    reasons: list[str] = field(default_factory=list)
    trip_kill_switch: bool = False
    escalate: bool = False


class RiskManager:
    def __init__(self, cfg: RiskCfg, market: str = "spot", off_limits: list[str] | None = None):
        self.cfg = cfg
        self.leverage_cap = 1.0 if market == "spot" else cfg.max_leverage  # en spot no se puede pedir prestado
        self.off_limits = set(off_limits or [])

    # --- estado de riesgo calculado en código (el del modelo solo puede endurecerlo) ---
    def state(self, account: AccountView, open_positions: int) -> RiskState:
        dd, day = account.drawdown * 100, -account.day_return * 100
        kill, daily = self.cfg.kill_switch_drawdown_pct, self.cfg.max_daily_loss_pct
        if dd >= 0.8 * kill or day >= 0.8 * daily:
            return "reduce"
        if dd >= 0.5 * kill or day >= 0.5 * daily or open_positions >= self.cfg.max_open_positions:
            return "near_limit"
        return "safe"

    def kill_switch_due(self, account: AccountView) -> bool:
        return account.drawdown * 100 >= self.cfg.kill_switch_drawdown_pct

    def daily_loss_hit(self, account: AccountView) -> bool:
        return -account.day_return * 100 >= self.cfg.max_daily_loss_pct

    def check_entry(self, symbol: str, qty: float, price: float, stop: float, account: AccountView,
                    open_positions: int, decision: ReflexDecision, kill_switch_active: bool) -> Verdict:
        """¿Se puede abrir esta posición? Puede recortar el tamaño; nunca lo aumenta."""
        cfg = self.cfg
        if kill_switch_active:
            return Verdict(False, 0.0, ["interruptor de emergencia activo"])
        if self.kill_switch_due(account):
            return Verdict(False, 0.0, [f"caída del {account.drawdown * 100:.1f} % >= {cfg.kill_switch_drawdown_pct} %"],
                           trip_kill_switch=True, escalate=True)
        if self.daily_loss_hit(account):
            return Verdict(False, 0.0, [f"pérdida diaria >= {cfg.max_daily_loss_pct} %"], escalate=True)
        if decision.regime == "crisis":
            return Verdict(False, 0.0, ["régimen de crisis: no se abre riesgo nuevo"], escalate=True)
        if decision.direction_confidence < cfg.pause_below_confidence:
            return Verdict(False, 0.0, [f"confianza {decision.direction_confidence:.2f} < {cfg.pause_below_confidence}"],
                           escalate=True)
        if symbol in self.off_limits:
            return Verdict(False, 0.0, ["ticker vetado"])
        if open_positions >= cfg.max_open_positions:
            return Verdict(False, 0.0, [f"ya hay {open_positions} posiciones abiertas"])
        if symbol in account.position_values:
            return Verdict(False, 0.0, ["ya hay posición en este ticker"])
        if qty <= 0 or price <= 0 or not (0 < stop < price):
            return Verdict(False, 0.0, ["orden no válida"])

        reasons: list[str] = []
        equity = account.equity
        # 1) riesgo por operación
        max_risk = cfg.risk_per_trade_pct / 100.0 * equity
        if qty * (price - stop) > max_risk:
            qty = max_risk / (price - stop)
            reasons.append("recortado al riesgo máximo por operación")
        # 2) tamaño por posición y exposición total
        per_position = self.leverage_cap * equity / cfg.max_open_positions
        room = self.leverage_cap * equity - sum(account.position_values.values())
        cap = min(per_position, room, account.cash)  # en spot tampoco se puede gastar más del efectivo
        if qty * price > cap:
            qty = max(0.0, cap) / price
            reasons.append("recortado por límite de exposición o efectivo")
        if qty * price < cfg.min_order_quote:
            return Verdict(False, 0.0, reasons + ["importe por debajo del mínimo"])
        return Verdict(True, qty, reasons)

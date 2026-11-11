"""Tamaño de la posición: Kelly fraccionado a partir de una probabilidad calibrada."""
from __future__ import annotations

from dataclasses import dataclass

from .config import PaperCfg, PolicyCfg, RiskCfg


def kelly(p: float, b: float) -> float:
    """Fracción de Kelly para una apuesta que gana b veces lo arriesgado con probabilidad p."""
    if b <= 0 or not (0.0 <= p <= 1.0):
        return 0.0
    return max(0.0, p - (1.0 - p) / b)


def net_payoff(target_r: float, stop_dist_pct: float, round_trip_cost_pct: float) -> float:
    """Ganancia/pérdida real tras costes: las comisiones recortan la ganancia y agrandan la pérdida."""
    if stop_dist_pct <= 0:
        return 0.0
    cost_r = round_trip_cost_pct / stop_dist_pct
    return max(0.0, (target_r - cost_r) / (1.0 + cost_r))


@dataclass(frozen=True)
class Plan:
    qty: float
    stop: float
    target: float
    risk_fraction: float   # fracción de la cartera que se pierde si salta el stop
    kelly_full: float
    reason: str


def plan_position(equity: float, price: float, atr: float, p_win: float,
                  policy: PolicyCfg, risk: RiskCfg, paper: PaperCfg) -> Plan:
    stop = price - policy.stop_atr * atr
    if price <= 0 or atr <= 0 or stop <= 0 or equity <= 0:
        return Plan(0.0, 0.0, 0.0, 0.0, 0.0, "precio o ATR no válidos")
    stop_dist = (price - stop) / price
    target = price + policy.target_r * (price - stop)
    cost = 2 * (paper.fee_bps + paper.slippage_bps) / 1e4
    f = kelly(p_win, net_payoff(policy.target_r, stop_dist, cost))
    risk_fraction = min(policy.kelly_fraction * f, risk.risk_per_trade_pct / 100.0)
    if risk_fraction <= 0:
        return Plan(0.0, stop, target, 0.0, f, "sin ventaja calibrada: Kelly es cero")
    qty = risk_fraction * equity / stop_dist / price
    return Plan(qty, stop, target, risk_fraction, f, "ok")

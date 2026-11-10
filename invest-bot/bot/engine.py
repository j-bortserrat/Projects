"""Lógica de decisión del bot: cuándo comprar y cuándo vender.

Las funciones ``check_exit`` y ``rank_candidates`` son puras y las comparten el
bot en vivo (``run_cycle``) y el backtest, para que ambos decidan exactamente igual.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .indicators import MIN_BARS, compute_indicators
from .portfolio import Account, Position
from .rules import evaluate

LIVE_HISTORY_DAYS = 800  # ~2,2 años: suficiente para SMA200 y máximo de 52 semanas
_EPS = 1e-9


def check_exit(pos: Position, price: float, score_pct: float | None, s: dict) -> str | None:
    """Devuelve el motivo de venta, o None si hay que mantener la posición."""
    change_pct = 100.0 * (price / pos.avg_price - 1)
    if change_pct <= -s["stop_loss_pct"] + _EPS:
        return f"Stop-loss ({change_pct:+.1f} %)"
    if s["take_profit_pct"] > 0 and change_pct >= s["take_profit_pct"] - _EPS:
        return f"Objetivo de beneficio ({change_pct:+.1f} %)"
    high = max(pos.high_water, price)
    drop_pct = 100.0 * (1 - price / high)
    if s["trailing_stop_pct"] > 0 and drop_pct >= s["trailing_stop_pct"] - _EPS:
        return f"Trailing stop (-{drop_pct:.1f} % desde el máximo)"
    if score_pct is not None and score_pct < s["exit_score_pct"] - _EPS:
        return f"Condiciones deterioradas ({score_pct:.0f} % < {s['exit_score_pct']:.0f} %)"
    return None


def rank_candidates(signals: list[dict], held: set[str], s: dict) -> list[dict]:
    """Señales que alcanzan el umbral de compra, de mejor a peor."""
    ok = [
        sig for sig in signals
        if sig["status"] == "ok"
        and sig["symbol"] not in held
        and sig["score_pct"] >= s["buy_threshold_pct"] - _EPS
    ]
    return sorted(ok, key=lambda sig: (-sig["score_pct"], -(sig.get("ret63") or 0), sig["symbol"]))


def build_signal(symbol: str, row: dict | None, ts: str, error: str | None = None) -> dict:
    if row is None or error:
        return {"symbol": symbol, "ts": ts, "price": None, "score_pct": None, "passed": None,
                "total": None, "status": error or "sin datos", "results": [], "ret63": None}
    if any(math.isnan(row[k]) for k in ("sma200", "high52")):
        return {"symbol": symbol, "ts": ts, "price": row["close"], "score_pct": None, "passed": None,
                "total": None, "status": f"historia insuficiente (<{MIN_BARS} sesiones)",
                "results": [], "ret63": None}
    ev = evaluate(row)
    ret63 = None if math.isnan(row["ret63"]) else row["ret63"]
    return {"symbol": symbol, "ts": ts, "price": row["close"], "status": "ok", "ret63": ret63, **ev}


def run_cycle(provider, store, now: datetime | None = None) -> dict:
    """Un ciclo completo: analiza el universo, vende lo que toque y compra lo que cumpla."""
    s = store.get_settings()
    ts = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    account: Account = store.load_account(s["fee_pct"])

    symbols = list(dict.fromkeys(list(s["universe"]) + list(account.positions)))
    signals: list[dict] = []
    errors: dict[str, str] = {}
    for sym in symbols:
        try:
            ind = compute_indicators(provider.history(sym, LIVE_HISTORY_DAYS))
            signals.append(build_signal(sym, ind.iloc[-1].to_dict(), ts))
        except Exception as exc:  # un ticker roto no debe parar el ciclo
            errors[sym] = str(exc)
            signals.append(build_signal(sym, None, ts, error=f"error: {exc}"))
    by_symbol = {sig["symbol"]: sig for sig in signals}

    sells, buys = [], []
    # 1) Ventas primero: liberan efectivo y huecos.
    for sym, pos in list(account.positions.items()):
        sig = by_symbol.get(sym)
        if sig is None or sig["price"] is None:
            continue  # sin precio fiable no se toca la posición
        pos.mark(sig["price"])
        reason = check_exit(pos, sig["price"], sig["score_pct"], s)
        if reason:
            sells.append(account.sell(sym, sig["price"], ts, sig["score_pct"], reason))

    # 2) Compras: solo si se cumple el umbral de condiciones.
    #    Un ticker vendido hoy no se recompra hasta mañana (evita entrar y salir en bucle).
    sold_today = {t["symbol"] for t in sells} | store.symbols_sold_on(ts[:10])
    for sig in rank_candidates(signals, set(account.positions) | sold_today, s):
        if len(account.positions) >= s["max_positions"]:
            break
        reason = f"{sig['passed']}/{sig['total']} condiciones ({sig['score_pct']:.0f} %)"
        trade = account.buy(sig["symbol"], sig["price"], s["position_size"], ts, sig["score_pct"], reason)
        if trade is None:
            break  # sin efectivo suficiente
        buys.append(trade)

    summary = {
        "ts": ts,
        "provider": provider.name,
        "evaluated": sum(1 for sig in signals if sig["status"] == "ok"),
        "qualified": sum(
            1 for sig in signals
            if sig["status"] == "ok" and sig["score_pct"] >= s["buy_threshold_pct"] - _EPS
        ),
        "buys": [t["symbol"] for t in buys],
        "sells": [{"symbol": t["symbol"], "reason": t["reason"], "pnl": t["pnl"]} for t in sells],
        "errors": errors,
        "equity": account.equity,
    }
    store.save_cycle(account, signals, summary)
    return summary

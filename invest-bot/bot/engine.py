"""Lógica de decisión del bot: cuándo comprar y cuándo vender.

Las funciones puras (``check_exit``, ``rank_candidates``, ``position_amount``,
``market_is_bullish``) las comparten el bot en vivo (``run_cycle``) y el
backtest, para que ambos decidan exactamente igual.
"""
from __future__ import annotations

import math
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from .indicators import MIN_BARS, compute_indicators
from .portfolio import Account, Position
from .rules import evaluate

LIVE_HISTORY_DAYS = 800  # ~2,2 años: suficiente para SMA200 y máximo de 52 semanas
_EPS = 1e-9
_NY = ZoneInfo("America/New_York")
_OPEN, _CLOSE = time(9, 30), time(16, 0)


# --- funciones puras compartidas con el backtest -----------------------------
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


def rank_by_momentum(signals: list[dict], s: dict) -> list[dict]:
    """Tickers elegibles para la rotación, del momentum más fuerte al más débil."""
    eligible = [
        sig for sig in signals
        if sig["status"] == "ok"
        and sig.get("mom") is not None
        and (sig["trend_ok"] or not s["trend_filter"])
        and sig["score_pct"] >= s["rotation_min_score_pct"] - _EPS
    ]
    return sorted(eligible, key=lambda sig: (-sig["mom"], sig["symbol"]))


def plan_rotation(signals: list[dict], held: set[str], s: dict) -> tuple[dict[str, str], list[dict]]:
    """Rotación mensual: quedarse con los `max_positions` tickers de mayor momentum.

    Devuelve (ventas {ticker: motivo}, señales a comprar en orden de preferencia).
    Un ticker en cartera cuyo dato falta hoy no se vende por ello.
    """
    top = rank_by_momentum(signals, s)[: s["max_positions"]]
    top_symbols = {sig["symbol"] for sig in top}
    evaluated = {sig["symbol"] for sig in signals if sig["status"] == "ok"}
    sells = {
        sym: f"Rotación mensual: ya no está entre los {s['max_positions']} más fuertes"
        for sym in held if sym in evaluated and sym not in top_symbols
    }
    return sells, [sig for sig in top if sig["symbol"] not in held]


def buy_reason(sig: dict, s: dict) -> str:
    if s["strategy"] == "rotation":
        return f"Rotación mensual: entre los {s['max_positions']} más fuertes (momentum {sig['mom'] * 100:+.0f} %)"
    return f"{sig['passed']}/{sig['total']} condiciones ({sig['score_pct']:.0f} %)"


def position_amount(account: Account, s: dict) -> float:
    """Cuánto invertir en la próxima compra (0 si no hay efectivo suficiente).

    En modo ``percent`` el importe es un % del valor total de la cartera, así las
    ganancias se reinvierten. Si queda algo menos del importe objetivo (por
    comisiones o movimientos de precio) se usa el efectivo que haya.
    """
    if s["sizing_mode"] == "fixed":
        target = s["position_size"]
    else:
        target = account.equity * s["position_pct"] / 100.0
    if target < 1.0:
        return 0.0
    if account.cash + _EPS >= target:
        return target
    return account.cash if account.cash >= 0.8 * target else 0.0


def market_is_bullish(row: dict | None) -> bool | None:
    """Filtro de mercado: el índice de referencia cotiza sobre su media de 200 días.
    Devuelve None si no se puede saber."""
    if row is None or math.isnan(row["close"]) or math.isnan(row["sma200"]):
        return None
    return row["close"] > row["sma200"]


def build_signal(symbol: str, row: dict | None, ts: str, error: str | None = None,
                 price: float | None = None) -> dict:
    """Evalúa las condiciones sobre `row` (última vela completa). `price` es el
    precio actual, si es distinto del cierre de esa vela."""
    empty = {"symbol": symbol, "ts": ts, "price": price, "score_pct": None, "passed": None,
             "total": None, "results": [], "ret63": None, "mom": None, "trend_ok": None}
    if row is None or error:
        return {**empty, "status": error or "sin datos"}
    price = row["close"] if price is None else price
    if any(math.isnan(row[k]) for k in ("sma200", "high52")):
        return {**empty, "price": price, "status": f"historia insuficiente (<{MIN_BARS} sesiones)"}
    ev = evaluate(row)
    ret63 = None if math.isnan(row["ret63"]) else row["ret63"]
    # Momentum: media de la rentabilidad a 3, 6 y 12 meses.
    parts = [row.get("ret63", math.nan), row.get("ret126", math.nan), row.get("ret252", math.nan)]
    mom = None if any(math.isnan(x) for x in parts) else sum(parts) / 3
    return {"symbol": symbol, "ts": ts, "price": price, "status": "ok", "ret63": ret63,
            "mom": mom, "trend_ok": bool(row["close"] > row["sma200"]), **ev}


# --- reloj de mercado ---------------------------------------------------------
def us_market_open(now: datetime) -> bool:
    """Horario regular de la bolsa de EE. UU. (no tiene en cuenta festivos)."""
    ny = now.astimezone(_NY)
    return ny.weekday() < 5 and _OPEN <= ny.time() < _CLOSE


def split_live_bars(df: pd.DataFrame, now: datetime) -> tuple[pd.DataFrame, float]:
    """Separa las velas completas del precio actual.

    Durante la sesión, la última vela diaria está a medias (su volumen y su cierre
    no son definitivos). Las condiciones se calculan solo con velas completas, igual
    que en el backtest; la vela a medias solo aporta el precio actual.
    """
    price = float(df["Close"].iloc[-1])
    ny = now.astimezone(_NY)
    if len(df) > 1 and df.index[-1].date() == ny.date() and ny.time() < time(16, 10):
        return df.iloc[:-1], price
    return df, price


# --- ciclo en vivo --------------------------------------------------------------
def run_cycle(provider, store, now: datetime | None = None) -> dict:
    """Un ciclo completo: analiza el universo, vende lo que toque y compra lo que cumpla."""
    s = store.get_settings()
    now = now or datetime.now(timezone.utc)
    ts = now.isoformat(timespec="seconds")
    real_time = getattr(provider, "real_time", False)
    rotation = s["strategy"] == "rotation"
    account: Account = store.load_account(s["fee_pct"])
    notes: list[str] = []

    def last_row(symbol: str) -> tuple[dict, float]:
        df = provider.history(symbol, LIVE_HISTORY_DAYS)
        bars, price = split_live_bars(df, now) if real_time else (df, float(df["Close"].iloc[-1]))
        return compute_indicators(bars).iloc[-1].to_dict(), price

    symbols = list(dict.fromkeys(list(s["universe"]) + list(account.positions)))
    signals: list[dict] = []
    errors: dict[str, str] = {}
    for sym in symbols:
        try:
            row, price = last_row(sym)
            signals.append(build_signal(sym, row, ts, price=price))
        except Exception as exc:  # un ticker roto no debe parar el ciclo
            errors[sym] = str(exc)
            signals.append(build_signal(sym, None, ts, error=f"error: {exc}"))
    by_symbol = {sig["symbol"]: sig for sig in signals}
    in_universe = [sig for sig in signals if sig["symbol"] in set(s["universe"])]
    for i, sig in enumerate(rank_by_momentum(in_universe, s)):
        sig["rank"], sig["target"] = i + 1, i < s["max_positions"]

    # Filtro de mercado: sin tendencia alcista general no se abren posiciones nuevas.
    market_ok: bool | None = True
    if s["market_filter"]:
        try:
            market_ok = market_is_bullish(last_row(s["benchmark"])[0])
        except Exception as exc:
            market_ok = None
            errors.setdefault(s["benchmark"], str(exc))
        if market_ok is False:
            notes.append(f"Mercado bajista ({s['benchmark']} bajo su media de 200 días): no se abren compras nuevas.")
        elif market_ok is None:
            notes.append(f"No se pudo evaluar el mercado ({s['benchmark']}): por prudencia no se compra.")

    # Con datos reales solo se opera con la bolsa abierta, para que el precio simulado sea realista.
    can_trade = us_market_open(now) if real_time else True
    if not can_trade:
        notes.append("Bolsa cerrada: señales actualizadas; las órdenes esperan a la próxima sesión.")

    for sym, pos in account.positions.items():
        sig = by_symbol.get(sym)
        if sig is not None and sig["price"] is not None:
            pos.mark(sig["price"])

    month_key = now.astimezone(_NY).strftime("%Y-%m")
    rebalance_due = rotation and store.get_meta("last_rebalance") != month_key
    rebalanced = False
    meta: dict = {}
    sells, buys = [], []

    def sell(sym: str, reason: str) -> None:
        sig = by_symbol[sym]
        sells.append(account.sell(sym, sig["price"], ts, sig["score_pct"], reason))

    if can_trade:
        # 1) Salidas de protección (stop-loss, trailing...). En la estrategia de condiciones,
        #    también cuando las condiciones se deterioran.
        for sym, pos in list(account.positions.items()):
            sig = by_symbol.get(sym)
            if sig is None or sig["price"] is None:
                continue  # sin precio fiable no se toca la posición
            reason = check_exit(pos, sig["price"], None if rotation else sig["score_pct"], s)
            if reason:
                sell(sym, reason)

        # 2) Qué comprar.
        candidates: list[dict] = []
        if rotation:
            if rebalance_due:
                n_ok = sum(1 for sig in in_universe if sig["status"] == "ok")
                if n_ok < 0.7 * len(s["universe"]):
                    notes.append("Faltan datos de demasiados tickers: la rotación mensual se reintentará.")
                else:
                    rot_sells, candidates = plan_rotation(in_universe, set(account.positions), s)
                    for sym, reason in rot_sells.items():
                        if by_symbol[sym]["price"] is not None:
                            sell(sym, reason)
                    rebalanced, meta["last_rebalance"] = True, month_key
        else:
            candidates = rank_candidates(signals, set(account.positions), s)

        # 3) Compras, si el mercado acompaña. Un ticker vendido hoy no se recompra hasta
        #    mañana (evita entrar y salir en bucle).
        if market_ok:
            sold_today = {t["symbol"] for t in sells} | store.symbols_sold_on(ts[:10])
            for sig in candidates:
                if sig["symbol"] in sold_today or sig["symbol"] in account.positions:
                    continue
                if len(account.positions) >= s["max_positions"]:
                    break
                amount = position_amount(account, s)
                if amount <= 0:
                    break  # sin efectivo suficiente
                trade = account.buy(sig["symbol"], sig["price"], amount, ts, sig["score_pct"], buy_reason(sig, s))
                if trade:
                    buys.append(trade)
    elif rebalance_due:
        notes.append("Rotación mensual pendiente: se hará en la próxima sesión de bolsa.")

    summary = {
        "ts": ts,
        "provider": provider.name,
        "strategy": s["strategy"],
        "evaluated": sum(1 for sig in signals if sig["status"] == "ok"),
        "qualified": sum(
            1 for sig in signals
            if sig["status"] == "ok" and sig["score_pct"] >= s["buy_threshold_pct"] - _EPS
        ),
        "market_ok": market_ok,
        "can_trade": can_trade,
        "rebalanced": rebalanced,
        "notes": notes,
        "buys": [{"symbol": t["symbol"], "amount": t["amount"], "price": t["price"], "reason": t["reason"]}
                 for t in buys],
        "sells": [{"symbol": t["symbol"], "reason": t["reason"], "pnl": t["pnl"], "pnl_pct": t["pnl_pct"]}
                  for t in sells],
        "errors": errors,
        "equity": account.equity,
    }
    store.save_cycle(account, signals, summary, meta)
    return summary

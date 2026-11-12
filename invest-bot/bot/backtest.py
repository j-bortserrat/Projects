"""Backtest: aplica las mismas reglas del bot sobre datos históricos.

Para no "ver el futuro", las decisiones se toman con el cierre del día D y las
órdenes se ejecutan a la apertura del día D+1.

Limitaciones honestas (también se muestran en el panel):
* El universo son tickers que existen HOY (sesgo de supervivencia).
* Las reglas se han escrito mirando el pasado; un buen resultado histórico no
  garantiza nada sobre el futuro.
"""
from __future__ import annotations

import math

import pandas as pd

from .engine import (build_signal, buy_reason, check_exit, execute_allocation, market_is_bullish,
                     plan_allocation, plan_rotation, position_amount, rank_candidates, tracked_symbols)
from .indicators import compute_indicators
from .portfolio import Account

WARMUP_DAYS = 420  # días naturales extra para que los indicadores estén listos al empezar


def _max_drawdown_pct(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, 1 - v / peak)
    return 100.0 * worst


def _sharpe(values: list[float]) -> float | None:
    """Rentabilidad por unidad de riesgo (anualizada, sin tipo libre de riesgo)."""
    rets = pd.Series(values).pct_change().dropna()
    if len(rets) < 20 or rets.std() == 0:
        return None
    return float(rets.mean() / rets.std() * math.sqrt(252))


def _downsample(points: list[dict], max_points: int = 300) -> list[dict]:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    picked = [points[int(i * step)] for i in range(max_points)]
    if picked[-1] is not points[-1]:
        picked.append(points[-1])
    return picked


def _by_year(dates: list, bot: list[float], market: list[float | None]) -> list[dict]:
    """Rentabilidad del bot y del índice en cada año natural."""
    out = []
    frame = pd.DataFrame({"bot": bot, "market": market}, index=pd.DatetimeIndex(dates))
    prev = None
    for year, chunk in frame.groupby(frame.index.year):
        start = chunk.iloc[0] if prev is None else prev
        end = chunk.iloc[-1]
        row = {"year": int(year), "bot_pct": 100.0 * (end["bot"] / start["bot"] - 1), "market_pct": None}
        if pd.notna(start["market"]) and pd.notna(end["market"]) and start["market"] > 0:
            row["market_pct"] = 100.0 * (end["market"] / start["market"] - 1)
        out.append(row)
        prev = end
    return out


def run_backtest(provider, settings: dict, years: float = 3.0, end=None) -> dict:
    """Simula `years` años que terminan en `end` (por defecto, el último dato disponible)."""
    s = settings
    end = None if end is None else pd.Timestamp(end)
    days = int(years * 365) + WARMUP_DAYS
    if end is not None:
        days += max(0, (pd.Timestamp.today().normalize() - end).days)
    if s["strategy"] == "allocation":
        s = {**s, "market_filter": False}   # el filtro de mercado no aplica a una cartera fija

    def load(sym: str) -> pd.DataFrame:
        ind = compute_indicators(provider.history(sym, days))
        return ind if end is None else ind[ind.index <= end]

    rows: dict[str, dict] = {}       # symbol -> {fecha -> indicadores}
    closes: dict[str, pd.Series] = {}
    errors: dict[str, str] = {}
    for sym in tracked_symbols(s):
        try:
            ind = load(sym)
            if ind.empty:
                raise RuntimeError("sin datos en el periodo")
            rows[sym] = ind.to_dict("index")
            closes[sym] = ind["close"]
        except Exception as exc:
            errors[sym] = str(exc)
    if not rows:
        raise RuntimeError("No hay datos para ningún ticker")
    allocation = s["strategy"] == "allocation"
    if allocation and errors:
        raise RuntimeError("Faltan datos de la cartera: " + ", ".join(errors))

    bench_sym = s["benchmark"]
    bench_rows: dict = {}
    bench_close: pd.Series | None = None
    try:
        bench_ind = load(bench_sym)
        bench_rows, bench_close = bench_ind.to_dict("index"), bench_ind["close"]
    except Exception as exc:
        errors.setdefault(bench_sym, str(exc))

    all_dates = sorted(set().union(*(r.keys() for r in rows.values())))
    start = all_dates[-1] - pd.Timedelta(days=int(years * 365))
    if allocation:   # una cartera fija solo puede empezar cuando existen todos sus ETFs
        start = max(start, max(series.index[0] for series in closes.values()))
    dates = [d for d in all_dates if d >= start]
    if len(dates) < 2:
        raise RuntimeError("No hay suficiente historia para simular ese periodo")

    account = Account(cash=s["initial_cash"], fee_pct=s["fee_pct"])
    pending_sells: dict[str, tuple[str, float | None]] = {}  # symbol -> (motivo, score)
    pending_buys: list[dict] = []
    curve: list[dict] = []
    exposure: list[float] = []
    blocked_days = 0
    weights = {sym: pct / 100.0 for sym, pct in s["allocation"].items()}
    pending_alloc: bool | None = None   # None: nada pendiente · True: invertir sí o sí · False: revisión mensual
    contribution, contributed = s["monthly_contribution"], 0.0
    units = s["initial_cash"]           # valor / participaciones = rentabilidad sin contar lo aportado

    rotation = s["strategy"] == "rotation"
    for i, d in enumerate(dates):
        ts = d.strftime("%Y-%m-%d")
        today = {sym: r[d] for sym, r in rows.items() if d in r}

        # 0) Aportación mensual: dinero nuevo el primer día de bolsa de cada mes.
        if contribution > 0 and i > 0 and dates[i - 1].month != d.month:
            units += contribution / (account.equity / units)
            account.cash += contribution
            contributed += contribution

        # 1) Ejecutar a la apertura las órdenes decididas ayer.
        for sym in list(pending_sells):
            if sym in today and not math.isnan(today[sym]["open"]):
                reason, score = pending_sells.pop(sym)
                account.sell(sym, today[sym]["open"], ts, score, reason)
        for sig in pending_buys:
            sym = sig["symbol"]
            if len(account.positions) >= s["max_positions"]:
                break
            if sym in today and not math.isnan(today[sym]["open"]):
                amount = position_amount(account, s)
                if amount <= 0:
                    break
                account.buy(sym, today[sym]["open"], amount, ts, sig["score_pct"], buy_reason(sig, s))
        pending_buys = []
        if allocation and pending_alloc is not None:
            needed = set(weights) | set(account.positions)
            opens = {sym: today[sym]["open"] for sym in needed if sym in today and not math.isnan(today[sym]["open"])}
            if len(opens) == len(needed):
                orders = plan_allocation(account, opens, weights, s, force=pending_alloc)
                execute_allocation(account, orders, opens, ts)
                pending_alloc = None

        # 2) Al cierre: valorar, y decidir las órdenes de mañana.
        signals = [build_signal(sym, row, ts) for sym, row in today.items()]
        by_symbol = {sig["symbol"]: sig for sig in signals}
        for sym, pos in account.positions.items():
            sig = by_symbol.get(sym)
            if sig is None or sym in pending_sells:
                continue
            pos.mark(sig["price"])
            reason = None if allocation else check_exit(pos, sig["price"], None if rotation else sig["score_pct"], s)
            if reason:
                pending_sells[sym] = (reason, sig["score_pct"])

        market_ok = market_is_bullish(bench_rows.get(d)) if s["market_filter"] and not allocation else True
        if not market_ok:
            blocked_days += 1
        month_end = i + 1 < len(dates) and dates[i + 1].month != d.month
        if allocation:
            # Revisión con el último cierre de cada mes; la primera inversión, cuanto antes.
            if not account.positions:
                pending_alloc = True
            elif month_end and pending_alloc is None:
                pending_alloc = False
        elif rotation:
            # Se rota con el último cierre de cada mes (y el primer día, para no empezar en efectivo).
            if i == 0 or month_end:
                rot_sells, candidates = plan_rotation(signals, set(account.positions), s)
                for sym, reason in rot_sells.items():
                    pending_sells.setdefault(sym, (reason, by_symbol[sym]["score_pct"]))
                if market_ok:
                    pending_buys = [c for c in candidates if c["symbol"] not in pending_sells]
        elif market_ok:
            pending_buys = rank_candidates(signals, set(account.positions), s)

        equity = account.equity
        # Con aportaciones, la curva sigue el valor de la participación: así es comparable
        # con el índice y no confunde dinero aportado con ganancia.
        curve.append({"ts": ts, "equity": equity / units * s["initial_cash"]})
        exposure.append(1 - account.cash / equity if equity > 0 else 0.0)

    initial = s["initial_cash"]
    # Referencia 1: comprar y mantener el índice (p. ej. SPY).
    market_values: list[float | None] = [None] * len(dates)
    if bench_close is not None:
        series = bench_close.reindex(dates).ffill().bfill()
        if series.notna().all():
            market_values = [float(v) for v in series / series.iloc[0] * initial]
    # Referencia 2: comprar y mantener todo el universo a partes iguales.
    panel = pd.DataFrame(closes).reindex(dates).ffill()
    universe_values = [float(v) for v in (panel / panel.bfill().iloc[0]).mean(axis=1) * initial]

    equity_values = [p["equity"] for p in curve]
    for point, m, u in zip(curve, market_values, universe_values):
        point["market"] = m
        point["universe"] = u

    span_years = max((dates[-1] - dates[0]).days / 365.25, 1e-9)
    sells = [t for t in account.trades if t["side"] == "SELL"]
    wins = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)

    def stats(values: list) -> dict | None:
        if values[-1] is None:
            return None
        return {
            "final": values[-1],
            "total_return_pct": 100.0 * (values[-1] / initial - 1),
            "cagr_pct": 100.0 * ((values[-1] / initial) ** (1 / span_years) - 1),
            "max_drawdown_pct": _max_drawdown_pct(values),
            "sharpe": _sharpe(values),
        }

    return {
        "provider": provider.name,
        "strategy": s["strategy"],
        "years": years,
        "start": dates[0].strftime("%Y-%m-%d"),
        "end": dates[-1].strftime("%Y-%m-%d"),
        "initial_cash": initial,
        "contributed": contributed,
        "invested_total": initial + contributed,
        "final_value": account.equity,
        "benchmark": bench_sym,
        "bot": stats(equity_values),
        "market": stats(market_values),
        "universe": stats(universe_values),
        "avg_exposure_pct": 100.0 * sum(exposure) / len(exposure),
        "market_filter_blocked_pct": 100.0 * blocked_days / len(dates) if s["market_filter"] else None,
        "trades_closed": len(sells),
        "open_positions": len(account.positions),
        "win_rate_pct": 100.0 * len(wins) / len(sells) if sells else None,
        "avg_win_pct": sum(t["pnl_pct"] for t in wins) / len(wins) if wins else None,
        "avg_loss_pct": sum(t["pnl_pct"] for t in losses) / len(losses) if losses else None,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "fees_paid": sum(t["fee"] for t in account.trades),
        "by_year": _by_year(dates, equity_values, market_values),
        "curve": _downsample(curve),
        "last_trades": account.trades[-30:][::-1],
        "errors": errors,
    }

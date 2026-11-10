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

from .engine import build_signal, check_exit, rank_candidates
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


def _downsample(points: list[dict], max_points: int = 300) -> list[dict]:
    if len(points) <= max_points:
        return points
    step = len(points) / max_points
    picked = [points[int(i * step)] for i in range(max_points)]
    if picked[-1] is not points[-1]:
        picked.append(points[-1])
    return picked


def run_backtest(provider, settings: dict, years: float = 3.0) -> dict:
    s = settings
    days = int(years * 365) + WARMUP_DAYS
    rows: dict[str, dict] = {}       # symbol -> {fecha -> indicadores}
    closes: dict[str, pd.Series] = {}
    errors: dict[str, str] = {}
    for sym in s["universe"]:
        try:
            ind = compute_indicators(provider.history(sym, days))
            rows[sym] = ind.to_dict("index")
            closes[sym] = ind["close"]
        except Exception as exc:
            errors[sym] = str(exc)
    if not rows:
        raise RuntimeError("No hay datos para ningún ticker del universo")

    all_dates = sorted(set().union(*(r.keys() for r in rows.values())))
    start = all_dates[-1] - pd.Timedelta(days=int(years * 365))
    dates = [d for d in all_dates if d >= start]

    account = Account(cash=s["initial_cash"], fee_pct=s["fee_pct"])
    pending_sells: dict[str, tuple[str, float | None]] = {}  # symbol -> (motivo, score)
    pending_buys: list[dict] = []
    curve: list[dict] = []

    for d in dates:
        ts = d.strftime("%Y-%m-%d")
        today = {sym: r[d] for sym, r in rows.items() if d in r}

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
                reason = f"{sig['passed']}/{sig['total']} condiciones ({sig['score_pct']:.0f} %)"
                if account.buy(sym, today[sym]["open"], s["position_size"], ts, sig["score_pct"], reason) is None:
                    break
        pending_buys = []

        # 2) Al cierre: valorar, y decidir las órdenes de mañana.
        signals = [build_signal(sym, row, ts) for sym, row in today.items()]
        by_symbol = {sig["symbol"]: sig for sig in signals}
        for sym, pos in account.positions.items():
            sig = by_symbol.get(sym)
            if sig is None or sym in pending_sells:
                continue
            pos.mark(sig["price"])
            reason = check_exit(pos, sig["price"], sig["score_pct"], s)
            if reason:
                pending_sells[sym] = (reason, sig["score_pct"])
        pending_buys = rank_candidates(signals, set(account.positions), s)

        curve.append({"ts": ts, "equity": account.equity})

    # Referencia: comprar y mantener todo el universo a partes iguales.
    panel = pd.DataFrame(closes).reindex(dates).ffill()
    first = panel.bfill().iloc[0]
    bench = (panel / first).mean(axis=1) * s["initial_cash"]
    bench_values = [float(v) for v in bench]

    equity_values = [p["equity"] for p in curve]
    final = equity_values[-1]
    span_years = max((dates[-1] - dates[0]).days / 365.25, 1e-9)
    sells = [t for t in account.trades if t["side"] == "SELL"]
    wins = [t for t in sells if t["pnl"] > 0]
    losses = [t for t in sells if t["pnl"] <= 0]

    def cagr(end_value: float) -> float:
        return 100.0 * ((end_value / s["initial_cash"]) ** (1 / span_years) - 1)

    for point, b in zip(curve, bench_values):
        point["benchmark"] = b

    return {
        "provider": provider.name,
        "years": years,
        "start": dates[0].strftime("%Y-%m-%d"),
        "end": dates[-1].strftime("%Y-%m-%d"),
        "initial_cash": s["initial_cash"],
        "final_equity": final,
        "total_return_pct": 100.0 * (final / s["initial_cash"] - 1),
        "cagr_pct": cagr(final),
        "max_drawdown_pct": _max_drawdown_pct(equity_values),
        "benchmark_return_pct": 100.0 * (bench_values[-1] / s["initial_cash"] - 1),
        "benchmark_cagr_pct": cagr(bench_values[-1]),
        "benchmark_max_drawdown_pct": _max_drawdown_pct(bench_values),
        "trades_closed": len(sells),
        "open_positions": len(account.positions),
        "win_rate_pct": 100.0 * len(wins) / len(sells) if sells else None,
        "avg_win_pct": sum(t["pnl_pct"] for t in wins) / len(wins) if wins else None,
        "avg_loss_pct": sum(t["pnl_pct"] for t in losses) / len(losses) if losses else None,
        "fees_paid": sum(t["fee"] for t in account.trades),
        "curve": _downsample(curve),
        "last_trades": account.trades[-30:][::-1],
        "errors": errors,
    }

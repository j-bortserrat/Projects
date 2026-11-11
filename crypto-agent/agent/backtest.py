"""Backtest con avance en el tiempo (walk-forward).

* Usa el mismo `Engine` que la simulación en vivo, vela a vela.
* Decide con el cierre de una vela y ejecuta a la apertura de la siguiente.
* Comisiones y deslizamiento siempre activos.
* La calibración solo aprende de señales ya resueltas: en cada instante el agente sabe
  únicamente lo que habría sabido entonces.
* Compara siempre con comprar BTC y no tocarlo, también con costes.

Aviso: con un REFLEX basado en un modelo de lenguaje, solo valen los datos posteriores a
su fecha de entrenamiento (junio de 2026 para Haiku 5.5); antes, el modelo "recuerda" lo
que pasó. Los datos anteriores solo sirven para probar reglas escritas en código.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .config import Settings
from .engine import Engine, MemoryJournal
from .market import Candle
from .state import LOOKBACK, MIN_CANDLES

MODEL_CUTOFF_MS = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _quarter(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return f"{d.year}-T{(d.month - 1) // 3 + 1}"


def _max_drawdown(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        worst = max(worst, 1 - v / peak)
    return 100.0 * worst


def _stats(curve: list[tuple[int, float]], initial: float) -> dict:
    values = [v for _, v in curve]
    years = max((curve[-1][0] - curve[0][0]) / (365.25 * 86_400_000), 1e-9)
    daily: dict[str, float] = {}
    for ts, v in curve:
        daily[_day(ts)] = v
    dv = list(daily.values())
    rets = [b / a - 1 for a, b in zip(dv, dv[1:])]
    sharpe = None
    if len(rets) > 20:
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
        sharpe = mean / sd * math.sqrt(365) if sd > 0 else None
    return {
        "final": values[-1],
        "total_return_pct": 100.0 * (values[-1] / initial - 1),
        "cagr_pct": 100.0 * ((values[-1] / initial) ** (1 / years) - 1),
        "max_drawdown_pct": _max_drawdown(values),
        "sharpe": sharpe,
    }


def run_backtest(settings: Settings, data: dict[str, list[Candle]], reflex,
                 start_ms: int | None = None, end_ms: int | None = None, engine: Engine | None = None) -> dict:
    s, tf = settings, settings.tf_ms
    ctx_sym = s.universe.context_symbol
    timeline = [c.ts for c in data[ctx_sym]]
    if len(timeline) <= MIN_CANDLES:
        raise ValueError("histórico insuficiente para el backtest")
    first = timeline[MIN_CANDLES]
    start_ms = max(start_ms or first, first)
    end_ms = end_ms or timeline[-1] + tf
    steps = [t for t in timeline if start_ms <= t + tf <= end_ms]
    if len(steps) < 2:
        raise ValueError("el periodo pedido no contiene velas")

    journal = MemoryJournal(keep_decisions=False)
    engine = engine or Engine(s, reflex, journal)
    engine.journal = journal
    symbols = list(dict.fromkeys(s.tradable + [ctx_sym]))
    ptr = {sym: -1 for sym in symbols}
    initial = s.paper.balance
    cost = (s.paper.fee_bps + s.paper.slippage_bps) / 1e4

    bench_qty = None
    bench_curve: list[tuple[int, float]] = []
    exposure_sum = 0.0
    actions: dict[str, int] = {}
    latencies: list[float] = []
    n_decisions = n_miss = 0

    for ts in steps:
        now = ts + tf
        window: dict[str, list[Candle]] = {}
        entry: dict[str, float] = {}
        for sym in symbols:
            arr = data.get(sym) or []
            i = ptr[sym]
            while i + 1 < len(arr) and arr[i + 1].ts <= ts:
                i += 1
            ptr[sym] = i
            if i >= 0:
                window[sym] = arr[max(0, i - LOOKBACK + 1): i + 1]
                if i + 1 < len(arr) and arr[i + 1].ts == now:
                    entry[sym] = arr[i + 1].open   # la orden se ejecuta a la apertura siguiente
        report = engine.on_candle(now, window, entry_prices=entry)

        for rec in report.decisions:
            n_decisions += 1
            actions[rec["action"]] = actions.get(rec["action"], 0) + 1
            if rec["action"] == "miss":
                n_miss += 1
            else:
                latencies.append(rec["latency_ms"])
        eq = report.equity
        exposure_sum += 1 - engine.broker.cash / eq if eq > 0 else 0.0

        ctx_close = window[ctx_sym][-1].close
        if bench_qty is None:   # referencia: comprar BTC en la primera vela, con los mismos costes
            price = entry.get(ctx_sym, ctx_close) * (1 + s.paper.slippage_bps / 1e4)
            bench_qty = initial / (price * (1 + s.paper.fee_bps / 1e4))
        bench_curve.append((now, bench_qty * ctx_close))

    curve = journal.equity_curve
    sells = [f for f in journal.fills if f.side == "SELL" and f.status != "rejected"]
    wins = [f for f in sells if f.pnl > 0]
    losses = [f for f in sells if f.pnl <= 0]
    gross_loss = -sum(f.pnl for f in losses)
    rs = [f.pnl_r for f in sells if f.pnl_r is not None]

    folds: dict[str, dict] = {}
    for (ts, v), (_, b) in zip(curve, bench_curve):
        q = folds.setdefault(_quarter(ts - 1), {"bot0": v, "btc0": b})
        q["bot1"], q["btc1"] = v, b
    prev = None
    fold_rows = []
    for name, q in folds.items():
        bot0, btc0 = (prev["bot1"], prev["btc1"]) if prev else (initial, initial)
        fold_rows.append({"periodo": name, "bot_pct": 100 * (q["bot1"] / bot0 - 1), "btc_pct": 100 * (q["btc1"] / btc0 - 1)})
        prev = q

    lat = sorted(latencies)
    bot, btc = _stats(curve, initial), _stats(bench_curve, initial)
    return {
        "reflex": reflex.name,
        "start": _day(steps[0]), "end": _day(steps[-1] + tf), "candles": len(steps),
        "timeframe": s.exchange.timeframe, "symbols": s.tradable,
        "model_data_valid": getattr(reflex, "local", False) or steps[0] >= MODEL_CUTOFF_MS,
        "initial": initial, "bot": bot, "btc_hold": btc,
        "beats_btc": bot["final"] > btc["final"],
        "trades": len(sells),
        "win_rate_pct": 100 * len(wins) / len(sells) if sells else None,
        "avg_r": sum(rs) / len(rs) if rs else None,
        "profit_factor": sum(f.pnl for f in wins) / gross_loss if gross_loss > 0 else None,
        "fees_paid": sum(f.fee for f in journal.fills),
        "cost_per_side_pct": 100 * cost,
        "avg_exposure_pct": 100 * exposure_sum / len(steps),
        "open_positions_at_end": len(engine.broker.positions),
        "decisions": n_decisions, "misses": n_miss, "actions": actions,
        "latency_ms_p50": lat[len(lat) // 2] if lat else None,
        "latency_ms_p95": lat[int(len(lat) * 0.95)] if lat else None,
        "kill_switch": engine.kill_switch,
        "reconciliation_issues": engine.broker.reconcile(initial, journal.fills),
        "shadow_signals": engine.cal.count,
        "base_rate": engine.cal.base_rate,
        "brier_raw": engine.cal.brier_raw,
        "brier_calibrated": engine.cal.brier_calibrated,
        "brier_base_rate": engine.cal.brier_base_rate,
        "no_edge_p": engine.cal.no_edge_p,
        "breakeven_p": engine.cal.breakeven_p,
        "calibration": engine.cal.table(),
        "folds": fold_rows,
        "curve": [(ts, round(v, 2), round(b, 2)) for (ts, v), (_, b) in list(zip(curve, bench_curve))[:: max(1, len(curve) // 400)]],
        "exit_reasons": _count([f.reason for f in sells]),
    }


def _count(items: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))

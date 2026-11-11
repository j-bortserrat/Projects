"""Comandos del agente:  python -m agent <comando>

  doctor      comprueba configuración, conexión, reloj y REFLEX
  backtest    simula sobre histórico real y compara con mantener BTC
  once        procesa una sola vela (útil para probar)
  run         bucle 24/7 en simulación
  status      cartera, posiciones y últimas órdenes
  review      revisión nocturna bajo demanda
  policy      list | promote N --confirm | rollback --confirm
  reset-kill  rearma el interruptor de emergencia (--confirm)
  health      código 0 si el agente está vivo (para Docker)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

from .config import Settings, load_settings

# Estimación del coste del REFLEX con Haiku 5.5. Precios por millón de tokens a 2026-11-03;
# compruébalos en la web de Anthropic antes de fiarte del número.
HAIKU_IN, HAIKU_OUT, EST_IN_TOKENS, EST_OUT_TOKENS = 0.10, 0.50, 900, 500


def _when(ms: int | None) -> str:
    return "—" if not ms else datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _market(s: Settings):
    from .market import CcxtMarket
    return CcxtMarket(s.exchange.id, s.exchange.public_api_url)


def cmd_doctor(s: Settings, args) -> int:
    from .alerts import telegram_configured
    from .reflex import make_reflex
    ok = True
    print(f"Modo: {s.mode} · exchange: {s.exchange.id} ({s.exchange.market}) · velas: {s.exchange.timeframe}")
    print(f"Tickers: {', '.join(s.tradable)}")
    print(f"Riesgo: {s.risk.risk_per_trade_pct} % por operación · {s.risk.max_daily_loss_pct} % pérdida diaria · "
          f"{s.risk.max_open_positions} posiciones · interruptor al {s.risk.kill_switch_drawdown_pct} % de caída")
    try:
        m = _market(s)
        server = m.server_time_ms()
        skew = int(time.time() * 1000) - server
        print(f"[ok] Conexión con {s.exchange.id}. Desfase del reloj: {skew:+d} ms" + ("" if abs(skew) < 30_000 else "  <-- DEMASIADO"))
        ok &= abs(skew) < 30_000
        candles = m.candles(s.universe.context_symbol, s.exchange.timeframe, limit=5)
        print(f"[ok] Datos de {s.universe.context_symbol}: último precio {candles[-1].close:g}")
    except Exception as exc:
        ok = False
        print(f"[FALLO] No se pudo leer el exchange: {type(exc).__name__}: {str(exc)[:200]}")
        print("        Si tu red bloquea api.binance.com, pon en config.toml:")
        print('        [exchange]\n        public_api_url = "https://data-api.binance.vision/api/v3"')
    try:
        reflex = make_reflex(s.reflex)
        print(f"[ok] REFLEX: {reflex.name}")
        if reflex.name.startswith("haiku"):
            calls = len(s.tradable) * 30 * 86_400_000 // s.tf_ms
            cost = calls * (EST_IN_TOKENS * HAIKU_IN + EST_OUT_TOKENS * HAIKU_OUT) / 1e6
            print(f"     ~{calls} llamadas al mes · coste estimado ~{cost:.2f} $/mes (estimación, no medición)")
        else:
            print("     Sin clave de Anthropic: se usa la referencia de reglas. Define ANTHROPIC_API_KEY para usar Haiku.")
    except Exception as exc:
        ok = False
        print(f"[FALLO] REFLEX: {exc}")
    print(f"[{'ok' if telegram_configured() else '--'}] Alertas de Telegram {'configuradas' if telegram_configured() else 'sin configurar (opcional)'}")
    print("\nTodo listo." if ok else "\nHay fallos que resolver antes de arrancar.")
    return 0 if ok else 1


def cmd_backtest(s: Settings, args) -> int:
    from .backtest import MODEL_CUTOFF_MS, run_backtest
    from .history import load_or_fetch
    from .reflex import RuleReflex, make_reflex
    from .state import MIN_CANDLES
    tf = s.tf_ms
    end = int(time.time() * 1000) // tf * tf
    start = end - args.days * 86_400_000
    reflex = RuleReflex() if args.reflex == "rules" else make_reflex(s.reflex)
    if not getattr(reflex, "local", False) and start < MODEL_CUTOFF_MS:
        start = MODEL_CUTOFF_MS
        print("Aviso: con un modelo de lenguaje solo valen datos posteriores a junio de 2026; se recorta el periodo.")
    market, cache = _market(s), os.path.join(s.data_dir, "cache")
    data = {}
    for sym in dict.fromkeys(s.tradable + [s.universe.context_symbol]):
        print(f"Descargando {sym}…", file=sys.stderr)
        data[sym] = load_or_fetch(market, cache, sym, s.exchange.timeframe, tf, start - (MIN_CANDLES + 5) * tf, end)
    r = run_backtest(s, data, reflex, start_ms=start, end_ms=end)
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0
    b, h = r["bot"], r["btc_hold"]
    print(f"\nBacktest {r['start']} → {r['end']} · {r['candles']} velas de {r['timeframe']} · REFLEX {r['reflex']}")
    print("SUPERA a mantener BTC." if r["beats_btc"] else "NO supera a mantener BTC (tras comisiones y deslizamiento).")
    print(f"  Agente        {b['total_return_pct']:+8.1f} %   anual {b['cagr_pct']:+7.1f} %   peor caída -{b['max_drawdown_pct']:.1f} %")
    print(f"  Mantener BTC  {h['total_return_pct']:+8.1f} %   anual {h['cagr_pct']:+7.1f} %   peor caída -{h['max_drawdown_pct']:.1f} %")
    wr = "—" if r["win_rate_pct"] is None else f"{r['win_rate_pct']:.0f} %"
    print(f"  Operaciones {r['trades']} · con ganancia {wr} · comisiones {r['fees_paid']:.2f} $ · capital invertido de media {r['avg_exposure_pct']:.1f} %")
    print(f"  Señales medidas {r['shadow_signals']} · acierto medio {(r['base_rate'] or 0) * 100:.1f} % · "
          f"acierto necesario para no perder tras costes {r['breakeven_p'] * 100:.1f} %")
    if r["brier_calibrated"] is not None:
        verdict = "aporta información" if r["brier_calibrated"] < r["brier_base_rate"] else "NO aporta información"
        print(f"  Confianza del REFLEX: {verdict} (Brier {r['brier_calibrated']:.4f} frente a {r['brier_base_rate']:.4f} de la tasa media)")
    for row in r["calibration"]:
        acc = "—" if row["acierto"] is None else f"{row['acierto'] * 100:.0f} %"
        print(f"    confianza {row['tramo']}: {row['n']:5d} señales · acierto {acc}")
    print("  Por trimestre (agente / BTC): " + "  ".join(f"{f['periodo']} {f['bot_pct']:+.0f}/{f['btc_pct']:+.0f}" for f in r["folds"]))
    if r["reconciliation_issues"]:
        print("  CONCILIACIÓN FALLIDA:", r["reconciliation_issues"])
    return 0


def _runtime(s: Settings):
    from .runtime import Runtime
    return Runtime(s)


def cmd_once(s: Settings, args) -> int:
    rt = _runtime(s)
    report = rt.step_once()
    print(f"Vela procesada a las {_when(report.ts)} · REFLEX {rt.reflex.name} · cartera {report.equity:.2f} $")
    for d in report.decisions:
        dec = d["decision"]
        judged = "sin decisión" if not dec else (f"{dec['regime']}, {dec['direction']} "
                                                 f"(confianza {dec['direction_confidence']:.2f}, calidad {dec['setup_quality']})")
        print(f"  {d['symbol']:<10} {d['price']:>12g}  {judged:<52} → {d['action']}: {'; '.join(d['reasons'])}")
    for sym, issues in report.issues.items():
        print(f"  INCIDENCIA {sym}: {', '.join(issues)}")
    for e in report.escalations:
        print("  ATENCIÓN:", e)
    return 0


def cmd_run(s: Settings, args) -> int:
    try:
        _runtime(s).run_forever()
    except KeyboardInterrupt:
        print("\nAgente detenido. El estado queda guardado; `python -m agent run` continúa donde estaba.")
    return 0


def cmd_status(s: Settings, args) -> int:
    from .store import Store
    path = os.path.join(s.data_dir, "agent.db")
    if not os.path.exists(path):
        print("Todavía no hay datos: arranca el agente con `python -m agent run`.")
        return 0
    store = Store(path)
    st, meta = store.load_state("engine"), store.load_state("meta") or {}
    if not st:
        print("Todavía no se ha procesado ninguna vela.")
        return 0
    prices, broker = st["prices"], st["broker"]
    equity = broker["cash"] + sum(p["qty"] * prices.get(sym, p["entry"]) for sym, p in broker["positions"].items())
    initial = s.paper.balance
    print(f"Cartera (simulación): {equity:.2f} $ · resultado {equity - initial:+.2f} $ ({(equity / initial - 1) * 100:+.2f} %) · efectivo {broker['cash']:.2f} $")
    print(f"REFLEX {meta.get('reflex', '?')} · política v{meta.get('policy_version', '?')} · máximo histórico {st['peak_equity']:.2f} $")
    if st["kill_switch"]:
        print("INTERRUPTOR DE EMERGENCIA ACTIVO:", st["kill_switch"])
    if st["halt_entries"]:
        print("COMPRAS DETENIDAS:", st["halt_entries"])
    for sym, p in broker["positions"].items():
        px = prices.get(sym, p["entry"])
        print(f"  {sym}: {p['qty']:.6g} a {p['entry']:.6g} · ahora {px:.6g} ({(px / p['entry'] - 1) * 100:+.2f} %) · stop {p['stop']:.6g} · objetivo {p['target']:.6g}")
    if not broker["positions"]:
        print("  Sin posiciones abiertas.")
    cal = st["calibrator"]
    print(f"Señales medidas: {cal['count']}" + (f" · acierto medio {cal['sum_y'] / cal['count'] * 100:.1f} %" if cal["count"] else ""))
    for f in store.fills_since(0)[-8:]:
        pnl = "" if f["pnl"] is None else f" · {f['pnl']:+.2f} $"
        print(f"  {_when(f['ts'])}  {f['side']:<4} {f['symbol']:<10} {f['status']:<8} {f['price']:.6g}{pnl}  {f['reason']}")
    return 0


def cmd_review(s: Settings, args) -> int:
    from .review import render
    result = _runtime(s).review()
    print(render(result))
    print("Informe guardado en", result.get("report_path"))
    return 0


def cmd_policy(s: Settings, args) -> int:
    from .policy import PolicyStore
    store = PolicyStore(s.data_dir)
    active, _ = store.active(s.policy)
    if args.action == "list":
        for v in store.versions():
            mark = "ACTIVA" if v["version"] == active else "      "
            print(f"v{v['version']:<3} {mark} {v['created']}  {v['reason']}\n       {v['policy']}")
        return 0
    if not args.confirm:
        print("Cambiar la política en vigor requiere tu confirmación: repite el comando con --confirm.")
        return 2
    if args.action == "promote":
        if args.version is None:
            print("Indica la versión: policy promote N --confirm")
            return 2
        print(f"Política v{store.promote(args.version)} activa. Reinicia el agente para aplicarla.")
    else:
        print(f"Se ha vuelto a la política v{store.rollback()}. Reinicia el agente para aplicarla.")
    return 0


def cmd_reset_kill(s: Settings, args) -> int:
    from .store import Store
    if not args.confirm:
        print("Rearmar el interruptor de emergencia requiere tu confirmación: añade --confirm.")
        return 2
    store = Store(os.path.join(s.data_dir, "agent.db"))
    st = store.load_state("engine")
    if not st or not st["kill_switch"]:
        print("El interruptor no está activo.")
        return 0
    equity = st["broker"]["cash"] + sum(p["qty"] * st["prices"].get(k, p["entry"]) for k, p in st["broker"]["positions"].items())
    st["kill_switch"], st["peak_equity"] = None, equity   # la caída se vuelve a medir desde aquí
    store.save_state("engine", st)
    store.event(int(time.time() * 1000), "kill_switch_reset", "Interruptor rearmado manualmente")
    print("Interruptor rearmado. La caída se mide desde el valor actual de la cartera.")
    return 0


def cmd_health(s: Settings, args) -> int:
    path = os.path.join(s.data_dir, "heartbeat.json")
    try:
        with open(path) as fh:
            beat = json.load(fh)
    except (OSError, ValueError):
        print("sin latido")
        return 1
    age = (time.time() * 1000 - beat["ts"]) / 1000
    stale_step = beat.get("last_step") and time.time() * 1000 - beat["last_step"] > 2 * s.tf_ms + 600_000
    if age > 180 or stale_step:
        print(f"no responde (latido hace {age:.0f} s, última vela {_when(beat.get('last_step'))})")
        return 1
    print(f"ok (latido hace {age:.0f} s, última vela {_when(beat.get('last_step'))})")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="ruta de config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    bt = sub.add_parser("backtest")
    bt.add_argument("--days", type=int, default=365)
    bt.add_argument("--reflex", choices=["rules", "config"], default="rules")
    bt.add_argument("--json", action="store_true")
    for name in ("once", "run", "status", "review", "health"):
        sub.add_parser(name)
    pol = sub.add_parser("policy")
    pol.add_argument("action", choices=["list", "promote", "rollback"])
    pol.add_argument("version", nargs="?", type=int)
    pol.add_argument("--confirm", action="store_true")
    rk = sub.add_parser("reset-kill")
    rk.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    settings = load_settings(args.config)
    handlers = {"doctor": cmd_doctor, "backtest": cmd_backtest, "once": cmd_once, "run": cmd_run,
                "status": cmd_status, "review": cmd_review, "policy": cmd_policy, "reset-kill": cmd_reset_kill,
                "health": cmd_health}
    try:
        code = handlers[args.command](settings, args)
    except Exception as exc:  # mensaje claro en vez de una traza para el usuario
        print(f"Error: {exc}", file=sys.stderr)
        code = 1
    sys.exit(code)

"""Panel web del bot de inversión simulada.

Uso:
    python app.py                         # datos reales de Yahoo Finance
    DATA_PROVIDER=synthetic python app.py # datos inventados (solo para probar)

Variables de entorno:
    DATA_PROVIDER       yahoo (por defecto) | synthetic
    BOT_DB              ruta de la base de datos (por defecto data/bot.db)
    AUTO_CYCLE_MINUTES  cada cuántos minutos analiza solo (por defecto 15; 0 = nunca)
    HOST / PORT         por defecto 127.0.0.1:5000
    PANEL_PASSWORD      contraseña del panel (obligatoria si HOST no es local)
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   alertas de compras y ventas (opcional)
"""
from __future__ import annotations

import hmac
import os
import sys
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, request

from bot.backtest import run_backtest
from bot.config import PRESETS, SettingsError, validate_settings
from bot.data import make_provider
from bot.engine import run_cycle
from bot.notify import notify_cycle, telegram_configured
from bot.store import Store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def create_app(provider=None, db_path: str | None = None, auto_cycle_minutes: float = 0,
               password: str | None = None) -> Flask:
    app = Flask(__name__)
    provider = provider or make_provider(os.environ.get("DATA_PROVIDER"))
    store = Store(db_path or os.environ.get("BOT_DB") or os.path.join(BASE_DIR, "data", "bot.db"))
    cycle_lock = threading.Lock()  # un solo análisis a la vez (manual o automático)

    def do_cycle() -> dict:
        with cycle_lock:
            summary = run_cycle(provider, store)
        notify_cycle(summary)
        return summary

    if password:
        @app.before_request
        def require_password():
            auth = request.authorization
            if auth is None or not hmac.compare_digest((auth.password or "").encode(), password.encode()):
                return Response("Contraseña necesaria", 401, {"WWW-Authenticate": 'Basic realm="Bot de inversion"'})

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/state")
    def state():
        s = store.get_settings()
        account = store.load_account(s["fee_pct"])
        initial = store.get_initial_cash()
        contributed = store.get_contributed()
        invested = initial + contributed
        equity = account.equity
        positions = sorted((p.to_dict() for p in account.positions.values()), key=lambda p: -p["value"])
        created = datetime.fromisoformat(store.get_meta("created_at"))
        months = max(0.0, (datetime.now(timezone.utc) - created).days / 30.44)
        costs = months * s["monthly_cost"]
        month_key = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m")

        # Cartera fija: peso objetivo frente a peso real de cada ETF.
        weights = []
        if s["strategy"] == "allocation":
            held = {p["symbol"]: p["value"] for p in positions}
            for sym in dict.fromkeys(list(s["allocation"]) + list(held)):
                target, value = s["allocation"].get(sym, 0.0), held.get(sym, 0.0)
                actual = 100.0 * value / equity if equity > 0 else 0.0
                weights.append({"symbol": sym, "target_pct": target, "actual_pct": actual,
                                "drift_pct": actual - target, "value": value})

        return jsonify({
            "provider": provider.name,
            "auto_cycle_minutes": auto_cycle_minutes,
            "telegram": telegram_configured(),
            "settings": s,
            "presets": PRESETS,
            "account": {
                "initial_cash": initial,
                "contributed": contributed,
                "invested": invested,
                "cash": account.cash,
                "in_positions": equity - account.cash,
                "equity": equity,
                "pnl": equity - invested,
                # Rentabilidad por participación: no cuenta como ganancia el dinero aportado.
                "pnl_pct": 100.0 * (equity / store.get_units() - 1),
                "realized_pnl": store.realized_pnl(),
                "unrealized_pnl": sum(p["pnl"] for p in positions),
                "created_at": created.isoformat(timespec="seconds"),
                "costs_to_date": costs,
                "net_pnl": equity - invested - costs,
            },
            "rebalance_pending": s["strategy"] != "checklist" and store.get_meta("last_rebalance") != month_key,
            "weights": weights,
            "positions": positions,
            "trades": store.trades(200),
            "equity": store.equity_history(),
            "signals": store.signals(),
            "last_cycle": store.get_last_cycle(),
        })

    @app.post("/api/cycle")
    def cycle():
        try:
            return jsonify(do_cycle())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.post("/api/settings")
    def save_settings():
        old = store.get_settings()
        try:
            new = validate_settings(request.get_json(force=True) or {}, old)
        except SettingsError as exc:
            return jsonify({"error": str(exc)}), 400
        store.save_settings(new)
        # Si cambia qué se compra, la revisión mensual se repite en el próximo análisis
        # en vez de esperar al mes siguiente.
        if any(old[k] != new[k] for k in ("strategy", "allocation", "universe", "max_positions")):
            store.clear_meta("last_rebalance")
        return jsonify(new)

    @app.post("/api/reset")
    def reset():
        with cycle_lock:
            store.reset()
        return jsonify({"ok": True})

    @app.get("/api/backtest")
    def backtest():
        """Simula los ajustes actuales. `strategy` permite probar la otra estrategia sin guardarla."""
        try:
            years = float(request.args.get("years", 5))
        except ValueError:
            return jsonify({"error": "years debe ser un número"}), 400
        if not 0.5 <= years <= 25:
            return jsonify({"error": "years debe estar entre 0.5 y 25"}), 400
        settings = store.get_settings()
        if "strategy" in request.args:
            try:
                settings = validate_settings({"strategy": request.args["strategy"]}, settings)
            except SettingsError as exc:
                return jsonify({"error": str(exc)}), 400
        try:
            return jsonify(run_backtest(provider, settings, years))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    if auto_cycle_minutes > 0:
        def loop():
            while True:
                try:
                    do_cycle()
                except Exception as exc:  # el bot no debe morir por un fallo puntual
                    app.logger.error("Fallo en el ciclo automático: %s", exc)
                time.sleep(auto_cycle_minutes * 60)

        threading.Thread(target=loop, daemon=True, name="auto-cycle").start()

    return app


def main() -> None:
    minutes = float(os.environ.get("AUTO_CYCLE_MINUTES", "15"))
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    password = os.environ.get("PANEL_PASSWORD") or None
    if host not in LOCAL_HOSTS and not password:
        sys.exit("Por seguridad, define PANEL_PASSWORD para abrir el panel fuera de este equipo (HOST=%s)." % host)
    app = create_app(auto_cycle_minutes=minutes, password=password)
    print(f"Panel en http://{host}:{port}  (Ctrl+C para parar)")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=4)
    except ImportError:
        app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()

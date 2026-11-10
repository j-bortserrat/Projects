"""Panel web del bot de inversión simulada.

Uso:
    python app.py                         # datos reales de Yahoo Finance
    DATA_PROVIDER=synthetic python app.py # datos inventados (solo para probar)

Variables de entorno:
    DATA_PROVIDER       yahoo (por defecto) | synthetic
    BOT_DB              ruta de la base de datos (por defecto data/bot.db)
    AUTO_CYCLE_MINUTES  cada cuántos minutos analiza solo (por defecto 30; 0 = nunca)
    HOST / PORT         por defecto 127.0.0.1:5000
"""
from __future__ import annotations

import os
import threading
import time

from flask import Flask, jsonify, request

from bot.backtest import run_backtest
from bot.config import SettingsError, validate_settings
from bot.data import make_provider
from bot.engine import run_cycle
from bot.store import Store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app(provider=None, db_path: str | None = None, auto_cycle_minutes: float = 0) -> Flask:
    app = Flask(__name__)
    provider = provider or make_provider(os.environ.get("DATA_PROVIDER"))
    store = Store(db_path or os.environ.get("BOT_DB") or os.path.join(BASE_DIR, "data", "bot.db"))
    cycle_lock = threading.Lock()  # un solo análisis a la vez (manual o automático)

    def do_cycle() -> dict:
        with cycle_lock:
            return run_cycle(provider, store)

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/state")
    def state():
        s = store.get_settings()
        account = store.load_account(s["fee_pct"])
        initial = store.get_initial_cash()
        equity = account.equity
        positions = sorted((p.to_dict() for p in account.positions.values()), key=lambda p: -p["value"])
        return jsonify({
            "provider": provider.name,
            "auto_cycle_minutes": auto_cycle_minutes,
            "settings": s,
            "account": {
                "initial_cash": initial,
                "cash": account.cash,
                "invested": equity - account.cash,
                "equity": equity,
                "pnl": equity - initial,
                "pnl_pct": 100.0 * (equity - initial) / initial,
                "realized_pnl": store.realized_pnl(),
                "unrealized_pnl": sum(p["pnl"] for p in positions),
            },
            "positions": positions,
            "trades": store.trades(200),
            "equity": store.equity_history(),
            "signals": sorted(
                store.signals(),
                key=lambda g: (g["score_pct"] is None, -(g["score_pct"] or 0), g["symbol"]),
            ),
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
        try:
            new = validate_settings(request.get_json(force=True) or {}, store.get_settings())
        except SettingsError as exc:
            return jsonify({"error": str(exc)}), 400
        store.save_settings(new)
        return jsonify(new)

    @app.post("/api/reset")
    def reset():
        with cycle_lock:
            store.reset()
        return jsonify({"ok": True})

    @app.get("/api/backtest")
    def backtest():
        try:
            years = float(request.args.get("years", 3))
        except ValueError:
            return jsonify({"error": "years debe ser un número"}), 400
        if not 0.5 <= years <= 15:
            return jsonify({"error": "years debe estar entre 0.5 y 15"}), 400
        try:
            return jsonify(run_backtest(provider, store.get_settings(), years))
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


if __name__ == "__main__":
    minutes = float(os.environ.get("AUTO_CYCLE_MINUTES", "30"))
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    create_app(auto_cycle_minutes=minutes).run(host=host, port=port, debug=False)

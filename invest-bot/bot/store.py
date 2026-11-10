"""Persistencia en SQLite de la cartera ficticia, las operaciones y las señales."""
from __future__ import annotations

import json
import os
import sqlite3
import threading

from .config import DEFAULT_SETTINGS, validate_settings
from .portfolio import Account, Position

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY, qty REAL, cost REAL, opened_at TEXT,
    entry_score REAL, high_water REAL, last_price REAL);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, qty REAL,
    price REAL, fee REAL, amount REAL, pnl REAL, pnl_pct REAL, reason TEXT, score REAL);
CREATE TABLE IF NOT EXISTS equity (ts TEXT, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS signals (
    symbol TEXT PRIMARY KEY, ts TEXT, price REAL, score_pct REAL, passed INTEGER,
    total INTEGER, status TEXT, details TEXT);
"""


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.executescript(_SCHEMA)
        if self._get("cash") is None:
            self.reset()

    # --- meta -----------------------------------------------------------------
    def _get(self, key: str):
        row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else json.loads(row["value"])

    def _set(self, key: str, value) -> None:
        self._db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def get_settings(self) -> dict:
        with self._lock:
            saved = self._get("settings") or {}
        return validate_settings(saved, DEFAULT_SETTINGS)

    def save_settings(self, settings: dict) -> None:
        with self._lock, self._db:
            self._set("settings", settings)

    def get_last_cycle(self):
        with self._lock:
            return self._get("last_cycle")

    def get_initial_cash(self) -> float:
        with self._lock:
            return float(self._get("initial_cash"))

    # --- cuenta ---------------------------------------------------------------
    def reset(self) -> None:
        """Borra cartera e historial y empieza de nuevo con el capital inicial configurado."""
        with self._lock, self._db:
            for table in ("positions", "trades", "equity", "signals"):
                self._db.execute(f"DELETE FROM {table}")
            saved = self._get("settings") or {}
            initial = float(validate_settings(saved, DEFAULT_SETTINGS)["initial_cash"])
            self._set("cash", initial)
            self._set("initial_cash", initial)
            self._db.execute("DELETE FROM meta WHERE key='last_cycle'")

    def load_account(self, fee_pct: float) -> Account:
        with self._lock:
            account = Account(cash=float(self._get("cash")), fee_pct=fee_pct)
            for r in self._db.execute("SELECT * FROM positions ORDER BY opened_at"):
                account.positions[r["symbol"]] = Position(
                    r["symbol"], r["qty"], r["cost"], r["opened_at"], r["entry_score"],
                    r["high_water"], r["last_price"],
                )
        return account

    def save_cycle(self, account: Account, signals: list[dict], summary: dict) -> None:
        """Guarda de forma atómica todo lo que ha cambiado en un ciclo de análisis."""
        with self._lock, self._db:
            self._set("cash", account.cash)
            self._db.execute("DELETE FROM positions")
            self._db.executemany(
                "INSERT INTO positions VALUES(?,?,?,?,?,?,?)",
                [(p.symbol, p.qty, p.cost, p.opened_at, p.entry_score, p.high_water, p.last_price)
                 for p in account.positions.values()],
            )
            self._db.executemany(
                "INSERT INTO trades(ts,symbol,side,qty,price,fee,amount,pnl,pnl_pct,reason,score) "
                "VALUES(:ts,:symbol,:side,:qty,:price,:fee,:amount,:pnl,:pnl_pct,:reason,:score)",
                account.trades,
            )
            account.trades = []
            self._db.execute("DELETE FROM signals")
            self._db.executemany(
                "INSERT INTO signals VALUES(?,?,?,?,?,?,?,?)",
                [(s["symbol"], s["ts"], s["price"], s["score_pct"], s["passed"], s["total"],
                  s["status"], json.dumps(s["results"])) for s in signals],
            )
            self._db.execute("INSERT INTO equity VALUES(?,?,?)", (summary["ts"], account.equity, account.cash))
            self._set("last_cycle", summary)

    # --- lecturas para el panel -----------------------------------------------
    def trades(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def symbols_sold_on(self, day: str) -> set[str]:
        """Tickers vendidos en la fecha indicada (AAAA-MM-DD)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT DISTINCT symbol FROM trades WHERE side='SELL' AND substr(ts,1,10)=?", (day,)
            ).fetchall()
        return {r["symbol"] for r in rows}

    def realized_pnl(self) -> float:
        with self._lock:
            row = self._db.execute("SELECT COALESCE(SUM(pnl),0) AS p FROM trades WHERE side='SELL'").fetchone()
        return float(row["p"])

    def equity_history(self, limit: int = 1000) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM (SELECT rowid AS rid, ts, equity, cash FROM equity ORDER BY rowid DESC LIMIT ?) "
                "ORDER BY rid", (limit,)
            ).fetchall()
        return [{"ts": r["ts"], "equity": r["equity"], "cash": r["cash"]} for r in rows]

    def signals(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM signals").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["results"] = json.loads(d.pop("details"))
            out.append(d)
        return out

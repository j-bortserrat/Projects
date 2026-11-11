"""Diario persistente en SQLite: estado del agente, órdenes, decisiones, señales y eventos.
Es lo que permite apagar el equipo y retomar exactamente donde estaba."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import asdict

from .broker import Fill

_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, symbol TEXT, side TEXT,
    qty REAL, price REAL, fee REAL, status TEXT, reason TEXT, pnl REAL, pnl_r REAL);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, symbol TEXT, reflex TEXT,
    price REAL, latency_ms REAL, error TEXT, action TEXT, reasons TEXT, decision TEXT);
CREATE TABLE IF NOT EXISTS shadows (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, opened_ts INTEGER,
    closed_ts INTEGER, confidence REAL, p_cal REAL, net_r REAL, won INTEGER, exit TEXT);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS equity (ts INTEGER PRIMARY KEY, equity REAL);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions(ts);
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

    def _exec(self, sql: str, args: tuple = ()) -> None:
        with self._lock, self._db:
            self._db.execute(sql, args)

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # --- interfaz de diario que usa el motor ------------------------------------------
    def fill(self, f: Fill) -> None:
        d = asdict(f)
        self._exec("INSERT INTO fills(ts,symbol,side,qty,price,fee,status,reason,pnl,pnl_r) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   tuple(d[k] for k in ("ts", "symbol", "side", "qty", "price", "fee", "status", "reason", "pnl", "pnl_r")))

    def decision(self, rec: dict) -> None:
        self._exec("INSERT INTO decisions(ts,symbol,reflex,price,latency_ms,error,action,reasons,decision) VALUES(?,?,?,?,?,?,?,?,?)",
                   (rec["ts"], rec["symbol"], rec["reflex"], rec["price"], rec["latency_ms"], rec["error"], rec["action"],
                    json.dumps(rec["reasons"], ensure_ascii=False), json.dumps(rec["decision"])))

    def shadow(self, rec: dict) -> None:
        self._exec("INSERT INTO shadows(symbol,opened_ts,closed_ts,confidence,p_cal,net_r,won,exit) VALUES(?,?,?,?,?,?,?,?)",
                   (rec["symbol"], rec["opened_ts"], rec["closed_ts"], rec["confidence"], rec["p_cal"], rec["net_r"],
                    int(rec["won"]), rec["exit"]))

    def event(self, ts: int, kind: str, text: str) -> None:
        self._exec("INSERT INTO events(ts,kind,text) VALUES(?,?,?)", (ts, kind, text))

    def equity(self, ts: int, value: float) -> None:
        self._exec("INSERT OR REPLACE INTO equity(ts,equity) VALUES(?,?)", (ts, value))

    def all_fills(self) -> list[Fill]:
        return [Fill(**{k: r[k] for k in ("ts", "symbol", "side", "qty", "price", "fee", "status", "reason", "pnl", "pnl_r")})
                for r in self._rows("SELECT * FROM fills ORDER BY id")]

    # --- estado ---------------------------------------------------------------------
    def save_state(self, key: str, value: dict) -> None:
        self._exec("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value)))

    def load_state(self, key: str) -> dict | None:
        rows = self._rows("SELECT value FROM state WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else None

    # --- consultas para el estado y la revisión nocturna --------------------------------
    def fills_since(self, ts: int) -> list[dict]:
        return self._rows("SELECT * FROM fills WHERE ts>=? ORDER BY id", (ts,))

    def decisions_since(self, ts: int) -> list[dict]:
        rows = self._rows("SELECT * FROM decisions WHERE ts>=? ORDER BY id", (ts,))
        for r in rows:
            r["reasons"], r["decision"] = json.loads(r["reasons"]), json.loads(r["decision"])
        return rows

    def events_since(self, ts: int) -> list[dict]:
        return self._rows("SELECT * FROM events WHERE ts>=? ORDER BY id", (ts,))

    def equity_curve(self, limit: int = 2000) -> list[dict]:
        return self._rows("SELECT * FROM (SELECT * FROM equity ORDER BY ts DESC LIMIT ?) ORDER BY ts", (limit,))

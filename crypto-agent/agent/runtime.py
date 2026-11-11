"""Bucle 24/7 en simulación. Sobrevive a reinicios: todo el estado está en SQLite.

No envía órdenes reales. El modo real no está implementado y, cuando lo esté, exigirá
tu aprobación explícita y claves solo de trading.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from .alerts import notify
from .calibration import Calibrator
from .config import Settings
from .engine import Engine, StepReport
from .market import CcxtMarket, closed_only
from .policy import PolicyStore
from .reflex import make_reflex
from .review import nightly_review
from .schema import Setup
from .state import LOOKBACK
from .store import Store

log = logging.getLogger("agent")


class LiveTradingNotApproved(RuntimeError):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


def load_setups(path: str = "setups.json") -> list[Setup]:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [Setup.model_validate(x) for x in json.load(fh)]


class Runtime:
    def __init__(self, settings: Settings, market=None, store=None, reflex=None, setups: list[Setup] | None = None):
        if settings.mode != "paper":
            raise LiveTradingNotApproved(
                "El modo real no está implementado. Este agente solo opera en simulación; pasar a dinero real "
                "requiere tu aprobación explícita y claves del exchange solo de trading, sin permiso de retirada.")
        os.makedirs(settings.data_dir, exist_ok=True)
        self.policies = PolicyStore(settings.data_dir)
        self.policy_version, policy = self.policies.active(settings.policy)
        self.s = settings.model_copy(update={"policy": policy})
        self.market = market or CcxtMarket(self.s.exchange.id, self.s.exchange.public_api_url)
        self.store = store or Store(os.path.join(self.s.data_dir, "agent.db"))
        self.reflex = reflex or make_reflex(self.s.reflex)
        self.engine = Engine(self.s, self.reflex, self.store, setups=load_setups() if setups is None else setups)
        self.heartbeat_path = os.path.join(self.s.data_dir, "heartbeat.json")
        self.illiquid: dict[str, str] = {}
        self.liquidity_day: str | None = None
        self._restore()

    # La calibración mide una estructura de operación concreta (stop, objetivo, duración)
    # y un REFLEX concreto: si cambian, los aciertos anteriores ya no son comparables.
    def _calibration_key(self) -> str:
        p = self.s.policy
        return f"{self.reflex.name}|{p.stop_atr}|{p.target_r}|{p.max_hold_candles}|{self.s.exchange.timeframe}"

    def _restore(self) -> None:
        saved = self.store.load_state("engine")
        meta = self.store.load_state("meta") or {}
        if saved:
            self.engine.load(saved)
            if meta.get("calibration_key") != self._calibration_key():
                self.engine.cal = Calibrator(self.s.policy.target_r)
                self.engine.shadows = {}
                self.store.event(now_ms(), "calibration_reset",
                                 "Cambió el REFLEX o la estructura de la operación: la calibración empieza de cero.")
        self.last_review_day = meta.get("last_review_day")

    def _persist(self) -> None:
        self.store.save_state("engine", self.engine.to_dict())
        self.store.save_state("meta", {"calibration_key": self._calibration_key(),
                                       "last_review_day": self.last_review_day,
                                       "policy_version": self.policy_version, "reflex": self.reflex.name})

    def _heartbeat(self, last_step: int | None = None, error: str | None = None) -> None:
        prev = {}
        if os.path.exists(self.heartbeat_path):
            try:
                with open(self.heartbeat_path) as fh:
                    prev = json.load(fh)
            except (OSError, ValueError):
                prev = {}
        beat = {"ts": now_ms(), "last_step": last_step or prev.get("last_step"), "error": error}
        tmp = self.heartbeat_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(beat, fh)
        os.replace(tmp, self.heartbeat_path)

    def _refresh_liquidity(self, day: str) -> None:
        """Una vez al día: los tickers con poco volumen se dejan de operar ese día."""
        if day == self.liquidity_day:
            return
        self.illiquid = {}
        for sym in self.s.tradable:
            try:
                volume = self.market.quote_volume_24h(sym)
                if volume < self.s.universe.min_quote_volume_24h:
                    self.illiquid[sym] = f"volumen 24 h de {volume / 1e6:.1f} M$ < mínimo"
            except Exception as exc:
                log.warning("No se pudo comprobar la liquidez de %s: %s", sym, exc)
        self.liquidity_day = day

    # --- un paso -----------------------------------------------------------------------
    def step_once(self) -> StepReport:
        server = self.market.server_time_ms()
        skew = now_ms() - server
        day = datetime.fromtimestamp(server / 1000, timezone.utc).strftime("%Y-%m-%d")
        self._refresh_liquidity(day)
        s, tf = self.s, self.s.tf_ms
        needed = list(dict.fromkeys(s.tradable + [s.universe.context_symbol] + list(self.engine.broker.positions)))
        data, books, entry, errors = {}, {}, {}, {}
        for sym in needed:
            if sym in self.illiquid and sym not in self.engine.broker.positions:
                continue
            try:
                data[sym] = closed_only(self.market.candles(sym, s.exchange.timeframe, limit=LOOKBACK + 2), tf, server)
            except Exception as exc:  # error del exchange: ese ticker no opera en esta vela
                errors[sym] = f"{type(exc).__name__}: {exc}"[:160]
                continue
            try:
                book = self.market.order_book(sym, 20)
                if book.bids and book.asks:
                    books[sym] = book
                    entry[sym] = (book.bids[0][0] + book.asks[0][0]) / 2
            except Exception as exc:
                errors[sym] = f"libro: {type(exc).__name__}"

        report = self.engine.on_candle(server, data, entry, books, clock_skew_ms=skew)
        for sym, why in {**errors, **self.illiquid}.items():
            report.issues.setdefault(sym, []).append(why)
        self.engine.reconcile(server)
        self._persist()
        self._heartbeat(last_step=server)

        for f in report.fills:
            if f.status == "rejected":
                notify(f"[simulación] Orden RECHAZADA {f.side} {f.symbol}: {f.reason}")
            elif f.side == "BUY":
                notify(f"[simulación] COMPRA {f.symbol}: {f.qty:.6g} a {f.price:.6g} ({f.qty * f.price:.2f} $). {f.reason}")
            else:
                notify(f"[simulación] VENTA {f.symbol} a {f.price:.6g}: {f.pnl:+.2f} $ ({f.reason}). "
                       f"Valor de la cartera: {report.equity:.2f} $")
        for text in report.escalations:
            notify("[simulación] ATENCIÓN: " + text)
        return report

    def review(self) -> dict:
        result = nightly_review(self.store, self.s, self.engine.cal, self.policies, now_ms(), self.policy_version)
        self.last_review_day = result["day"]
        self._persist()
        return result

    # --- bucle ------------------------------------------------------------------------
    def run_forever(self, max_steps: int | None = None) -> None:
        tf, steps, last_error = self.s.tf_ms, 0, None
        log.warning("Agente en simulación · REFLEX %s · política v%d · velas de %s · tickers %s",
                    self.reflex.name, self.policy_version, self.s.exchange.timeframe, ", ".join(self.s.tradable))
        while max_steps is None or steps < max_steps:
            try:
                report = self.step_once()
                steps += 1
                last_error = None
                log.warning("Vela procesada · cartera %.2f $ · órdenes %d · velas perdidas %d · incidencias %s",
                            report.equity, len(report.fills), len(report.misses), report.issues or "ninguna")
                day = datetime.fromtimestamp(report.ts / 1000, timezone.utc).strftime("%Y-%m-%d")
                if self.last_review_day is None:
                    self.last_review_day = day
                    self._persist()
                elif day != self.last_review_day:
                    result = self.review()
                    notify("[simulación] Revisión nocturna: " + " ".join(result["notes"]))
                wake = (report.ts // tf + 1) * tf + 15_000   # 15 s tras el cierre de la próxima vela
            except Exception as exc:  # el agente no debe morir por un fallo puntual
                text = f"{type(exc).__name__}: {exc}"[:300]
                log.error("Fallo en el paso: %s", text)
                self._heartbeat(error=text)
                if text != last_error:
                    notify("[simulación] Fallo en el agente, se reintenta en 1 minuto: " + text)
                    last_error = text
                wake = now_ms() + 60_000
            if max_steps is not None and steps >= max_steps:
                break
            while now_ms() < wake:
                self._heartbeat(error=last_error)
                time.sleep(min(30.0, max(0.5, (wake - now_ms()) / 1000)))

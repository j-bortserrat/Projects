"""Motor del agente. Procesa cada vela cerrada y es EXACTAMENTE el mismo código en
simulación en vivo y en backtest.

Orden de cada paso (importa):
1. Validar los datos de cada ticker. Sin datos fiables no hay órdenes.
2. Salidas de protección (stop, objetivo, tiempo) en código, antes de consultar a nadie.
3. Valorar la cartera y comprobar el interruptor de emergencia.
4. Resolver las señales de seguimiento y actualizar la calibración.
5. Solo entonces, pedir opinión al REFLEX y pasar su decisión por las puertas y el riesgo.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .broker import Fill, PaperBroker
from .calibration import Calibrator
from .config import Settings
from .market import Candle, OrderBook
from .reflex import timed_decide
from .risk import RiskManager
from .schema import Setup, worse_risk_state
from .sizing import plan_position
from .state import AccountView, build_snapshot, to_array, trend_features, validate_feed


MAX_CLOCK_SKEW_MS = 30_000


@dataclass
class Shadow:
    """Señal de seguimiento: una compra hipotética que solo sirve para medir aciertos."""
    symbol: str
    opened_ts: int
    entry: float
    stop: float
    target: float
    confidence: float
    p_cal: float
    checked_ts: int


@dataclass
class StepReport:
    ts: int
    equity: float = 0.0
    fills: list[Fill] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    misses: list[dict] = field(default_factory=list)
    issues: dict[str, list[str]] = field(default_factory=dict)
    escalations: list[str] = field(default_factory=list)


class MemoryJournal:
    """Diario en memoria (tests y backtests). El de producción es `store.Store`."""

    def __init__(self, keep_decisions: bool = True):
        self.fills: list[Fill] = []
        self.decisions: list[dict] = []
        self.shadows: list[dict] = []
        self.events: list[tuple[int, str, str]] = []
        self.equity_curve: list[tuple[int, float]] = []
        self.keep_decisions = keep_decisions

    def fill(self, f: Fill) -> None:
        self.fills.append(f)

    def decision(self, rec: dict) -> None:
        if self.keep_decisions:
            self.decisions.append(rec)

    def shadow(self, rec: dict) -> None:
        self.shadows.append(rec)

    def event(self, ts: int, kind: str, text: str) -> None:
        self.events.append((ts, kind, text))

    def equity(self, ts: int, value: float) -> None:
        self.equity_curve.append((ts, value))

    def all_fills(self) -> list[Fill]:
        return self.fills


def barrier_exit(c: Candle, stop: float, target: float) -> tuple[float, str] | None:
    """¿Toca la vela el stop o el objetivo? Si toca ambos, se asume el peor caso: el stop."""
    if c.open <= stop:
        return c.open, "stop (apertura con hueco)"
    if c.open >= target:
        return c.open, "objetivo (apertura con hueco)"
    if c.low <= stop:
        return stop, "stop"
    if c.high >= target:
        return target, "objetivo"
    return None


def _newer(candles: list[Candle], after_ts: int) -> list[Candle]:
    """Velas con apertura posterior a `after_ts` (normalmente, solo la última)."""
    i = len(candles)
    while i > 0 and candles[i - 1].ts > after_ts:
        i -= 1
    return candles[i:]


class Engine:
    def __init__(self, settings: Settings, reflex, journal, calibrator: Calibrator | None = None,
                 setups: list[Setup] | None = None):
        self.s = settings
        self.reflex = reflex
        self.journal = journal
        p = settings.paper
        self.broker = PaperBroker(p.balance, p.fee_bps, p.slippage_bps, p.max_participation)
        self.risk = RiskManager(settings.risk, settings.exchange.market, settings.universe.off_limits)
        self.cal = calibrator or Calibrator(settings.policy.target_r)
        self.setups = {x.symbol: x for x in (setups or [])}
        self.prices: dict[str, float] = {}
        self.peak_equity = p.balance
        self.day: str | None = None
        self.day_start_equity = p.balance
        self.kill_switch: str | None = None      # motivo; solo se rearma a mano
        self.halt_entries: str | None = None     # p. ej. fallo de conciliación
        self.last_ts: dict[str, int] = {}        # última vela ya decidida por ticker
        self.checked_ts: dict[str, int] = {}     # última vela ya revisada para salidas
        self.pending_exit: dict[str, str] = {}   # salidas que fallaron y hay que reintentar
        self.shadows: dict[str, Shadow] = {}
        self.escalated: set[str] = set()

    # --- utilidades -----------------------------------------------------------------
    def account(self) -> AccountView:
        return AccountView(self.broker.equity(self.prices), self.broker.cash, self.peak_equity,
                           self.day_start_equity, self.broker.position_values(self.prices))

    def _escalate(self, report: StepReport, key: str, text: str) -> None:
        """Avisa una sola vez por motivo, para no repetir la misma alerta cada vela."""
        if key not in self.escalated:
            self.escalated.add(key)
            report.escalations.append(text)
            self.journal.event(report.ts, "escalation", text)

    def _record_fill(self, report: StepReport, fill: Fill) -> Fill:
        report.fills.append(fill)
        self.journal.fill(fill)
        return fill

    def _sell(self, report: StepReport, symbol: str, price: float, reason: str) -> bool:
        fill = self._record_fill(report, self.broker.sell(symbol, price, report.ts, reason))
        if fill.status == "rejected":
            self.pending_exit[symbol] = reason
            self._escalate(report, f"exit-failed:{symbol}:{report.ts}",
                           f"No se pudo cerrar {symbol} ({reason}). Se reintenta en la próxima vela.")
            return False
        self.pending_exit.pop(symbol, None)
        self.checked_ts.pop(symbol, None)
        return True

    def reconcile(self, ts: int) -> list[str]:
        issues = self.broker.reconcile(self.s.paper.balance, self.journal.all_fills())
        if issues and not self.halt_entries:
            self.halt_entries = "conciliación fallida: " + "; ".join(issues)
            self.journal.event(ts, "escalation", self.halt_entries)
        return issues

    # --- paso principal ---------------------------------------------------------------
    def on_candle(self, now_ms: int, data: dict[str, list[Candle]], entry_prices: dict[str, float] | None = None,
                  books: dict[str, OrderBook] | None = None, clock_skew_ms: int | None = None) -> StepReport:
        """`now_ms` es la hora del exchange. `clock_skew_ms` es la diferencia con el reloj local:
        si es grande no se abre riesgo nuevo, pero las salidas se siguen vigilando."""
        s, tf, pol = self.s, self.s.tf_ms, self.s.policy
        report = StepReport(ts=now_ms)
        entry_prices, books = entry_prices or {}, books or {}
        skewed = clock_skew_ms is not None and abs(clock_skew_ms) > MAX_CLOCK_SKEW_MS
        if skewed:
            report.issues["reloj"] = [f"reloj_desfasado ({clock_skew_ms / 1000:+.0f} s)"]
            self._escalate(report, f"clock:{now_ms // 86_400_000}",
                           f"El reloj de este equipo va {clock_skew_ms / 1000:+.0f} s respecto al exchange. "
                           "No se abren posiciones hasta que se sincronice.")

        # 1) Datos
        needed = list(dict.fromkeys(s.tradable + [s.universe.context_symbol] + list(self.broker.positions)))
        valid: dict[str, list[Candle]] = {}
        arrays: dict = {}
        for sym in needed:
            candles = data.get(sym) or []
            arr = to_array(candles) if candles else None
            issues = validate_feed(candles, tf, now_ms, arr=arr)
            if issues:
                report.issues[sym] = issues
                if sym in self.broker.positions:
                    self._escalate(report, f"feed:{sym}:{now_ms // 86_400_000}",
                                   f"Sin datos fiables de {sym} con posición abierta ({', '.join(issues)}): su stop no se puede vigilar.")
            else:
                valid[sym], arrays[sym] = candles, arr
                self.prices[sym] = candles[-1].close

        # 2) Salidas de protección: en código y antes de cualquier llamada al modelo
        for sym in list(self.broker.positions):
            if sym not in valid:
                continue
            pos = self.broker.positions[sym]
            if sym in self.pending_exit:
                self._sell(report, sym, self.prices[sym], self.pending_exit[sym] + " (reintento)")
                continue
            for c in _newer(valid[sym], max(pos.opened_ts - 1, self.checked_ts.get(sym, -1))):
                self.checked_ts[sym] = c.ts
                hit = barrier_exit(c, pos.stop, pos.target)
                if hit is None and (c.ts + tf - pos.opened_ts) // tf >= pol.max_hold_candles:
                    hit = (c.close, f"tiempo máximo ({pol.max_hold_candles} velas)")
                if hit:
                    self._sell(report, sym, hit[0], hit[1])
                    break

        # 3) Valoración, cambio de día e interruptor de emergencia
        equity = self.broker.equity(self.prices)
        day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.day_start_equity = day, equity
        self.peak_equity = max(self.peak_equity, equity)
        if not self.kill_switch and self.risk.kill_switch_due(self.account()):
            self.kill_switch = (f"caída del {self.account().drawdown * 100:.1f} % desde el máximo "
                                f"(límite {s.risk.kill_switch_drawdown_pct} %) el {day}")
            self.journal.event(now_ms, "kill_switch", self.kill_switch)
            report.escalations.append("INTERRUPTOR DE EMERGENCIA: " + self.kill_switch + ". Se cierra todo y no se opera hasta que lo rearmes.")
            for sym in list(self.broker.positions):
                if sym in valid:
                    self._sell(report, sym, self.prices[sym], "interruptor de emergencia")

        # 4) Señales de seguimiento -> calibración
        cost = 2 * (s.paper.fee_bps + s.paper.slippage_bps) / 1e4
        for sym, sh in list(self.shadows.items()):
            if sym not in valid:
                continue
            for c in _newer(valid[sym], max(sh.opened_ts - 1, sh.checked_ts)):
                sh.checked_ts = c.ts
                hit = barrier_exit(c, sh.stop, sh.target)
                if hit is None and (c.ts + tf - sh.opened_ts) // tf >= pol.max_hold_candles:
                    hit = (c.close, "tiempo")
                if hit:
                    risk_per_unit = sh.entry - sh.stop
                    cost_r = cost * sh.entry / risk_per_unit
                    net_r = (hit[0] - sh.entry) / risk_per_unit - cost_r
                    self.cal.add(sh.confidence, sh.p_cal, net_r > 0, cost_r)
                    self.journal.shadow({"symbol": sym, "opened_ts": sh.opened_ts, "closed_ts": c.ts + tf,
                                         "confidence": sh.confidence, "p_cal": sh.p_cal, "net_r": net_r,
                                         "won": net_r > 0, "exit": hit[1]})
                    del self.shadows[sym]
                    break

        # 5) Decisiones
        ctx_sym = s.universe.context_symbol
        ctx_feats = trend_features(valid[ctx_sym], arrays[ctx_sym])[0] if ctx_sym in valid else None
        for sym in s.tradable:
            if sym not in valid or valid[sym][-1].ts <= self.last_ts.get(sym, -1):
                continue
            candles = valid[sym]
            self.last_ts[sym] = candles[-1].ts
            account = self.account()
            snap = build_snapshot(sym, candles, tf, account, books.get(sym), arr=arrays[sym], context_features=ctx_feats)
            setup = self.setups.get(sym) or Setup(symbol=sym)
            rec: dict = {"ts": now_ms, "symbol": sym, "reflex": self.reflex.name, "price": snap.price,
                         "latency_ms": 0.0, "error": None, "decision": None, "action": "", "reasons": []}

            if setup.invalidation_price and snap.price < setup.invalidation_price:
                rec["action"], rec["reasons"] = "skip", [f"tesis invalidada: precio < {setup.invalidation_price}"]
                self._escalate(report, f"invalid:{sym}", f"{sym}: la tesis ha quedado invalidada (precio {snap.price:g}).")
                self._finish(report, rec)
                continue

            res = timed_decide(self.reflex, snap, setup, s.reflex.timeout_s)
            rec["latency_ms"], rec["error"] = round(res.latency_ms, 2), res.error
            if res.decision is None:
                rec["action"], rec["reasons"] = "miss", [res.error or "sin decisión"]
                report.misses.append(rec)
                self._finish(report, rec)
                continue
            d = res.decision
            rec["decision"] = d.model_dump()
            ref = entry_prices.get(sym, snap.price)
            n_open = len(self.broker.positions)
            state = worse_risk_state(self.risk.state(account, n_open), d.risk_state)

            if d.regime == "crisis":
                self._escalate(report, f"crisis:{sym}:{day}", f"{sym}: el REFLEX ve régimen de crisis. No se abre riesgo nuevo.")

            if sym in self.broker.positions:
                contrary = d.direction == "short" and d.direction_confidence > pol.min_direction_confidence
                if d.regime == "crisis" or contrary:
                    why = "salida por régimen de crisis" if d.regime == "crisis" else "salida por señal contraria"
                    rec["action"] = "exit" if self._sell(report, sym, ref, why) else "exit_failed"
                    rec["reasons"] = [why]
                else:
                    rec["action"] = "hold"
                    if d.direction_confidence < s.risk.pause_below_confidence:
                        self._escalate(report, f"lowconf:{sym}:{self.broker.positions[sym].opened_ts}",
                                       f"{sym}: confianza {d.direction_confidence:.2f} por debajo de "
                                       f"{s.risk.pause_below_confidence} con posición abierta. Revísala.")
            else:
                reasons = []
                if d.direction != "long":
                    reasons.append("venta en corto no disponible en spot" if d.direction == "short" else "sin dirección")
                if d.setup_quality < pol.min_setup_quality:
                    reasons.append(f"calidad {d.setup_quality} < {pol.min_setup_quality}")
                if not d.direction_confidence > pol.min_direction_confidence:
                    reasons.append(f"confianza {d.direction_confidence:.2f} <= {pol.min_direction_confidence}")
                if state != "safe":
                    reasons.append(f"estado de riesgo: {state}")
                if d.toxic_flow is True:
                    reasons.append("flujo de órdenes adverso")
                if any(f.symbol == sym and f.side == "SELL" for f in report.fills):
                    reasons.append("recién cerrada: sin reentrada en la misma vela")
                if self.halt_entries:
                    reasons.append(self.halt_entries)
                if skewed:
                    reasons.append("reloj desfasado")
                if reasons:
                    rec["action"], rec["reasons"] = "no_trade", reasons
                else:
                    p_win = self.cal.p_conservative(d.direction_confidence)
                    plan = plan_position(account.equity, ref, snap.atr, p_win, pol, s.risk, s.paper)
                    if plan.qty <= 0:
                        rec["action"], rec["reasons"] = "no_trade", [plan.reason]
                    else:
                        v = self.risk.check_entry(sym, plan.qty, ref, plan.stop, account, n_open, d, bool(self.kill_switch))
                        if v.escalate:
                            self._escalate(report, f"risk:{sym}:{day}", f"{sym}: " + "; ".join(v.reasons))
                        if not v.allowed:
                            rec["action"], rec["reasons"] = "no_trade", v.reasons
                        else:
                            why = (f"confianza {d.direction_confidence:.2f}, calidad {d.setup_quality}, "
                                   f"p calibrada {p_win:.2f}, riesgo {plan.risk_fraction * 100:.2f} %")
                            fill = self._record_fill(report, self.broker.buy(
                                sym, v.qty, ref, now_ms, why, plan.stop, plan.target, d.direction_confidence,
                                p_win, liquidity_quote=candles[-1].quote_volume))
                            rec["action"] = "buy" if fill.status != "rejected" else "buy_rejected"
                            rec["reasons"] = v.reasons + [fill.reason]
                            if fill.status != "rejected":
                                self.checked_ts[sym] = candles[-1].ts

            # Señal de seguimiento: toda opinión de compra se mide, se opere o no.
            if d.direction == "long" and sym not in self.shadows and d.direction_confidence >= 0.5 and snap.atr > 0:
                stop = ref - pol.stop_atr * snap.atr
                if stop > 0:
                    self.shadows[sym] = Shadow(sym, now_ms, ref, stop, ref + pol.target_r * (ref - stop),
                                               d.direction_confidence, self.cal.p_win(d.direction_confidence),
                                               candles[-1].ts)
            self._finish(report, rec)

        report.equity = self.broker.equity(self.prices)
        self.journal.equity(now_ms, report.equity)
        return report

    def _finish(self, report: StepReport, rec: dict) -> None:
        report.decisions.append(rec)
        self.journal.decision(rec)

    # --- persistencia -----------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "broker": self.broker.to_dict(), "calibrator": self.cal.to_dict(), "prices": self.prices,
            "peak_equity": self.peak_equity, "day": self.day, "day_start_equity": self.day_start_equity,
            "kill_switch": self.kill_switch, "halt_entries": self.halt_entries, "last_ts": self.last_ts,
            "checked_ts": self.checked_ts, "pending_exit": self.pending_exit,
            "shadows": {k: asdict(v) for k, v in self.shadows.items()}, "escalated": sorted(self.escalated)[-500:],
        }

    def load(self, d: dict) -> None:
        self.broker.load(d["broker"])
        self.cal = Calibrator.from_dict(d["calibrator"])
        for k in ("prices", "peak_equity", "day", "day_start_equity", "kill_switch", "halt_entries",
                  "last_ts", "checked_ts", "pending_exit"):
            setattr(self, k, d[k])
        self.shadows = {k: Shadow(**v) for k, v in d["shadows"].items()}
        self.escalated = set(d["escalated"])

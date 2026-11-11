from agent.calibration import Calibrator
from agent.config import Settings
from agent.engine import Engine, MemoryJournal
from agent.market import Candle
from agent.schema import ReflexDecision

TF = 3_600_000
BTC, ALT = "BTC/USDT", "AAA/USDT"


def candles(prices, start=0, tf=TF, vol=1000.0, wick=0.002):
    """Velas contiguas a partir de una lista de cierres."""
    out, prev = [], prices[0]
    for i, p in enumerate(prices):
        out.append(Candle(start + i * tf, prev, max(prev, p) * (1 + wick), min(prev, p) * (1 - wick), p, vol))
        prev = p
    return out


def bar(prev: Candle, close, high=None, low=None, open_=None, vol=1000.0):
    """La vela siguiente a `prev`, con los extremos que se indiquen."""
    o = prev.close if open_ is None else open_
    return Candle(prev.ts + TF, o, max(o, close) * 1.002 if high is None else high,
                  min(o, close) * 0.998 if low is None else low, close, vol)


def decision(direction="long", confidence=0.9, quality=3, regime="trending", risk_state="safe", toxic=None):
    return ReflexDecision(regime=regime, direction=direction, direction_confidence=confidence,
                          toxic_flow=toxic, setup_quality=quality, risk_state=risk_state)


class Scripted:
    """REFLEX de prueba: devuelve lo que diga la función."""
    name, local = "scripted", True

    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def decide(self, snap, setup):
        self.calls += 1
        out = self.fn(snap)
        if isinstance(out, Exception):
            raise out
        return out


def settings(**over):
    base = {"universe": {"symbols": [BTC, ALT], "off_limits": [], "context_symbol": BTC}}
    base.update(over)
    return Settings.model_validate(base)


def proven_calibrator(target_r=2.0, wins=70, n=100, confidence=0.85):
    """Calibración con evidencia suficiente de ventaja en el tramo 0,8-0,9."""
    cal = Calibrator(target_r)
    for i in range(n):
        cal.add(confidence, 0.5, i < wins)
    return cal


class World:
    """Dos tickers con precio plano en 100 y un motor listo para recibir velas."""

    def __init__(self, reflex, calibrated=True, **over):
        self.s = settings(**over)
        self.journal = MemoryJournal()
        self.engine = Engine(self.s, reflex, self.journal,
                             calibrator=proven_calibrator(self.s.policy.target_r) if calibrated else None)
        self.data = {BTC: candles([100.0] * 300), ALT: candles([100.0] * 300)}

    @property
    def now(self):
        return self.data[BTC][-1].ts + TF

    def step(self, **kw):
        return self.engine.on_candle(self.now, {k: list(v) for k, v in self.data.items()}, **kw)

    def push(self, alt=None, btc=None):
        """Añade una vela a cada ticker (plana si no se indica) y procesa el paso."""
        for sym, c in ((BTC, btc), (ALT, alt)):
            self.data[sym].append(c or bar(self.data[sym][-1], self.data[sym][-1].close))
        return self.step()

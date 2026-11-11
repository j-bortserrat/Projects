import json
import os
import time
import types

import pytest

from agent import alerts, cli, runtime
from agent.backtest import run_backtest
from agent.calibration import Calibrator
from agent.config import PolicyCfg, ReflexCfg, Settings
from agent.market import Candle, MemoryMarket
from agent.policy import PolicyStore
from agent.reflex import HaikuReflex, ReflexError, RuleReflex, make_reflex, timed_decide
from agent.review import analyse, nightly_review
from agent.runtime import LiveTradingNotApproved, Runtime
from agent.schema import ReflexDecision, Setup
from agent.state import AccountView, build_snapshot
from agent.store import Store
from tests.helpers import ALT, BTC, TF, Scripted, bar, candles, decision, proven_calibrator, settings

FLAT = AccountView(1000.0, 1000.0, 1000.0, 1000.0)


def snap_of(prices):
    series = candles(prices)
    return build_snapshot(ALT, series, TF, FLAT, context=series)


# --- REFLEX ------------------------------------------------------------------------
def test_rule_reflex_reads_trends_and_crashes():
    rule = RuleReflex()
    up = rule.decide(snap_of([100 * 1.002 ** i for i in range(400)]), Setup(symbol=ALT))
    # Subida sin un solo retroceso: tendencia clara, pero sobrecomprada (RSI 100), así que resta confianza.
    assert up.regime == "trending" and up.direction == "long" and 0.7 <= up.direction_confidence < 0.8
    down = rule.decide(snap_of([100 * 0.9995 ** i for i in range(400)]), Setup(symbol=ALT))
    assert down.direction == "short"
    crash = rule.decide(snap_of([100.0] * 390 + [100 - 3 * i for i in range(1, 11)]), Setup(symbol=ALT))
    assert crash.regime == "crisis"
    flat = rule.decide(snap_of([100.0] * 400), Setup(symbol=ALT))
    assert flat.direction == "neutral" and flat.direction_confidence == 0.5 and flat.toxic_flow is None
    for d in (up, down, crash, flat):
        assert d.problems() == []


def test_slow_failing_or_invalid_reflex_is_a_miss():
    snap = snap_of([100.0] * 400)

    class Slow:
        name, local = "slow", False

        def decide(self, s, setup):
            time.sleep(0.4)
            return decision()

    res = timed_decide(Slow(), snap, Setup(symbol=ALT), timeout_s=0.05)
    assert res.decision is None and "sin respuesta" in res.error and res.latency_ms < 300
    res = timed_decide(Scripted(lambda s: RuntimeError("boom")), snap, Setup(symbol=ALT), 1.0)
    assert res.decision is None and "boom" in res.error
    res = timed_decide(Scripted(lambda s: decision(confidence=1.7)), snap, Setup(symbol=ALT), 1.0)
    assert res.decision is None and "fuera de rango" in res.error
    res = timed_decide(Scripted(lambda s: decision(quality=9)), snap, Setup(symbol=ALT), 1.0)
    assert res.decision is None
    ok = timed_decide(Scripted(lambda s: decision()), snap, Setup(symbol=ALT), 1.0)
    assert ok.decision.direction == "long" and ok.error is None


class FakeMessages:
    def __init__(self, stop_reason="end_turn", parsed=None):
        self.calls, self.stop_reason, self.parsed = [], stop_reason, parsed

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(stop_reason=self.stop_reason, parsed_output=self.parsed)


def test_haiku_reflex_request_shape_and_failure_modes():
    """Sin clave real: se comprueba la petición que se enviaría y el manejo de respuestas."""
    snap = snap_of([100.0] * 400)
    messages = FakeMessages(parsed=decision("long", 0.7, 2))
    reflex = HaikuReflex(ReflexCfg(), client=types.SimpleNamespace(messages=messages))
    out = reflex.decide(snap, Setup(symbol=ALT, thesis="tesis", invalidation_price=80.0))
    call = messages.calls[0]
    assert out.direction == "long" and reflex.name == "haiku:claude-haiku-5-5"
    assert call["model"] == "claude-haiku-5-5" and call["output_config"] == {"effort": "low"}
    assert call["output_format"] is ReflexDecision and "thinking" not in call and "temperature" not in call
    user = call["messages"][0]["content"]
    assert snap.to_prompt() in user and '"invalidation_price":80.0' in user
    assert "sk-" not in json.dumps(call, default=str)

    for kwargs in ({"stop_reason": "refusal"}, {"stop_reason": "max_tokens"}, {"parsed": None}):
        bad = HaikuReflex(ReflexCfg(), client=types.SimpleNamespace(messages=FakeMessages(**kwargs)))
        with pytest.raises(ReflexError):
            bad.decide(snap, Setup(symbol=ALT))


def test_reflex_selection_depends_on_the_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert make_reflex(ReflexCfg(provider="auto")).name == "rules-v1"
    assert make_reflex(ReflexCfg(provider="rules")).name == "rules-v1"
    with pytest.raises(ReflexError):
        make_reflex(ReflexCfg(provider="haiku"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    assert make_reflex(ReflexCfg(provider="auto")).name == "haiku:claude-haiku-5-5"
    assert make_reflex(ReflexCfg(provider="rules")).name == "rules-v1"


# --- backtest ------------------------------------------------------------------------
def trending_world(n=700):
    import math
    alt = candles([100 * (1 + 0.25 * math.sin(i / 40)) * 1.0005 ** i for i in range(n)])
    btc = candles([100 * 1.0004 ** i for i in range(n)])
    return {BTC: btc, ALT: alt}


def stable(result):
    """El resultado sin las latencias, que dependen del reloj de la máquina."""
    return {k: v for k, v in result.items() if not k.startswith("latency")}


def test_backtest_is_deterministic_and_reconciles():
    data = trending_world()
    a = run_backtest(settings(), data, RuleReflex())
    b = run_backtest(settings(), data, RuleReflex())
    assert stable(a) == stable(b) and a["reconciliation_issues"] == [] and a["candles"] == 700 - 260 + 1
    assert a["decisions"] == 2 * a["candles"] and a["misses"] == 0
    assert a["model_data_valid"] is True and a["bot"]["final"] == pytest.approx(a["curve"][-1][1], abs=5)
    json.dumps(a)


def test_backtest_benchmark_pays_costs_and_orders_fill_at_next_open():
    flat = {BTC: candles([100.0] * 400), ALT: candles([100.0] * 400)}
    r = run_backtest(settings(), flat, RuleReflex())
    assert r["trades"] == 0 and r["bot"]["final"] == 1000.0
    assert r["btc_hold"]["final"] == pytest.approx(1000 / (1.0005 * 1.001))   # mantener BTC también paga entrar
    assert r["beats_btc"] is True                                             # no hacer nada gana a pagar costes

    data = trending_world()
    from agent.engine import Engine, MemoryJournal
    eng = Engine(settings(), Scripted(lambda s: decision("long", 0.85, 3) if s.symbol == ALT else decision("neutral", 0.5, 0)),
                 MemoryJournal(), calibrator=proven_calibrator())
    run_backtest(settings(), data, eng.reflex, engine=eng)
    first = eng.journal.fills[0]
    nxt = next(c for c in data[ALT] if c.ts == first.ts)       # la vela que ABRE en el instante de la decisión
    assert first.side == "BUY" and first.price == pytest.approx(nxt.open * 1.0005)


def test_backtest_does_not_look_ahead():
    """Cambiar los precios posteriores al final del periodo no altera el resultado."""
    data = trending_world(700)
    end = data[BTC][500].ts
    poisoned = {k: v[:501] + [bar(v[500], 5.0)] + v[502:] for k, v in data.items()}
    a = run_backtest(settings(), data, RuleReflex(), end_ms=end)
    b = run_backtest(settings(), poisoned, RuleReflex(), end_ms=end)
    assert stable(a) == stable(b) and a["candles"] == 500 - 260 + 1


# --- persistencia y bucle --------------------------------------------------------------
def make_runtime(tmp_path, monkeypatch, reflex, data=None, **over):
    data = data or {BTC: candles([100.0] * 300), ALT: candles([100.0] * 300)}
    market = MemoryMarket(data, TF, now_ms=data[BTC][-1].ts + TF)
    monkeypatch.setattr(runtime, "now_ms", lambda: market.now_ms)
    monkeypatch.setattr(runtime, "notify", lambda text: sent.append(text))
    s = settings(data_dir=str(tmp_path), **over)
    return Runtime(s, market=market, store=Store(os.path.join(str(tmp_path), "agent.db")), reflex=reflex, setups=[]), market


sent: list[str] = []


def test_runtime_persists_and_resumes_after_restart(tmp_path, monkeypatch):
    sent.clear()
    reflex = Scripted(lambda s: decision("long", 0.85, 3) if s.symbol == ALT else decision("neutral", 0.5, 0))
    rt, market = make_runtime(tmp_path, monkeypatch, reflex)
    rt.engine.cal = proven_calibrator()
    r = rt.step_once()
    assert [f.side for f in r.fills] == ["BUY"] and any("COMPRA" in t for t in sent)
    stop = rt.engine.broker.positions[ALT].stop
    assert os.path.exists(rt.heartbeat_path)

    # "Reinicio": un proceso nuevo sobre la misma base de datos.
    rt2, market2 = make_runtime(tmp_path, monkeypatch, reflex, data=market.data)
    assert rt2.engine.broker.positions[ALT].stop == stop and rt2.engine.cal.count == 100
    assert rt2.engine.reconcile(0) == []
    market2.data[ALT].append(bar(market2.data[ALT][-1], 98.0, low=97.5))
    market2.data[BTC].append(bar(market2.data[BTC][-1], 100.0))
    market2.now_ms += TF
    r = rt2.step_once()
    assert r.fills[0].reason == "stop" and any("VENTA" in t for t in sent)
    assert [f.side for f in rt2.store.all_fills()] == ["BUY", "SELL"]


def test_runtime_handles_exchange_errors_and_refuses_live(tmp_path, monkeypatch):
    rt, market = make_runtime(tmp_path, monkeypatch, RuleReflex())
    market.fail.add(ALT)
    r = rt.step_once()
    assert any("error simulado" in x for x in r.issues[ALT]) and [d["symbol"] for d in r.decisions] == [BTC]
    with pytest.raises(LiveTradingNotApproved):
        Runtime(settings(mode="live", data_dir=str(tmp_path)), market=market)


def test_calibration_resets_when_the_reflex_changes(tmp_path, monkeypatch):
    rt, market = make_runtime(tmp_path, monkeypatch, RuleReflex())
    rt.engine.cal = proven_calibrator()
    rt.step_once()
    same, _ = make_runtime(tmp_path, monkeypatch, RuleReflex(), data=market.data)
    assert same.engine.cal.count == 100
    other, _ = make_runtime(tmp_path, monkeypatch, Scripted(lambda s: decision("neutral", 0.5, 0)), data=market.data)
    assert other.engine.cal.count == 0                          # aciertos de otro REFLEX no valen para este


def test_run_forever_survives_a_failing_step(tmp_path, monkeypatch):
    sent.clear()
    rt, market = make_runtime(tmp_path, monkeypatch, RuleReflex())
    calls = {"n": 0}
    real = rt.step_once

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("red caída")
        return real()

    def fake_sleep(seconds):
        market.now_ms += 3_600_000

    monkeypatch.setattr(rt, "step_once", flaky)
    monkeypatch.setattr(runtime.time, "sleep", fake_sleep)
    rt.run_forever(max_steps=1)
    assert calls["n"] == 2 and any("red caída" in t for t in sent)


# --- política y revisión nocturna ----------------------------------------------------------
def test_policy_versions_promote_and_rollback(tmp_path):
    store = PolicyStore(str(tmp_path))
    version, policy = store.active(PolicyCfg())
    assert version == 1 and policy == PolicyCfg()
    candidate = store.propose(PolicyCfg(min_direction_confidence=0.9), "prueba")
    assert candidate == 2 and store.active(PolicyCfg())[0] == 1          # proponer NO activa
    store.promote(2)
    assert store.active(PolicyCfg())[1].min_direction_confidence == 0.9
    assert store.rollback() == 1 and store.active(PolicyCfg())[0] == 1
    with pytest.raises(ValueError):
        store.rollback()
    with pytest.raises(Exception):
        store.promote(99)
    assert [v["version"] for v in store.versions()] == [1, 2]             # nada se borra


def test_review_analysis_only_proposes_with_evidence():
    few = Calibrator(2.0)
    few.add(0.85, 0.4, True)
    assert analyse(few, 0.8)["new_threshold"] is None and "pronto" in analyse(few, 0.8)["notes"][0]

    noise = Calibrator(2.0)                                # 35 % de acierto en todos los tramos: sin ventaja
    for conf in (0.65, 0.75, 0.85, 0.95):
        for i in range(100):
            noise.add(conf, 0.35, i % 20 < 7)
    out = analyse(noise, 0.8)
    assert out["new_threshold"] is None and "seguirá sin arriesgar" in out["notes"][0]

    high = Calibrator(2.0)                                 # solo el tramo 0,9+ acierta de verdad
    for i in range(200):
        high.add(0.85, 0.35, i % 20 < 7)
        high.add(0.95, 0.6, i % 20 < 13)
    out = analyse(high, 0.8)
    assert out["new_threshold"] == 0.89 and out["skill"] is True and "subir" in out["notes"][0]
    assert analyse(high, 0.89)["new_threshold"] is None    # ya aplicada: nada que proponer


def test_nightly_review_writes_report_and_never_activates(tmp_path):
    store, policies = Store(":memory:"), PolicyStore(str(tmp_path))
    version, _ = policies.active(PolicyCfg())
    now = 10 * 86_400_000
    store.decision({"ts": now - 1000, "symbol": ALT, "reflex": "x", "price": 1.0, "latency_ms": 12.0, "error": None,
                    "action": "no_trade", "reasons": ["calidad"], "decision": decision().model_dump()})
    store.decision({"ts": now - 900, "symbol": ALT, "reflex": "x", "price": 1.0, "latency_ms": 0.0,
                    "error": "sin respuesta en 20 s", "action": "miss", "reasons": [], "decision": None})
    cal = Calibrator(2.0)
    for i in range(200):
        cal.add(0.85, 0.35, i % 20 < 7)
        cal.add(0.95, 0.6, i % 20 < 13)
    result = nightly_review(store, settings(data_dir=str(tmp_path)), cal, policies, now, version)
    assert result["candidate_version"] == 2 and policies.active(PolicyCfg())[0] == 1
    assert result["misses"] == 1 and result["miss_rate"] == 0.5 and any("perdió" in n for n in result["notes"])
    text = open(result["report_path"]).read()
    assert "Calibración" in text and "0.9-1.0" in text and "NO está activa" in text


# --- avisos y comandos ----------------------------------------------------------------------
def test_telegram_alerts(monkeypatch, caplog):
    calls = []

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(request, timeout):
        calls.append((request.full_url, json.loads(request.data)))
        return Response()

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert alerts.notify("hola", opener) is False and calls == []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SECRETO")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "7")
    assert alerts.notify("hola", opener) is True and calls[0][1] == {"chat_id": "7", "text": "hola"}

    def broken(request, timeout):
        raise OSError("fallo con SECRETO en la URL")
    assert alerts.notify("hola", broken) is False
    assert "SECRETO" not in caplog.text                      # el token nunca llega a los registros


def test_cli_requires_explicit_confirmation(tmp_path, capsys):
    s = settings(data_dir=str(tmp_path))
    args = types.SimpleNamespace(action="promote", version=2, confirm=False)
    assert cli.cmd_policy(s, args) == 2 and PolicyStore(str(tmp_path)).active(PolicyCfg())[0] == 1
    assert cli.cmd_reset_kill(s, types.SimpleNamespace(confirm=False)) == 2
    assert cli.cmd_health(s, None) == 1                      # sin latido: no está vivo
    with open(os.path.join(str(tmp_path), "heartbeat.json"), "w") as fh:
        json.dump({"ts": time.time() * 1000, "last_step": time.time() * 1000, "error": None}, fh)
    assert cli.cmd_health(s, None) == 0
    with open(os.path.join(str(tmp_path), "heartbeat.json"), "w") as fh:
        json.dump({"ts": time.time() * 1000 - 600_000, "last_step": time.time() * 1000, "error": None}, fh)
    assert cli.cmd_health(s, None) == 1


def test_reset_kill_switch_rearms_from_current_equity(tmp_path):
    from agent.engine import Engine, MemoryJournal
    s = settings(data_dir=str(tmp_path))
    eng = Engine(s, RuleReflex(), MemoryJournal())
    eng.kill_switch, eng.peak_equity, eng.broker.cash = "caída", 1000.0, 800.0
    store = Store(os.path.join(str(tmp_path), "agent.db"))
    store.save_state("engine", eng.to_dict())
    assert cli.cmd_reset_kill(s, types.SimpleNamespace(confirm=True)) == 0
    st = Store(os.path.join(str(tmp_path), "agent.db")).load_state("engine")
    assert st["kill_switch"] is None and st["peak_equity"] == 800.0

import pytest

from agent.broker import Fill
from agent.engine import Engine, MemoryJournal, barrier_exit
from agent.market import Candle
from agent.schema import Setup
from tests.helpers import ALT, BTC, TF, Scripted, World, bar, decision


def only_alt(d):
    """Opina `d` sobre ALT y neutral sobre lo demás."""
    return Scripted(lambda snap: d if snap.symbol == ALT else decision("neutral", 0.5, 0))


def actions(report):
    return {d["symbol"]: d for d in report.decisions}


# --- puertas de entrada -----------------------------------------------------------
def test_buys_only_when_every_gate_passes():
    w = World(only_alt(decision("long", 0.85, 3)))
    r = w.step()
    assert actions(r)[ALT]["action"] == "buy" and actions(r)[BTC]["action"] == "no_trade"
    pos = w.engine.broker.positions[ALT]
    assert pos.stop == pytest.approx(100 - 2 * 0.4) and pos.target == pytest.approx(100 + 4 * 0.4)
    assert pos.qty * pos.entry == pytest.approx(1000 / 3, rel=1e-3)        # un tercio de la cartera como mucho
    assert w.engine.reconcile(w.now) == []


@pytest.mark.parametrize("d, why", [
    (decision("long", 0.80, 3), "confianza"),                 # 0,80 no supera el umbral: tiene que ser MAYOR
    (decision("long", 0.85, 1), "calidad"),
    (decision("long", 0.85, 3, risk_state="near_limit"), "estado de riesgo"),
    (decision("long", 0.85, 3, risk_state="reduce"), "estado de riesgo"),
    (decision("long", 0.85, 3, toxic=True), "flujo"),
    (decision("short", 0.95, 3), "corto"),
    (decision("neutral", 0.5, 0), "sin dirección"),
    (decision("long", 0.85, 3, regime="crisis"), "crisis"),
])
def test_each_gate_blocks_the_trade(d, why):
    w = World(only_alt(d))
    rec = actions(w.step())[ALT]
    assert rec["action"] == "no_trade" and any(why in x for x in rec["reasons"]), rec["reasons"]
    assert not w.engine.broker.positions


def test_no_trade_without_calibrated_evidence():
    """Con un REFLEX muy seguro pero sin historial de aciertos, el tamaño es cero."""
    w = World(only_alt(decision("long", 0.95, 3)), calibrated=False)
    rec = actions(w.step())[ALT]
    assert rec["action"] == "no_trade" and "Kelly" in rec["reasons"][0]
    assert ALT in w.engine.shadows                           # pero la opinión se mide igualmente


def test_respects_max_positions_and_off_limits():
    w = World(Scripted(lambda snap: decision("long", 0.85, 3)),
              universe={"symbols": [BTC, ALT], "off_limits": [ALT], "context_symbol": BTC},
              risk={"max_open_positions": 1})
    r = w.step()
    assert [d["symbol"] for d in r.decisions] == [BTC]       # el ticker vetado ni se consulta
    assert set(w.engine.broker.positions) == {BTC}


# --- salidas: siempre en código -----------------------------------------------------
def test_barrier_exit_is_conservative():
    c = lambda o, h, l, cl: Candle(0, o, h, l, cl, 1)  # noqa: E731
    assert barrier_exit(c(100, 101, 99.5, 100), 99, 104) is None
    assert barrier_exit(c(100, 101, 98.5, 100), 99, 104) == (99, "stop")
    assert barrier_exit(c(100, 105, 99.5, 104), 99, 104) == (104, "objetivo")
    assert barrier_exit(c(100, 105, 98.0, 100), 99, 104) == (99, "stop")          # toca ambos: el peor caso
    assert barrier_exit(c(97, 98, 96, 97), 99, 104)[0] == 97                       # hueco: sale a la apertura
    assert barrier_exit(c(106, 107, 105, 106), 99, 104)[0] == 106


def test_stop_loss_fires_even_when_the_reflex_is_down():
    state = {"down": False}
    reflex = Scripted(lambda snap: RuntimeError("API caída") if state["down"]
                      else (decision("long", 0.85, 3) if snap.symbol == ALT else decision("neutral", 0.5, 0)))
    w = World(reflex)
    w.step()
    stop = w.engine.broker.positions[ALT].stop
    state["down"] = True
    r = w.push(alt=bar(w.data[ALT][-1], 98.0, low=97.5))
    assert [f.side for f in r.fills] == ["SELL"] and r.fills[0].reason == "stop"
    assert r.fills[0].price == pytest.approx(stop * 0.9995) and r.fills[0].pnl < 0
    assert len(r.misses) == 2 and all(d["action"] == "miss" for d in r.decisions)   # la vela se pierde, no se compra a ciegas
    assert not w.engine.broker.positions


def test_target_and_time_exits():
    w = World(only_alt(decision("long", 0.85, 3)))
    w.step()
    r = w.push(alt=bar(w.data[ALT][-1], 102.0, high=102.5))
    assert r.fills[0].reason == "objetivo" and r.fills[0].pnl > 0

    w = World(only_alt(decision("long", 0.85, 3)), policy={"max_hold_candles": 3})
    w.step()
    w.engine.reflex = only_alt(decision("neutral", 0.7, 0))
    assert w.push().fills == [] and w.push().fills == []
    r = w.push()
    assert r.fills[0].reason.startswith("tiempo máximo") and r.fills[0].pnl < 0     # precio plano: pierde los costes


def test_failed_exit_is_retried_next_candle():
    w = World(only_alt(decision("long", 0.85, 3)))
    w.step()
    w.engine.reflex = only_alt(decision("neutral", 0.7, 0))
    w.engine.broker.fail_next = 1
    r = w.push(alt=bar(w.data[ALT][-1], 98.0, low=97.5))
    assert r.fills[0].status == "rejected" and ALT in w.engine.broker.positions and r.escalations
    r = w.push(alt=bar(w.data[ALT][-1], 99.5))                # el precio rebota, pero la salida sigue pendiente
    assert r.fills[0].status == "filled" and "reintento" in r.fills[0].reason
    assert not w.engine.broker.positions and w.engine.reconcile(w.now) == []


def test_exits_on_crisis_or_strong_contrary_signal():
    for d, why in ((decision("neutral", 0.5, 0, regime="crisis"), "crisis"), (decision("short", 0.9, 3), "contraria")):
        w = World(only_alt(decision("long", 0.85, 3)))
        w.step()
        w.engine.reflex = only_alt(d)
        r = w.push()
        assert actions(r)[ALT]["action"] == "exit" and why in r.fills[0].reason
    w = World(only_alt(decision("long", 0.85, 3)))
    w.step()
    w.engine.reflex = only_alt(decision("short", 0.7, 3))     # señal contraria débil: se mantiene
    r = w.push()
    assert actions(r)[ALT]["action"] == "hold" and not r.fills


def test_low_confidence_on_open_position_escalates_once():
    w = World(only_alt(decision("long", 0.85, 3)))
    w.step()
    w.engine.reflex = only_alt(decision("long", 0.55, 1))
    assert any("confianza 0.55" in e for e in w.push().escalations)
    assert w.push().escalations == []                         # no repite el mismo aviso cada vela


# --- límites globales ---------------------------------------------------------------
def test_kill_switch_flattens_blocks_and_survives_restart():
    w = World(Scripted(lambda snap: decision("long", 0.85, 3)), risk={"max_open_positions": 3})
    w.step()
    assert len(w.engine.broker.positions) == 2
    w.engine.peak_equity = 1200.0                              # la cartera venía de 1.200: ya cae más del 15 %
    r = w.push()
    assert w.engine.kill_switch and not w.engine.broker.positions
    assert {f.reason for f in r.fills} == {"interruptor de emergencia"} and "INTERRUPTOR" in r.escalations[0]
    assert all(d["action"] == "no_trade" for d in w.push().decisions)

    saved = w.engine.to_dict()
    again = Engine(w.s, w.engine.reflex, MemoryJournal())
    again.load(saved)
    assert again.kill_switch == w.engine.kill_switch            # un reinicio no lo rearma
    w.engine = again
    assert all(d["action"] == "no_trade" for d in w.push().decisions)


def test_daily_loss_limit_pauses_new_entries():
    w = World(only_alt(decision("long", 0.85, 3)))
    w.engine.day, w.engine.day_start_equity = "1970-01-13", 1060.0   # hoy ya se pierde más del 5 %
    r = w.step()
    rec = actions(r)[ALT]
    assert rec["action"] == "no_trade" and any("pérdida diaria" in x or "riesgo" in x for x in rec["reasons"])


def test_bad_feed_means_no_decision_for_that_symbol():
    w = World(Scripted(lambda snap: decision("long", 0.85, 3)))
    del w.data[ALT][150]                                       # hueco en el histórico
    r = w.step()
    assert "huecos_o_desorden" in r.issues[ALT] and [d["symbol"] for d in r.decisions] == [BTC]
    w = World(Scripted(lambda snap: decision("long", 0.85, 3)))
    w.data[ALT] = w.data[ALT][:-3]                             # feed caducado
    r = w.step()
    assert "feed_caducado" in r.issues[ALT] and ALT not in w.engine.broker.positions


def test_clock_skew_blocks_entries_but_not_exits():
    w = World(only_alt(decision("long", 0.85, 3)))
    r = w.step(clock_skew_ms=120_000)
    assert actions(r)[ALT]["action"] == "no_trade" and "reloj" in r.issues and r.escalations
    w.step()                                                    # misma vela: ya decidida, no repite
    w2 = World(only_alt(decision("long", 0.85, 3)))
    w2.step()
    w2.data[ALT].append(bar(w2.data[ALT][-1], 98.0, low=97.5))
    w2.data[BTC].append(bar(w2.data[BTC][-1], 100.0))
    r = w2.step(clock_skew_ms=120_000)
    assert r.fills and r.fills[0].reason == "stop"


def test_invalidated_thesis_is_skipped_and_reported():
    w = World(Scripted(lambda snap: decision("long", 0.85, 3)))
    w.engine.setups = {ALT: Setup(symbol=ALT, thesis="x", invalidation_price=120.0)}
    r = w.step()
    assert actions(r)[ALT]["action"] == "skip" and any("invalidada" in e for e in r.escalations)
    assert w.engine.reflex.calls == 1                           # ni siquiera se consulta al REFLEX para ALT


# --- señales de seguimiento y calibración ----------------------------------------------
def test_shadow_signals_feed_the_calibrator():
    w = World(only_alt(decision("long", 0.65, 0)), calibrated=False)   # no pasa las puertas, pero se mide
    w.step()
    assert not w.engine.broker.positions and w.engine.shadows[ALT].confidence == 0.65
    w.push(alt=bar(w.data[ALT][-1], 102.0, high=102.5))                # habría llegado al objetivo
    assert w.engine.cal.count == 1 and w.engine.cal.wins[1] == 1
    assert w.journal.shadows[0]["won"] and w.journal.shadows[0]["net_r"] == pytest.approx(2 - 0.003 * 100 / 0.8)
    assert w.engine.shadows[ALT].entry == 102.0                        # y ya se mide la siguiente opinión
    w.push(alt=bar(w.data[ALT][-1], 95.0, low=94.0))                   # esta acaba en el stop
    assert w.engine.cal.count == 2 and w.engine.cal.wins[1] == 1 and not w.journal.shadows[1]["won"]
    assert w.engine.cal.base_rate == 0.5


def test_each_candle_is_decided_once_and_state_roundtrips():
    w = World(only_alt(decision("long", 0.85, 3)))
    w.step()
    calls = w.engine.reflex.calls
    assert w.step().decisions == [] and w.engine.reflex.calls == calls   # repetir el paso no duplica nada
    clone = Engine(w.s, w.engine.reflex, w.journal)
    clone.load(w.engine.to_dict())
    assert clone.to_dict() == w.engine.to_dict()
    assert clone.broker.positions[ALT].stop == w.engine.broker.positions[ALT].stop


def test_reconciliation_mismatch_halts_entries():
    w = World(only_alt(decision("long", 0.85, 3)))
    w.journal.fills.append(Fill(0, BTC, "BUY", 1.0, 100.0, 0.1, "filled", "orden fantasma"))
    assert w.engine.reconcile(w.now) and "conciliación" in w.engine.halt_entries
    rec = actions(w.step())[ALT]
    assert rec["action"] == "no_trade" and any("conciliación" in x for x in rec["reasons"])

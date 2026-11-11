import math

import pytest
from pydantic import ValidationError

from agent.broker import Fill, PaperBroker
from agent.calibration import Calibrator
from agent.config import PaperCfg, PolicyCfg, RiskCfg, Settings
from agent.market import Candle, OrderBook, closed_only
from agent.risk import RiskManager
from agent.sizing import kelly, net_payoff, plan_position
from agent.state import (MAX_PROMPT_TOKENS, AccountView, build_snapshot, estimate_tokens, trend_features,
                         validate_feed)
from tests.helpers import ALT, TF, bar, candles, decision

FLAT = AccountView(1000.0, 1000.0, 1000.0, 1000.0)


def wavy(n=400):
    return candles([100 * (1 + 0.1 * math.sin(i / 17) + 0.0005 * i) for i in range(n)])


# --- motor de estado -------------------------------------------------------------
def test_snapshot_fits_the_token_budget_and_is_numeric():
    book = OrderBook(0, [(99.9, 2.0), (99.8, 1.0)], [(100.1, 1.0), (100.2, 1.0)])
    account = AccountView(950.0, 500.0, 1000.0, 980.0, {ALT: 300.0, "X/USDT": 150.0})
    snap = build_snapshot(ALT, wavy(), TF, account, book, context=wavy())
    text = snap.to_prompt()
    assert estimate_tokens(text) < MAX_PROMPT_TOKENS
    assert all(v is None or isinstance(v, (int, float)) for v in snap.features.values())
    f = snap.features
    assert f["spread_bps"] == pytest.approx(20.0, abs=0.1) and f["imbalance"] == pytest.approx(0.2)
    assert f["inv"] == pytest.approx(300 / 950, abs=1e-3) and f["expo"] == pytest.approx(450 / 950, abs=1e-3)
    assert f["dd"] == pytest.approx(5.0) and f["day"] == pytest.approx(-3.06, abs=0.01) and f["npos"] == 2
    assert snap.as_of == wavy()[-1].ts + TF


def test_snapshot_never_sees_the_future():
    series = wavy(500)
    past = series[:400]
    a = build_snapshot(ALT, past, TF, FLAT, context=past)
    # El contexto trae velas posteriores con precios disparatados: no deben influir.
    poisoned = past + [bar(past[-1], 9999.0) for _ in range(1)]
    b = build_snapshot(ALT, past, TF, FLAT, context=poisoned)
    assert a.features == b.features and a.as_of == b.as_of
    # Y una vela todavía abierta nunca llega a los cálculos.
    now = past[-1].ts + TF
    assert closed_only(series, TF, now) == past


def test_feed_validation_catches_every_problem():
    good = wavy()
    now = good[-1].ts + TF
    assert validate_feed(good, TF, now) == []
    assert validate_feed([], TF, now) == ["sin_datos"]
    assert "historia_insuficiente" in validate_feed(good[-100:], TF, now)
    assert "huecos_o_desorden" in validate_feed(good[:200] + good[201:], TF, now)
    assert "huecos_o_desorden" in validate_feed(good[:-2] + [good[-1], good[-2]], TF, now + TF)
    assert "feed_caducado" in validate_feed(good, TF, now + 2 * TF)
    assert "vela_en_curso" in validate_feed(good, TF, now - 1)
    assert "reloj_desfasado" in validate_feed(good, TF, now, server_ms=now + 60_000)
    bad = good[:-1] + [Candle(good[-1].ts, 100, 90, 95, 100, 1)]          # máximo por debajo del cierre
    assert "datos_invalidos" in validate_feed(bad, TF, now)
    nan = good[:-1] + [Candle(good[-1].ts, 100, 101, 99, float("nan"), 1)]
    assert "datos_invalidos" in validate_feed(nan, TF, now)
    assert "datos_invalidos" in validate_feed(good[:-1] + [Candle(good[-1].ts, 0, 0, 0, 0, 1)], TF, now)


def test_trend_features_on_a_steady_uptrend():
    up = candles([100 * 1.002 ** i for i in range(400)])
    f, atr = trend_features(up)
    assert f["r24"] > 0 and f["r168"] > 0 and f["ema50_gap"] > 0 and f["ema200_gap"] > f["ema50_gap"]
    assert f["rsi"] == 100.0 and atr > 0 and f["dd168"] == pytest.approx(-0.2, abs=0.05)


# --- tamaño ------------------------------------------------------------------------
def test_kelly_and_costs():
    assert kelly(1 / 3, 2.0) == pytest.approx(0.0)          # sin ventaja no se apuesta
    assert kelly(0.5, 2.0) == pytest.approx(0.25)
    assert kelly(0.2, 2.0) == 0.0 and kelly(0.6, 0.0) == 0.0 and kelly(1.5, 2.0) == 0.0
    assert net_payoff(2.0, 0.01, 0.0) == pytest.approx(2.0)
    assert net_payoff(2.0, 0.01, 0.003) == pytest.approx(1.7 / 1.3)   # los costes recortan la ganancia
    assert net_payoff(2.0, 0.001, 0.003) == 0.0                       # stop tan corto que los costes lo anulan


def test_position_plan_respects_quarter_kelly_and_risk_cap():
    policy, risk, paper = PolicyCfg(), RiskCfg(), PaperCfg()
    none = plan_position(1000, 100, 1.0, 1 / 3, policy, risk, paper)
    assert none.qty == 0 and "Kelly" in none.reason
    plan = plan_position(1000, 100, 1.0, 0.9, policy, risk, paper)
    assert plan.stop == 98.0 and plan.target == 104.0
    assert plan.risk_fraction == pytest.approx(0.02)                  # topado por el 2 % por operación
    assert plan.qty * (100 - plan.stop) == pytest.approx(20.0)
    small = plan_position(1000, 100, 1.0, 0.42, policy, risk, paper)   # ventaja pequeña: manda el cuarto de Kelly
    assert 0 < small.risk_fraction < 0.02 and small.risk_fraction == pytest.approx(0.25 * small.kelly_full)
    assert plan_position(1000, 100, 60.0, 0.9, policy, risk, paper).qty == 0   # stop por debajo de cero


def test_config_hard_caps_cannot_be_exceeded():
    for bad in ({"risk": {"risk_per_trade_pct": 10}}, {"risk": {"max_leverage": 5}},
                {"policy": {"kelly_fraction": 0.5}}, {"risk": {"kill_switch_drawdown_pct": 80}},
                {"exchange": {"market": "perps"}}):
        with pytest.raises(ValidationError):
            Settings.model_validate(bad)
    s = Settings.model_validate({"universe": {"symbols": ["A/USDT", "B/USDT"], "off_limits": ["B/USDT"]}})
    assert s.tradable == ["A/USDT"]


# --- calibración -------------------------------------------------------------------
def test_calibrator_starts_at_no_edge_and_needs_evidence():
    cal = Calibrator(target_r=2.0)
    assert cal.p_win(0.95) == pytest.approx(1 / 3) and cal.p_conservative(0.95) == pytest.approx(1 / 3)
    assert cal.p_win(0.3) == pytest.approx(1 / 3) and cal.bucket(0.49) is None and cal.bucket(1.0) == 4
    for i in range(20):                                   # 20 aciertos seguidos: todavía pocas muestras
        cal.add(0.85, 1 / 3, True)
    assert cal.p_win(0.85) > 0.6 and cal.p_conservative(0.85) == pytest.approx(1 / 3)
    for i in range(80):
        cal.add(0.85, 0.5, i % 2 == 0)
    assert cal.n[3] == 100 and cal.p_conservative(0.85) < cal.p_win(0.85) < 0.6
    assert cal.p_win(0.65) == pytest.approx(1 / 3)        # otro tramo no se contamina
    assert cal.breakeven_p == pytest.approx(1 / 3)        # sin costes registrados
    cal.add(0.65, 1 / 3, True, cost_r=0.25 * 101)         # coste medio de 0,25 R en 101 señales
    assert cal.breakeven_p == pytest.approx(1.25 / 3)     # con costes hay que acertar más


def test_brier_scores_and_roundtrip():
    cal = Calibrator(2.0)
    cal.add(0.9, 0.4, True)
    cal.add(0.9, 0.4, False)
    assert cal.brier_raw == pytest.approx((0.01 + 0.81) / 2)
    assert cal.brier_calibrated == pytest.approx((0.36 + 0.16) / 2)
    assert cal.base_rate == 0.5 and cal.brier_base_rate == 0.25
    clone = Calibrator.from_dict(cal.to_dict())
    assert clone.to_dict() == cal.to_dict() and clone.table() == cal.table()


# --- riesgo ------------------------------------------------------------------------
def account(equity=1000.0, cash=1000.0, peak=1000.0, day_start=1000.0, positions=None):
    return AccountView(equity, cash, peak, day_start, positions or {})


def check(rm, acc=None, qty=1.0, price=100.0, stop=98.0, n_open=0, dec=None, kill=False, symbol=ALT):
    return rm.check_entry(symbol, qty, price, stop, acc or account(), n_open, dec or decision(), kill)


def test_risk_layer_blocks_every_unsafe_entry():
    rm = RiskManager(RiskCfg(), "spot", off_limits=["BAD/USDT"])
    assert check(rm).allowed
    assert not check(rm, kill=True).allowed
    v = check(rm, acc=account(equity=850, cash=850))                      # caída del 15 %
    assert not v.allowed and v.trip_kill_switch and v.escalate
    assert not check(rm, acc=account(equity=950, cash=950, peak=950)).allowed          # -5 % en el día
    assert check(rm, acc=account(equity=951, cash=951, peak=951)).allowed
    v = check(rm, dec=decision(regime="crisis"))
    assert not v.allowed and v.escalate
    v = check(rm, dec=decision(confidence=0.59))
    assert not v.allowed and v.escalate
    assert not check(rm, symbol="BAD/USDT").allowed
    assert not check(rm, n_open=3).allowed
    assert not check(rm, acc=account(positions={ALT: 100.0})).allowed
    assert not check(rm, stop=101.0).allowed and not check(rm, qty=0).allowed
    assert not check(rm, qty=0.05).allowed                                  # 5 $ < mínimo de 10 $


def test_risk_layer_only_ever_shrinks_orders():
    rm = RiskManager(RiskCfg(), "spot")
    v = check(rm, qty=1.0, price=100.0, stop=50.0)        # arriesgaría 50 $ = 5 %
    assert v.allowed and v.qty == pytest.approx(0.4) and v.qty * 50 == pytest.approx(20.0)
    v = check(rm, qty=100.0, stop=99.9)                   # 10.000 $ de posición
    assert v.allowed and v.qty * 100 == pytest.approx(1000 / 3)   # un tercio de la cartera, sin apalancar
    v = check(rm, qty=100.0, stop=99.9, acc=account(cash=50.0, positions={"X/USDT": 950.0}), n_open=1)
    assert v.allowed and v.qty * 100 == pytest.approx(50.0)       # no gasta más efectivo del que hay
    assert rm.leverage_cap == 1.0                                   # en spot el 2x de la configuración no aplica


def test_code_risk_state_thresholds():
    rm = RiskManager(RiskCfg(), "spot")
    assert rm.state(account(), 0) == "safe"
    assert rm.state(account(), 3) == "near_limit"
    assert rm.state(account(equity=920, peak=1000, day_start=920), 0) == "near_limit"   # caída del 8 %
    assert rm.state(account(equity=880, peak=1000, day_start=880), 0) == "reduce"       # caída del 12 %
    assert rm.state(account(equity=959, peak=959, day_start=1000), 0) == "reduce"       # -4,1 % hoy


# --- bróker simulado ------------------------------------------------------------------
def buy(b, qty=1.0, price=100.0, **kw):
    return b.buy(ALT, qty, price, 1, "t", 98.0, 104.0, 0.9, 0.6, **kw)


def test_broker_applies_fees_and_slippage_both_ways():
    b = PaperBroker(1000.0, fee_bps=10, slippage_bps=5)
    f = buy(b)
    assert f.status == "filled" and f.price == pytest.approx(100.05) and f.fee == pytest.approx(0.10005)
    assert b.cash == pytest.approx(1000 - 100.05 - 0.10005)
    assert b.equity({ALT: 100.0}) == pytest.approx(1000 - 0.05 - 0.10005)   # ya se pierde el coste de entrar
    s = b.sell(ALT, 104.0, 2, "objetivo")
    assert s.price == pytest.approx(104 * 0.9995) and s.pnl == pytest.approx(103.948 - 0.103948 - 100.15005)
    assert s.pnl_r == pytest.approx(s.pnl / (100.05 - 98.0)) and not b.positions
    assert b.sell(ALT, 104.0, 3, "x").status == "rejected"                  # no hay nada que vender


def test_broker_liquidity_cash_and_failed_orders():
    b = PaperBroker(1000.0, max_participation=0.01)
    f = buy(b, qty=5.0, liquidity_quote=10_000.0)          # solo puede tomar el 1 % del volumen: 100 $
    assert f.status == "partial" and f.qty * f.price == pytest.approx(100.0)
    b = PaperBroker(100.0)
    f = buy(b, qty=5.0)
    assert f.status == "partial" and b.cash == pytest.approx(0.0, abs=1e-9)   # nunca en negativo
    b = PaperBroker(1000.0, fail_next=1)
    assert buy(b).status == "rejected" and b.cash == 1000.0 and not b.positions
    assert buy(b).status == "filled"
    b.fail_next = 1
    assert b.sell(ALT, 100.0, 2, "stop").status == "rejected" and ALT in b.positions   # la posición sigue viva


def test_reconciliation_detects_any_mismatch():
    b = PaperBroker(1000.0)
    fills = [buy(b), b.sell(ALT, 101.0, 2, "x"), buy(b, qty=2.0)]
    assert b.reconcile(1000.0, fills) == []
    assert b.reconcile(1000.0, fills + [Fill(3, ALT, "BUY", 0, 0, 0, "rejected", "x")]) == []
    b.cash += 5.0
    assert any("efectivo" in x for x in b.reconcile(1000.0, fills))
    b.cash -= 5.0
    b.positions[ALT].qty *= 2
    assert any(ALT in x for x in b.reconcile(1000.0, fills))
    assert PaperBroker(1000.0).reconcile(1000.0, fills)     # un bróker vacío no cuadra con ese diario

import base64
import json
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app import create_app
from bot import engine, notify
from bot.backtest import run_backtest
from bot.config import DEFAULT_SETTINGS, PRESETS, SettingsError, parse_allocation, validate_settings
from bot.data import SyntheticProvider
from bot.engine import (check_exit, execute_allocation, plan_allocation, plan_rotation, position_amount,
                        rank_candidates, run_cycle, split_live_bars, us_market_open)
from bot.markets import closed_exchanges, exchange_of
from bot.indicators import compute_indicators, rsi
from bot.portfolio import Account, Position
from bot.rules import RULES, evaluate
from bot.store import Store

# Fila de indicadores que cumple las 10 condiciones.
GOOD_ROW = {
    "open": 100.0, "close": 100.0, "sma50": 95.0, "sma200": 90.0, "rsi": 60.0,
    "macd": 1.0, "macd_signal": 0.5, "macd_hist": 0.5, "macd_hist_prev": 0.3,
    "vol_ratio": 1.1, "high52": 102.0, "ret63": 0.08, "ret126": 0.15, "ret252": 0.25, "vol30": 0.20,
}
# La estrategia original: lista de condiciones con importe fijo.
CHECKLIST = dict(strategy="checklist", sizing_mode="fixed", position_size=100, max_positions=10,
                 stop_loss_pct=8, trailing_stop_pct=12, exit_score_pct=50, fee_pct=0)
UTC = timezone.utc


def frame(closes, end=None):
    idx = (pd.bdate_range("2020-01-01", periods=len(closes)) if end is None
           else pd.bdate_range(end=end, periods=len(closes)))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1000.0}, index=idx)


class FakeProvider:
    """Serie plana con el precio indicado; la puntuación se controla con `patch_scores`."""
    name = "fake"

    def __init__(self, prices):
        self.prices = prices

    def history(self, symbol, days):
        if symbol not in self.prices:
            raise RuntimeError("ticker desconocido")
        return frame([self.prices[symbol]] * 300)


class TrendProvider:
    """Cada ticker crece (o cae) a un ritmo diario constante y termina en 100."""
    name = "trend"

    def __init__(self, growth):
        self.growth = growth

    def history(self, symbol, days):
        g = self.growth[symbol]
        return frame(100 * (1 + g) ** np.arange(-299, 1))


def patch_scores(monkeypatch, scores):
    """Hace que cada ticker obtenga la puntuación indicada (n condiciones de 10)."""
    def fake_build(symbol, row, ts, error=None, price=None):
        base = {"symbol": symbol, "ts": ts, "results": [], "ret63": 0.0, "mom": 0.0, "trend_ok": True}
        if row is None:
            return {**base, "price": None, "score_pct": None, "passed": None, "total": None, "status": error}
        n = scores[symbol]
        return {**base, "price": row["close"] if price is None else price, "score_pct": n * 10.0,
                "passed": n, "total": 10, "status": "ok"}
    monkeypatch.setattr(engine, "build_signal", fake_build)


def make_store(**overrides):
    store = Store(":memory:")
    store.save_settings(validate_settings(overrides))
    store.reset()
    return store


# --- indicadores -------------------------------------------------------------
def test_sma_and_rsi_extremes():
    up = compute_indicators(frame(np.arange(1, 301)))
    assert up["sma50"].iloc[-1] == pytest.approx(np.arange(251, 301).mean())
    assert up["rsi"].iloc[-1] == pytest.approx(100.0)
    down = rsi(pd.Series(np.arange(300, 0, -1), dtype=float))
    assert down.iloc[-1] == pytest.approx(0.0)
    assert math.isnan(up["sma200"].iloc[100])  # aún sin historia suficiente
    assert up["ret126"].iloc[-1] == pytest.approx(300 / 174 - 1)


# --- condiciones -------------------------------------------------------------
def test_all_rules_pass_on_good_row():
    ev = evaluate(GOOD_ROW)
    assert len(RULES) == 10 and ev["passed"] == 10 and ev["score_pct"] == 100.0


@pytest.mark.parametrize("override", [
    {"sma200": 120.0, "sma50": 119.0},
    {"rsi": 80.0}, {"rsi": 30.0}, {"macd": 0.1}, {"macd_hist_prev": 0.9},
    {"vol_ratio": 0.5}, {"high52": 150.0}, {"ret63": -0.01}, {"vol30": 0.8},
])
def test_each_rule_can_fail(override):
    assert evaluate({**GOOD_ROW, **override})["passed"] < 10


def test_missing_data_counts_as_failed():
    ev = evaluate({**GOOD_ROW, "rsi": float("nan")})
    assert ev["passed"] == 9 and ev["score_pct"] == 90.0


def test_signal_has_momentum_and_trend():
    sig = engine.build_signal("A", GOOD_ROW, "t", price=101.0)
    assert sig["mom"] == pytest.approx((0.08 + 0.15 + 0.25) / 3) and sig["trend_ok"] is True
    assert sig["price"] == 101.0 and sig["status"] == "ok"
    assert engine.build_signal("A", {**GOOD_ROW, "ret252": float("nan")}, "t")["mom"] is None
    assert engine.build_signal("A", {**GOOD_ROW, "sma200": float("nan")}, "t")["status"].startswith("historia")


# --- cartera -----------------------------------------------------------------
def test_buy_and_sell_math_with_fees():
    acc = Account(cash=1000.0, fee_pct=1.0)
    buy = acc.buy("AAA", 10.0, 100.0, "t0", 90.0, "x")
    assert buy["qty"] == pytest.approx(9.9) and acc.cash == pytest.approx(900.0)
    assert acc.buy("AAA", 10.0, 100.0, "t0", 90.0, "x") is None  # ya en cartera
    assert acc.equity == pytest.approx(999.0)  # se pierde la comisión de 1 $
    assert acc.sell("NOPE", 10.0, "t1", None, "y") is None
    sell = acc.sell("AAA", 20.0, "t1", None, "y")
    assert sell["amount"] == pytest.approx(9.9 * 20 * 0.99)
    assert sell["pnl"] == pytest.approx(9.9 * 20 * 0.99 - 100)
    assert acc.cash == pytest.approx(900 + 196.02) and not acc.positions


def test_adding_to_a_position_and_selling_part_of_it():
    acc = Account(cash=1000.0, fee_pct=0.0)
    acc.buy("AAA", 10.0, 100.0, "t0", 0.0, "x")
    acc.buy("AAA", 20.0, 100.0, "t1", 0.0, "x", add=True)          # 10 + 5 participaciones por 200
    pos = acc.positions["AAA"]
    assert pos.qty == pytest.approx(15.0) and pos.cost == pytest.approx(200.0) and pos.avg_price == pytest.approx(200 / 15)
    part = acc.sell("AAA", 20.0, "t2", None, "y", qty=6.0)            # vende 6 de 15
    assert part["amount"] == pytest.approx(120.0) and part["pnl"] == pytest.approx(120 - 200 * 6 / 15)
    assert pos.qty == pytest.approx(9.0) and pos.cost == pytest.approx(120.0) and acc.cash == pytest.approx(920.0)
    assert acc.sell("AAA", 20.0, "t3", None, "y", qty=0) is None
    rest = acc.sell("AAA", 20.0, "t3", None, "y", qty=999)            # pedir de más vende lo que hay
    assert rest["qty"] == pytest.approx(9.0) and "AAA" not in acc.positions
    assert acc.cash == pytest.approx(1100.0)                           # 200 invertidos, 300 recuperados


def test_cannot_buy_without_cash():
    acc = Account(cash=50.0)
    assert acc.buy("AAA", 10.0, 100.0, "t0", 90.0, "x") is None and acc.cash == 50.0


def test_position_amount():
    s = validate_settings({"sizing_mode": "percent", "position_pct": 20})
    acc = Account(cash=1000.0)
    assert position_amount(acc, s) == pytest.approx(200.0)
    acc.buy("A", 10.0, 200.0, "t", 0, "x")
    acc.positions["A"].mark(20.0)                      # la cartera vale 1.200
    assert position_amount(acc, s) == pytest.approx(240.0)  # se reinvierten las ganancias
    acc.cash = 95.0                                    # cartera 495 -> objetivo 99
    assert position_amount(acc, s) == pytest.approx(95.0)   # casi llega: usa lo que queda
    acc.cash = 50.0                                    # cartera 450 -> objetivo 90
    assert position_amount(acc, s) == 0.0                   # muy por debajo: no compra
    fixed = validate_settings({"sizing_mode": "fixed", "position_size": 100})
    assert position_amount(Account(cash=1000.0), fixed) == 100.0


# --- decisiones --------------------------------------------------------------
def test_buy_threshold_is_90_percent():
    s = validate_settings(CHECKLIST)
    sigs = [{"symbol": x, "status": "ok", "score_pct": p, "ret63": 0.0}
            for x, p in [("A", 100.0), ("B", 90.0), ("C", 80.0), ("D", 89.9)]]
    assert [x["symbol"] for x in rank_candidates(sigs, set(), s)] == ["A", "B"]
    assert [x["symbol"] for x in rank_candidates(sigs, {"A"}, s)] == ["B"]


def test_exit_reasons():
    s = validate_settings(CHECKLIST)
    pos = Position("A", 1.0, 100.0, "t", 90.0, 100.0, 100.0)
    assert check_exit(pos, 100.0, 90.0, s) is None
    assert check_exit(pos, 92.0, 90.0, s).startswith("Stop-loss")
    assert check_exit(pos, 100.0, 40.0, s).startswith("Condiciones deterioradas")
    assert check_exit(pos, 100.0, 50.0, s) is None  # justo en el límite se mantiene
    assert check_exit(pos, 100.0, None, s) is None  # sin puntuación no se vende por condiciones
    pos.mark(150.0)
    assert check_exit(pos, 131.0, 90.0, s).startswith("Trailing stop")
    assert check_exit(pos, 135.0, 90.0, s) is None
    assert check_exit(pos, 135.0, 90.0, {**s, "take_profit_pct": 30.0}).startswith("Objetivo")
    assert check_exit(pos, 131.0, 90.0, {**s, "trailing_stop_pct": 0.0}) is None


def test_plan_rotation_picks_strongest_and_respects_filters():
    s = validate_settings({"max_positions": 2, "rotation_min_score_pct": 60, "trend_filter": True})
    def sig(sym, mom, score=80.0, trend=True, status="ok"):
        return {"symbol": sym, "status": status, "mom": mom, "score_pct": score, "trend_ok": trend}
    signals = [sig("A", 0.50), sig("B", 0.40, score=50.0), sig("C", 0.30, trend=False),
               sig("D", 0.20), sig("E", 0.10), sig("F", None), sig("G", None, status="error")]
    sells, buys = plan_rotation(signals, {"E", "G"}, s)
    assert [b["symbol"] for b in buys] == ["A", "D"]  # B: pocas condiciones; C: bajo su media
    assert list(sells) == ["E"]                        # G no tiene datos hoy: no se toca
    _, buys = plan_rotation(signals, set(), {**s, "trend_filter": False, "rotation_min_score_pct": 0})
    assert [b["symbol"] for b in buys] == ["A", "B"]


# --- cartera de pesos fijos -----------------------------------------------------
ALLOC = validate_settings({"strategy": "allocation", "allocation": {"A": 60, "B": 40}, "fee_pct": 0})
W = {"A": 0.6, "B": 0.4}


def test_allocation_initial_investment_and_band():
    acc = Account(cash=1000.0)
    prices = {"A": 10.0, "B": 20.0}
    orders = plan_allocation(acc, prices, W, ALLOC, force=True)
    assert [(o[0], round(o[1], 2)) for o in orders] == [("B", 400.0), ("A", 600.0)]
    execute_allocation(acc, orders, prices, "t")
    assert acc.cash == pytest.approx(0.0) and acc.positions["A"].qty == pytest.approx(60.0)

    # A sube un 15 %: pesos 63,3 / 36,7 -> dentro de la banda de 5 puntos, no se toca.
    assert plan_allocation(acc, {"A": 11.5, "B": 20.0}, W, ALLOC) == []
    # A sube un 30 %: pesos 66,1 / 33,9 -> fuera de banda: vende A y compra B hasta 60/40.
    prices = {"A": 13.0, "B": 20.0}
    orders = plan_allocation(acc, prices, W, ALLOC)
    assert orders[0][0] == "A" and orders[0][1] < 0 < orders[1][1] and "Reequilibrio" in orders[0][2]
    execute_allocation(acc, orders, prices, "t")
    equity = acc.cash + acc.positions["A"].qty * 13 + acc.positions["B"].qty * 20
    assert acc.positions["A"].qty * 13 / equity == pytest.approx(0.6) and acc.cash == pytest.approx(0.0, abs=1e-6)


def test_allocation_new_cash_only_buys_what_is_underweight():
    acc = Account(cash=1000.0)
    prices = {"A": 10.0, "B": 20.0}
    execute_allocation(acc, plan_allocation(acc, prices, W, ALLOC, force=True), prices, "t")
    acc.cash += 100.0                                              # llega una aportación
    prices = {"A": 11.0, "B": 20.0}                                # A está algo por encima de su peso
    orders = plan_allocation(acc, prices, W, ALLOC)
    assert orders and all(delta > 0 for _, delta, _ in orders)     # no vende nada
    assert max(orders, key=lambda o: o[1])[0] == "B"               # la mayor parte va a lo que falta
    assert sum(o[1] for o in orders) == pytest.approx(100.0)
    acc.cash = 5.0                                                 # calderilla: no merece una orden
    assert plan_allocation(acc, prices, W, ALLOC) == []


def test_allocation_sells_what_no_longer_belongs_and_survives_fees():
    s = {**ALLOC, "fee_pct": 0.5}
    acc = Account(cash=1000.0, fee_pct=0.5)
    acc.buy("OLD", 10.0, 500.0, "t", 0.0, "x")
    prices = {"A": 10.0, "B": 20.0, "OLD": 10.0}
    orders = plan_allocation(acc, prices, W, s)
    assert orders[0][0] == "OLD" and "Ya no está" in orders[0][2]
    execute_allocation(acc, orders, prices, "t")
    assert "OLD" not in acc.positions and set(acc.positions) == {"A", "B"}
    assert 0 <= acc.cash < 5.0                                     # nunca gasta más de lo que hay


def test_allocation_cycle_invests_once_a_month_and_respects_the_band():
    store = make_store(strategy="allocation", allocation="A 60, B 40", fee_pct=0)
    jan = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    first = run_cycle(FakeProvider({"A": 10.0, "B": 20.0}), store, now=jan)
    assert first["rebalanced"] and {b["symbol"] for b in first["buys"]} == {"A", "B"} and first["evaluated"] == 2
    again = run_cycle(FakeProvider({"A": 20.0, "B": 20.0}), store, now=jan.replace(day=20))
    assert again["buys"] == [] and again["sells"] == []            # el mismo mes no se revisa dos veces
    feb = run_cycle(FakeProvider({"A": 10.5, "B": 20.0}), store, now=datetime(2026, 2, 2, 15, 0, tzinfo=UTC))
    assert not feb["rebalanced"] and "dentro de la banda" in feb["notes"][0]
    mar = run_cycle(FakeProvider({"A": 20.0, "B": 20.0}), store, now=datetime(2026, 3, 2, 15, 0, tzinfo=UTC))
    assert mar["rebalanced"] and [x["symbol"] for x in mar["sells"]] == ["A"] and [x["symbol"] for x in mar["buys"]] == ["B"]
    acc = store.load_account(0)
    assert acc.positions["A"].value / acc.equity == pytest.approx(0.6)
    # Un ETF sin precio: se espera, no se opera a ciegas.
    apr = run_cycle(FakeProvider({"A": 40.0}), store, now=datetime(2026, 4, 1, 15, 0, tzinfo=UTC))
    assert apr["buys"] == [] and apr["sells"] == [] and "reintentará" in apr["notes"][0]


def test_monthly_contribution_adds_cash_without_counting_as_profit():
    store = make_store(strategy="allocation", allocation="A 60, B 40", fee_pct=0, monthly_contribution=100)
    prices = FakeProvider({"A": 10.0, "B": 20.0})
    jan = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    run_cycle(prices, store, now=jan)
    assert store.get_contributed() == 0.0                           # el mes de arranque no aporta
    run_cycle(prices, store, now=jan.replace(day=25))
    assert store.get_contributed() == 0.0
    feb = run_cycle(prices, store, now=datetime(2026, 2, 2, 15, 0, tzinfo=UTC))
    assert store.get_contributed() == 100.0 and any("Aportación" in n for n in feb["notes"])
    acc = store.load_account(0)
    assert acc.equity == pytest.approx(1100.0) and acc.cash == pytest.approx(0.0, abs=1e-6)   # y se invierte
    assert acc.equity / store.get_units() == pytest.approx(1.0)     # precios planos: rentabilidad 0 %
    run_cycle(prices, store, now=datetime(2026, 2, 20, 15, 0, tzinfo=UTC))
    assert store.get_contributed() == 100.0                         # una sola vez por mes
    doubled = FakeProvider({"A": 20.0, "B": 40.0})
    run_cycle(doubled, store, now=datetime(2026, 3, 2, 15, 0, tzinfo=UTC))
    assert store.get_contributed() == 200.0
    acc = store.load_account(0)
    assert acc.equity == pytest.approx(2300.0)                      # 1.100 x 2 + 100 nuevos
    assert acc.equity / store.get_units() == pytest.approx(2.0)     # la rentabilidad es +100 %, no +130 %
    assert store.equity_history()[-1]["invested"] == 1200.0
    store.reset()
    assert store.get_contributed() == 0.0 and store.get_units() == 1000.0


# --- bolsas y horarios ------------------------------------------------------------
def test_exchange_hours_by_ticker_suffix():
    assert exchange_of("SPY").name == "Nueva York" and exchange_of("BRK.B").name == "Nueva York"
    assert exchange_of("VWCE.DE").name == "Xetra" and exchange_of("CSPX.L").name == "Londres"
    noon_utc = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)               # lunes: Europa abierta, EE. UU. aún no
    assert closed_exchanges(["VWCE.DE", "EUNA.DE"], noon_utc) == []
    assert closed_exchanges(["SPY"], noon_utc) == ["Nueva York"]
    assert closed_exchanges(["SPY", "VWCE.DE"], noon_utc) == ["Nueva York"]
    assert closed_exchanges(["SPY", "VWCE.DE"], noon_utc.replace(hour=15)) == []   # las dos coinciden abiertas
    assert closed_exchanges(["SPY", "VWCE.DE"], noon_utc.replace(hour=18)) == ["Xetra"]
    assert closed_exchanges(["VWCE.DE"], datetime(2026, 1, 3, 12, 0, tzinfo=UTC)) == ["Xetra"]   # sábado

    df = frame([10.0] * 9 + [11.0], end="2026-01-05")
    xetra = exchange_of("VWCE.DE")
    bars, price = split_live_bars(df, noon_utc, xetra)                # en sesión en Xetra: vela a medias
    assert len(bars) == 9 and price == 11.0
    bars, _ = split_live_bars(df, noon_utc.replace(hour=17), xetra)   # 18:00 en Berlín: ya cerró
    assert len(bars) == 10


def test_european_etfs_trade_in_european_hours():
    class Live(FakeProvider):
        real_time = True
    store = make_store(strategy="allocation", allocation="X.DE 60, Y.DE 40", fee_pct=0)
    prices = Live({"X.DE": 10.0, "Y.DE": 20.0})
    evening = run_cycle(prices, store, now=datetime(2026, 1, 5, 19, 0, tzinfo=UTC))   # 20:00 en Berlín
    assert evening["can_trade"] is False and evening["buys"] == [] and "Xetra" in evening["notes"][0]
    morning = run_cycle(prices, store, now=datetime(2026, 1, 6, 9, 0, tzinfo=UTC))     # Nueva York aún cerrada
    assert morning["can_trade"] is True and len(morning["buys"]) == 2


# --- ciclo: lista de condiciones ---------------------------------------------
def test_checklist_cycle_buys_above_threshold_and_sells_on_stop(monkeypatch):
    store = make_store(universe="AAA BBB CCC", **CHECKLIST)
    patch_scores(monkeypatch, {"AAA": 9, "BBB": 8, "CCC": 10})
    summary = run_cycle(FakeProvider({"AAA": 10.0, "BBB": 10.0, "CCC": 10.0}), store)
    assert [b["symbol"] for b in summary["buys"]] == ["CCC", "AAA"] and summary["sells"] == []
    acc = store.load_account(0)
    assert set(acc.positions) == {"AAA", "CCC"} and acc.cash == pytest.approx(800.0)

    # AAA cae un 10 % -> stop-loss. CCC se mantiene. BBB sigue sin llegar al 90 %.
    summary = run_cycle(FakeProvider({"AAA": 9.0, "BBB": 10.0, "CCC": 10.0}), store)
    assert [x["symbol"] for x in summary["sells"]] == ["AAA"] and summary["buys"] == []
    acc = store.load_account(0)
    assert set(acc.positions) == {"CCC"} and acc.cash == pytest.approx(890.0)
    assert store.realized_pnl() == pytest.approx(-10.0)
    assert len(store.equity_history()) == 2 and len(store.trades()) == 3


def test_checklist_cycle_respects_max_positions_and_cash(monkeypatch):
    store = make_store(universe="A B C D", **{**CHECKLIST, "max_positions": 2})
    patch_scores(monkeypatch, {"A": 10, "B": 10, "C": 10, "D": 10})
    prices = {"A": 5.0, "B": 5.0, "C": 5.0, "D": 5.0}
    assert len(run_cycle(FakeProvider(prices), store)["buys"]) == 2
    assert run_cycle(FakeProvider(prices), store)["buys"] == []  # ya está lleno

    store = make_store(universe="A B C D", initial_cash=250, **CHECKLIST)
    assert len(run_cycle(FakeProvider(prices), store)["buys"]) == 2  # solo hay efectivo para dos


def test_cycle_survives_broken_ticker_and_keeps_position(monkeypatch):
    store = make_store(universe="AAA BBB", **CHECKLIST)
    patch_scores(monkeypatch, {"AAA": 10, "BBB": 10})
    run_cycle(FakeProvider({"AAA": 10.0, "BBB": 10.0}), store)
    summary = run_cycle(FakeProvider({"AAA": 10.0}), store)  # BBB deja de tener datos
    assert "BBB" in summary["errors"] and summary["sells"] == []
    assert set(store.load_account(0).positions) == {"AAA", "BBB"}


def test_no_rebuy_on_the_day_of_a_sale(monkeypatch):
    store = make_store(universe="AAA", **CHECKLIST)
    patch_scores(monkeypatch, {"AAA": 10})
    day1 = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    assert len(run_cycle(FakeProvider({"AAA": 10.0}), store, now=day1)["buys"]) == 1
    later = day1.replace(hour=16)
    assert len(run_cycle(FakeProvider({"AAA": 9.0}), store, now=later)["sells"]) == 1  # stop-loss
    assert run_cycle(FakeProvider({"AAA": 9.0}), store, now=later.replace(hour=17))["buys"] == []
    assert len(run_cycle(FakeProvider({"AAA": 9.0}), store, now=day1.replace(day=6))["buys"]) == 1


def test_market_filter_blocks_new_buys(monkeypatch):
    store = make_store(universe="AAA", market_filter=True, benchmark="IDX", **CHECKLIST)
    patch_scores(monkeypatch, {"AAA": 10})
    falling = run_cycle(TrendProvider({"AAA": 0.001, "IDX": -0.001}), store)
    assert falling["market_ok"] is False and falling["buys"] == [] and falling["notes"]
    rising = run_cycle(TrendProvider({"AAA": 0.001, "IDX": 0.001}), store)
    assert rising["market_ok"] is True and len(rising["buys"]) == 1


# --- ciclo: rotación mensual -------------------------------------------------
ROTATION = dict(strategy="rotation", universe="A B C D", max_positions=2, position_pct=50,
                rotation_min_score_pct=0, fee_pct=0, stop_loss_pct=25)


def test_rotation_enters_once_a_month_and_rotates(monkeypatch):
    store = make_store(**ROTATION)
    jan = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    first = run_cycle(TrendProvider({"A": 0.003, "B": 0.002, "C": 0.001, "D": -0.001}), store, now=jan)
    assert first["rebalanced"] and [b["symbol"] for b in first["buys"]] == ["A", "B"]
    assert store.load_account(0).cash == pytest.approx(0.0)  # 2 x 50 %

    # Mismo mes: aunque cambie el ranking, no se rota otra vez.
    changed = TrendProvider({"A": -0.001, "B": 0.002, "C": 0.004, "D": 0.001})
    same_month = run_cycle(changed, store, now=jan.replace(day=20))
    assert not same_month["rebalanced"] and same_month["buys"] == [] and same_month["sells"] == []

    # Mes siguiente: A ha perdido fuerza (y está bajo su media) -> sale; entra C.
    feb = run_cycle(changed, store, now=datetime(2026, 2, 2, 15, 0, tzinfo=UTC))
    assert feb["rebalanced"] and [x["symbol"] for x in feb["sells"]] == ["A"]
    assert [b["symbol"] for b in feb["buys"]] == ["C"]
    assert set(store.load_account(0).positions) == {"B", "C"}
    signals = {g["symbol"]: g for g in store.signals()}
    assert signals["C"]["rank"] == 1 and signals["C"]["target"] and signals["A"]["rank"] is None


def test_rotation_keeps_stop_loss_between_rebalances():
    store = make_store(**ROTATION)
    jan = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    run_cycle(FakeProviderWithHistory({"A": 100.0, "B": 100.0, "C": 95.0, "D": 95.0}), store, now=jan)
    assert set(store.load_account(0).positions) == {"A", "B"}
    crash = run_cycle(FakeProviderWithHistory({"A": 70.0, "B": 100.0, "C": 95.0, "D": 95.0}), store,
                      now=jan.replace(day=12))
    assert [x["symbol"] for x in crash["sells"]] == ["A"] and crash["sells"][0]["reason"].startswith("Stop-loss")
    assert not crash["rebalanced"] and crash["buys"] == []  # el hueco se rellena en la próxima rotación


class FakeProviderWithHistory:
    """Tendencia alcista suave que termina en el precio indicado (último día: salto al precio)."""
    name = "fake"

    def __init__(self, last):
        self.last = last

    def history(self, symbol, days):
        if symbol not in self.last:
            raise RuntimeError("ticker desconocido")
        closes = list(100 * 1.001 ** np.arange(-299, 0)) + [self.last[symbol]]
        return frame(closes)


def test_rotation_waits_when_too_many_tickers_fail():
    store = make_store(**ROTATION)
    summary = run_cycle(TrendProvider({"A": 0.003}), store)  # faltan B, C y D
    assert not summary["rebalanced"] and summary["buys"] == [] and summary["notes"]


# --- reloj de mercado --------------------------------------------------------
def test_market_hours_and_partial_bar():
    assert us_market_open(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))        # lunes 10:00 NY
    assert not us_market_open(datetime(2026, 1, 5, 13, 0, tzinfo=UTC))    # lunes 08:00 NY
    assert not us_market_open(datetime(2026, 1, 5, 21, 30, tzinfo=UTC))   # lunes 16:30 NY
    assert not us_market_open(datetime(2026, 1, 3, 15, 0, tzinfo=UTC))    # sábado

    df = frame([10.0] * 9 + [11.0], end="2026-01-05")
    bars, price = split_live_bars(df, datetime(2026, 1, 5, 15, 0, tzinfo=UTC))
    assert len(bars) == 9 and price == 11.0   # en sesión: la vela de hoy no cuenta para los indicadores
    bars, price = split_live_bars(df, datetime(2026, 1, 5, 22, 0, tzinfo=UTC))
    assert len(bars) == 10 and price == 11.0  # tras el cierre ya es una vela completa


def test_real_time_provider_only_trades_during_market_hours(monkeypatch):
    class Live(FakeProvider):
        real_time = True
    store = make_store(universe="AAA", **CHECKLIST)
    patch_scores(monkeypatch, {"AAA": 10})
    closed = run_cycle(Live({"AAA": 10.0}), store, now=datetime(2026, 1, 3, 15, 0, tzinfo=UTC))
    assert closed["can_trade"] is False and closed["buys"] == [] and closed["evaluated"] == 1
    opened = run_cycle(Live({"AAA": 10.0}), store, now=datetime(2026, 1, 5, 15, 0, tzinfo=UTC))
    assert opened["can_trade"] is True and len(opened["buys"]) == 1


# --- ajustes -----------------------------------------------------------------
def test_settings_validation():
    s = validate_settings({"universe": "spy, qqq\nSPY", "buy_threshold_pct": "85", "max_positions": "3",
                           "market_filter": "true", "trend_filter": False, "benchmark": "qqq"})
    assert s["universe"] == ["SPY", "QQQ"] and s["buy_threshold_pct"] == 85.0 and s["max_positions"] == 3
    assert s["market_filter"] is True and s["trend_filter"] is False and s["benchmark"] == "QQQ"
    for bad in ({"universe": "<script>"}, {"universe": ""}, {"buy_threshold_pct": 120},
                {"position_size": "abc"}, {"stop_loss_pct": float("nan")}, {"strategy": "magia"},
                {"sizing_mode": "todo"}, {"market_filter": "quizá"}, {"benchmark": "a b"},
                {"allocation": "VT 60, BND 30"}, {"allocation": "VT 50 VT 50"}, {"allocation": "VT"},
                {"allocation": "<x> 100"}, {"allocation": "VT -10 BND 110"}, {"currency": "GBP"},
                {"rebalance_band_pct": 0}, {"monthly_contribution": -5}):
        with pytest.raises(SettingsError):
            validate_settings(bad)
    assert validate_settings({}) == DEFAULT_SETTINGS
    assert parse_allocation("vwce.de: 60%\nvagf.de = 40") == {"VWCE.DE": 60.0, "VAGF.DE": 40.0}
    assert parse_allocation({"SPY": 100}) == {"SPY": 100.0}
    assert DEFAULT_SETTINGS["strategy"] == "allocation" and sum(DEFAULT_SETTINGS["allocation"].values()) == 100
    for preset in PRESETS:                                          # los ejemplos del panel son válidos
        assert validate_settings(preset["settings"])["allocation"] == preset["settings"]["allocation"]


# --- backtest ----------------------------------------------------------------
@pytest.mark.parametrize("strategy", ["allocation", "rotation", "checklist"])
def test_backtest_is_consistent_and_deterministic(strategy):
    settings = validate_settings({"universe": "SPY QQQ AAPL MSFT KO XOM", "strategy": strategy,
                                  "allocation": "SPY 60, KO 40"})
    a = run_backtest(SyntheticProvider(), settings, years=2)
    b = run_backtest(SyntheticProvider(), settings, years=2)
    assert a["bot"] == b["bot"] and a["strategy"] == strategy
    assert a["curve"][0]["equity"] == pytest.approx(1000.0, rel=0.01)
    assert a["curve"][-1]["equity"] == pytest.approx(a["bot"]["final"])
    assert a["bot"]["total_return_pct"] == pytest.approx(100 * (a["bot"]["final"] / 1000 - 1))
    assert 0 <= a["bot"]["max_drawdown_pct"] <= 100 and 0 <= a["avg_exposure_pct"] <= 100
    assert a["market"]["final"] == pytest.approx(a["curve"][-1]["market"])
    assert [y["year"] for y in a["by_year"]] == sorted(y["year"] for y in a["by_year"])
    total = np.prod([1 + y["bot_pct"] / 100 for y in a["by_year"]])
    assert total == pytest.approx(a["bot"]["final"] / a["curve"][0]["equity"])  # los años encadenan
    json.dumps(a, allow_nan=False)  # debe ser JSON válido (sin NaN)


def test_allocation_backtest_holds_weights_and_handles_contributions():
    provider = SyntheticProvider()
    base = {"strategy": "allocation", "allocation": "SPY 60, KO 40", "fee_pct": 0}
    plain = run_backtest(provider, validate_settings(base), years=3)
    assert plain["avg_exposure_pct"] > 99 and plain["open_positions"] == 2 and plain["contributed"] == 0
    assert plain["final_value"] == pytest.approx(plain["bot"]["final"])
    buys = [t for t in plain["last_trades"] if t["side"] == "BUY"]
    assert all("Reequilibrio" in t["reason"] or "Inversión inicial" in t["reason"] for t in buys)

    saver = run_backtest(provider, validate_settings({**base, "monthly_contribution": 50}), years=3)
    assert saver["contributed"] == pytest.approx(50 * 36, abs=50) and saver["invested_total"] == 1000 + saver["contributed"]
    assert saver["final_value"] > plain["final_value"]                # hay más dinero dentro...
    # ...pero la rentabilidad casi no cambia: lo aportado no cuenta como ganancia.
    assert saver["bot"]["cagr_pct"] == pytest.approx(plain["bot"]["cagr_pct"], abs=1.5)
    json.dumps(saver, allow_nan=False)


def test_allocation_backtest_waits_for_the_youngest_etf():
    class Young(SyntheticProvider):
        def history(self, symbol, days):
            df = super().history(symbol, days)
            return df[df.index >= "2025-01-01"] if symbol == "NEW" else df

    settings = validate_settings({"strategy": "allocation", "allocation": "SPY 50, NEW 50"})
    res = run_backtest(Young(), settings, years=5)
    assert res["start"] >= "2025-01-01"                                # no finge historia que no existe


def test_backtest_executes_at_next_open_not_same_close():
    """Una orden decidida con el cierre de D se ejecuta a la apertura de D+1."""
    settings = validate_settings({"universe": "SPY QQQ AAPL MSFT KO XOM", "strategy": "rotation"})
    provider = SyntheticProvider()
    res = run_backtest(provider, settings, years=2)
    buys = [t for t in res["last_trades"] if t["side"] == "BUY"]
    assert buys
    for t in buys:
        df = provider.history(t["symbol"], 3000)
        assert t["price"] == pytest.approx(df.loc[t["ts"], "Open"])


def test_backtest_end_date_cuts_the_future():
    settings = validate_settings({"universe": "SPY QQQ AAPL MSFT KO XOM", "strategy": "rotation"})
    res = run_backtest(SyntheticProvider(), settings, years=2, end="2022-06-30")
    assert res["end"] == "2022-06-30" and res["start"].startswith("2020-0")


# --- alertas -----------------------------------------------------------------
def test_telegram_alert(monkeypatch):
    summary = {"buys": [{"symbol": "SPY", "amount": 200.0, "price": 500.0, "reason": "r1"}],
               "sells": [{"symbol": "KO", "pnl": -3.5, "pnl_pct": -1.7, "reason": "r2"}], "equity": 1010.0}
    text = notify.format_cycle(summary)
    assert "COMPRA SPY" in text and "VENTA KO" in text and "1010.00" in text
    assert notify.format_cycle({"buys": [], "sells": [], "equity": 1.0}) is None

    sent = []

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(request, timeout):
        sent.append((request.full_url, json.loads(request.data)))
        return Response()

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert notify.notify_cycle(summary, opener) is False and sent == []  # sin configurar no envía
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TKN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    assert notify.notify_cycle(summary, opener) is True
    assert sent[0][0].endswith("/botTKN/sendMessage") and sent[0][1]["chat_id"] == "42"

    def broken(request, timeout):
        raise OSError("sin red")
    assert notify.notify_cycle(summary, broken) is False  # un fallo de red no rompe el bot


def test_yahoo_provider_normalises_and_caches(monkeypatch):
    """Sin red: se simula yfinance para comprobar el tratamiento de la respuesta."""
    import yfinance
    from bot.data import DataError, YahooProvider
    calls = []

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, start, interval, auto_adjust):
            calls.append((self.symbol, interval, auto_adjust))
            if self.symbol == "NOPE":
                return pd.DataFrame()
            idx = pd.date_range("2026-01-05 00:00", periods=3, freq="D", tz="America/New_York")
            return pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": [1.5, None, 1.7],
                                 "Volume": 10, "Dividends": 0.0}, index=idx)

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    provider = YahooProvider()
    df = provider.history("SPY", 800)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.tz is None and len(df) == 2  # sin zona horaria y sin la fila vacía
    provider.history("SPY", 800)
    assert calls == [("SPY", "1d", True)]  # la segunda llamada sale de la caché
    with pytest.raises(DataError):
        provider.history("NOPE", 800)


# --- API ---------------------------------------------------------------------
def test_api_flow():
    client = create_app(provider=SyntheticProvider(), db_path=":memory:").test_client()
    assert client.get("/").status_code == 200
    state = client.get("/api/state").get_json()
    assert state["account"]["equity"] == 1000.0 and state["positions"] == [] and state["provider"] == "synthetic"
    assert state["rebalance_pending"] is True and state["settings"]["strategy"] == "allocation"
    assert [w["symbol"] for w in state["weights"]] == ["VT", "BND"] and len(state["presets"]) == 3

    # Modo cartera: invierte según los pesos y los muestra.
    cycle = client.post("/api/cycle").get_json()
    assert cycle["strategy"] == "allocation" and {b["symbol"] for b in cycle["buys"]} == {"VT", "BND"}
    state = client.get("/api/state").get_json()
    vt = next(w for w in state["weights"] if w["symbol"] == "VT")
    assert vt["target_pct"] == 60.0 and vt["actual_pct"] == pytest.approx(60.0, abs=0.2)
    assert state["rebalance_pending"] is False and state["account"]["pnl_pct"] == pytest.approx(-0.1, abs=0.05)

    # Cambiar la cartera obliga a revisarla en el siguiente análisis, sin esperar al mes que viene.
    saved = client.post("/api/settings", json={"allocation": "SPY 50, KO 50", "currency": "EUR"})
    assert saved.status_code == 200 and saved.get_json()["allocation"] == {"SPY": 50.0, "KO": 50.0}
    assert client.get("/api/state").get_json()["rebalance_pending"] is True
    cycle = client.post("/api/cycle").get_json()
    assert {x["symbol"] for x in cycle["sells"]} == {"VT", "BND"} and {b["symbol"] for b in cycle["buys"]} == {"SPY", "KO"}
    assert client.post("/api/settings", json={"allocation": "SPY 50"}).status_code == 400

    # Rotación sobre una lista propia.
    saved = client.post("/api/settings", json={"strategy": "rotation", "universe": "SPY QQQ KO XOM",
                                               "monthly_cost": 5, "trend_filter": False})
    assert saved.status_code == 200 and saved.get_json()["trend_filter"] is False
    bad = client.post("/api/settings", json={"buy_threshold_pct": 10})
    assert bad.status_code == 400 and "Umbral de compra" in bad.get_json()["error"]
    cycle = client.post("/api/cycle").get_json()
    assert cycle["evaluated"] == 4 and cycle["strategy"] == "rotation"
    state = client.get("/api/state").get_json()
    assert len(state["signals"]) == 4 and len(state["equity"]) == 3 and state["weights"] == []
    assert all(len(g["results"]) == 10 and "mom" in g for g in state["signals"])
    assert state["account"]["net_pnl"] <= state["account"]["pnl"]

    for strategy in ("allocation", "rotation", "checklist"):
        assert client.get(f"/api/backtest?years=1&strategy={strategy}").get_json()["strategy"] == strategy
    assert client.get("/api/state").get_json()["settings"]["strategy"] == "rotation"  # probar otra no la guarda
    assert client.get("/api/backtest?years=99").status_code == 400
    assert client.get("/api/backtest?years=1&strategy=x").status_code == 400

    assert client.post("/api/reset").get_json() == {"ok": True}
    state = client.get("/api/state").get_json()
    assert state["trades"] == [] and state["signals"] == [] and state["last_cycle"] is None


def test_panel_password():
    client = create_app(provider=SyntheticProvider(), db_path=":memory:", password="s3creta").test_client()
    assert client.get("/").status_code == 401
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/reset").status_code == 401
    wrong = {"Authorization": "Basic " + base64.b64encode(b"yo:otra").decode()}
    assert client.get("/api/state", headers=wrong).status_code == 401
    right = {"Authorization": "Basic " + base64.b64encode(b"yo:s3creta").decode()}
    assert client.get("/api/state", headers=right).status_code == 200

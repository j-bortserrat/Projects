import json
import math

import numpy as np
import pandas as pd
import pytest

from app import create_app
from bot import engine
from bot.backtest import run_backtest
from bot.config import DEFAULT_SETTINGS, SettingsError, validate_settings
from bot.data import SyntheticProvider
from bot.engine import check_exit, rank_candidates, run_cycle
from bot.indicators import compute_indicators, rsi
from bot.portfolio import Account, Position
from bot.rules import RULES, evaluate
from bot.store import Store

# Fila de indicadores que cumple las 10 condiciones.
GOOD_ROW = {
    "open": 100.0, "close": 100.0, "sma50": 95.0, "sma200": 90.0, "rsi": 60.0,
    "macd": 1.0, "macd_signal": 0.5, "macd_hist": 0.5, "macd_hist_prev": 0.3,
    "vol_ratio": 1.1, "high52": 102.0, "ret63": 0.08, "vol30": 0.20,
}


def frame(closes):
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1000.0}, index=idx)


class FakeProvider:
    """Devuelve una serie plana con el precio indicado; la puntuación se controla aparte."""
    name = "fake"

    def __init__(self, prices):
        self.prices = prices

    def history(self, symbol, days):
        if symbol not in self.prices:
            raise RuntimeError("ticker desconocido")
        return frame([self.prices[symbol]] * 300)


def patch_scores(monkeypatch, scores):
    """Hace que cada ticker obtenga la puntuación indicada (n condiciones de 10)."""
    def fake_build(symbol, row, ts, error=None):
        if row is None:
            return {"symbol": symbol, "ts": ts, "price": None, "score_pct": None, "passed": None,
                    "total": None, "status": error, "results": [], "ret63": None}
        n = scores[symbol]
        return {"symbol": symbol, "ts": ts, "price": row["close"], "score_pct": n * 10.0, "passed": n,
                "total": 10, "status": "ok", "results": [], "ret63": 0.0}
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


# --- condiciones -------------------------------------------------------------
def test_all_rules_pass_on_good_row():
    ev = evaluate(GOOD_ROW)
    assert len(RULES) == 10 and ev["passed"] == 10 and ev["score_pct"] == 100.0


@pytest.mark.parametrize("override", [
    {"sma200": 120.0, "sma50": 119.0},  # rompe trend_200 (y otras)
    {"rsi": 80.0}, {"rsi": 30.0}, {"macd": 0.1}, {"macd_hist_prev": 0.9},
    {"vol_ratio": 0.5}, {"high52": 150.0}, {"ret63": -0.01}, {"vol30": 0.8},
])
def test_each_rule_can_fail(override):
    assert evaluate({**GOOD_ROW, **override})["passed"] < 10


def test_missing_data_counts_as_failed():
    ev = evaluate({**GOOD_ROW, "rsi": float("nan")})
    assert ev["passed"] == 9 and ev["score_pct"] == 90.0


# --- cartera -----------------------------------------------------------------
def test_buy_and_sell_math_with_fees():
    acc = Account(cash=1000.0, fee_pct=1.0)
    buy = acc.buy("AAA", 10.0, 100.0, "t0", 90.0, "x")
    assert buy["qty"] == pytest.approx(9.9) and acc.cash == pytest.approx(900.0)
    assert acc.buy("AAA", 10.0, 100.0, "t0", 90.0, "x") is None  # ya en cartera
    assert acc.equity == pytest.approx(999.0)  # se pierde la comisión de 1 $
    sell = acc.sell("AAA", 20.0, "t1", None, "y")
    assert sell["amount"] == pytest.approx(9.9 * 20 * 0.99)
    assert sell["pnl"] == pytest.approx(9.9 * 20 * 0.99 - 100)
    assert acc.cash == pytest.approx(900 + 196.02) and not acc.positions


def test_cannot_buy_without_cash():
    acc = Account(cash=50.0)
    assert acc.buy("AAA", 10.0, 100.0, "t0", 90.0, "x") is None and acc.cash == 50.0


# --- decisiones --------------------------------------------------------------
def test_buy_threshold_is_90_percent():
    sigs = [{"symbol": s, "status": "ok", "score_pct": p, "ret63": 0.0}
            for s, p in [("A", 100.0), ("B", 90.0), ("C", 80.0), ("D", 89.9)]]
    assert [s["symbol"] for s in rank_candidates(sigs, set(), DEFAULT_SETTINGS)] == ["A", "B"]
    assert [s["symbol"] for s in rank_candidates(sigs, {"A"}, DEFAULT_SETTINGS)] == ["B"]


def test_exit_reasons():
    s = dict(DEFAULT_SETTINGS)
    pos = Position("A", 1.0, 100.0, "t", 90.0, 100.0, 100.0)
    assert check_exit(pos, 100.0, 90.0, s) is None
    assert check_exit(pos, 92.0, 90.0, s).startswith("Stop-loss")
    assert check_exit(pos, 100.0, 40.0, s).startswith("Condiciones deterioradas")
    assert check_exit(pos, 100.0, 50.0, s) is None  # justo en el límite se mantiene
    pos.mark(150.0)
    assert check_exit(pos, 131.0, 90.0, s).startswith("Trailing stop")
    assert check_exit(pos, 135.0, 90.0, s) is None
    assert check_exit(pos, 135.0, 90.0, {**s, "take_profit_pct": 30.0}).startswith("Objetivo")


def test_cycle_buys_only_above_threshold_and_sells_on_stop(monkeypatch):
    store = make_store(universe="AAA BBB CCC", fee_pct=0)
    patch_scores(monkeypatch, {"AAA": 9, "BBB": 8, "CCC": 10})
    summary = run_cycle(FakeProvider({"AAA": 10.0, "BBB": 10.0, "CCC": 10.0}), store)
    assert summary["buys"] == ["CCC", "AAA"] and summary["sells"] == []
    acc = store.load_account(0)
    assert set(acc.positions) == {"AAA", "CCC"} and acc.cash == pytest.approx(800.0)

    # AAA cae un 10 % -> stop-loss. CCC se mantiene. BBB sigue sin llegar al 90 %.
    summary = run_cycle(FakeProvider({"AAA": 9.0, "BBB": 10.0, "CCC": 10.0}), store)
    assert [s["symbol"] for s in summary["sells"]] == ["AAA"] and summary["buys"] == []
    acc = store.load_account(0)
    assert set(acc.positions) == {"CCC"} and acc.cash == pytest.approx(890.0)
    assert store.realized_pnl() == pytest.approx(-10.0)
    assert len(store.equity_history()) == 2 and len(store.trades()) == 3


def test_cycle_respects_max_positions_and_cash(monkeypatch):
    store = make_store(universe="A B C D", max_positions=2, fee_pct=0)
    patch_scores(monkeypatch, {"A": 10, "B": 10, "C": 10, "D": 10})
    prices = {"A": 5.0, "B": 5.0, "C": 5.0, "D": 5.0}
    assert len(run_cycle(FakeProvider(prices), store)["buys"]) == 2
    assert run_cycle(FakeProvider(prices), store)["buys"] == []  # ya está lleno

    store = make_store(universe="A B C D", initial_cash=250, fee_pct=0)
    assert len(run_cycle(FakeProvider(prices), store)["buys"]) == 2  # solo hay efectivo para dos


def test_cycle_survives_broken_ticker_and_keeps_position(monkeypatch):
    store = make_store(universe="AAA BBB", fee_pct=0)
    patch_scores(monkeypatch, {"AAA": 10, "BBB": 10})
    run_cycle(FakeProvider({"AAA": 10.0, "BBB": 10.0}), store)
    summary = run_cycle(FakeProvider({"AAA": 10.0}), store)  # BBB deja de tener datos
    assert "BBB" in summary["errors"] and summary["sells"] == []
    assert set(store.load_account(0).positions) == {"AAA", "BBB"}


# --- ajustes -----------------------------------------------------------------
def test_settings_validation():
    s = validate_settings({"universe": "spy, qqq\nSPY", "buy_threshold_pct": "85", "max_positions": "3"})
    assert s["universe"] == ["SPY", "QQQ"] and s["buy_threshold_pct"] == 85.0 and s["max_positions"] == 3
    for bad in ({"universe": "<script>"}, {"universe": ""}, {"buy_threshold_pct": 120},
                {"position_size": "abc"}, {"stop_loss_pct": float("nan")}):
        with pytest.raises(SettingsError):
            validate_settings(bad)


# --- backtest ----------------------------------------------------------------
def test_backtest_is_consistent_and_deterministic():
    settings = validate_settings({"universe": "SPY QQQ AAPL MSFT KO XOM"})
    a = run_backtest(SyntheticProvider(), settings, years=2)
    b = run_backtest(SyntheticProvider(), settings, years=2)
    assert a["final_equity"] == b["final_equity"]
    assert a["curve"][0]["equity"] == pytest.approx(1000.0)
    assert a["curve"][-1]["equity"] == pytest.approx(a["final_equity"])
    assert a["total_return_pct"] == pytest.approx(100 * (a["final_equity"] / 1000 - 1))
    assert 0 <= a["max_drawdown_pct"] <= 100
    json.dumps(a, allow_nan=False)  # debe ser JSON válido (sin NaN)


def test_backtest_executes_at_next_open_not_same_close():
    """Una orden decidida con el cierre de D se ejecuta a la apertura de D+1."""
    settings = validate_settings({"universe": "SPY QQQ AAPL MSFT KO XOM"})
    provider = SyntheticProvider()
    res = run_backtest(provider, settings, years=2)
    buys = [t for t in res["last_trades"] if t["side"] == "BUY"]
    assert buys
    for t in buys:
        df = provider.history(t["symbol"], 3000)
        assert t["price"] == pytest.approx(df.loc[t["ts"], "Open"])


# --- API ---------------------------------------------------------------------
def test_api_flow():
    client = create_app(provider=SyntheticProvider(), db_path=":memory:").test_client()
    assert client.get("/").status_code == 200
    state = client.get("/api/state").get_json()
    assert state["account"]["equity"] == 1000.0 and state["positions"] == [] and state["provider"] == "synthetic"

    assert client.post("/api/settings", json={"universe": "SPY QQQ", "position_size": 50}).status_code == 200
    assert client.post("/api/settings", json={"buy_threshold_pct": 10}).status_code == 400

    cycle = client.post("/api/cycle").get_json()
    assert cycle["evaluated"] == 2
    state = client.get("/api/state").get_json()
    assert [s["symbol"] for s in state["signals"]] and len(state["equity"]) == 1
    assert all(len(s["results"]) == 10 for s in state["signals"])

    assert client.get("/api/backtest?years=1").status_code == 200
    assert client.get("/api/backtest?years=99").status_code == 400

    assert client.post("/api/reset").get_json() == {"ok": True}
    state = client.get("/api/state").get_json()
    assert state["trades"] == [] and state["signals"] == [] and state["last_cycle"] is None


def test_no_rebuy_on_the_day_of_a_sale(monkeypatch):
    from datetime import datetime, timezone
    store = make_store(universe="AAA", fee_pct=0)
    patch_scores(monkeypatch, {"AAA": 10})
    day1 = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)
    assert run_cycle(FakeProvider({"AAA": 10.0}), store, now=day1)["buys"] == ["AAA"]
    later = day1.replace(hour=16)
    assert len(run_cycle(FakeProvider({"AAA": 9.0}), store, now=later)["sells"]) == 1  # stop-loss
    assert run_cycle(FakeProvider({"AAA": 9.0}), store, now=later.replace(hour=17))["buys"] == []
    day2 = day1.replace(day=6)
    assert run_cycle(FakeProvider({"AAA": 9.0}), store, now=day2)["buys"] == ["AAA"]


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
            df = pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": [1.5, None, 1.7],
                               "Volume": 10, "Dividends": 0.0}, index=idx)
            return df

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
    provider = YahooProvider()
    df = provider.history("SPY", 800)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.tz is None and len(df) == 2  # sin zona horaria y sin la fila vacía
    provider.history("SPY", 800)
    assert calls == [("SPY", "1d", True)]  # la segunda llamada sale de la caché
    with pytest.raises(DataError):
        provider.history("NOPE", 800)

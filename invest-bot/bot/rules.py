"""Condiciones de compra.

Cada condición es una comprobación independiente sobre los indicadores del día.
La puntuación es el % de condiciones cumplidas; el bot solo compra si llega al
umbral configurado (90 % por defecto, es decir, 9 de 10).

Para añadir, quitar o cambiar condiciones basta con editar la lista ``RULES``.
Un dato ausente (NaN) hace que la condición cuente como NO cumplida.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

Row = Mapping[str, float]


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    check: Callable[[Row], bool]
    show: Callable[[Row], str]


RULES: list[Rule] = [
    Rule("trend_200", "Precio por encima de la media de 200 días (tendencia larga alcista)",
         lambda r: r["close"] > r["sma200"],
         lambda r: f"precio {r['close']:.2f} / media200 {r['sma200']:.2f}"),
    Rule("golden", "Media de 50 días por encima de la de 200",
         lambda r: r["sma50"] > r["sma200"],
         lambda r: f"media50 {r['sma50']:.2f} / media200 {r['sma200']:.2f}"),
    Rule("trend_50", "Precio por encima de la media de 50 días (tendencia media alcista)",
         lambda r: r["close"] > r["sma50"],
         lambda r: f"precio {r['close']:.2f} / media50 {r['sma50']:.2f}"),
    Rule("rsi", "RSI entre 45 y 68 (con fuerza, sin estar sobrecomprado)",
         lambda r: 45 <= r["rsi"] <= 68,
         lambda r: f"RSI {r['rsi']:.1f}"),
    Rule("macd", "MACD por encima de su señal",
         lambda r: r["macd"] > r["macd_signal"],
         lambda r: f"MACD {r['macd']:.3f} / señal {r['macd_signal']:.3f}"),
    Rule("macd_accel", "Impulso acelerando (histograma MACD mayor que hace 3 días)",
         lambda r: r["macd_hist"] > r["macd_hist_prev"],
         lambda r: f"hist {r['macd_hist']:.3f} / hace 3 días {r['macd_hist_prev']:.3f}"),
    Rule("volume", "Volumen sostenido (media 20 días ≥ 90 % de la de 60 días)",
         lambda r: r["vol_ratio"] >= 0.9,
         lambda r: f"ratio {r['vol_ratio']:.2f}"),
    Rule("near_high", "A menos de un 10 % del máximo de 52 semanas",
         lambda r: r["close"] >= 0.9 * r["high52"],
         lambda r: f"precio {r['close']:.2f} / máximo {r['high52']:.2f}"),
    Rule("momentum", "Rentabilidad positiva en los últimos 3 meses",
         lambda r: r["ret63"] > 0,
         lambda r: f"{r['ret63'] * 100:+.1f} %"),
    Rule("volatility", "Volatilidad anualizada por debajo del 50 %",
         lambda r: r["vol30"] <= 0.50,
         lambda r: f"{r['vol30'] * 100:.1f} %"),
]


def evaluate(row: Row, rules: list[Rule] = RULES) -> dict:
    results = []
    for rule in rules:
        try:
            passed = bool(rule.check(row))
            value = rule.show(row)
        except (KeyError, TypeError, ValueError):
            passed, value = False, "sin datos"
        results.append({"key": rule.key, "label": rule.label, "passed": passed, "value": value})
    n_passed = sum(r["passed"] for r in results)
    return {
        "score_pct": 100.0 * n_passed / len(rules),
        "passed": n_passed,
        "total": len(rules),
        "results": results,
    }

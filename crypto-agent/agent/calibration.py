"""Calibración: traduce la confianza que declara el REFLEX en una probabilidad de acierto
medida con resultados reales, y mide lo buena que es esa confianza (Brier)."""
from __future__ import annotations

EDGES = (0.5, 0.6, 0.7, 0.8, 0.9)  # inicio de cada tramo; el último llega hasta 1,0


class Calibrator:
    """Frecuencia de acierto por tramo de confianza, con un punto de partida prudente.

    El punto de partida es la probabilidad SIN ventaja, 1/(1+b): la de acertar por azar
    con un objetivo de b veces el riesgo. Mientras no haya resultados, el tamaño de Kelly
    es cero y el agente no arriesga nada.
    """

    def __init__(self, target_r: float, prior_strength: float = 20.0):
        self.target_r, self.prior_strength = target_r, prior_strength
        self.wins = [0] * len(EDGES)
        self.n = [0] * len(EDGES)
        self.count = 0
        self.sum_y = 0.0
        self.sq_raw = 0.0   # suma de (confianza declarada - resultado)^2
        self.sq_cal = 0.0   # suma de (probabilidad calibrada en el momento - resultado)^2
        self.sum_cost_r = 0.0  # suma del coste de cada señal, medido en unidades de riesgo

    @property
    def no_edge_p(self) -> float:
        return 1.0 / (1.0 + self.target_r)

    @property
    def breakeven_p(self) -> float:
        """Acierto mínimo para no perder dinero DESPUÉS de comisiones y deslizamiento.
        Los costes recortan cada ganancia y agrandan cada pérdida: (1 + c) / (1 + b)."""
        cost_r = self.sum_cost_r / self.count if self.count else 0.0
        return min(1.0, (1.0 + cost_r) / (1.0 + self.target_r))

    @staticmethod
    def bucket(confidence: float) -> int | None:
        if confidence < EDGES[0]:
            return None
        idx = 0
        for i, edge in enumerate(EDGES):
            if confidence >= edge:
                idx = i
        return idx

    def p_win(self, confidence: float) -> float:
        i = self.bucket(confidence)
        if i is None:
            return self.no_edge_p
        k = self.prior_strength
        return (self.wins[i] + k * self.no_edge_p) / (self.n[i] + k)

    def p_conservative(self, confidence: float, min_samples: int = 50, z: float = 2.0) -> float:
        """Probabilidad para decidir el TAMAÑO: exige un mínimo de muestras y resta dos
        errores típicos. Con pocas muestras un tramo puede parecer bueno por pura suerte;
        apostar sobre eso es la forma más habitual de perder dinero con un sistema nuevo."""
        i = self.bucket(confidence)
        if i is None or self.n[i] < min_samples:
            return self.no_edge_p
        p = self.p_win(confidence)
        return max(0.0, p - z * (p * (1 - p) / self.n[i]) ** 0.5)

    def add(self, confidence: float, p_at_decision: float, won: bool, cost_r: float = 0.0) -> None:
        i = self.bucket(confidence)
        if i is None:
            return
        y = 1.0 if won else 0.0
        self.sum_cost_r += cost_r
        self.wins[i] += int(won)
        self.n[i] += 1
        self.count += 1
        self.sum_y += y
        self.sq_raw += (confidence - y) ** 2
        self.sq_cal += (p_at_decision - y) ** 2

    # --- métricas -----------------------------------------------------------------
    @property
    def base_rate(self) -> float | None:
        return self.sum_y / self.count if self.count else None

    @property
    def brier_raw(self) -> float | None:
        return self.sq_raw / self.count if self.count else None

    @property
    def brier_calibrated(self) -> float | None:
        return self.sq_cal / self.count if self.count else None

    @property
    def brier_base_rate(self) -> float | None:
        """Brier de predecir siempre la tasa media: la referencia a batir."""
        if not self.count:
            return None
        p = self.sum_y / self.count
        return p * (1 - p)

    def table(self) -> list[dict]:
        rows = []
        for i, lo in enumerate(EDGES):
            hi = EDGES[i + 1] if i + 1 < len(EDGES) else 1.0
            rows.append({
                "tramo": f"{lo:.1f}-{hi:.1f}", "n": self.n[i],
                "acierto": self.wins[i] / self.n[i] if self.n[i] else None,
                "p_calibrada": self.p_win(lo),
            })
        return rows

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("target_r", "prior_strength", "wins", "n", "count", "sum_y", "sq_raw", "sq_cal", "sum_cost_r")}

    @classmethod
    def from_dict(cls, d: dict) -> "Calibrator":
        cal = cls(d["target_r"], d["prior_strength"])
        for k in ("wins", "n", "count", "sum_y", "sq_raw", "sq_cal"):
            setattr(cal, k, d[k])
        cal.sum_cost_r = d.get("sum_cost_r", 0.0)
        return cal

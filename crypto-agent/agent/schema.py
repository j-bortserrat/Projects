"""Esquema tipado que evalúa el REFLEX en cada vela, y ficha de cada candidato."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

Regime = Literal["trending", "mean_reverting", "high_vol", "crisis"]
Direction = Literal["long", "short", "neutral"]
RiskState = Literal["safe", "near_limit", "reduce"]
RISK_ORDER = {"safe": 0, "near_limit": 1, "reduce": 2}


class ReflexDecision(BaseModel):
    """Lo único que devuelve el REFLEX: un juicio. Tamaños, órdenes y umbrales son del código.

    Los rangos numéricos se validan en `problems()` y no en el esquema, porque las salidas
    estructuradas no garantizan mínimos y máximos.
    """
    regime: Regime
    direction: Direction
    direction_confidence: float       # 0..1
    toxic_flow: Optional[bool]        # None si no hay libro de órdenes que mirar
    setup_quality: int                # 0..3
    risk_state: RiskState

    def problems(self) -> list[str]:
        out = []
        if not (0.0 <= self.direction_confidence <= 1.0):
            out.append(f"direction_confidence fuera de rango: {self.direction_confidence}")
        if self.setup_quality not in (0, 1, 2, 3):
            out.append(f"setup_quality fuera de rango: {self.setup_quality}")
        return out


class Source(BaseModel):
    title: str
    url: str
    date: str  # AAAA-MM-DD de la fuente


class Setup(BaseModel):
    """Ficha de un candidato. La rellena la capa de investigación; sin tesis, el ticker
    se opera solo por liquidez y así se etiqueta."""
    symbol: str
    thesis: Optional[str] = None
    catalysts: list[str] = []
    bear_case: Optional[str] = None
    invalidation_price: Optional[float] = None  # por debajo de este precio el candidato se desactiva
    reviewed_on: Optional[str] = None
    sources: list[Source] = []


def worse_risk_state(a: RiskState, b: RiskState) -> RiskState:
    return a if RISK_ORDER[a] >= RISK_ORDER[b] else b

"""Ajustes del agente. Los límites de riesgo viven aquí, en código y con topes duros:
ningún modelo puede cambiarlos y el fichero de configuración no puede superarlos."""
from __future__ import annotations

import os
import tomllib
from typing import Literal

from pydantic import BaseModel, Field

TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


class ExchangeCfg(BaseModel):
    id: str = "binance"
    market: Literal["spot"] = "spot"      # los perpetuos no están implementados
    timeframe: Literal["15m", "1h", "4h", "1d"] = "1h"
    public_api_url: str = ""              # vacío = el de ccxt; útil si tu red bloquea api.binance.com


class UniverseCfg(BaseModel):
    symbols: list[str] = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "LINK/USDT"]
    context_symbol: str = "BTC/USDT"      # referencia de mercado para todos los demás
    off_limits: list[str] = ["DOGE/USDT", "SHIB/USDT", "PEPE/USDT", "BONK/USDT", "WIF/USDT", "FLOKI/USDT"]
    min_quote_volume_24h: float = 20_000_000.0


class PaperCfg(BaseModel):
    balance: float = Field(1000.0, gt=0)
    fee_bps: float = Field(10.0, ge=0, le=100)        # 0,10 % por operación
    slippage_bps: float = Field(5.0, ge=0, le=200)    # deslizamiento adverso
    max_participation: float = Field(0.01, gt=0, le=0.2)  # máx. fracción del volumen de la vela


class RiskCfg(BaseModel):
    """Límites duros. Los `le=` son topes que ni la configuración puede rebasar."""
    risk_per_trade_pct: float = Field(2.0, gt=0, le=5)
    max_daily_loss_pct: float = Field(5.0, gt=0, le=20)
    max_open_positions: int = Field(3, ge=1, le=10)
    max_leverage: float = Field(2.0, ge=1, le=3)       # en spot el tope efectivo es 1x
    kill_switch_drawdown_pct: float = Field(15.0, gt=0, le=50)
    pause_below_confidence: float = Field(0.60, ge=0, le=1)
    min_order_quote: float = Field(10.0, gt=0)         # importe mínimo de una orden


class PolicyCfg(BaseModel):
    """Umbrales ajustables por el ciclo de revisión nocturna (siempre con tu aprobación).
    Son hipótesis de partida, no evidencia de que exista una ventaja."""
    min_setup_quality: int = Field(2, ge=0, le=3)
    min_direction_confidence: float = Field(0.80, ge=0.5, le=1.01)
    kelly_fraction: float = Field(0.25, gt=0, le=0.25)  # como mucho un cuarto de Kelly
    stop_atr: float = Field(2.0, gt=0, le=10)           # stop a N veces el ATR
    target_r: float = Field(2.0, gt=0, le=10)           # objetivo a N veces el riesgo
    max_hold_candles: int = Field(48, ge=1, le=2000)


class ReflexCfg(BaseModel):
    provider: Literal["auto", "rules", "haiku"] = "auto"  # auto: haiku si hay clave, si no reglas
    model: str = "claude-haiku-5-5"
    effort: Literal["low", "medium", "high"] = "low"
    timeout_s: float = Field(20.0, gt=0, le=120)


class Settings(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    data_dir: str = "data"
    exchange: ExchangeCfg = ExchangeCfg()
    universe: UniverseCfg = UniverseCfg()
    paper: PaperCfg = PaperCfg()
    risk: RiskCfg = RiskCfg()
    policy: PolicyCfg = PolicyCfg()
    reflex: ReflexCfg = ReflexCfg()

    @property
    def tf_ms(self) -> int:
        return TF_MS[self.exchange.timeframe]

    @property
    def tradable(self) -> list[str]:
        banned = set(self.universe.off_limits)
        return [s for s in self.universe.symbols if s not in banned]


def load_settings(path: str | None = None) -> Settings:
    """Lee `config.toml` si existe; lo que falte toma el valor por defecto."""
    path = path or os.environ.get("AGENT_CONFIG", "config.toml")
    if not os.path.isfile(path):   # sin fichero (o si Docker creó una carpeta en su lugar): valores por defecto
        return Settings()
    with open(path, "rb") as fh:
        return Settings.model_validate(tomllib.load(fh))

"""Configuration loading. Env vars for secrets, YAML for strategy parameters."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, PositiveFloat, PositiveInt


class Secrets(BaseModel):
    private_key: str
    funder: str
    signature_type: int = 1
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    log_level: str = "INFO"


class ScannerCfg(BaseModel):
    min_liquidity_usd: float = 5000
    min_volume_24h_usd: float = 2000
    rewards_only: bool = True
    max_concurrent_markets: PositiveInt = 25
    refresh_interval_sec: PositiveInt = 60
    exclude_closing_within_hours: float = 2.0


class RiskCfg(BaseModel):
    max_notional_usd: PositiveFloat = 100.0
    max_per_market_usd: PositiveFloat = 20.0
    max_daily_loss_usd: PositiveFloat = 20.0
    max_inventory_skew: PositiveFloat = 200.0
    min_order_usd: PositiveFloat = 5.0


class MarketMakerCfg(BaseModel):
    enabled: bool = True
    target_spread_bps: PositiveFloat = 50
    min_edge_over_mid_bps: PositiveFloat = 10
    requote_drift_bps: PositiveFloat = 25
    requote_interval_sec: PositiveInt = 30
    quote_size_usd: PositiveFloat = 10.0
    join_if_wider_bps: PositiveFloat = 30
    tick_size: float = 0.001


class ArbitrageCfg(BaseModel):
    enabled: bool = True
    min_profit_bps: PositiveFloat = 300
    max_size_per_arb_usd: PositiveFloat = 25.0
    taker_fee_bps: PositiveFloat = 200


class MeanReversionCfg(BaseModel):
    enabled: bool = False
    window_sec: PositiveInt = 300
    zscore_trigger: PositiveFloat = 1.5
    position_size_usd: PositiveFloat = 5.0
    max_hold_sec: PositiveInt = 1800


class CrossMarketArbCfg(BaseModel):
    enabled: bool = False


class LoopCfg(BaseModel):
    tick_interval_sec: PositiveInt = 2
    http_timeout_sec: PositiveInt = 10
    retry_max_attempts: PositiveInt = 5


class Config(BaseModel):
    scanner: ScannerCfg = Field(default_factory=ScannerCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    market_maker: MarketMakerCfg = Field(default_factory=MarketMakerCfg)
    arbitrage: ArbitrageCfg = Field(default_factory=ArbitrageCfg)
    mean_reversion: MeanReversionCfg = Field(default_factory=MeanReversionCfg)
    cross_market_arb: CrossMarketArbCfg = Field(default_factory=CrossMarketArbCfg)
    loop: LoopCfg = Field(default_factory=LoopCfg)


def load_secrets() -> Secrets:
    load_dotenv()
    pk = os.getenv("PK_PRIVATE_KEY", "").strip()
    funder = os.getenv("PK_FUNDER", "").strip()
    if not pk or not funder:
        raise RuntimeError(
            "PK_PRIVATE_KEY and PK_FUNDER must be set in the environment "
            "(see .env.example)."
        )
    return Secrets(
        private_key=pk,
        funder=funder,
        signature_type=int(os.getenv("PK_SIGNATURE_TYPE", "1")),
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
        gamma_host=os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


def load_config(path: Optional[str] = None) -> Config:
    path = path or os.getenv("POLYBOT_CONFIG", "config.yaml")
    p = Path(path)
    if not p.exists():
        return Config()
    with p.open() as f:
        raw = yaml.safe_load(f) or {}
    return Config(**raw)

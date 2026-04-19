"""Configuration loading. Env vars for secrets, YAML for strategy parameters."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, PositiveFloat, PositiveInt


class Secrets(BaseModel):
    private_key: str
    funder: str
    signature_type: int = 1
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
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
    max_drawdown_from_peak_usd: PositiveFloat = 30.0
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
    # Inventory-aware (Avellaneda-Stoikov) pricing.
    use_inventory_skew: bool = True
    inventory_risk_aversion: float = 0.05  # γ
    max_skew_shift: float = 0.02  # hard cap on reservation-price shift
    # Adaptive-spread (widen when σ is high).
    use_adaptive_spread: bool = True
    sigma_to_spread_multiplier: float = 8.0


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


class BreakerCfg(BaseModel):
    enabled: bool = True
    max_errors_per_minute: PositiveInt = 30
    max_feed_age_sec: PositiveFloat = 120.0
    crash_bps_per_min: PositiveFloat = 500.0
    crash_market_share: float = 0.3
    max_api_failure_rate: float = 0.5
    cooldown_sec: PositiveInt = 60


class VolatilityCfg(BaseModel):
    window_sec: PositiveInt = 300
    min_samples: PositiveInt = 8


class ResearchCfg(BaseModel):
    enabled: bool = True
    interval_hours: PositiveFloat = 6.0
    leaderboard_window: str = "7d"  # 24h | 7d | 30d | all
    top_wallets: PositiveInt = 50
    report_dir: str = "state/research"
    auto_apply: bool = False  # if True the tuner writes config.yaml changes


class CopyTradingCfg(BaseModel):
    enabled: bool = False
    max_wallets_to_follow: PositiveInt = 5
    mirror_size_usd: PositiveFloat = 5.0
    min_wallet_pnl_usd: PositiveFloat = 10000.0
    allow_markets: List[str] = Field(default_factory=list)  # empty = any
    require_same_side: bool = True


class TunerCfg(BaseModel):
    enabled: bool = False
    evaluation_window_hours: PositiveFloat = 24.0
    target_fill_rate: float = 0.3
    # How aggressively to move parameters each evaluation (0..1).
    learning_rate: float = 0.2


class LadderCfg(BaseModel):
    enabled: bool = True
    levels: PositiveInt = 3
    step_bps: PositiveFloat = 20
    size_decay: float = 0.7  # each subsequent level carries this fraction


class AllocatorCfg(BaseModel):
    enabled: bool = True
    min_per_market_usd: PositiveFloat = 5.0


class HistoryCfg(BaseModel):
    enabled: bool = True
    snapshot_interval_sec: PositiveFloat = 15.0
    retention_hours: PositiveFloat = 72.0
    db_path: str = "state/book_history.sqlite"


class ReconcilerCfg(BaseModel):
    enabled: bool = True
    interval_sec: PositiveFloat = 120.0
    auto_correct: bool = True


class HealthCfg(BaseModel):
    enabled: bool = True
    port: PositiveInt = 8080
    host: str = "0.0.0.0"
    max_tick_age_sec: PositiveFloat = 30.0


class WebSocketCfg(BaseModel):
    enabled: bool = True
    url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ping_interval_sec: PositiveFloat = 10.0
    max_staleness_sec: PositiveFloat = 15.0  # fall back to REST beyond this


class ObservabilityCfg(BaseModel):
    prometheus_enabled: bool = True
    prometheus_port: PositiveInt = 9464
    alert_webhook_url: Optional[str] = None
    json_logs: bool = False


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
    breaker: BreakerCfg = Field(default_factory=BreakerCfg)
    volatility: VolatilityCfg = Field(default_factory=VolatilityCfg)
    research: ResearchCfg = Field(default_factory=ResearchCfg)
    copy_trading: CopyTradingCfg = Field(default_factory=CopyTradingCfg)
    tuner: TunerCfg = Field(default_factory=TunerCfg)
    ladder: LadderCfg = Field(default_factory=LadderCfg)
    allocator: AllocatorCfg = Field(default_factory=AllocatorCfg)
    history: HistoryCfg = Field(default_factory=HistoryCfg)
    reconciler: ReconcilerCfg = Field(default_factory=ReconcilerCfg)
    health: HealthCfg = Field(default_factory=HealthCfg)
    websocket: WebSocketCfg = Field(default_factory=WebSocketCfg)
    observability: ObservabilityCfg = Field(default_factory=ObservabilityCfg)
    loop: LoopCfg = Field(default_factory=LoopCfg)


def load_secrets(require_wallet: bool = True) -> Secrets:
    load_dotenv()
    pk = os.getenv("PK_PRIVATE_KEY", "").strip()
    funder = os.getenv("PK_FUNDER", "").strip()
    if require_wallet and (not pk or not funder):
        raise RuntimeError(
            "PK_PRIVATE_KEY and PK_FUNDER must be set in the environment "
            "(see .env.example)."
        )
    return Secrets(
        private_key=pk or "0x" + "0" * 64,
        funder=funder or "0x" + "0" * 40,
        signature_type=int(os.getenv("PK_SIGNATURE_TYPE", "1")),
        clob_host=os.getenv("CLOB_HOST", "https://clob.polymarket.com"),
        gamma_host=os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com"),
        data_host=os.getenv("DATA_HOST", "https://data-api.polymarket.com"),
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


def write_config(cfg: Config, path: Optional[str] = None) -> None:
    path = path or os.getenv("POLYBOT_CONFIG", "config.yaml")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as f:
        yaml.safe_dump(cfg.model_dump(mode="json"), f, sort_keys=False)

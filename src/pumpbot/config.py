"""Typed settings: config.yaml (tunables) + .env (secrets) -> Settings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class WalletConfig(BaseModel):
    min_balance_sol: float
    max_balance_sol: float


class TradingConfig(BaseModel):
    position_sol: float
    max_concurrent_positions: int
    v2_features_unlock_balance_sol: float
    slippage_tolerance_fraction: float


class FeesConfig(BaseModel):
    max_fee_fraction: float
    max_fee_absolute_sol: float
    priority_fee_ceiling_sol: float
    close_fee_reserve_sol: float


class ExitsConfig(BaseModel):
    take_profit_1_multiple: float
    take_profit_1_fraction: float
    take_profit_2_multiple: float
    stop_loss_fraction: float
    timeout_seconds: int


class Tier1FilterConfig(BaseModel):
    max_creator_supply_fraction: float
    max_mints_per_second: int
    curve_completion_guard_fraction: float


class Tier2FilterConfig(BaseModel):
    enabled: bool
    mode: str
    min_socials: int
    fetch_timeout_seconds: float
    fail_closed: bool
    cache_max_entries: int

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v: str) -> str:
        if v not in ("enforce", "observe"):
            raise ValueError(f"filters.tier2.mode must be 'enforce' or 'observe', got {v!r}")
        return v


class FiltersConfig(BaseModel):
    tier1: Tier1FilterConfig
    tier2: Tier2FilterConfig
    creator_blocklist_path: str
    name_symbol_blocklist_path: str


class RpcConfig(BaseModel):
    rps_limit: int
    credit_costs: dict[str, int]
    daily_credit_halt: int
    max_retries: int
    backoff_base_seconds: float


class ReconciliationConfig(BaseModel):
    interval_seconds: int


class HeartbeatConfig(BaseModel):
    interval_seconds: int
    idle_alarm_heartbeats: int


class ExecutionConfig(BaseModel):
    skip_preflight: bool
    confirm_poll_interval_seconds: float
    confirm_timeout_seconds: float
    max_resubmit_attempts: int
    ata_close_max_retries: int
    jito_bundle_enabled: bool


class FailsafeConfig(BaseModel):
    consecutive_failure_limit: int


class ProbeConfig(BaseModel):
    default_hours: float


class AppConfig(BaseModel):
    """Everything loaded from config.yaml."""

    wallet: WalletConfig
    trading: TradingConfig
    fees: FeesConfig
    exits: ExitsConfig
    filters: FiltersConfig
    rpc: RpcConfig
    reconciliation: ReconciliationConfig
    heartbeat: HeartbeatConfig
    execution: ExecutionConfig
    failsafe: FailsafeConfig
    probe: ProbeConfig


class Secrets(BaseSettings):
    """Everything loaded from .env / the real environment."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    quicknode_http_url: str
    quicknode_wss_url: str
    wallet_keypair_path: str = "./wallet.json"
    dry_run: bool = True


class Settings:
    """Combined view: config.yaml tunables + .env secrets."""

    def __init__(self, config: AppConfig, secrets: Secrets) -> None:
        self.config = config
        self.secrets = secrets

    @classmethod
    def load(cls, config_path: Path | None = None) -> "Settings":
        path = config_path or (PROJECT_ROOT / "config.yaml")
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(config=AppConfig.model_validate(raw), secrets=Secrets())


def load_settings(config_path: Path | None = None) -> Settings:
    return Settings.load(config_path)

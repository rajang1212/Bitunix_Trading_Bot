from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass
class TradingConfig:
    """Legacy DAY agent configuration kept for database/settings compatibility. New DAY entries are disabled in v1.8."""
    version: str = "1.8.3"
    enabled: bool = False
    universe_size: int = 20
    min_quote_volume_usdt: float = 25_000_000.0
    leverage: int = 5
    paper_margin_usdt: float = 500.0
    max_positions: int = 5
    max_same_direction_positions: int = 4
    correlation_lookback_15m: int = 32
    high_correlation_threshold: float = 0.82
    max_highly_correlated_positions: int = 2
    max_open_risk_usdt: float = 500.0
    session_loss_limit_usdt: float = 500.0
    max_trades_per_day: int = 20
    consecutive_loss_pause_count: int = 3
    consecutive_loss_pause_minutes: int = 120
    symbol_cooldown_minutes: int = 60

    max_hold_minutes: int = 1425
    scan_interval_minutes: int = 15
    monitor_seconds: int = 5
    max_spread_pct: float = 0.15
    btc_shock_atr_15m_pct: float = 1.80
    btc_shock_range_atr_multiple: float = 3.50

    minimum_rule_score: float = 65.0
    minimum_stop_pct: float = 0.70
    maximum_stop_pct: float = 5.00
    structural_atr_buffer: float = 0.35
    minimum_net_rr: float = 1.50
    planning_target_r: float = 2.50
    partial_take_profit_r: float = 1.50
    partial_fraction: float = 0.50
    runner_lock_r_after_partial: float = 0.25
    stagnation_minutes: int = 360
    stagnation_mfe_r: float = 0.40
    thesis_failure_score: float = 52.0
    thesis_exit_min_minutes: int = 45
    thesis_flip_score_gap: float = 12.0
    breadth_fast_change: float = 0.15
    breadth_hard_change: float = 0.20
    breadth_entry_penalty_points: float = 8.0

    taker_fee_per_side_pct: float = 0.06
    modeled_roundtrip_slippage_pct: float = 0.04


@dataclass
class SwingTradingConfig:
    enabled: bool = True
    paper_margin_usdt: float = 1000.0
    leverage: int = 5
    leverage_cap: float = 5.0  # fixed leverage for new SWING trades
    max_risk_per_trade_usdt: float = 100.0
    max_positions: int = 7
    max_same_direction_positions: int = 6
    max_open_risk_usdt: float = 700.0
    max_trades_per_day: int = 10
    max_new_positions_per_scan: int = 1
    symbol_cooldown_minutes: int = 720

    scan_interval_minutes: int = 60
    max_hold_minutes: int = 10080  # 7 days
    max_spread_pct: float = 0.20
    minimum_rule_score: float = 65.0
    minimum_stop_pct: float = 1.50
    maximum_stop_pct: float = 8.00
    structural_atr_buffer: float = 0.40
    minimum_net_rr: float = 1.60
    planning_target_r: float = 3.00
    partial_take_profit_r: float = 2.00
    partial_fraction: float = 0.50
    runner_lock_r_after_partial: float = 0.50
    thesis_exit_min_minutes: int = 720  # 12h; avoid reacting to ordinary intraday noise
    stagnation_minutes: int = 2880      # 48h
    stagnation_mfe_r: float = 0.35
    thesis_failure_score: float = 48.0
    thesis_flip_score_gap: float = 15.0

    # Correction detector retained for shadow research; it has no trade authority in v1.8.2.
    correction_breadth_1h_max: float = 0.45
    correction_breadth_4h_max: float = 0.50
    correction_breadth_change_1h_max: float = -0.10
    correction_short_parent_points: float = 25.0
    correction_exit_min_minutes: int = 240
    consecutive_direction_loss_pause_count: int = 3
    consecutive_direction_loss_pause_minutes: int = 360
    second_loss_risk_multiplier: float = 0.50
    recovery_same_direction_cap: int = 1
    correction_shadow_only: bool = True

    taker_fee_per_side_pct: float = 0.06
    modeled_roundtrip_slippage_pct: float = 0.06


@dataclass
class GovernorConfig:
    max_day_positions: int = 5
    max_swing_positions: int = 7
    max_total_positions: int = 12
    max_total_same_direction_positions: int = 11
    max_total_open_risk_usdt: float = 1000.0
    daily_realized_loss_limit_usdt: float = 350.0
    correlation_lookback_15m: int = 32
    high_correlation_threshold: float = 0.82
    max_highly_correlated_same_direction: int = 2
    one_position_per_symbol: bool = True


@dataclass
class CopyTradingConfig:
    enabled: bool = True
    paper_only: bool = True
    chain: str = "sol"
    paper_trade_usdt: float = 500.0
    max_positions: int = 6
    source_wallets: list[str] = field(default_factory=lambda: [
        "HmBmSYwYEgEZuBUYuDs9xofyqBAkw4ywugB1d7R7sTGh",
        "78N177fzNJpp8pG49xDv1efYcTMSzo9tPTKEA9mAVkh2",
        "Bi4rd5FH5bYEN8scZ7wevxNZyNmKHdaBcvewdPFxYdLt",
    ])


@dataclass
class CapitalConfig:
    """v1.8 paper-account defaults. DAY is retired; its former allocation starts in RESERVE."""
    day_initial_usdt: float = 0.0
    swing_initial_usdt: float = 5000.0
    reserve_initial_usdt: float = 7500.0
    copy_initial_usdt: float = 3000.0
    copy_max_allocation_usdt: float = 3000.0
    swing_max_margin_usdt: float = 1500.0

@dataclass
class LearningConfig:
    enabled: bool = True
    daily_test_hour_local: int = 21
    daily_test_minute_local: int = 0
    label_horizon_minutes: int = 1440
    label_target_r: float = 2.0
    label_stop_r: float = 1.0
    min_bars_for_label: int = 80
    train_fraction: float = 0.80
    purge_minutes: int = 1440
    min_train_examples: int = 400
    min_test_examples: int = 100
    mlp_hidden_layers: list[int] = field(default_factory=lambda: [96, 48])
    mlp_alpha: float = 0.001
    mlp_max_iter: int = 400
    mlp_batch_size: int = 128
    mlp_learning_rate_init: float = 0.001
    mlp_early_stopping: bool = True
    logistic_c: float = 0.5
    hgb_learning_rate: float = 0.05
    hgb_max_iter: int = 250
    hgb_max_leaf_nodes: int = 15
    random_state: int = 42
    shadow_predictions: bool = True


@dataclass
class SwingLearningConfig:
    enabled: bool = True
    label_horizon_minutes: int = 10080  # 7 days
    label_target_r: float = 3.0
    label_stop_r: float = 1.0
    min_bars_for_label: int = 150       # 1h bars across most of the 7-day window
    train_fraction: float = 0.80
    purge_minutes: int = 10080
    min_train_examples: int = 300
    min_test_examples: int = 80
    mlp_hidden_layers: list[int] = field(default_factory=lambda: [96, 48])
    mlp_alpha: float = 0.001
    mlp_max_iter: int = 400
    mlp_batch_size: int = 128
    mlp_learning_rate_init: float = 0.001
    mlp_early_stopping: bool = True
    logistic_c: float = 0.5
    hgb_learning_rate: float = 0.05
    hgb_max_iter: int = 250
    hgb_max_leaf_nodes: int = 15
    random_state: int = 42
    shadow_predictions: bool = True


@dataclass
class IntelligenceConfig:
    enabled: bool = True
    research_interval_minutes: int = 5
    lookback_hours: int = 12
    gdelt_max_records: int = 50
    max_governor_adjustment_points: float = 8.0
    max_market_adjustment_points: float = 2.0
    stale_after_minutes: int = 30
    critical_block_enabled: bool = True
    critical_min_impact: float = 80.0
    critical_min_confidence: float = 78.0


@dataclass
class AppConfig:
    trading: TradingConfig = field(default_factory=TradingConfig)
    swing: SwingTradingConfig = field(default_factory=SwingTradingConfig)
    copy: CopyTradingConfig = field(default_factory=CopyTradingConfig)
    governor: GovernorConfig = field(default_factory=GovernorConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    swing_learning: SwingLearningConfig = field(default_factory=SwingLearningConfig)
    intelligence: IntelligenceConfig = field(default_factory=IntelligenceConfig)
    capital: CapitalConfig = field(default_factory=CapitalConfig)

    def as_dict(self) -> dict[str, Any]:
        return {
            "trading": asdict(self.trading),
            "swing": asdict(self.swing),
            "copy": asdict(self.copy),
            "governor": asdict(self.governor),
            "learning": asdict(self.learning),
            "swing_learning": asdict(self.swing_learning),
            "intelligence": asdict(self.intelligence),
            "capital": asdict(self.capital),
        }


def _merge_dataclass(cls, values: dict[str, Any]):
    allowed = set(cls.__dataclass_fields__.keys())
    clean = {k: v for k, v in values.items() if k in allowed}
    return cls(**clean)


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        cfg = AppConfig()
        save_config(path, cfg)
        return cfg
    with path.open("r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("settings.json must contain an object")

    raw_trading = raw.get("trading", {}) or {}
    old_version = str(raw_trading.get("version") or "")
    trading = _merge_dataclass(TradingConfig, raw_trading)
    if old_version.startswith("1.1") and float(raw_trading.get("partial_take_profit_r", 2.0) or 2.0) == 2.0:
        trading.partial_take_profit_r = 1.50
    # v1.3 splits the former 10-slot pool into five DAY and five SWING slots.
    # Existing custom entry/risk settings are retained; only the legacy capacity is migrated.
    if old_version.startswith(("1.0", "1.1", "1.2")) and int(raw_trading.get("max_positions", 10) or 10) == 10:
        trading.max_positions = 5
    # v1.4 keeps strategy scans on completed candles but monitors open paper positions every ~5 seconds.
    if old_version.startswith(("1.0", "1.1", "1.2", "1.3")) and int(raw_trading.get("monitor_seconds", 45) or 45) == 45:
        trading.monitor_seconds = 5
    trading.version = TradingConfig().version

    raw_swing = raw.get("swing", {}) or {}
    swing = _merge_dataclass(SwingTradingConfig, raw_swing)
    # v1.7 expands SWING capacity to seven slots. The same-direction cap defaults to six and can be set to seven.
    if old_version.startswith(("1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6")):
        if int(raw_swing.get("max_positions", 5) or 5) == 5:
            swing.max_positions = 7
        if int(raw_swing.get("max_same_direction_positions", 4) or 4) == 4:
            swing.max_same_direction_positions = 6
        if float(raw_swing.get("max_open_risk_usdt", 500) or 500) == 500:
            swing.max_open_risk_usdt = 700.0
        if int(raw_swing.get("max_trades_per_day", 6) or 6) == 6:
            swing.max_trades_per_day = 10
    swing.max_same_direction_positions = max(3, min(7, int(swing.max_same_direction_positions)))
    swing.leverage = 5
    swing.leverage_cap = 5.0

    governor = _merge_dataclass(GovernorConfig, raw.get("governor", {}) or {})
    # v1.8.2 tightens the paper validation loss brake. Preserve custom values; migrate only the old default.
    if old_version == "1.8.1" and float((raw.get("governor", {}) or {}).get("daily_realized_loss_limit_usdt", 1000.0) or 1000.0) == 1000.0:
        governor.daily_realized_loss_limit_usdt = 350.0
    if old_version.startswith(("1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6")):
        if int((raw.get("governor", {}) or {}).get("max_swing_positions", 5) or 5) == 5:
            governor.max_swing_positions = 7
        if int((raw.get("governor", {}) or {}).get("max_total_positions", 10) or 10) == 10:
            governor.max_total_positions = 12
        if int((raw.get("governor", {}) or {}).get("max_total_same_direction_positions", 6) or 6) == 6:
            governor.max_total_same_direction_positions = 11

    capital = _merge_dataclass(CapitalConfig, raw.get("capital", {}) or {})
    if old_version and not old_version.startswith("1.8"):
        raw_capital = raw.get("capital", {}) or {}
        if float(raw_capital.get("day_initial_usdt", 2500.0) or 0.0) == 2500.0 and float(raw_capital.get("reserve_initial_usdt", 5000.0) or 0.0) == 5000.0:
            capital.day_initial_usdt = 0.0
            capital.reserve_initial_usdt = 7500.0

    raw_copy = raw.get("copy", {}) or {}
    copy_cfg = _merge_dataclass(CopyTradingConfig, raw_copy)
    # v1.8.1 switches the COPY paper pilot to Solana and seeds the three user-approved source wallets.
    # Only migrate an untouched v1.8.0 COPY block; preserve any custom chain/wallet configuration.
    if old_version == "1.8.0" and str(raw_copy.get("chain") or "eth").lower() == "eth" and not (raw_copy.get("source_wallets") or []):
        copy_cfg = CopyTradingConfig()

    cfg = AppConfig(
        trading=trading,
        swing=swing,
        copy=copy_cfg,
        governor=governor,
        learning=_merge_dataclass(LearningConfig, raw.get("learning", {}) or {}),
        swing_learning=_merge_dataclass(SwingLearningConfig, raw.get("swing_learning", {}) or {}),
        intelligence=_merge_dataclass(IntelligenceConfig, raw.get("intelligence", {}) or {}),
        capital=capital,
    )
    save_config(path, cfg)
    return cfg


def save_config(path: Path, cfg: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(cfg.as_dict(), f, indent=2, sort_keys=True)
        f.write("\n")

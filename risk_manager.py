"""
Position sizing, risk rules, global pause logic, and Kraken order minimums.
All monetary calculations use Decimal to avoid floating-point errors.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Optional

from loguru import logger

# ── Kraken minimum order sizes ────────────────────────────────────────────────
MIN_ORDER_SIZE: dict[str, Decimal] = {
    "BTC":  Decimal("0.0001"),
    "ETH":  Decimal("0.01"),
    "SOL":  Decimal("0.5"),
    "XRP":  Decimal("10"),
    "ADA":  Decimal("5"),
    "AVAX": Decimal("0.1"),
    "DOT":  Decimal("0.5"),
    "LINK": Decimal("0.2"),
    "LTC":  Decimal("0.05"),
    "ATOM": Decimal("0.5"),
}

ASSET_DECIMALS: dict[str, int] = {
    "BTC":  6,
    "ETH":  5,
    "SOL":  4,
    "XRP":  2,
    "ADA":  2,
    "AVAX": 4,
    "DOT":  4,
    "LINK": 4,
    "LTC":  5,
    "ATOM": 4,
}

# Must sum to 1.0 — tier-weighted by market cap
ALLOCATIONS: dict[str, Decimal] = {
    "BTC":  Decimal("0.20"),
    "ETH":  Decimal("0.18"),
    "SOL":  Decimal("0.12"),
    "XRP":  Decimal("0.08"),
    "ADA":  Decimal("0.08"),
    "AVAX": Decimal("0.08"),
    "DOT":  Decimal("0.08"),
    "LINK": Decimal("0.07"),
    "LTC":  Decimal("0.06"),
    "ATOM": Decimal("0.05"),
}

PAUSE_FILE = Path(".pause_until")


# ── Strategy configuration ────────────────────────────────────────────────────

@dataclass(frozen=True)
class StrategyConfig:
    name: str
    # Signal thresholds
    rsi_buy_low: int
    rsi_buy_high: int
    rsi_sell: int
    ema_ref: str                        # "ema20" | "ema50"
    volume_multiplier: float
    # Trend filters
    adx_min: int                        # minimum ADX to confirm trend (0 = disabled)
    use_ema200_filter: bool             # only long when close > EMA200
    # Risk/reward
    sl_pct: Decimal                     # fallback SL if ATR unavailable
    atr_sl_multiplier: Decimal          # SL = entry - multiplier * ATR(14)
    tp1_pct: Decimal
    tp1_close_pct: Decimal
    tp2_pct: Decimal
    tp2_close_pct: Decimal
    tp3_pct: Optional[Decimal]
    tp3_close_pct: Optional[Decimal]
    # Exit improvements
    trailing_stop_pct: Decimal          # trail SL at X% below high after TP1
    max_position_hours: int             # close flat positions after N hours (0 = disabled)
    flat_threshold_pct: Decimal         # |pnl| < this % = "flat" for time exit
    # Behaviour
    cooldown_hours: int
    max_consecutive_losses: int
    max_open_positions: int             # global concurrent position limit
    size_multiplier: Decimal


CONSERVATIVE = StrategyConfig(
    name="conservative",
    rsi_buy_low=35,
    rsi_buy_high=50,
    rsi_sell=72,
    ema_ref="ema50",
    volume_multiplier=1.3,
    adx_min=20,
    use_ema200_filter=True,
    sl_pct=Decimal("0.03"),
    atr_sl_multiplier=Decimal("2.0"),
    tp1_pct=Decimal("0.05"),
    tp1_close_pct=Decimal("0.50"),
    tp2_pct=Decimal("0.10"),
    tp2_close_pct=Decimal("0.30"),
    tp3_pct=None,
    tp3_close_pct=None,
    trailing_stop_pct=Decimal("0.03"),
    max_position_hours=96,
    flat_threshold_pct=Decimal("0.005"),
    cooldown_hours=4,
    max_consecutive_losses=2,
    max_open_positions=4,
    size_multiplier=Decimal("1.0"),
)

AGGRESSIVE = StrategyConfig(
    name="aggressive",
    rsi_buy_low=30,
    rsi_buy_high=58,
    rsi_sell=78,
    ema_ref="ema20",
    volume_multiplier=1.1,
    adx_min=15,
    use_ema200_filter=False,
    sl_pct=Decimal("0.015"),
    atr_sl_multiplier=Decimal("1.5"),
    tp1_pct=Decimal("0.03"),
    tp1_close_pct=Decimal("0.40"),
    tp2_pct=Decimal("0.07"),
    tp2_close_pct=Decimal("0.35"),
    tp3_pct=Decimal("0.15"),
    tp3_close_pct=Decimal("0.25"),
    trailing_stop_pct=Decimal("0.02"),
    max_position_hours=48,
    flat_threshold_pct=Decimal("0.005"),
    cooldown_hours=1,
    max_consecutive_losses=3,
    max_open_positions=6,
    size_multiplier=Decimal("1.25"),
)

_CONFIGS = {"conservative": CONSERVATIVE, "aggressive": AGGRESSIVE}


def get_strategy_config() -> StrategyConfig:
    name = os.getenv("STRATEGIA", "conservative").lower().strip()
    cfg = _CONFIGS.get(name)
    if cfg is None:
        logger.warning(f"Unknown STRATEGIA='{name}', falling back to conservative")
        return CONSERVATIVE
    return cfg


# ── Global pause ──────────────────────────────────────────────────────────────

def set_global_pause(hours: int = 24) -> datetime:
    resume_at = datetime.now(timezone.utc) + timedelta(hours=hours)
    PAUSE_FILE.write_text(resume_at.isoformat(), encoding="utf-8")
    logger.warning(f"Global pause activated until {resume_at.isoformat()}")
    return resume_at


def is_globally_paused() -> bool:
    if not PAUSE_FILE.exists():
        return False
    try:
        resume_at = datetime.fromisoformat(PAUSE_FILE.read_text().strip())
        if resume_at.tzinfo is None:
            resume_at = resume_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < resume_at:
            return True
        PAUSE_FILE.unlink(missing_ok=True)
        return False
    except Exception:
        return False


def get_pause_resume_time() -> Optional[datetime]:
    if not PAUSE_FILE.exists():
        return None
    try:
        return datetime.fromisoformat(PAUSE_FILE.read_text().strip())
    except Exception:
        return None


# ── Position sizing ───────────────────────────────────────────────────────────

def calculate_position_size(
    asset: str,
    price: Decimal,
    capital: Decimal,
    available_balance: Decimal,
    cfg: Optional[StrategyConfig] = None,
) -> tuple[Decimal, Decimal]:
    if cfg is None:
        cfg = get_strategy_config()

    allocation = ALLOCATIONS[asset]
    target_eur = (capital * allocation * cfg.size_multiplier).quantize(Decimal("0.01"))

    size_eur = min(target_eur, available_balance)
    if size_eur <= Decimal("0"):
        logger.warning(f"{asset}: no balance available (need €{target_eur}, have €{available_balance})")
        return Decimal("0"), Decimal("0")

    decimals = ASSET_DECIMALS[asset]
    size_asset = (size_eur / price).quantize(Decimal(f"1e-{decimals}"), rounding=ROUND_DOWN)

    min_size = MIN_ORDER_SIZE[asset]
    if size_asset < min_size:
        min_cost = (min_size * price).quantize(Decimal("0.01"))
        if min_cost <= available_balance:
            # Scale up to minimum order size if balance allows
            logger.info(f"{asset}: size {size_asset} below minimum {min_size}, scaling up to minimum (€{min_cost})")
            return min_cost, min_size
        logger.warning(f"{asset}: computed size {size_asset} < minimum {min_size} and balance €{available_balance} insufficient for minimum €{min_cost}. Skipping.")
        return Decimal("0"), Decimal("0")

    size_eur = (size_asset * price).quantize(Decimal("0.01"))
    return size_eur, size_asset


def calculate_levels(
    entry_price: Decimal,
    cfg: Optional[StrategyConfig] = None,
    atr: Optional[float] = None,
) -> tuple[Decimal, Decimal, Decimal, Optional[Decimal]]:
    """Returns (stop_loss, tp1, tp2, tp3_or_None).
    Uses ATR-based SL when atr is provided, falls back to percentage.
    """
    if cfg is None:
        cfg = get_strategy_config()

    if atr and atr > 0:
        atr_dec = Decimal(str(round(atr, 4)))
        sl = (entry_price - cfg.atr_sl_multiplier * atr_dec).quantize(Decimal("0.01"))
        # Safety cap: ATR-SL cannot be worse than 2× the percentage SL
        max_sl_drop = entry_price * cfg.sl_pct * Decimal("2")
        sl = max(sl, entry_price - max_sl_drop)
    else:
        sl = (entry_price * (Decimal("1") - cfg.sl_pct)).quantize(Decimal("0.01"))

    tp1 = (entry_price * (Decimal("1") + cfg.tp1_pct)).quantize(Decimal("0.01"))
    tp2 = (entry_price * (Decimal("1") + cfg.tp2_pct)).quantize(Decimal("0.01"))
    tp3 = (
        (entry_price * (Decimal("1") + cfg.tp3_pct)).quantize(Decimal("0.01"))
        if cfg.tp3_pct is not None else None
    )
    return sl, tp1, tp2, tp3


def floor_asset(value: Decimal, asset: str) -> Decimal:
    decimals = ASSET_DECIMALS[asset]
    return value.quantize(Decimal(f"1e-{decimals}"), rounding=ROUND_DOWN)


def get_capital() -> Decimal:
    return Decimal(os.getenv("CAPITALE_TOTALE", "100"))


def get_risk_per_trade() -> Decimal:
    return Decimal(os.getenv("RISCHIO_PER_TRADE", "0.015"))


def is_paper_trading() -> bool:
    return os.getenv("PAPER_TRADING", "true").lower() == "true"

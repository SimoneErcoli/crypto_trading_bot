"""
Position sizing, risk rules, global pause logic, and Kraken order minimums.
All monetary calculations use Decimal to avoid floating-point errors.
"""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Optional

from loguru import logger

# ── Kraken minimum order sizes ────────────────────────────────────────────────
MIN_ORDER_SIZE: dict[str, Decimal] = {
    "BTC": Decimal("0.0001"),
    "ETH": Decimal("0.01"),
    "SOL": Decimal("0.5"),
    "XRP": Decimal("10"),
}

# Decimal precision for each asset
ASSET_DECIMALS: dict[str, int] = {
    "BTC": 6,
    "ETH": 5,
    "SOL": 4,
    "XRP": 2,
}

# Capital allocations (must sum to 1.0)
ALLOCATIONS: dict[str, Decimal] = {
    "BTC": Decimal("0.40"),
    "ETH": Decimal("0.30"),
    "SOL": Decimal("0.20"),
    "XRP": Decimal("0.10"),
}

STOP_LOSS_PCT = Decimal("0.03")     # -3%
TP1_PCT = Decimal("0.05")           # +5%
TP2_PCT = Decimal("0.10")           # +10%

PAUSE_FILE = Path(".pause_until")


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
) -> tuple[Decimal, Decimal]:
    """
    Returns (size_eur, size_asset) respecting allocation, available balance,
    and Kraken minimums.  Returns (0, 0) if the trade cannot be sized.
    """
    allocation = ALLOCATIONS[asset]
    target_eur = (capital * allocation).quantize(Decimal("0.01"))

    # Never spend more than what's available
    size_eur = min(target_eur, available_balance)
    if size_eur <= Decimal("0"):
        logger.warning(f"{asset}: no balance available (need €{target_eur}, have €{available_balance})")
        return Decimal("0"), Decimal("0")

    decimals = ASSET_DECIMALS[asset]
    size_asset = (size_eur / price).quantize(Decimal(f"1e-{decimals}"), rounding=ROUND_DOWN)

    min_size = MIN_ORDER_SIZE[asset]
    if size_asset < min_size:
        logger.warning(
            f"{asset}: computed size {size_asset} < minimum {min_size}. Skipping."
        )
        return Decimal("0"), Decimal("0")

    # Recalculate EUR after rounding down
    size_eur = (size_asset * price).quantize(Decimal("0.01"))
    return size_eur, size_asset


def calculate_levels(
    entry_price: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Returns (stop_loss, tp1, tp2) prices."""
    sl = (entry_price * (Decimal("1") - STOP_LOSS_PCT)).quantize(Decimal("0.01"))
    tp1 = (entry_price * (Decimal("1") + TP1_PCT)).quantize(Decimal("0.01"))
    tp2 = (entry_price * (Decimal("1") + TP2_PCT)).quantize(Decimal("0.01"))
    return sl, tp1, tp2


def floor_asset(value: Decimal, asset: str) -> Decimal:
    decimals = ASSET_DECIMALS[asset]
    return value.quantize(Decimal(f"1e-{decimals}"), rounding=ROUND_DOWN)


def get_capital() -> Decimal:
    return Decimal(os.getenv("CAPITALE_TOTALE", "100"))


def get_risk_per_trade() -> Decimal:
    return Decimal(os.getenv("RISCHIO_PER_TRADE", "0.015"))


def is_paper_trading() -> bool:
    return os.getenv("PAPER_TRADING", "true").lower() == "true"

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

# ── Kraken minimum order sizes (fallback) ─────────────────────────────────────
# These are used as a safety net when the live Kraken AssetPairs fetch fails.
# At runtime, kraken_client.warmup_ordermin_cache() fetches the real values and
# overrides these. Update this table whenever Kraken changes a minimum.
#
# Last verified against Kraken AssetPairs API (errors observed in production):
#   ATOM  1.7635 rejected → minimum ≥ 2.0
#   LINK  0.5304 rejected → minimum ≥ 0.6
#   DOT   1.8082 rejected → minimum ≥ 2.0
#   AVAX  0.2487 rejected → minimum ≥ 0.25
MIN_ORDER_SIZE: dict[str, Decimal] = {
    "BTC":  Decimal("0.0001"),
    "ETH":  Decimal("0.01"),
    "SOL":  Decimal("1.0"),    # Kraken SOL/EUR ordermin (conservative; 0.5 may be too low)
    "XRP":  Decimal("10"),
    "ADA":  Decimal("15"),     # Kraken ADA/EUR ordermin (conservative; 5 is too low)
    "AVAX": Decimal("0.25"),   # verified: 0.2487 rejected
    "DOT":  Decimal("2.0"),    # verified: 1.8082 rejected
    "LINK": Decimal("0.6"),    # verified: 0.5304 rejected
    "LTC":  Decimal("0.1"),    # Kraken LTC/EUR ordermin (conservative; 0.05 may be too low)
    "ATOM": Decimal("2.0"),    # verified: 1.7635 rejected
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

# Default asset universe for the swing strategies (conservative / aggressive)
SWING_ASSETS: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "DOT", "LINK", "LTC", "ATOM",
)

# Scalping works on BTC only: the most liquid EUR pair on Kraken (tight spread,
# deep book) and the only one whose ordermin (~€10) suits a small account.
SCALP_ASSETS: tuple[str, ...] = ("BTC",)


# ── Strategy configuration ────────────────────────────────────────────────────

@dataclass(frozen=True)
class StrategyConfig:
    name: str
    # Market scope / timing
    assets: tuple[str, ...]             # tradable universe for this strategy
    timeframe_minutes: int              # OHLC candle interval
    scan_interval_minutes: int          # how often the signal scan runs
    signal_mode: str                    # "swing" | "scalp" — selects signal logic
    # Signal thresholds
    rsi_buy_low: int
    rsi_buy_high: int
    rsi_sell: int
    ema_ref: str                        # "ema20" | "ema21" | "ema50"
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
    # Order handling
    entry_timeout_minutes: int          # cancel unfilled limit entry after N minutes
    exit_poll_seconds: int              # exit-monitor / fill-poll cadence
    # Behaviour
    cooldown_minutes: int
    max_consecutive_losses: int
    max_open_positions: int             # global concurrent position limit
    size_multiplier: Decimal
    use_risk_sizing: bool               # size from equity risk instead of fixed allocations


CONSERVATIVE = StrategyConfig(
    name="conservative",
    assets=SWING_ASSETS,
    timeframe_minutes=240,
    scan_interval_minutes=240,
    signal_mode="swing",
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
    entry_timeout_minutes=30,
    exit_poll_seconds=60,
    cooldown_minutes=240,
    max_consecutive_losses=2,
    max_open_positions=4,
    size_multiplier=Decimal("1.0"),
    use_risk_sizing=False,
)

AGGRESSIVE = StrategyConfig(
    name="aggressive",
    assets=SWING_ASSETS,
    timeframe_minutes=240,
    scan_interval_minutes=240,
    signal_mode="swing",
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
    entry_timeout_minutes=30,
    exit_poll_seconds=60,
    cooldown_minutes=60,
    max_consecutive_losses=3,
    max_open_positions=6,
    size_multiplier=Decimal("1.25"),
    use_risk_sizing=False,
)

# Scalping: 15m candles on BTC only, scan every 5 minutes, one position at
# a time. Targets are sized so that TP1 (+1.2%) clears a full maker round
# trip (~0.32%) with margin; SL is ATR-based and capped at -0.8%.
# Funds are managed dynamically: position size derives from live equity
# (EUR balance + open positions); with a single slot the whole equity
# (minus the fee buffer) backs the trade, risking ~0.8% of it per stop.
SCALPING = StrategyConfig(
    name="scalping",
    assets=SCALP_ASSETS,
    timeframe_minutes=15,
    scan_interval_minutes=5,
    signal_mode="scalp",
    rsi_buy_low=38,
    rsi_buy_high=62,
    rsi_sell=78,
    ema_ref="ema21",
    volume_multiplier=1.1,
    adx_min=15,
    use_ema200_filter=False,
    sl_pct=Decimal("0.008"),
    atr_sl_multiplier=Decimal("1.2"),
    tp1_pct=Decimal("0.012"),
    tp1_close_pct=Decimal("0.50"),
    tp2_pct=Decimal("0.025"),
    tp2_close_pct=Decimal("0.50"),
    tp3_pct=None,
    tp3_close_pct=None,
    trailing_stop_pct=Decimal("0.008"),
    max_position_hours=6,
    flat_threshold_pct=Decimal("0.002"),
    entry_timeout_minutes=5,
    exit_poll_seconds=15,
    cooldown_minutes=30,
    max_consecutive_losses=3,
    max_open_positions=1,
    size_multiplier=Decimal("1.0"),
    use_risk_sizing=True,
)

_CONFIGS = {
    "conservative": CONSERVATIVE,
    "aggressive": AGGRESSIVE,
    "scalping": SCALPING,
}


def get_strategy_config() -> StrategyConfig:
    name = os.getenv("STRATEGIA", "conservative").lower().strip()
    cfg = _CONFIGS.get(name)
    if cfg is None:
        logger.warning(f"Unknown STRATEGIA='{name}', falling back to conservative")
        return CONSERVATIVE
    return cfg


def get_config_by_name(name: Optional[str]) -> StrategyConfig:
    """Resolve a strategy name stored in positions.json back to its config."""
    return _CONFIGS.get((name or "").lower().strip(), CONSERVATIVE)


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

# Keep a slice of the available EUR unspent so fees never push the account
# into "insufficient funds" on subsequent orders.
FEE_BUFFER = Decimal("0.99")


def get_effective_capital(eur_balance: Decimal) -> Decimal:
    """
    Live equity estimate: free EUR plus the book value of all open positions.
    Keeps sizing proportional to the real account instead of the static
    CAPITALE_TOTALE env value.
    """
    import position_manager as pm  # local import to avoid cycles at module load
    open_value = sum(
        Decimal(str(p.get("size_eur", "0")))
        for p in pm.get_all_positions().values()
        if p.get("active")
    )
    return eur_balance + open_value


def calculate_position_size(
    asset: str,
    price: Decimal,
    capital: Decimal,
    available_balance: Decimal,
    cfg: Optional[StrategyConfig] = None,
) -> tuple[Decimal, Decimal]:
    if cfg is None:
        cfg = get_strategy_config()

    if cfg.use_risk_sizing:
        # Risk-based sizing: risk a fixed fraction of equity per trade given the
        # SL distance, capped by an equal split of equity across position slots.
        risk_amount = capital * get_risk_per_trade()
        risk_target = risk_amount / cfg.sl_pct
        slot_cap = capital / Decimal(cfg.max_open_positions)
        target_eur = (min(risk_target, slot_cap) * cfg.size_multiplier).quantize(Decimal("0.01"))
    else:
        allocation = ALLOCATIONS[asset]
        target_eur = (capital * allocation * cfg.size_multiplier).quantize(Decimal("0.01"))

    size_eur = min(target_eur, (available_balance * FEE_BUFFER).quantize(Decimal("0.01")))
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

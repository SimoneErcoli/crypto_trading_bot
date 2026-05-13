"""
Computes technical indicators (RSI, EMA, MACD, Volume) and generates
BUY / SELL / HOLD signals for each asset on 4h candles.
Signal thresholds are read from the active StrategyConfig.
"""

from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd
import pandas_ta as ta
from loguru import logger

Signal = Literal["BUY", "SELL", "HOLD"]


@dataclass
class SignalResult:
    signal: Signal
    rsi: float
    ema_above: bool         # True if close > configured EMA reference
    macd_bullish: bool
    volume_surge: bool
    close: float
    reason: str
    ema_ref: str            # "ema20" | "ema50" — which EMA was used


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"] = ta.rsi(df["close"], length=14)
    df["ema20"] = ta.ema(df["close"], length=20)
    df["ema50"] = ta.ema(df["close"], length=50)
    df["ema200"] = ta.ema(df["close"], length=200)

    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    df["macd"] = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    df["macd_hist"] = macd["MACDh_12_26_9"]

    df["vol_ma20"] = df["volume"].rolling(20).mean()
    return df


def _is_macd_bullish_crossover(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    return df["macd_hist"].iloc[-2] <= 0 and df["macd_hist"].iloc[-1] > 0


def _is_bearish_divergence(df: pd.DataFrame, lookback: int = 5) -> bool:
    if len(df) < lookback + 1:
        return False
    recent = df.iloc[-lookback:]
    price_higher = recent["close"].iloc[-1] > recent["close"].iloc[0]
    macd_lower = recent["macd_hist"].iloc[-1] < recent["macd_hist"].iloc[0]
    return price_higher and macd_lower


def evaluate_signal(
    df: pd.DataFrame,
    asset: str,
    cfg=None,
) -> SignalResult:
    """
    Evaluate the latest closed candle and return a SignalResult.
    cfg is a StrategyConfig; if None, the active env config is loaded.
    """
    from risk_manager import get_strategy_config
    if cfg is None:
        cfg = get_strategy_config()

    if len(df) < 200:
        logger.warning(f"{asset}: not enough candles ({len(df)} < 200), returning HOLD")
        return SignalResult("HOLD", 0, False, False, False, 0, "insufficient data", cfg.ema_ref)

    df = compute_indicators(df)

    row = df.iloc[-2]

    rsi      = float(row["rsi"])
    close    = float(row["close"])
    ema_val  = float(row[cfg.ema_ref])
    macd_hist = float(row["macd_hist"])
    volume   = float(row["volume"])
    vol_ma20 = float(row["vol_ma20"])

    ema_above = close > ema_val

    # ── SELL conditions ───────────────────────────────────────────────────────
    if rsi > cfg.rsi_sell:
        return SignalResult("SELL", rsi, ema_above, False, False, close,
                            f"RSI overbought ({rsi:.1f} > {cfg.rsi_sell})", cfg.ema_ref)

    if _is_bearish_divergence(df):
        return SignalResult("SELL", rsi, ema_above, False, False, close,
                            "bearish MACD divergence", cfg.ema_ref)

    # ── BUY conditions ────────────────────────────────────────────────────────
    rsi_ok       = cfg.rsi_buy_low <= rsi <= cfg.rsi_buy_high
    macd_bullish = macd_hist > 0 or _is_macd_bullish_crossover(df)
    volume_surge = vol_ma20 > 0 and volume > vol_ma20 * cfg.volume_multiplier

    if rsi_ok and ema_above and macd_bullish and volume_surge:
        return SignalResult("BUY", rsi, ema_above, macd_bullish, volume_surge, close,
                            "all buy conditions met", cfg.ema_ref)

    missing = []
    if not rsi_ok:
        missing.append(f"RSI={rsi:.1f} (need {cfg.rsi_buy_low}-{cfg.rsi_buy_high})")
    if not ema_above:
        missing.append(f"close {close:.2f} < {cfg.ema_ref.upper()} {ema_val:.2f}")
    if not macd_bullish:
        missing.append("MACD not bullish")
    if not volume_surge:
        vol_ratio = volume / vol_ma20 if vol_ma20 > 0 else 0
        missing.append(f"volume {vol_ratio:.2f}x (need >{cfg.volume_multiplier}x)")

    return SignalResult("HOLD", rsi, ema_above, macd_bullish, volume_surge, close,
                        "missing: " + "; ".join(missing), cfg.ema_ref)


def check_exit_conditions(
    asset: str,
    current_price: float,
    position: dict,
) -> tuple[bool, str]:
    from decimal import Decimal

    price  = Decimal(str(current_price))
    sl     = Decimal(str(position["sl"]))
    tp1    = Decimal(str(position["tp1"]))
    tp2    = Decimal(str(position["tp2"]))
    tp3_raw = position.get("tp3")
    tp3    = Decimal(str(tp3_raw)) if tp3_raw else None

    if price <= sl:
        return True, "stop_loss"
    if tp3 and not position.get("tp3_hit") and not position.get("tp2_hit") and price >= tp3:
        return True, "tp3"
    if not position.get("tp2_hit") and price >= tp2:
        return True, "tp2"
    if not position.get("tp1_hit") and price >= tp1:
        return True, "tp1"
    return False, ""

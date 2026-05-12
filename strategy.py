"""
Computes technical indicators (RSI, EMA, MACD, Volume) and generates
BUY / SELL / HOLD signals for each asset on 4h candles.
"""

from dataclasses import dataclass
from typing import Literal

import pandas as pd
import pandas_ta as ta
from loguru import logger

Signal = Literal["BUY", "SELL", "HOLD"]

RSI_BUY_LOW = 35
RSI_BUY_HIGH = 50
RSI_SELL = 72
VOLUME_MULTIPLIER = 1.3


@dataclass
class SignalResult:
    signal: Signal
    rsi: float
    ema50_above: bool
    macd_bullish: bool
    volume_surge: bool
    close: float
    reason: str


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add RSI, EMA20/50/200, MACD and Volume MA columns to the DataFrame."""
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
    """True if MACD crossed above signal on the last candle."""
    if len(df) < 2:
        return False
    prev_hist = df["macd_hist"].iloc[-2]
    curr_hist = df["macd_hist"].iloc[-1]
    return prev_hist <= 0 and curr_hist > 0


def _is_bearish_divergence(df: pd.DataFrame, lookback: int = 5) -> bool:
    """
    Bearish divergence: price made higher high but MACD histogram made lower high
    over the last `lookback` candles.
    """
    if len(df) < lookback + 1:
        return False
    recent = df.iloc[-lookback:]
    price_higher = recent["close"].iloc[-1] > recent["close"].iloc[0]
    macd_lower = recent["macd_hist"].iloc[-1] < recent["macd_hist"].iloc[0]
    return price_higher and macd_lower


def evaluate_signal(df: pd.DataFrame, asset: str) -> SignalResult:
    """
    Evaluate the latest closed candle and return a SignalResult.
    The DataFrame must have at least 200 rows with columns:
    open, high, low, close, volume.
    """
    if len(df) < 200:
        logger.warning(f"{asset}: not enough candles ({len(df)} < 200), returning HOLD")
        return SignalResult("HOLD", 0, False, False, False, 0, "insufficient data")

    df = compute_indicators(df)

    # Use the second-to-last row so we only trade on fully closed candles
    row = df.iloc[-2]
    prev_row = df.iloc[-3] if len(df) >= 3 else df.iloc[-2]

    rsi = float(row["rsi"])
    close = float(row["close"])
    ema50 = float(row["ema50"])
    macd_hist = float(row["macd_hist"])
    volume = float(row["volume"])
    vol_ma20 = float(row["vol_ma20"])

    # ── SELL conditions (any one triggers) ───────────────────────────────────
    if rsi > RSI_SELL:
        return SignalResult("SELL", rsi, close > ema50, False, False, close,
                            f"RSI overbought ({rsi:.1f} > {RSI_SELL})")

    if _is_bearish_divergence(df):
        return SignalResult("SELL", rsi, close > ema50, False, False, close,
                            "bearish MACD divergence")

    # ── BUY conditions (all must be true) ────────────────────────────────────
    rsi_ok = RSI_BUY_LOW <= rsi <= RSI_BUY_HIGH
    ema50_above = close > ema50
    macd_bullish = macd_hist > 0 or _is_macd_bullish_crossover(df)
    volume_surge = vol_ma20 > 0 and volume > vol_ma20 * VOLUME_MULTIPLIER

    if rsi_ok and ema50_above and macd_bullish and volume_surge:
        return SignalResult("BUY", rsi, ema50_above, macd_bullish, volume_surge, close,
                            "all buy conditions met")

    missing = []
    if not rsi_ok:
        missing.append(f"RSI={rsi:.1f} (need {RSI_BUY_LOW}–{RSI_BUY_HIGH})")
    if not ema50_above:
        missing.append(f"close {close:.2f} < EMA50 {ema50:.2f}")
    if not macd_bullish:
        missing.append("MACD not bullish")
    if not volume_surge:
        vol_ratio = volume / vol_ma20 if vol_ma20 > 0 else 0
        missing.append(f"volume ratio {vol_ratio:.2f}x (need >{VOLUME_MULTIPLIER}x)")

    return SignalResult("HOLD", rsi, ema50_above, macd_bullish, volume_surge, close,
                        "missing: " + "; ".join(missing))


def check_exit_conditions(
    asset: str,
    current_price: float,
    position: dict,
) -> tuple[bool, str]:
    """
    Returns (should_exit, reason) based on price levels alone.
    Order-driven exits (SL/TP) are handled by Kraken native orders;
    this catches any case where the bot needs to act directly.
    """
    from decimal import Decimal

    entry = Decimal(str(position["entry_price"]))
    sl = Decimal(str(position["sl"]))
    tp1 = Decimal(str(position["tp1"]))
    tp2 = Decimal(str(position["tp2"]))
    price = Decimal(str(current_price))

    if price <= sl:
        return True, "stop_loss"
    if not position.get("tp2_hit") and price >= tp2:
        return True, "tp2"
    if not position.get("tp1_hit") and price >= tp1:
        return True, "tp1"
    return False, ""

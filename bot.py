"""
Entry point. Loads config, starts the scheduling loop, and catches every
unhandled exception so the bot never crashes silently.
"""

import sys
import os
import time
import signal
import traceback
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import schedule
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

# ── Logging setup ─────────────────────────────────────────────────────────────
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO",
)
logger.add(
    "bot.log",
    rotation="00:00",          # rotate daily at midnight
    retention="7 days",
    compression="zip",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{line} | {message}",
)

import kraken_client as kc
import position_manager as pm
import risk_manager as rm
import strategy as st
import order_manager as om
from telegram_notify import (
    notify_startup,
    notify_daily_report,
    notify_scan_summary,
    notify_error,
    notify_max_positions_reached,
)

ASSETS = ["BTC", "ETH", "SOL", "XRP", "ADA", "AVAX", "DOT", "LINK", "LTC", "ATOM"]
SCAN_INTERVAL_MINUTES = 240  # 4h

# Track daily P&L (resets at midnight)
_daily_trades: list[dict] = []
_daily_fees: Decimal = Decimal("0")
_session_start_capital: Decimal = Decimal("0")


def _next_scan_time() -> str:
    now = datetime.now(timezone.utc)
    nxt = now + timedelta(minutes=SCAN_INTERVAL_MINUTES)
    return nxt.strftime("%H:%M")


def scan_all_assets() -> None:
    """Main trading loop — runs every 4h via schedule."""
    logger.info("── Starting asset scan ──")

    if rm.is_globally_paused():
        resume = rm.get_pause_resume_time()
        logger.info(f"Bot is paused until {resume}. Skipping scan.")
        return

    cfg = rm.get_strategy_config()
    scan_results = []
    for asset in ASSETS:
        try:
            result = _scan_asset(asset, cfg)
            if result:
                scan_results.append(result)
        except Exception as exc:
            logger.exception(f"{asset}: unhandled error in scan: {exc}")
            notify_error(f"scan {asset}", traceback.format_exc()[-300:])
            scan_results.append({
                "asset": asset, "skipped": True,
                "skip_reason": f"errore: {str(exc)[:60]}",
            })

    if scan_results:
        try:
            notify_scan_summary(scan_results, _next_scan_time(), strategy=cfg.name)
        except Exception as exc:
            logger.warning(f"Could not send scan summary: {exc}")

    logger.info("── Scan complete ──")


def _scan_asset(asset: str, cfg=None) -> dict:
    # Skip if already in position
    if pm.has_active_position(asset):
        logger.info(f"{asset}: position already open — checking exit conditions")
        _check_exit_for_open_position(asset, cfg)
        return {"asset": asset, "skipped": True, "skip_reason": "posizione aperta"}

    # Skip if in cooldown after close
    if pm.is_in_cooldown(asset, cooldown_hours=4):
        logger.info(f"{asset}: in 4h cooldown after last close")
        return {"asset": asset, "skipped": True, "skip_reason": "cooldown 4h"}

    # Fetch candles and evaluate signal
    if cfg is None:
        cfg = rm.get_strategy_config()
    df = kc.get_ohlcv(asset, interval=240, count=200)
    result = st.evaluate_signal(df, asset, cfg)
    logger.info(f"{asset}: signal={result.signal} | {result.reason}")

    if result.signal == "BUY":
        open_count = sum(1 for a in ASSETS if pm.has_active_position(a))
        if open_count >= cfg.max_open_positions:
            logger.info(f"{asset}: BUY signal but max positions reached ({open_count}/{cfg.max_open_positions})")
            notify_max_positions_reached(asset, open_count, cfg.max_open_positions)
            return {"asset": asset, "skipped": True,
                    "skip_reason": f"max posizioni ({open_count}/{cfg.max_open_positions})"}
        logger.info(f"{asset}: BUY signal → opening position")
        om.open_position(asset, result)

    elif result.signal == "SELL" and pm.has_active_position(asset):
        logger.info(f"{asset}: SELL signal on open position → signal exit")
        current_price = kc.get_ticker_price(asset)
        om.close_position_on_signal(asset, current_price)

    return {
        "asset": asset,
        "skipped": False,
        "signal": result.signal,
        "rsi": result.rsi,
        "rsi_ok": cfg.rsi_buy_low <= result.rsi <= cfg.rsi_buy_high,
        "ema_above": result.ema_above,
        "ema_ref": result.ema_ref,
        "macd_bullish": result.macd_bullish,
        "volume_surge": result.volume_surge,
        "adx": result.adx,
        "adx_ok": result.adx >= cfg.adx_min,
        "ema200_above": result.ema200_above,
        "close": result.close,
        "reason": result.reason,
    }


def _check_exit_for_open_position(asset: str, cfg) -> None:
    """
    Supplementary price-level exit check for assets with open positions.
    Used as a fallback when the exit monitor thread is not running
    (e.g. first scan after bot restart).
    """
    pos = pm.get_position(asset)
    if not pos:
        return
    price = float(kc.get_ticker_price(asset))
    should_exit, reason = st.check_exit_conditions(asset, price, pos)
    if should_exit:
        logger.info(f"{asset}: supplementary exit triggered ({reason}) at {price}")


def _resume_exit_monitors() -> None:
    """
    Called once on startup. For each asset that has an active position in
    positions.json (left over from a previous run), restart the exit monitor
    thread so TP/SL/trailing/time-exit logic keeps running.
    """
    for asset in ASSETS:
        if pm.has_active_position(asset):
            logger.info(f"{asset}: restarting exit monitor for pre-existing position")
            om._monitor_exit(asset)


def daily_report() -> None:
    """Scheduled at 20:00 UTC every day."""
    logger.info("Sending daily report")
    try:
        capital = kc.get_eur_balance()
        pnl_today = sum(Decimal(str(t["pnl"])) for t in _daily_trades)
        pnl_total = capital - _session_start_capital
        open_positions = {
            a: pm.get_position(a)
            for a in ASSETS
            if pm.has_active_position(a)
        }
        notify_daily_report(
            date_str=datetime.now(timezone.utc).strftime("%-d %B %Y"),
            capital=capital,
            pnl_today=pnl_today,
            pnl_total=pnl_total,
            trades_today=_daily_trades[:],
            open_positions=open_positions,
            fees_today=_daily_fees,
            next_scan=_next_scan_time(),
        )
        # Reset daily counters
        _daily_trades.clear()
    except Exception as exc:
        logger.exception(f"daily_report error: {exc}")
        notify_error("daily_report", str(exc))


def _align_to_next_4h() -> None:
    """Sleep until the next 4h boundary (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC)."""
    now = datetime.now(timezone.utc)
    hour_block = (now.hour // 4 + 1) * 4
    if hour_block >= 24:
        hour_block = 0
        next_run = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    else:
        next_run = now.replace(hour=hour_block, minute=0, second=0, microsecond=0)
    wait_seconds = (next_run - now).total_seconds()
    logger.info(f"Aligning to 4h candle boundary — waiting {wait_seconds:.0f}s until {next_run.strftime('%H:%M')} UTC")
    time.sleep(max(0, wait_seconds))


def _graceful_shutdown(signum, frame) -> None:
    logger.info("Received shutdown signal — stopping bot gracefully")
    notify_error("shutdown", "Bot received shutdown signal (SIGTERM/SIGINT). SL orders on Kraken remain active.")
    sys.exit(0)


def main() -> None:
    global _session_start_capital

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    paper = rm.is_paper_trading()
    capital = rm.get_capital()
    cfg = rm.get_strategy_config()

    logger.info(f"Bot starting — paper={paper}, capital=€{capital}, strategy={cfg.name}")

    # Try to read actual Kraken balance; fall back to configured capital
    try:
        if not paper:
            _session_start_capital = kc.get_eur_balance()
        else:
            _session_start_capital = capital
    except Exception as exc:
        logger.warning(f"Could not fetch initial balance: {exc}")
        _session_start_capital = capital

    # Restart exit monitors for positions that survived a previous run
    _resume_exit_monitors()

    next_scan = _next_scan_time()
    notify_startup(
        capital=capital,
        assets=ASSETS,
        paper=paper,
        next_scan=next_scan,
        strategy=cfg.name,
    )

    # Align first run to the 4h candle boundary
    _align_to_next_4h()

    # Run immediately on start (post alignment)
    scan_all_assets()

    # Schedule recurring scan every 4h
    schedule.every(SCAN_INTERVAL_MINUTES).minutes.do(scan_all_assets)

    # Daily report at 20:00 UTC
    schedule.every().day.at("20:00").do(daily_report)

    logger.info("Scheduler started — entering main loop")

    while True:
        try:
            schedule.run_pending()
        except Exception as exc:
            logger.exception(f"Unhandled exception in main loop: {exc}")
            notify_error("main_loop", traceback.format_exc()[-500:])
        time.sleep(1)


if __name__ == "__main__":
    main()

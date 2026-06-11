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
from typing import Optional

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
    notify_startup_reconcile,
)

# Asset universe and timing come from the active strategy config:
#   conservative/aggressive → 10 assets, 4h candles, scan every 4h
#   scalping                → BTC/ETH/SOL, 15m candles, scan every 5m
_BOOT_CFG = rm.get_strategy_config()
ASSETS = list(_BOOT_CFG.assets)
SCAN_INTERVAL_MINUTES = _BOOT_CFG.scan_interval_minutes
TIMEFRAME_MINUTES = _BOOT_CFG.timeframe_minutes

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

    if scan_results and _should_notify_scan(scan_results, cfg):
        try:
            notify_scan_summary(scan_results, _next_scan_time(), strategy=cfg.name)
        except Exception as exc:
            logger.warning(f"Could not send scan summary: {exc}")

    logger.info("── Scan complete ──")


def _should_notify_scan(scan_results: list[dict], cfg) -> bool:
    """
    With a 4h scan every summary is useful; with a 5-minute scalping scan it
    would flood Telegram. In scalp mode only notify when something actually
    happened: a BUY/SELL signal or an error.
    """
    if cfg.signal_mode != "scalp":
        return True
    for r in scan_results:
        if not r.get("skipped") and r.get("signal") in ("BUY", "SELL"):
            return True
        if "errore" in str(r.get("skip_reason", "")):
            return True
    return False


def _scan_asset(asset: str, cfg=None) -> dict:
    if cfg is None:
        cfg = rm.get_strategy_config()

    # Skip if already in position
    if pm.has_active_position(asset):
        logger.info(f"{asset}: position already open — checking exit conditions")
        _check_exit_for_open_position(asset, cfg)
        return {"asset": asset, "skipped": True, "skip_reason": "posizione aperta"}

    # Skip if in cooldown after close
    cooldown_h = cfg.cooldown_minutes / 60
    if pm.is_in_cooldown(asset, cooldown_hours=cooldown_h):
        logger.info(f"{asset}: in {cfg.cooldown_minutes}m cooldown after last close")
        return {"asset": asset, "skipped": True, "skip_reason": f"cooldown {cfg.cooldown_minutes}m"}

    # Fetch candles and evaluate signal.
    # count=300 ensures EMA200 has enough history to be non-NaN on the last
    # closed candle in swing mode (Kraken returns up to 720 candles per call).
    df = kc.get_ohlcv(asset, interval=TIMEFRAME_MINUTES, count=300)
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


def _find_open_sl(asset: str, open_orders: dict) -> Optional[tuple[str, dict]]:
    """
    Scan the open-orders dict (from kc.get_open_orders()) for a native stop-loss
    sell on *asset*'s pair.  Returns (txid, order_info) or None.
    """
    pair = kc.KRAKEN_PAIRS[asset]
    for txid, info in open_orders.items():
        descr = info.get("descr", {})
        if (
            descr.get("type") == "sell"
            and descr.get("ordertype") == "stop-loss"
            and descr.get("pair") == pair
        ):
            return txid, info
    return None


def _startup_reconcile() -> None:
    """
    Run once at startup. Uses Kraken balances and open orders as the primary
    source of truth; positions.json is treated as metadata only.

    Decision matrix (per asset):

      Kraken balance  |  positions.json  |  Action
      ──────────────────────────────────────────────
      ≥ min_size      |  active          |  Verify / re-place SL if missing
      ≥ min_size      |  not active      |  Warn (untracked balance)
      < min_size      |  active          |  Position closed offline → auto-close
      < min_size      |  not active      |  Nothing to do

    Skips Kraken API calls in paper-trading mode.
    """
    logger.info("── Startup reconciliation ──")
    paper = rm.is_paper_trading()
    rows: list[dict] = []

    if paper:
        for asset in ASSETS:
            pos = pm.get_position(asset)
            active = bool(pos and pos.get("active"))
            rows.append({"asset": asset, "active": active, "action": "paper",
                         "note": "posizione attiva" if active else "inattivo"})
        for r in rows:
            icon = "🟢" if r["active"] else "⚫"
            logger.info(f"  {icon} {r['asset']:<5}: {r['note']}")
        try:
            notify_startup_reconcile(rows, Decimal("0"))
        except Exception as exc:
            logger.warning(f"reconcile notify failed: {exc}")
        return

    # ── Live mode: single-shot Kraken state ───────────────────────────────────
    try:
        balances    = kc.get_all_balances()
        open_orders = kc.get_open_orders()
        eur_balance = balances.get("EUR", Decimal("0"))
    except Exception as exc:
        logger.warning(f"Startup reconcile: cannot fetch Kraken state: {exc}")
        return

    for asset in ASSETS:
        actual_bal = balances.get(asset, Decimal("0"))
        min_size   = rm.MIN_ORDER_SIZE[asset]
        has_bal    = actual_bal >= min_size

        pos            = pm.get_position(asset)
        active_in_file = bool(pos and pos.get("active"))

        # Find an open native SL for this asset on Kraken
        sl_on_kraken = _find_open_sl(asset, open_orders)

        row: dict = {"asset": asset, "active": has_bal,
                     "balance": actual_bal, "action": "ok", "note": ""}

        # ── Quadrant 1: balance present + position known ──────────────────────
        if has_bal and active_in_file:
            sl_id = pos.get("order_id_sl", "")
            is_paper_sl = (not sl_id) or sl_id.startswith("PAPER-")

            if sl_on_kraken:
                kraken_sl_id, _ = sl_on_kraken
                if sl_id != kraken_sl_id:
                    # SL exists but ID drifted (e.g. re-placed manually); sync
                    pm.update_position(asset, order_id_sl=kraken_sl_id)
                    row["note"] = f"OK — SL sincronizzato ({kraken_sl_id[:12]}), balance {actual_bal} {asset}"
                    logger.info(f"{asset}: SL ID updated from {sl_id[:12] if sl_id else 'none'} → {kraken_sl_id[:12]}")
                else:
                    row["note"] = f"OK — SL attivo ({sl_id[:12]}), balance {actual_bal} {asset}"

            elif is_paper_sl:
                # Paper SL or no SL ID — normal for paper mode or fresh positions
                row["note"] = f"OK — balance {actual_bal} {asset} (SL gestito internamente)"

            else:
                # Real SL ID recorded but no matching order on Kraken → re-place
                sl_price = Decimal(str(pos["sl"]))
                sl_size  = rm.floor_asset(actual_bal, asset)
                if sl_size >= min_size:
                    try:
                        new_sl_id = kc.place_stop_loss(asset, sl_price, sl_size)
                        pm.update_position(asset, order_id_sl=new_sl_id)
                        row["action"] = "sl_replaced"
                        row["note"]   = f"SL ripiazzato @ €{sl_price} per {sl_size} {asset}"
                        logger.warning(f"{asset}: SL missing on Kraken — re-placed @ €{sl_price}")
                    except Exception as exc:
                        row["action"] = "warning"
                        row["note"]   = f"SL mancante, ripiazza fallito: {exc}"
                        logger.error(f"{asset}: could not re-place SL: {exc}")
                else:
                    row["action"] = "warning"
                    row["note"]   = f"SL mancante, balance {actual_bal} < min {min_size} — non ripiazzato"
                    logger.warning(f"{asset}: SL missing and balance {actual_bal} < min {min_size}")

        # ── Quadrant 2: balance present but not tracked in positions.json ─────
        elif has_bal and not active_in_file:
            row["action"] = "warning"
            row["note"]   = f"balance non tracciata: {actual_bal} {asset}"
            logger.warning(f"{asset}: untracked balance {actual_bal} on Kraken — no positions.json entry")

        # ── Quadrant 3: no balance but positions.json says active ─────────────
        elif not has_bal and active_in_file:
            # Position was closed while the bot was offline (SL fired or manual sell).
            # Try to get the real fill price from the recorded SL order.
            sl_id         = pos.get("order_id_sl", "")
            sl_fill_price = None

            if sl_id and not sl_id.startswith("PAPER-"):
                try:
                    sl_info   = kc.get_order_status(sl_id)
                    sl_status = sl_info.get("status", "unknown")
                    if sl_status == "closed":
                        raw_p = sl_info.get("price", "0")
                        if float(raw_p or 0) > 0:
                            sl_fill_price = Decimal(str(raw_p))
                except Exception:
                    pass

            # Fall back to the recorded SL price if no fill data available
            exit_price = sl_fill_price or Decimal(str(pos["sl"]))

            # Account for any partial closes already recorded (TP1/TP2)
            pos_cfg      = rm.get_config_by_name(pos.get("strategy"))
            remaining_pct = Decimal("1")
            if pos.get("tp1_hit"):
                remaining_pct -= pos_cfg.tp1_close_pct
            if pos.get("tp2_hit"):
                remaining_pct -= pos_cfg.tp2_close_pct

            size_asset  = Decimal(str(pos["size_asset"]))
            expected    = rm.floor_asset(size_asset * remaining_pct, asset)
            size_eur    = Decimal(str(pos["size_eur"]))
            entry_price = Decimal(str(pos["entry_price"]))

            exit_value = (exit_price * expected).quantize(Decimal("0.01"))
            fee        = (exit_value * Decimal("0.0016")).quantize(Decimal("0.01"))
            pnl        = exit_value - (size_eur * remaining_pct) - fee
            sign       = "+" if pnl >= 0 else ""

            pm.close_position(asset)
            pm.record_closed_trade(
                asset=asset, entry_price=entry_price, exit_price=exit_price,
                size_asset=expected, size_eur=size_eur * remaining_pct,
                pnl=pnl, fee=fee, reason="stop_loss_offline",
            )
            row["action"] = "auto_closed"
            row["active"] = False
            row["note"]   = f"chiuso offline @ €{exit_price} — P&L {sign}€{pnl:.2f}"
            logger.warning(f"{asset}: no balance on Kraken, was marked active — auto-closed (P&L {sign}€{pnl:.2f})")

        # ── Quadrant 4: no balance, no position ───────────────────────────────
        else:
            row["note"] = "inattivo"

        rows.append(row)

    # ── Log summary table ─────────────────────────────────────────────────────
    logger.info(f"Startup reconciliation — EUR balance: €{eur_balance}")
    for r in rows:
        icon = {"ok": "✓", "auto_closed": "✗", "sl_replaced": "⚠",
                "warning": "⚠", "paper": "~"}.get(r["action"], "?")
        logger.info(f"  {icon} {r['asset']:<5}: {r['note']}")
    logger.info("── Reconciliation complete ──")

    try:
        notify_startup_reconcile(rows, eur_balance)
    except Exception as exc:
        logger.warning(f"reconcile notify failed: {exc}")


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


def _align_to_next_boundary(interval_minutes: int) -> None:
    """Sleep until the next scan boundary (multiples of the interval from midnight UTC)."""
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes_into_day = (now - midnight).total_seconds() / 60
    next_block = (int(minutes_into_day) // interval_minutes + 1) * interval_minutes
    next_run = midnight + timedelta(minutes=next_block)
    wait_seconds = (next_run - now).total_seconds()
    logger.info(f"Aligning to {interval_minutes}m boundary — waiting {wait_seconds:.0f}s until {next_run.strftime('%H:%M')} UTC")
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

    # Pre-fetch live Kraken minimum order sizes for all assets (single public API call).
    # Runs in both live and paper mode — paper orders still validate sizes before simulating.
    kc.warmup_ordermin_cache(ASSETS)

    # Reconcile positions.json with actual Kraken state:
    # auto-closes SL-fired-offline positions, re-places missing SLs, warns on anomalies.
    _startup_reconcile()

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

    # Align first run to the scan-interval boundary (4h for swing, 5m for scalping)
    _align_to_next_boundary(SCAN_INTERVAL_MINUTES)

    # Run immediately on start (post alignment)
    scan_all_assets()

    # Schedule recurring scan
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

"""
Handles the full order lifecycle:
  - Place entry limit buy
  - Poll until filled (or cancel after 30 min)
  - On fill: place SL (native Kraken stop-loss) and TP limit sells
  - Monitor TP1/TP2/TP3 hits and update SL to breakeven after TP1

Exit improvements:
  - Trailing stop: after TP1, SL trails the highest price reached
  - Time-based exit: close flat positions open longer than max_position_hours
  - Signal-based exit: close on RSI/MACD SELL signal during 4h scan
"""

import threading
import time
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

import kraken_client as kc
import position_manager as pm
import risk_manager as rm
from risk_manager import StrategyConfig
from telegram_notify import (
    notify_order_sent,
    notify_position_opened,
    notify_order_expired,
    notify_tp1_hit,
    notify_tp2_hit,
    notify_tp3_hit,
    notify_stop_loss,
    notify_trailing_stop_exit,
    notify_time_exit,
    notify_signal_exit,
)

ORDER_TIMEOUT_SECONDS = 30 * 60
POLL_INTERVAL_SECONDS = 60


def _cancel_if_live(order_id: Optional[str]) -> None:
    """Cancel a Kraken order only when it is a real (non-PAPER) order ID."""
    if order_id and not _is_paper_order(order_id):
        kc.cancel_order(order_id)


def _clamp_to_min(asset: str, size: Decimal, max_size: Decimal) -> Decimal:
    """
    Return size clamped to Kraken's minimum order size.
    Prefers the live ordermin from the cache (populated at startup by warmup_ordermin_cache);
    falls back to the hardcoded MIN_ORDER_SIZE when the cache has no entry.
    Returns Decimal("0") when even the minimum exceeds max_size — caller skips the order.
    """
    live_min = kc.get_ordermin(asset)
    min_size = live_min if live_min is not None else rm.MIN_ORDER_SIZE[asset]
    if size >= min_size:
        return size
    if min_size <= max_size:
        logger.info(
            f"{asset}: computed TP size {size} below Kraken minimum {min_size} — bumping up to minimum"
        )
        return min_size
    logger.warning(
        f"{asset}: TP size {size} below minimum {min_size} and max allowed {max_size} is insufficient — skipping TP order"
    )
    return Decimal("0")


def open_position(asset: str, signal_result) -> None:
    threading.Thread(
        target=_open_position_worker,
        args=(asset, signal_result),
        name=f"open-{asset}",
        daemon=True,
    ).start()


def close_position_on_signal(asset: str, current_price: Decimal) -> None:
    """Called from bot.py when a SELL signal fires on an open position."""
    threading.Thread(
        target=_signal_exit_worker,
        args=(asset, current_price),
        name=f"signal-exit-{asset}",
        daemon=True,
    ).start()


def _open_position_worker(asset: str, signal_result) -> None:
    try:
        cfg = rm.get_strategy_config()
        capital = rm.get_capital()
        paper = rm.is_paper_trading()
        eur_balance = capital if paper else kc.get_eur_balance()
        price = kc.get_ticker_price(asset)

        size_eur, size_asset = rm.calculate_position_size(asset, price, capital, eur_balance, cfg)
        if size_asset == Decimal("0"):
            logger.warning(f"{asset}: cannot size position, skipping")
            return

        # Cross-check against the live Kraken ordermin (may differ from our hardcoded value)
        live_min = kc.get_ordermin(asset)
        if live_min and size_asset < live_min:
            min_cost = (live_min * price).quantize(Decimal("0.01"))
            if min_cost <= eur_balance:
                logger.info(
                    f"{asset}: buy size {size_asset} below live Kraken minimum {live_min} — "
                    f"scaling up to {live_min} (€{min_cost})"
                )
                size_asset = live_min
                size_eur = min_cost
            else:
                logger.warning(
                    f"{asset}: buy size {size_asset} below live minimum {live_min}; "
                    f"min cost €{min_cost} exceeds available balance €{eur_balance}. Skipping."
                )
                return

        # ATR-based SL (from signal indicators)
        atr = getattr(signal_result, "atr", None)
        sl, tp1, tp2, tp3 = rm.calculate_levels(price, cfg, atr=atr)

        if paper:
            order_id = f"PAPER-{asset}-{int(time.time())}"
            logger.info(f"[PAPER] Would place limit buy {size_asset} {asset} @ {price}")
        else:
            order_id = kc.place_limit_buy(asset, price, size_asset)

        notify_order_sent(asset, price, size_asset, size_eur, order_id)

        position = pm.build_new_position(
            asset, price, size_eur, size_asset, order_id,
            sl, tp1, tp2, tp3, strategy=cfg.name,
        )
        pm.save_position(asset, position)

        filled = _wait_for_fill(order_id, asset)
        if not filled:
            _handle_expired_order(order_id, asset, price)
            return

        fill_price = _get_fill_price(order_id) or price
        actual_value = (fill_price * size_asset).quantize(Decimal("0.01"))
        fee = (actual_value * (Decimal("0.0016") if paper else kc.get_trade_fee(asset))).quantize(Decimal("0.01"))

        ts = int(time.time())
        if paper:
            sl_order_id  = f"PAPER-SL-{asset}-{ts}"
        else:
            # On Kraken spot, a native SL reserves the full position, making simultaneous
            # native TP sell orders impossible (EOrder:Insufficient funds).
            # Solution: only place the native SL. All TP levels are monitored and executed
            # by the exit-monitor thread (price-based), which cancels the SL, sells the
            # partial amount, and re-places the SL at breakeven for the remainder.
            sl_order_id = kc.place_stop_loss(asset, sl, size_asset)
            logger.info(f"{asset}: native SL placed ({sl_order_id}); TPs managed by exit monitor")

        # TP order IDs use PAPER-style prefixes so the exit monitor uses price-based
        # logic for them in both paper and live mode.
        tp1_order_id = f"PAPER-TP1-{asset}-{ts}"
        tp2_order_id = f"PAPER-TP2-{asset}-{ts}"
        tp3_order_id = f"PAPER-TP3-{asset}-{ts}" if tp3 else None

        pm.update_position(
            asset,
            active=True,
            entry_price=str(fill_price),
            entry_time=datetime.now(timezone.utc).isoformat(),
            order_id_sl=sl_order_id,
            order_id_tp1=tp1_order_id,
            order_id_tp2=tp2_order_id,
            order_id_tp3=tp3_order_id,
        )

        notify_position_opened(
            asset=asset,
            entry_price=fill_price,
            size_asset=size_asset,
            size_eur=actual_value,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            cfg=cfg,
            rsi=signal_result.rsi,
            ema_above=signal_result.ema_above,
            ema_ref=signal_result.ema_ref,
            macd_bullish=signal_result.macd_bullish,
            volume_surge=signal_result.volume_surge,
            fee=fee,
            atr=atr,
        )

        _monitor_exit(asset)

    except Exception as exc:
        logger.exception(f"{asset}: error in open_position_worker: {exc}")


# ── Order polling ─────────────────────────────────────────────────────────────

def _is_paper_order(order_id: str) -> bool:
    return order_id.startswith("PAPER-")


def _wait_for_fill(order_id: str, asset: str) -> bool:
    # Paper orders are considered instantly filled
    if _is_paper_order(order_id):
        logger.info(f"[PAPER] {asset}: order {order_id} simulated as filled")
        return True

    deadline = time.time() + ORDER_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            status = kc.get_order_status(order_id).get("status", "unknown")
            if status == "closed":
                return True
            if status in ("canceled", "expired"):
                return False
            logger.debug(f"{asset}: order {order_id} still {status}")
        except Exception as exc:
            logger.warning(f"{asset}: poll error for {order_id}: {exc}")
    return False


def _handle_expired_order(order_id: str, asset: str, limit_price: Decimal) -> None:
    if not rm.is_paper_trading():
        kc.cancel_order(order_id)
    current_price = kc.get_ticker_price(asset)
    pm.close_position(asset)
    notify_order_expired(asset, order_id, current_price)


def _get_fill_price(order_id: str) -> Optional[Decimal]:
    if _is_paper_order(order_id):
        return None  # caller falls back to the limit price used at order time
    try:
        price = Decimal(str(kc.get_order_status(order_id).get("price", "0")))
        return price if price > 0 else None
    except Exception:
        return None


# ── Exit monitor ──────────────────────────────────────────────────────────────

def _monitor_exit(asset: str) -> None:
    threading.Thread(
        target=_exit_monitor_worker,
        args=(asset,),
        name=f"exit-{asset}",
        daemon=True,
    ).start()


def _exit_monitor_worker(asset: str) -> None:
    logger.info(f"{asset}: exit monitor started")
    while True:
        try:
            pos = pm.get_position(asset)
            if pos is None or not pos.get("active", False):
                logger.info(f"{asset}: exit monitor stopping")
                return

            cfg = _load_cfg(pos)
            price = kc.get_ticker_price(asset)

            # 1. Check exits
            # Paper: pure price-based (no Kraken calls).
            # Live:  native order check covers the real SL;
            #        price-based check covers TPs (no native TP orders placed).
            import strategy as st
            if rm.is_paper_trading():
                _, reason = st.check_exit_conditions(asset, float(price), pos)
            else:
                _, native_reason = _check_native_order_fills(asset, pos, price)
                if native_reason:
                    reason = native_reason
                else:
                    _, price_reason = st.check_exit_conditions(asset, float(price), pos)
                    # Ignore the price-based SL trigger — the native Kraken SL handles it.
                    reason = price_reason if price_reason != "stop_loss" else ""

            if reason == "tp1" and not pos.get("tp1_hit"):
                _handle_tp1(asset, pos, price)
                pos = pm.get_position(asset)  # reload after update
            elif reason == "tp2" and not pos.get("tp2_hit"):
                _handle_tp2(asset, pos, price)
            elif reason == "tp3" and not pos.get("tp3_hit"):
                _handle_tp3(asset, pos, price)
            elif reason == "stop_loss":
                _handle_stop_loss(asset, pos, price)
                return

            # 2. Trailing stop (only after TP1)
            if pos.get("tp1_hit") and _check_trailing_stop(asset, pos, price, cfg):
                _handle_trailing_exit(asset, pos, price, cfg)
                return

            # 3. Time-based exit (flat position)
            if cfg.max_position_hours > 0 and _check_time_exit(pos, price, cfg):
                _handle_time_exit(asset, pos, price)
                return

        except Exception as exc:
            logger.warning(f"{asset}: exit monitor error: {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


def _check_native_order_fills(
    asset: str, pos: dict, current_price: Decimal
) -> tuple[bool, str]:
    for label, key in [("tp3", "order_id_tp3"), ("tp2", "order_id_tp2"),
                       ("tp1", "order_id_tp1"), ("stop_loss", "order_id_sl")]:
        order_id = pos.get(key)
        if not order_id:
            continue
        if _is_paper_order(order_id):
            continue  # paper TP/SL are checked via price levels in strategy.check_exit_conditions
        try:
            if kc.get_order_status(order_id).get("status") == "closed":
                return True, label
        except Exception as exc:
            logger.debug(f"{asset}: order check {label}: {exc}")
    return False, ""


# ── Trailing stop ─────────────────────────────────────────────────────────────

def _check_trailing_stop(
    asset: str, pos: dict, price: Decimal, cfg: StrategyConfig
) -> bool:
    """Update trailing_high and return True if trailing SL is breached."""
    trailing_high_raw = pos.get("trailing_high")
    if trailing_high_raw is None:
        return False

    trailing_high = Decimal(str(trailing_high_raw))
    new_high = max(trailing_high, price)

    if new_high > trailing_high:
        pm.update_position(asset, trailing_high=str(new_high))
        trailing_high = new_high

    trailing_sl = trailing_high * (Decimal("1") - cfg.trailing_stop_pct)
    entry = Decimal(str(pos["entry_price"]))

    # Only trigger if trailing SL is above entry (guarantees profit)
    return trailing_sl > entry and price <= trailing_sl


def _handle_trailing_exit(
    asset: str, pos: dict, price: Decimal, cfg: StrategyConfig
) -> None:
    trailing_high = Decimal(str(pos.get("trailing_high") or pos["entry_price"]))
    trailing_sl = (trailing_high * (Decimal("1") - cfg.trailing_stop_pct)).quantize(Decimal("0.01"))

    # Estimate remaining size (after TP1 partial close)
    size_asset = Decimal(str(pos["size_asset"]))
    remaining_pct = Decimal("1") - cfg.tp1_close_pct
    remaining = rm.floor_asset(size_asset * remaining_pct, asset)
    size_eur = Decimal(str(pos["size_eur"]))
    remaining_eur = size_eur * remaining_pct
    exit_value = (price * remaining).quantize(Decimal("0.01"))
    fee = (exit_value * Decimal("0.0016")).quantize(Decimal("0.01"))
    profit = exit_value - remaining_eur - fee + _tp1_profit(pos, cfg)

    if not rm.is_paper_trading():
        for key in ["order_id_sl", "order_id_tp2", "order_id_tp3"]:
            _cancel_if_live(pos.get(key))
        kc.place_limit_sell(asset, price, remaining)

    pm.close_position(asset)
    pm.record_closed_trade(
        asset=asset, entry_price=Decimal(str(pos["entry_price"])),
        exit_price=price, size_asset=remaining, size_eur=remaining_eur,
        pnl=profit, fee=fee, reason="trailing_stop",
    )
    notify_trailing_stop_exit(
        asset=asset, exit_price=price, trailing_high=trailing_high,
        trailing_sl=trailing_sl, profit=profit,
    )


# ── Time-based exit ───────────────────────────────────────────────────────────

def _check_time_exit(pos: dict, price: Decimal, cfg: StrategyConfig) -> bool:
    entry_time_str = pos.get("entry_time")
    if not entry_time_str:
        return False
    entry_time = datetime.fromisoformat(entry_time_str)
    if entry_time.tzinfo is None:
        entry_time = entry_time.replace(tzinfo=timezone.utc)
    hours_open = (datetime.now(timezone.utc) - entry_time).total_seconds() / 3600
    if hours_open < cfg.max_position_hours:
        return False
    entry_price = Decimal(str(pos["entry_price"]))
    pnl_pct = abs((price - entry_price) / entry_price)
    return pnl_pct < cfg.flat_threshold_pct


def _handle_time_exit(asset: str, pos: dict, price: Decimal) -> None:
    cfg = _load_cfg(pos)
    size_asset = Decimal(str(pos["size_asset"]))
    size_eur = Decimal(str(pos["size_eur"]))
    exit_value = (price * size_asset).quantize(Decimal("0.01"))
    fee = (exit_value * Decimal("0.0016")).quantize(Decimal("0.01"))
    pnl = exit_value - size_eur - fee
    hours_open = int((datetime.now(timezone.utc) -
                      datetime.fromisoformat(pos["entry_time"]).replace(tzinfo=timezone.utc)
                      ).total_seconds() / 3600)

    if not rm.is_paper_trading():
        for key in ["order_id_sl", "order_id_tp1", "order_id_tp2", "order_id_tp3"]:
            _cancel_if_live(pos.get(key))
        kc.place_limit_sell(asset, price, size_asset)

    pm.close_position(asset)
    pm.record_closed_trade(
        asset=asset, entry_price=Decimal(str(pos["entry_price"])),
        exit_price=price, size_asset=size_asset, size_eur=size_eur,
        pnl=pnl, fee=fee, reason="time_exit",
    )
    notify_time_exit(asset=asset, exit_price=price, pnl=pnl,
                     hours_open=hours_open, max_hours=cfg.max_position_hours)


# ── Signal-based exit ─────────────────────────────────────────────────────────

def _signal_exit_worker(asset: str, current_price: Decimal) -> None:
    try:
        pos = pm.get_position(asset)
        if not pos or not pos.get("active"):
            return

        cfg = _load_cfg(pos)
        size_asset = Decimal(str(pos["size_asset"]))

        # Account for partial closes already executed
        if pos.get("tp2_hit"):
            remaining_pct = Decimal("1") - cfg.tp1_close_pct - cfg.tp2_close_pct
        elif pos.get("tp1_hit"):
            remaining_pct = Decimal("1") - cfg.tp1_close_pct
        else:
            remaining_pct = Decimal("1")

        remaining = rm.floor_asset(size_asset * remaining_pct, asset)
        size_eur = Decimal(str(pos["size_eur"]))
        remaining_eur = size_eur * remaining_pct
        exit_value = (current_price * remaining).quantize(Decimal("0.01"))
        fee = (exit_value * Decimal("0.0016")).quantize(Decimal("0.01"))
        pnl = exit_value - remaining_eur - fee

        if not rm.is_paper_trading():
            for key in ["order_id_sl", "order_id_tp1", "order_id_tp2", "order_id_tp3"]:
                _cancel_if_live(pos.get(key))
            kc.place_limit_sell(asset, current_price, remaining)

        pm.close_position(asset)
        pm.record_closed_trade(
            asset=asset, entry_price=Decimal(str(pos["entry_price"])),
            exit_price=current_price, size_asset=remaining, size_eur=remaining_eur,
            pnl=pnl, fee=fee, reason="signal_exit",
        )
        notify_signal_exit(asset=asset, exit_price=current_price, pnl=pnl)

    except Exception as exc:
        logger.exception(f"{asset}: signal exit error: {exc}")


# ── TP handlers ───────────────────────────────────────────────────────────────

def _handle_tp1(asset: str, pos: dict, price: Decimal) -> None:
    cfg = _load_cfg(pos)
    entry    = Decimal(str(pos["entry_price"]))
    tp1      = Decimal(str(pos["tp1"]))
    tp1_size = rm.floor_asset(Decimal(str(pos["size_asset"])) * cfg.tp1_close_pct, asset)
    tp1_value = (tp1 * tp1_size).quantize(Decimal("0.01"))
    fee      = (tp1_value * Decimal("0.0016")).quantize(Decimal("0.01"))
    cost     = (Decimal(str(pos["size_eur"])) * cfg.tp1_close_pct).quantize(Decimal("0.01"))
    profit   = tp1_value - cost - fee

    remaining_pct = Decimal("1") - cfg.tp1_close_pct
    remaining = rm.floor_asset(Decimal(str(pos["size_asset"])) * remaining_pct, asset)

    if not rm.is_paper_trading():
        # Cancel full-position SL so the asset is freed for the partial sell.
        _cancel_if_live(pos.get("order_id_sl"))
        # Execute the TP1 partial close.
        tp1_size_clamped = _clamp_to_min(asset, tp1_size, Decimal(str(pos["size_asset"])))
        if tp1_size_clamped > Decimal("0"):
            kc.place_limit_sell(asset, tp1, tp1_size_clamped)
        # Re-place native SL at breakeven for the remaining portion.
        new_sl_id = kc.place_stop_loss(asset, entry, remaining)
        pm.update_position(asset, order_id_sl=new_sl_id)

    # Initialise trailing_high at TP1 price for trailing stop logic
    pm.update_position(asset, tp1_hit=True, trailing_high=str(tp1))
    pm.record_closed_trade(
        asset=asset, entry_price=entry, exit_price=tp1,
        size_asset=tp1_size, size_eur=cost, pnl=profit, fee=fee, reason="tp1",
    )

    notify_tp1_hit(
        asset=asset, tp1_price=tp1, tp1_value=tp1_value, profit=profit,
        breakeven=entry, tp2=Decimal(str(pos["tp2"])),
        tp3=Decimal(str(pos["tp3"])) if pos.get("tp3") else None,
        remaining_size=remaining,
        remaining_value=(entry * remaining).quantize(Decimal("0.01")),
        trailing_pct=cfg.trailing_stop_pct,
    )


def _handle_tp2(asset: str, pos: dict, price: Decimal) -> None:
    cfg = _load_cfg(pos)
    tp2       = Decimal(str(pos["tp2"]))
    tp2_size  = rm.floor_asset(Decimal(str(pos["size_asset"])) * cfg.tp2_close_pct, asset)
    tp2_value = (tp2 * tp2_size).quantize(Decimal("0.01"))
    fee       = (tp2_value * Decimal("0.0016")).quantize(Decimal("0.01"))
    entry_eur = Decimal(str(pos["size_eur"]))
    entry     = Decimal(str(pos["entry_price"]))

    has_tp3 = pos.get("tp3") is not None

    if not rm.is_paper_trading():
        # Cancel breakeven SL (placed after TP1); free asset for the partial sell.
        _cancel_if_live(pos.get("order_id_sl"))
        tp2_size_clamped = _clamp_to_min(asset, tp2_size, Decimal(str(pos["size_asset"])))
        if tp2_size_clamped > Decimal("0"):
            kc.place_limit_sell(asset, tp2, tp2_size_clamped)
        if has_tp3:
            # Re-place SL at breakeven for the remaining tp3 portion.
            tp3_size = rm.floor_asset(Decimal(str(pos["size_asset"])) * cfg.tp3_close_pct, asset)
            if tp3_size > Decimal("0"):
                new_sl_id = kc.place_stop_loss(asset, entry, tp3_size)
                pm.update_position(asset, order_id_sl=new_sl_id)

    if not has_tp3:
        pm.close_position(asset)
    else:
        pm.update_position(asset, tp2_hit=True)

    partial_pnl = tp2_value - (entry_eur * cfg.tp2_close_pct) - fee
    pm.record_closed_trade(
        asset=asset, entry_price=entry, exit_price=tp2,
        size_asset=tp2_size, size_eur=entry_eur * cfg.tp2_close_pct,
        pnl=partial_pnl, fee=fee, reason="tp2",
    )

    if not has_tp3:
        profit_total = partial_pnl + _tp1_profit(pos, cfg)
        roi = (profit_total / entry_eur * Decimal("100")).quantize(Decimal("0.1"))
        notify_tp2_hit(asset=asset, tp2_size=tp2_size, tp2_price=tp2,
                       profit_total=profit_total, roi=roi, final=True)
    else:
        notify_tp2_hit(asset=asset, tp2_size=tp2_size, tp2_price=tp2,
                       profit_total=partial_pnl, roi=Decimal("0"), final=False,
                       tp3=Decimal(str(pos["tp3"])))


def _handle_tp3(asset: str, pos: dict, price: Decimal) -> None:
    cfg = _load_cfg(pos)
    tp3       = Decimal(str(pos["tp3"]))
    tp3_size  = rm.floor_asset(Decimal(str(pos["size_asset"])) * cfg.tp3_close_pct, asset)
    tp3_value = (tp3 * tp3_size).quantize(Decimal("0.01"))
    fee       = (tp3_value * Decimal("0.0016")).quantize(Decimal("0.01"))
    entry_eur = Decimal(str(pos["size_eur"]))
    partial_pnl  = tp3_value - (entry_eur * cfg.tp3_close_pct) - fee
    profit_total = _tp1_profit(pos, cfg) + _tp2_profit(pos, cfg) + partial_pnl
    roi = (profit_total / entry_eur * Decimal("100")).quantize(Decimal("0.1"))

    if not rm.is_paper_trading():
        # Cancel remaining SL (from after TP2), then sell the final portion.
        _cancel_if_live(pos.get("order_id_sl"))
        tp3_size_clamped = _clamp_to_min(asset, tp3_size, Decimal(str(pos["size_asset"])))
        if tp3_size_clamped > Decimal("0"):
            kc.place_limit_sell(asset, tp3, tp3_size_clamped)

    pm.close_position(asset)
    pm.record_closed_trade(
        asset=asset, entry_price=Decimal(str(pos["entry_price"])), exit_price=tp3,
        size_asset=tp3_size, size_eur=entry_eur * cfg.tp3_close_pct,
        pnl=partial_pnl, fee=fee, reason="tp3",
    )
    notify_tp3_hit(asset=asset, tp3_size=tp3_size, tp3_price=tp3,
                   profit_total=profit_total, roi=roi)


def _handle_stop_loss(asset: str, pos: dict, price: Decimal) -> None:
    cfg = _load_cfg(pos)
    sl         = Decimal(str(pos["sl"]))
    size_asset = Decimal(str(pos["size_asset"]))
    size_eur   = Decimal(str(pos["size_eur"]))
    sl_value   = (sl * size_asset).quantize(Decimal("0.01"))
    fee        = (sl_value * Decimal("0.0016")).quantize(Decimal("0.01"))
    loss       = sl_value - size_eur - fee
    new_losses = pos.get("consecutive_losses", 0) + 1

    pm.close_position(asset)
    pm.update_position(asset, consecutive_losses=new_losses)
    pm.record_closed_trade(
        asset=asset, entry_price=Decimal(str(pos["entry_price"])), exit_price=sl,
        size_asset=size_asset, size_eur=size_eur, pnl=loss, fee=fee, reason="stop_loss",
    )
    notify_stop_loss(asset=asset, sl_price=sl, loss=loss, consecutive_losses=new_losses)

    if new_losses >= cfg.max_consecutive_losses:
        from telegram_notify import notify_pause_activated
        resume_at = rm.set_global_pause(24)
        notify_pause_activated(loss_total=loss * new_losses, resume_at=resume_at)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_cfg(pos: dict) -> StrategyConfig:
    from risk_manager import AGGRESSIVE
    return AGGRESSIVE if pos.get("strategy") == "aggressive" else rm.CONSERVATIVE


def _tp1_profit(pos: dict, cfg: StrategyConfig) -> Decimal:
    if not pos.get("tp1_hit"):
        return Decimal("0")
    tp1 = Decimal(str(pos["tp1"]))
    size = Decimal(str(pos["size_asset"]))
    cost = Decimal(str(pos["size_eur"])) * cfg.tp1_close_pct
    return (tp1 * size * cfg.tp1_close_pct) - cost


def _tp2_profit(pos: dict, cfg: StrategyConfig) -> Decimal:
    if not pos.get("tp2_hit"):
        return Decimal("0")
    tp2 = Decimal(str(pos["tp2"]))
    size = Decimal(str(pos["size_asset"]))
    cost = Decimal(str(pos["size_eur"])) * cfg.tp2_close_pct
    return (tp2 * size * cfg.tp2_close_pct) - cost

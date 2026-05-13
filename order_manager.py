"""
Handles the full order lifecycle:
  - Place entry limit buy
  - Poll until filled (or cancel after 30 min)
  - On fill: place SL (native Kraken stop-loss) and TP limit sells
  - Monitor TP1/TP2/TP3 hits and update SL to breakeven after TP1
Each asset gets its own polling thread so the main loop is never blocked.
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
)

ORDER_TIMEOUT_SECONDS = 30 * 60
POLL_INTERVAL_SECONDS = 60


def open_position(asset: str, signal_result) -> None:
    thread = threading.Thread(
        target=_open_position_worker,
        args=(asset, signal_result),
        name=f"open-{asset}",
        daemon=True,
    )
    thread.start()


def _open_position_worker(asset: str, signal_result) -> None:
    try:
        cfg = rm.get_strategy_config()
        capital = rm.get_capital()
        eur_balance = kc.get_eur_balance()
        price = kc.get_ticker_price(asset)

        size_eur, size_asset = rm.calculate_position_size(asset, price, capital, eur_balance, cfg)
        if size_asset == Decimal("0"):
            logger.warning(f"{asset}: cannot size position, skipping")
            return

        sl, tp1, tp2, tp3 = rm.calculate_levels(price, cfg)

        if rm.is_paper_trading():
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
        fee = (actual_value * kc.get_trade_fee(asset)).quantize(Decimal("0.01"))

        if rm.is_paper_trading():
            sl_order_id  = f"PAPER-SL-{asset}-{int(time.time())}"
            tp1_order_id = f"PAPER-TP1-{asset}-{int(time.time())}"
            tp2_order_id = f"PAPER-TP2-{asset}-{int(time.time())}"
            tp3_order_id = f"PAPER-TP3-{asset}-{int(time.time())}" if tp3 else None
        else:
            sl_order_id = kc.place_stop_loss(asset, sl, size_asset)
            tp1_size = rm.floor_asset(size_asset * cfg.tp1_close_pct, asset)
            tp2_size = rm.floor_asset(size_asset * cfg.tp2_close_pct, asset)
            tp1_order_id = kc.place_limit_sell(asset, tp1, tp1_size)
            tp2_order_id = kc.place_limit_sell(asset, tp2, tp2_size)
            if tp3 and cfg.tp3_close_pct:
                tp3_size = rm.floor_asset(size_asset * cfg.tp3_close_pct, asset)
                tp3_order_id = kc.place_limit_sell(asset, tp3, tp3_size)
            else:
                tp3_order_id = None

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
        )

        _monitor_exit(asset)

    except Exception as exc:
        logger.exception(f"{asset}: error in open_position_worker: {exc}")


def _wait_for_fill(order_id: str, asset: str) -> bool:
    deadline = time.time() + ORDER_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        try:
            info = kc.get_order_status(order_id)
            status = info.get("status", "unknown")
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
    try:
        info = kc.get_order_status(order_id)
        price = Decimal(str(info.get("price", "0")))
        return price if price > 0 else None
    except Exception:
        return None


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

            price = kc.get_ticker_price(asset)
            _, reason = _check_native_order_fills(asset, pos, price)

            if reason == "tp1" and not pos.get("tp1_hit"):
                _handle_tp1(asset, pos, price)
            elif reason == "tp2" and not pos.get("tp2_hit"):
                _handle_tp2(asset, pos, price)
            elif reason == "tp3" and not pos.get("tp3_hit"):
                _handle_tp3(asset, pos, price)
            elif reason == "stop_loss":
                _handle_stop_loss(asset, pos, price)

        except Exception as exc:
            logger.warning(f"{asset}: exit monitor error: {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


def _check_native_order_fills(
    asset: str, pos: dict, current_price: Decimal
) -> tuple[bool, str]:
    checks = [
        ("tp3", "order_id_tp3"),
        ("tp2", "order_id_tp2"),
        ("tp1", "order_id_tp1"),
        ("stop_loss", "order_id_sl"),
    ]
    for label, key in checks:
        order_id = pos.get(key)
        if not order_id:
            continue
        try:
            if kc.get_order_status(order_id).get("status") == "closed":
                return True, label
        except Exception as exc:
            logger.debug(f"{asset}: order status check failed for {label}: {exc}")
    return False, ""


def _load_cfg(pos: dict) -> StrategyConfig:
    from risk_manager import CONSERVATIVE, AGGRESSIVE
    return AGGRESSIVE if pos.get("strategy") == "aggressive" else CONSERVATIVE


def _handle_tp1(asset: str, pos: dict, price: Decimal) -> None:
    cfg = _load_cfg(pos)
    entry   = Decimal(str(pos["entry_price"]))
    tp1     = Decimal(str(pos["tp1"]))
    tp1_size = rm.floor_asset(Decimal(str(pos["size_asset"])) * cfg.tp1_close_pct, asset)
    tp1_value = (tp1 * tp1_size).quantize(Decimal("0.01"))
    fee     = (tp1_value * kc.get_trade_fee(asset)).quantize(Decimal("0.01"))
    cost    = (Decimal(str(pos["size_eur"])) * cfg.tp1_close_pct).quantize(Decimal("0.01"))
    profit  = tp1_value - cost - fee

    remaining_pct = Decimal("1") - cfg.tp1_close_pct
    remaining = rm.floor_asset(Decimal(str(pos["size_asset"])) * remaining_pct, asset)

    if not rm.is_paper_trading():
        if pos.get("order_id_sl"):
            kc.cancel_order(pos["order_id_sl"])
        new_sl_id = kc.place_stop_loss(asset, entry, remaining)
        pm.update_position(asset, order_id_sl=new_sl_id)

    pm.update_position(asset, tp1_hit=True)
    pm.record_closed_trade(
        asset=asset, entry_price=entry, exit_price=tp1,
        size_asset=tp1_size, size_eur=cost, pnl=profit, fee=fee, reason="tp1",
    )

    notify_tp1_hit(
        asset=asset,
        tp1_price=tp1,
        tp1_value=tp1_value,
        profit=profit,
        breakeven=entry,
        tp2=Decimal(str(pos["tp2"])),
        tp3=Decimal(str(pos["tp3"])) if pos.get("tp3") else None,
        remaining_size=remaining,
        remaining_value=(entry * remaining).quantize(Decimal("0.01")),
    )


def _handle_tp2(asset: str, pos: dict, price: Decimal) -> None:
    cfg = _load_cfg(pos)
    tp2     = Decimal(str(pos["tp2"]))
    tp2_size = rm.floor_asset(Decimal(str(pos["size_asset"])) * cfg.tp2_close_pct, asset)
    tp2_value = (tp2 * tp2_size).quantize(Decimal("0.01"))
    fee     = (tp2_value * kc.get_trade_fee(asset)).quantize(Decimal("0.01"))
    entry_eur = Decimal(str(pos["size_eur"]))

    # If there's a TP3, keep the position open for that leg
    has_tp3 = pos.get("tp3") is not None
    if not has_tp3:
        pm.close_position(asset)
    else:
        pm.update_position(asset, tp2_hit=True)

    tp1_profit = _tp1_profit(pos, cfg)
    partial_pnl = tp2_value - (entry_eur * cfg.tp2_close_pct) - fee
    pm.record_closed_trade(
        asset=asset,
        entry_price=Decimal(str(pos["entry_price"])),
        exit_price=tp2,
        size_asset=tp2_size,
        size_eur=entry_eur * cfg.tp2_close_pct,
        pnl=partial_pnl,
        fee=fee,
        reason="tp2",
    )

    if not has_tp3:
        profit_total = partial_pnl + tp1_profit
        roi = (profit_total / entry_eur * Decimal("100")).quantize(Decimal("0.1"))
        notify_tp2_hit(asset=asset, tp2_size=tp2_size, tp2_price=tp2,
                       profit_total=profit_total, roi=roi, final=True)
    else:
        notify_tp2_hit(asset=asset, tp2_size=tp2_size, tp2_price=tp2,
                       profit_total=partial_pnl, roi=Decimal("0"), final=False,
                       tp3=Decimal(str(pos["tp3"])))


def _handle_tp3(asset: str, pos: dict, price: Decimal) -> None:
    cfg = _load_cfg(pos)
    tp3     = Decimal(str(pos["tp3"]))
    tp3_size = rm.floor_asset(Decimal(str(pos["size_asset"])) * cfg.tp3_close_pct, asset)
    tp3_value = (tp3 * tp3_size).quantize(Decimal("0.01"))
    fee     = (tp3_value * kc.get_trade_fee(asset)).quantize(Decimal("0.01"))
    entry_eur = Decimal(str(pos["size_eur"]))

    tp1_profit = _tp1_profit(pos, cfg)
    tp2_profit = _tp2_profit(pos, cfg)
    partial_pnl = tp3_value - (entry_eur * cfg.tp3_close_pct) - fee
    profit_total = tp1_profit + tp2_profit + partial_pnl
    roi = (profit_total / entry_eur * Decimal("100")).quantize(Decimal("0.1"))

    pm.close_position(asset)
    pm.record_closed_trade(
        asset=asset,
        entry_price=Decimal(str(pos["entry_price"])),
        exit_price=tp3,
        size_asset=tp3_size,
        size_eur=entry_eur * cfg.tp3_close_pct,
        pnl=partial_pnl,
        fee=fee,
        reason="tp3",
    )
    notify_tp3_hit(
        asset=asset, tp3_size=tp3_size, tp3_price=tp3,
        profit_total=profit_total, roi=roi,
    )


def _handle_stop_loss(asset: str, pos: dict, price: Decimal) -> None:
    cfg = _load_cfg(pos)
    sl       = Decimal(str(pos["sl"]))
    size_asset = Decimal(str(pos["size_asset"]))
    size_eur = Decimal(str(pos["size_eur"]))
    sl_value = (sl * size_asset).quantize(Decimal("0.01"))
    fee      = (sl_value * kc.get_trade_fee(asset)).quantize(Decimal("0.01"))
    loss     = sl_value - size_eur - fee

    old_losses = pos.get("consecutive_losses", 0)
    new_losses = old_losses + 1

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


def _tp1_profit(pos: dict, cfg: StrategyConfig) -> Decimal:
    if not pos.get("tp1_hit"):
        return Decimal("0")
    tp1 = Decimal(str(pos["tp1"]))
    size_asset = Decimal(str(pos["size_asset"]))
    cost = Decimal(str(pos["size_eur"])) * cfg.tp1_close_pct
    return (tp1 * size_asset * cfg.tp1_close_pct) - cost


def _tp2_profit(pos: dict, cfg: StrategyConfig) -> Decimal:
    if not pos.get("tp2_hit"):
        return Decimal("0")
    tp2 = Decimal(str(pos["tp2"]))
    size_asset = Decimal(str(pos["size_asset"]))
    cost = Decimal(str(pos["size_eur"])) * cfg.tp2_close_pct
    return (tp2 * size_asset * cfg.tp2_close_pct) - cost

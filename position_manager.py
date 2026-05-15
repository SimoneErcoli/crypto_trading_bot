"""
Reads and writes positions.json — single source of truth for all open positions.
Uses file locking to avoid race conditions with the order-polling threads.
"""

import json
import threading
from decimal import Decimal
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

POSITIONS_FILE = Path("positions.json")
TRADES_FILE = Path("trades_history.json")
_lock = threading.Lock()
_trades_lock = threading.Lock()


def _load_raw() -> dict:
    if not POSITIONS_FILE.exists():
        return {}
    try:
        return json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("positions.json corrupted — resetting to empty")
        return {}


def _save_raw(data: dict) -> None:
    POSITIONS_FILE.write_text(
        json.dumps(data, indent=2, default=str), encoding="utf-8"
    )


def get_position(asset: str) -> Optional[dict]:
    with _lock:
        return _load_raw().get(asset)


def get_all_positions() -> dict:
    with _lock:
        return _load_raw()


def save_position(asset: str, position: dict) -> None:
    with _lock:
        data = _load_raw()
        data[asset] = position
        _save_raw(data)
    logger.debug(f"Position saved for {asset}")


def close_position(asset: str) -> None:
    with _lock:
        data = _load_raw()
        if asset in data:
            data[asset]["active"] = False
            data[asset]["close_time"] = datetime.now(timezone.utc).isoformat()
            _save_raw(data)
    logger.info(f"Position closed for {asset}")


def update_position(asset: str, **kwargs) -> None:
    with _lock:
        data = _load_raw()
        if asset not in data:
            logger.warning(f"update_position: {asset} not found")
            return
        for key, value in kwargs.items():
            data[asset][key] = value
        _save_raw(data)


def has_active_position(asset: str) -> bool:
    pos = get_position(asset)
    return pos is not None and pos.get("active", False)


def is_in_cooldown(asset: str, cooldown_hours: int = 4) -> bool:
    pos = get_position(asset)
    if pos is None or pos.get("active", False):
        return False
    close_time_str = pos.get("close_time")
    if not close_time_str:
        return False
    close_time = datetime.fromisoformat(close_time_str)
    if close_time.tzinfo is None:
        close_time = close_time.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - close_time).total_seconds() / 3600
    return elapsed < cooldown_hours


def get_consecutive_losses() -> int:
    data = get_all_positions()
    max_losses = 0
    for pos in data.values():
        losses = pos.get("consecutive_losses", 0)
        if losses > max_losses:
            max_losses = losses
    return max_losses


def record_closed_trade(
    asset: str,
    entry_price: Decimal,
    exit_price: Decimal,
    size_asset: Decimal,
    size_eur: Decimal,
    pnl: Decimal,
    fee: Decimal,
    reason: str,
) -> None:
    """Append a closed trade to trades_history.json for the dashboard."""
    trade = {
        "asset": asset,
        "entry_price": str(entry_price),
        "exit_price": str(exit_price),
        "size_asset": str(size_asset),
        "size_eur": str(size_eur),
        "pnl": str(pnl),
        "fee": str(fee),
        "reason": reason,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }
    with _trades_lock:
        if TRADES_FILE.exists():
            try:
                history = json.loads(TRADES_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                history = []
        else:
            history = []
        history.append(trade)
        TRADES_FILE.write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")


def get_trade_history() -> list[dict]:
    with _trades_lock:
        if not TRADES_FILE.exists():
            return []
        try:
            return json.loads(TRADES_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []


def build_new_position(
    asset: str,
    entry_price: Decimal,
    size_eur: Decimal,
    size_asset: Decimal,
    order_id_entry: str,
    sl: Decimal,
    tp1: Decimal,
    tp2: Decimal,
    tp3: Optional[Decimal] = None,
    strategy: str = "conservative",
) -> dict:
    return {
        "active": False,
        "strategy": strategy,
        "entry_price": str(entry_price),
        "entry_time": None,
        "size_eur": str(size_eur),
        "size_asset": str(size_asset),
        "order_id_entry": order_id_entry,
        "order_id_sl": None,
        "order_id_tp1": None,
        "order_id_tp2": None,
        "order_id_tp3": None,
        "sl": str(sl),
        "tp1": str(tp1),
        "tp2": str(tp2),
        "tp3": str(tp3) if tp3 is not None else None,
        "tp1_hit": False,
        "tp2_hit": False,
        "tp3_hit": False,
        "trailing_high": None,      # set after TP1 for trailing stop
        "consecutive_losses": 0,
        "close_time": None,
    }

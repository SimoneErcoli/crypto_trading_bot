"""
Thin wrapper around krakenex + pykrakenapi.
Handles rate limiting and retry with exponential backoff.
Respects Kraken limits: 1 public call/s, 1 private call/2s.
"""

import os
import time
import functools
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

import krakenex
import pandas as pd
from loguru import logger

# Asset pair mapping: internal symbol → Kraken pair name
KRAKEN_PAIRS: dict[str, str] = {
    "BTC":  "XXBTZEUR",
    "ETH":  "XETHZEUR",
    "SOL":  "SOLEUR",
    "XRP":  "XXRPZEUR",
    "ADA":  "ADAEUR",
    "AVAX": "AVAXEUR",
    "DOT":  "DOTEUR",
    "LINK": "LINKEUR",
    "LTC":  "XLTCZEUR",
    "ATOM": "ATOMEUR",
}

# Kraken asset symbols (for balance lookup)
KRAKEN_ASSET_SYMBOLS: dict[str, str] = {
    "BTC":  "XXBT",
    "ETH":  "XETH",
    "SOL":  "SOL",
    "XRP":  "XXRP",
    "ADA":  "ADA",
    "AVAX": "AVAX",
    "DOT":  "DOT",
    "LINK": "LINK",
    "LTC":  "XLTC",
    "ATOM": "ATOM",
}

# Per-asset price decimal precision as enforced by Kraken
# Source: Kraken AssetPairs `pair_decimals` field
PRICE_DECIMALS: dict[str, int] = {
    "BTC":  1,
    "ETH":  2,
    "SOL":  2,
    "XRP":  5,
    "ADA":  6,
    "AVAX": 2,
    "DOT":  4,
    "LINK": 4,
    "LTC":  2,
    "ATOM": 4,
}

RETRY_DELAYS = [30, 60, 120]
_last_public_call = 0.0
_last_private_call = 0.0

_api: Optional[krakenex.API] = None
_ordermin_cache: dict[str, Decimal] = {}   # populated lazily by get_ordermin()


def round_price(asset: str, price: Decimal) -> Decimal:
    """Round a price to the decimal precision Kraken accepts for that asset pair."""
    decimals = PRICE_DECIMALS[asset]
    return price.quantize(Decimal(f"1e-{decimals}"), rounding=ROUND_HALF_UP)


def get_ordermin(asset: str) -> Optional[Decimal]:
    """
    Return the minimum order volume for *asset* as reported by Kraken's AssetPairs API.
    Results are cached for the process lifetime (the value never changes at runtime).
    Returns None if the API call fails — callers should fall back to MIN_ORDER_SIZE.
    """
    if asset in _ordermin_cache:
        return _ordermin_cache[asset]

    pair = KRAKEN_PAIRS[asset]
    api = get_api()
    try:
        _rate_limit_public()
        result = api.query_public("AssetPairs", {"pair": pair})
        if result.get("error"):
            logger.debug(f"{asset}: AssetPairs error {result['error']}")
            return None
        pair_data = next(iter(result["result"].values()))
        raw_min = pair_data.get("ordermin")
        if raw_min:
            ordermin = Decimal(str(raw_min))
            _ordermin_cache[asset] = ordermin
            logger.debug(f"{asset}: Kraken live ordermin={ordermin}")
            return ordermin
    except Exception as exc:
        logger.debug(f"{asset}: could not fetch ordermin: {exc}")
    return None


def warmup_ordermin_cache(assets: list[str]) -> None:
    """
    Pre-fetch the live minimum order sizes for all *assets* in a single public API call.
    Call once at bot startup so the cache is hot before any order is placed.
    For assets where the fetch fails the hardcoded MIN_ORDER_SIZE is used as fallback.
    Always prints a summary table (INFO level) so startup logs show the active minimums.
    """
    from risk_manager import MIN_ORDER_SIZE  # local import to avoid circular dep at module load

    pairs_str = ",".join(KRAKEN_PAIRS[a] for a in assets)
    api = get_api()
    try:
        _rate_limit_public()
        result = api.query_public("AssetPairs", {"pair": pairs_str})
        errors = result.get("error") or []
        if errors:
            logger.warning(f"warmup_ordermin_cache: AssetPairs returned errors {errors} — using hardcoded minimums")
        else:
            kraken_data = result.get("result", {})
            for asset in assets:
                pair = KRAKEN_PAIRS[asset]
                # Kraken may return the pair under its canonical name or an altname; try both
                pair_data = kraken_data.get(pair)
                if pair_data is None:
                    # fuzzy fallback: find any key that contains or is contained by the pair name
                    matched_key = next(
                        (k for k in kraken_data if pair.upper() in k.upper() or k.upper() in pair.upper()),
                        None,
                    )
                    if matched_key:
                        pair_data = kraken_data[matched_key]

                if pair_data is None:
                    logger.warning(f"{asset}: not found in AssetPairs response — will use hardcoded {MIN_ORDER_SIZE[asset]}")
                    continue

                raw_min = pair_data.get("ordermin")
                if raw_min:
                    ordermin = Decimal(str(raw_min))
                    _ordermin_cache[asset] = ordermin

    except Exception as exc:
        logger.warning(f"warmup_ordermin_cache: API call failed ({exc}) — all assets will use hardcoded minimums")

    # Always print the active minimum for every asset so startup logs are self-documenting
    lines = []
    for asset in assets:
        live = _ordermin_cache.get(asset)
        hardcoded = MIN_ORDER_SIZE[asset]
        if live is not None:
            flag = "⚠ DIFFERS" if live != hardcoded else "✓"
            lines.append(f"  {asset:<5} live={live}  hardcoded={hardcoded}  {flag}")
        else:
            lines.append(f"  {asset:<5} live=N/A  hardcoded={hardcoded}  (fallback)")
    logger.info("Order minimums in effect:\n" + "\n".join(lines))


def get_api() -> krakenex.API:
    global _api
    if _api is None:
        _api = krakenex.API()
        _api.key = os.getenv("KRAKEN_API_KEY", "")
        _api.secret = os.getenv("KRAKEN_API_SECRET", "")
    return _api


def _rate_limit_public() -> None:
    global _last_public_call
    elapsed = time.time() - _last_public_call
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_public_call = time.time()


def _rate_limit_private() -> None:
    global _last_private_call
    elapsed = time.time() - _last_private_call
    if elapsed < 2.0:
        time.sleep(2.0 - elapsed)
    _last_private_call = time.time()


def _with_retry(fn, *args, is_private: bool = False, **kwargs) -> Any:
    """Call fn with exponential backoff retry on error."""
    from telegram_notify import notify_api_error  # late import to avoid cycles

    for attempt, delay in enumerate(RETRY_DELAYS, start=1):
        try:
            if is_private:
                _rate_limit_private()
            else:
                _rate_limit_public()
            result = fn(*args, **kwargs)
            if isinstance(result, dict) and result.get("error"):
                errors = result["error"]
                raise RuntimeError(f"Kraken API error: {errors}")
            return result
        except Exception as exc:
            logger.warning(f"API attempt {attempt}/{len(RETRY_DELAYS)} failed: {exc}")
            try:
                notify_api_error(attempt, len(RETRY_DELAYS), str(exc), delay)
            except Exception:
                pass
            if attempt < len(RETRY_DELAYS):
                time.sleep(delay)
            else:
                logger.error(f"All {len(RETRY_DELAYS)} API attempts failed: {exc}")
                raise


def get_ohlcv(asset: str, interval: int = 240, count: int = 200) -> pd.DataFrame:
    """Fetch OHLCV candles. Returns DataFrame with columns: open,high,low,close,volume."""
    pair = KRAKEN_PAIRS[asset]
    api = get_api()

    def _call():
        return api.query_public("OHLC", {"pair": pair, "interval": interval})

    raw = _with_retry(_call)
    data = raw["result"][pair]

    df = pd.DataFrame(
        data,
        columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"],
    )
    for col in ["open", "high", "low", "close", "vwap", "volume"]:
        df[col] = pd.to_numeric(df[col])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df = df.set_index("time").sort_index()

    # Drop the last (potentially open) candle
    if len(df) > 1:
        df = df.iloc[:-1]

    return df.tail(count)


def get_ticker_price(asset: str) -> Decimal:
    """Return the last trade price for the asset."""
    pair = KRAKEN_PAIRS[asset]
    api = get_api()

    def _call():
        return api.query_public("Ticker", {"pair": pair})

    raw = _with_retry(_call)
    price_str = raw["result"][pair]["c"][0]
    return Decimal(price_str)


def get_all_balances() -> dict[str, Decimal]:
    """
    Fetch all account balances in a single private API call.
    Returns a dict keyed by internal symbol ('EUR', 'BTC', 'ETH', …).
    """
    api = get_api()

    def _call():
        return api.query_private("Balance")

    raw = _with_retry(_call, is_private=True)
    result = raw["result"]
    balances: dict[str, Decimal] = {}
    balances["EUR"] = Decimal(result.get("ZEUR", "0"))
    for asset, symbol in KRAKEN_ASSET_SYMBOLS.items():
        balances[asset] = Decimal(result.get(symbol, "0"))
    return balances


def get_eur_balance() -> Decimal:
    """Return available EUR balance (convenience wrapper over get_all_balances)."""
    api = get_api()

    def _call():
        return api.query_private("Balance")

    raw = _with_retry(_call, is_private=True)
    return Decimal(raw["result"].get("ZEUR", "0"))


def get_asset_balance(asset: str) -> Decimal:
    """Return available balance for a given asset."""
    api = get_api()
    kraken_symbol = KRAKEN_ASSET_SYMBOLS[asset]

    def _call():
        return api.query_private("Balance")

    raw = _with_retry(_call, is_private=True)
    return Decimal(raw["result"].get(kraken_symbol, "0"))


def place_limit_buy(
    asset: str,
    price: Decimal,
    volume: Decimal,
) -> str:
    """Place a limit buy order. Returns the order transaction ID."""
    pair = KRAKEN_PAIRS[asset]
    api = get_api()
    price = round_price(asset, price)

    def _call():
        return api.query_private("AddOrder", {
            "pair": pair,
            "type": "buy",
            "ordertype": "limit",
            "price": str(price),
            "volume": str(volume),
        })

    raw = _with_retry(_call, is_private=True)
    txid = raw["result"]["txid"][0]
    logger.info(f"Limit buy placed for {asset}: txid={txid}")
    return txid


def place_limit_sell(
    asset: str,
    price: Decimal,
    volume: Decimal,
) -> str:
    """Place a limit sell order. Returns the order transaction ID."""
    pair = KRAKEN_PAIRS[asset]
    api = get_api()
    price = round_price(asset, price)

    def _call():
        return api.query_private("AddOrder", {
            "pair": pair,
            "type": "sell",
            "ordertype": "limit",
            "price": str(price),
            "volume": str(volume),
        })

    raw = _with_retry(_call, is_private=True)
    txid = raw["result"]["txid"][0]
    logger.info(f"Limit sell placed for {asset} at {price}: txid={txid}")
    return txid


def place_stop_loss(
    asset: str,
    stop_price: Decimal,
    volume: Decimal,
) -> str:
    """Place a native Kraken stop-loss order. Stays active even if bot is offline."""
    pair = KRAKEN_PAIRS[asset]
    api = get_api()
    stop_price = round_price(asset, stop_price)

    def _call():
        return api.query_private("AddOrder", {
            "pair": pair,
            "type": "sell",
            "ordertype": "stop-loss",
            "price": str(stop_price),
            "volume": str(volume),
        })

    raw = _with_retry(_call, is_private=True)
    txid = raw["result"]["txid"][0]
    logger.info(f"Stop-loss placed for {asset} at {stop_price}: txid={txid}")
    return txid


def cancel_order(order_id: str) -> bool:
    """Cancel an open order. Returns True on success."""
    api = get_api()

    def _call():
        return api.query_private("CancelOrder", {"txid": order_id})

    try:
        raw = _with_retry(_call, is_private=True)
        count = raw["result"].get("count", 0)
        logger.info(f"Cancelled order {order_id} (count={count})")
        return count > 0
    except Exception as exc:
        logger.error(f"Failed to cancel order {order_id}: {exc}")
        return False


def get_order_status(order_id: str) -> dict:
    """
    Returns order info dict with at least 'status' key.
    Possible statuses: 'pending', 'open', 'closed', 'canceled', 'expired'.
    """
    api = get_api()

    def _call():
        return api.query_private("QueryOrders", {"txid": order_id, "trades": True})

    raw = _with_retry(_call, is_private=True)
    return raw["result"].get(order_id, {})


def get_open_orders() -> dict:
    """
    Return all currently open orders on the account, keyed by txid.
    Each value is an order-info dict as returned by Kraken's OpenOrders endpoint.
    Typical keys inside each order: descr (pair, type, ordertype, price), vol, vol_exec, status.
    """
    api = get_api()

    def _call():
        return api.query_private("OpenOrders")

    raw = _with_retry(_call, is_private=True)
    return raw["result"].get("open", {})


def get_trade_fee(asset: str) -> Decimal:
    """Return the taker fee for an asset (used in P&L estimates)."""
    pair = KRAKEN_PAIRS[asset]
    api = get_api()

    def _call():
        return api.query_private("TradeVolume", {"pair": pair})

    try:
        raw = _with_retry(_call, is_private=True)
        fee_str = raw["result"]["fees"][pair]["fee"]
        return Decimal(fee_str) / Decimal("100")
    except Exception:
        return Decimal("0.0016")  # default maker fee

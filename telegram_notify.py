"""
Formats and sends all Telegram messages.
Uses python-telegram-bot in synchronous mode (Bot.send_message via asyncio).
Falls back to a direct HTTP POST if the library call fails.
"""

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import requests
from loguru import logger


def _chat_id() -> str:
    return os.getenv("TELEGRAM_CHAT_ID", "")


def _token() -> str:
    return os.getenv("TELEGRAM_BOT_TOKEN", "")


def _send(text: str) -> None:
    """Send a message via Telegram Bot API (plain HTTP POST, no async overhead)."""
    token = _token()
    chat_id = _chat_id()
    if not token or not chat_id:
        logger.warning("Telegram not configured — skipping notification")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            logger.warning(f"Telegram send failed: {resp.status_code} {resp.text[:200]}")
    except Exception as exc:
        logger.warning(f"Telegram send exception: {exc}")


SEP = "━━━━━━━━━━━━━━━"


def notify_startup(
    capital: Decimal,
    assets: list[str],
    paper: bool,
    next_scan: str,
    strategy: str = "conservative",
) -> None:
    mode_line = "⚠️ Modalità: LIVE TRADING" if not paper else "ℹ️ Modalità: PAPER TRADING"
    strategy_icon = "🔥 Aggressiva" if strategy == "aggressive" else "🛡 Conservativa"
    assets_str = " ".join(assets)
    text = (
        f"🤖 Bot avviato — {'ORDINI REALI' if not paper else 'PAPER TRADING'}\n"
        f"{SEP}\n"
        f"{mode_line}\n"
        f"Strategia: {strategy_icon}\n"
        f"Capitale: €{capital}\n"
        f"Asset: {assets_str}\n"
        f"Scan ogni: 4h\n"
        f"Prossimo scan: {next_scan}"
    )
    _send(text)


def notify_order_sent(
    asset: str,
    price: Decimal,
    size_asset: Decimal,
    size_eur: Decimal,
    order_id: str,
) -> None:
    text = (
        f"⏳ ORDINE INVIATO — {asset}/EUR\n"
        f"{SEP}\n"
        f"Tipo: Limit Buy\n"
        f"Prezzo limit: €{price}\n"
        f"Quantità: {size_asset} {asset} (€{size_eur})\n"
        f"Order ID: {order_id}\n"
        f"In attesa di esecuzione..."
    )
    _send(text)


def notify_position_opened(
    asset: str,
    entry_price: Decimal,
    size_asset: Decimal,
    size_eur: Decimal,
    sl: Decimal,
    tp1: Decimal,
    tp2: Decimal,
    cfg,
    rsi: float,
    ema_above: bool,
    ema_ref: str,
    macd_bullish: bool,
    volume_surge: bool,
    fee: Decimal,
    tp3: Optional[Decimal] = None,
) -> None:
    sl_pct  = f"-{int(cfg.sl_pct * 100)}%"
    tp1_pct = f"+{int(cfg.tp1_pct * 100)}%"
    tp2_pct = f"+{int(cfg.tp2_pct * 100)}%"
    strategy_icon = "🔥" if cfg.name == "aggressive" else "🛡"

    lines = [
        f"🟢 POSIZIONE APERTA — {asset}/EUR  {strategy_icon}",
        SEP,
        f"✅ Ordine eseguito",
        f"💰 Prezzo entrata: €{entry_price}",
        f"📦 Quantità: {size_asset} {asset}",
        f"💵 Valore: €{size_eur}",
        f"🛡 Stop Loss: €{sl} ({sl_pct}) → ordine piazzato",
        f"🎯 TP1: €{tp1} ({tp1_pct}) → chiude {int(cfg.tp1_close_pct*100)}%",
        f"🎯 TP2: €{tp2} ({tp2_pct}) → chiude {int(cfg.tp2_close_pct*100)}%",
    ]
    if tp3 and cfg.tp3_pct:
        tp3_pct = f"+{int(cfg.tp3_pct * 100)}%"
        lines.append(f"🚀 TP3: €{tp3} ({tp3_pct}) → chiude {int(cfg.tp3_close_pct*100)}%")

    ema_label = ema_ref.upper()
    lines += [
        SEP,
        f"RSI: {rsi:.1f} {'✅' if cfg.rsi_buy_low <= rsi <= cfg.rsi_buy_high else '❌'}",
        f"{ema_label}: {'sopra ✅' if ema_above else 'sotto ❌'}",
        f"MACD: {'bullish ✅' if macd_bullish else 'bearish ❌'}",
        f"Volume: {'✅' if volume_surge else '❌'}",
        f"Fee pagata: €{fee}",
    ]
    _send("\n".join(lines))


def notify_order_expired(asset: str, order_id: str, current_price: Decimal) -> None:
    text = (
        f"⚠️ ORDINE SCADUTO — {asset}/EUR\n"
        f"{SEP}\n"
        f"Order ID: {order_id}\n"
        f"Cancellato dopo 30 minuti.\n"
        f"Prezzo attuale: €{current_price} (troppo mosso)\n"
        f"Il bot rianalizza al prossimo ciclo."
    )
    _send(text)


def notify_tp1_hit(
    asset: str,
    tp1_price: Decimal,
    tp1_value: Decimal,
    profit: Decimal,
    breakeven: Decimal,
    tp2: Decimal,
    remaining_size: Decimal,
    remaining_value: Decimal,
    tp3: Optional[Decimal] = None,
) -> None:
    sign = "+" if profit >= 0 else ""
    next_target = f"🚀 TP3 ancora aperto a €{tp3}" if tp3 else f"🎯 TP2 ancora aperto a €{tp2}"
    text = (
        f"🟡 TP1 RAGGIUNTO — {asset}/EUR\n"
        f"{SEP}\n"
        f"✅ Venduto parziale a €{tp1_price}\n"
        f"💵 Incassato: €{tp1_value}\n"
        f"📈 Profitto netto (fee incluse): {sign}€{profit}\n"
        f"📍 SL spostato al breakeven: €{breakeven}\n"
        f"🎯 TP2 ancora aperto a €{tp2}\n"
        f"{next_target}\n"
        f"Rimane: {remaining_size} {asset} (€{remaining_value})"
    )
    _send(text)


def notify_tp2_hit(
    asset: str,
    tp2_size: Decimal,
    tp2_price: Decimal,
    profit_total: Decimal,
    roi: Decimal,
    final: bool = True,
    tp3: Optional[Decimal] = None,
) -> None:
    sign = "+" if profit_total >= 0 else ""
    if not final and tp3:
        text = (
            f"🟠 TP2 RAGGIUNTO — {asset}/EUR\n"
            f"{SEP}\n"
            f"✅ Venduto parziale a €{tp2_price}\n"
            f"💵 Incassato: €{(tp2_size * tp2_price).quantize(Decimal('0.01'))}\n"
            f"📈 Profitto parziale: {sign}€{profit_total}\n"
            f"🚀 TP3 ancora aperto a €{tp3}\n"
            f"Posizione ancora attiva."
        )
        _send(text)
        return
    text = (
        f"🏆 TP2 RAGGIUNTO — {asset}/EUR\n"
        f"{SEP}\n"
        f"✅ Posizione chiusa completamente\n"
        f"Venduto {tp2_size} {asset} a €{tp2_price}\n"
        f"💵 Profitto totale trade: {sign}€{profit_total}\n"
        f"📊 ROI trade: {sign}{roi}%\n"
        f"⏸ {asset} in pausa per 4h"
    )
    _send(text)


def notify_tp3_hit(
    asset: str,
    tp3_size: Decimal,
    tp3_price: Decimal,
    profit_total: Decimal,
    roi: Decimal,
) -> None:
    sign = "+" if profit_total >= 0 else ""
    text = (
        f"🚀 TP3 RAGGIUNTO — {asset}/EUR\n"
        f"{SEP}\n"
        f"✅ Posizione chiusa completamente\n"
        f"Venduto {tp3_size} {asset} a €{tp3_price}\n"
        f"💵 Profitto totale trade: {sign}€{profit_total}\n"
        f"📊 ROI trade: {sign}{roi}%\n"
        f"⏸ {asset} in pausa per 1h"
    )
    _send(text)


def notify_stop_loss(
    asset: str,
    sl_price: Decimal,
    loss: Decimal,
    consecutive_losses: int,
) -> None:
    text = (
        f"🔴 STOP LOSS — {asset}/EUR\n"
        f"{SEP}\n"
        f"❌ Posizione chiusa a €{sl_price}\n"
        f"💸 Perdita netta (fee incluse): €{loss}\n"
        f"🔢 Stop loss consecutivi: {consecutive_losses}/2\n"
        f"⏸ {asset} in pausa per 4h"
    )
    _send(text)


def notify_scan_summary(scan_results: list[dict], next_scan: str, strategy: str = "conservative") -> None:
    """
    Single message with the outcome of every asset in the current 4h scan.
    scan_results: list of dicts with keys:
        asset, signal, rsi, ema50_above, macd_bullish, volume_surge,
        close, reason, skipped (bool), skip_reason (str)
    """
    strategy_icon = "🔥" if strategy == "aggressive" else "🛡"
    now = datetime.now(timezone.utc).strftime("%d/%m %H:%M")
    lines = [f"🔍 Scan completato — {now} UTC  {strategy_icon}", SEP]

    for r in scan_results:
        asset = r["asset"]
        if r.get("skipped"):
            lines.append(f"⏭ {asset}/EUR — {r['skip_reason']}")
            continue

        signal = r["signal"]
        if signal == "BUY":
            sig_icon = "🟢 BUY"
        elif signal == "SELL":
            sig_icon = "🔴 SELL"
        else:
            sig_icon = "⚪ HOLD"

        rsi = r["rsi"]
        rsi_ok = r.get("rsi_ok", False)
        ema_ref = r.get("ema_ref", "ema50").upper()
        lines.append(
            f"{sig_icon} — {asset}/EUR  €{r['close']:,.2f}\n"
            f"  RSI {rsi:.1f} {'✅' if rsi_ok else '❌'}  "
            f"{ema_ref} {'✅' if r['ema_above'] else '❌'}  "
            f"MACD {'✅' if r['macd_bullish'] else '❌'}  "
            f"Vol {'✅' if r['volume_surge'] else '❌'}"
        )
        if signal == "HOLD":
            lines.append(f"  ↳ {r['reason']}")

    lines += [SEP, f"Prossimo scan: {next_scan}"]
    _send("\n".join(lines))


def notify_pause_activated(loss_total: Decimal, resume_at: datetime) -> None:
    resume_str = resume_at.strftime("%d/%m/%Y %H:%M")
    text = (
        f"⚠️ PAUSA ATTIVATA\n"
        f"{SEP}\n"
        f"2 stop loss consecutivi rilevati.\n"
        f"Perdita sessione: €{loss_total}\n"
        f"Bot in pausa per 24h.\n"
        f"Tutte le posizioni aperte mantenute con SL nativi Kraken attivi.\n"
        f"Riprende: {resume_str}"
    )
    _send(text)


def notify_api_error(attempt: int, max_attempts: int, error: str, retry_in: int) -> None:
    text = (
        f"🚨 ERRORE API KRAKEN\n"
        f"{SEP}\n"
        f"Tentativo {attempt}/{max_attempts} fallito.\n"
        f"Errore: {error}\n"
        f"Retry tra {retry_in} secondi.\n"
        f"Gli ordini aperti su Kraken restano attivi autonomamente."
    )
    _send(text)


def notify_insufficient_balance(asset: str, needed: Decimal, available: Decimal) -> None:
    text = (
        f"⚠️ SALDO INSUFFICIENTE — {asset}\n"
        f"{SEP}\n"
        f"Necessario: €{needed}\n"
        f"Disponibile: €{available}\n"
        f"Ordine scalato proporzionalmente."
    )
    _send(text)


def notify_daily_report(
    date_str: str,
    capital: Decimal,
    pnl_today: Decimal,
    pnl_total: Decimal,
    trades_today: list[dict],
    open_positions: dict,
    fees_today: Decimal,
    next_scan: str,
) -> None:
    pnl_today_pct = (pnl_today / (capital - pnl_today) * 100).quantize(Decimal("0.01")) if capital != pnl_today else Decimal("0")
    pnl_total_pct = (pnl_total / (capital - pnl_total) * 100).quantize(Decimal("0.01")) if capital != pnl_total else Decimal("0")

    sign_today = "+" if pnl_today >= 0 else ""
    sign_total = "+" if pnl_total >= 0 else ""

    lines = [
        f"📊 Report giornaliero — {date_str}",
        SEP,
        f"💼 Capitale attuale: €{capital}",
        f"📈 P&L oggi: {sign_today}€{pnl_today} ({sign_today}{pnl_today_pct}%)",
        f"📈 P&L totale: {sign_total}€{pnl_total} ({sign_total}{pnl_total_pct}%)",
        SEP,
        f"Trade oggi: {len(trades_today)} chiusi",
    ]

    for trade in trades_today:
        emoji = "✅" if trade["pnl"] >= 0 else "❌"
        sign = "+" if trade["pnl"] >= 0 else ""
        lines.append(f"{emoji} {trade['asset']}: {sign}€{trade['pnl']} ({sign}{trade['roi']}%)")

    if open_positions:
        lines.append(SEP)
        lines.append("Posizioni aperte:")
        for asset, pos in open_positions.items():
            if pos.get("active"):
                entry = Decimal(str(pos["entry_price"]))
                current = kc_price_safe(asset)
                if current and entry > 0:
                    pct = ((current - entry) / entry * 100).quantize(Decimal("0.1"))
                    sign = "+" if pct >= 0 else ""
                    lines.append(f"📍 {asset}: entrata €{entry}, ora {sign}{pct}%")

    lines.append(f"Fee totali pagate oggi: €{fees_today}")
    lines.append(f"Prossimo scan: {next_scan}")

    _send("\n".join(lines))


def kc_price_safe(asset: str) -> Optional[Decimal]:
    try:
        import kraken_client as kc
        return kc.get_ticker_price(asset)
    except Exception:
        return None


def notify_error(context: str, error: str) -> None:
    text = (
        f"🚨 ERRORE NON GESTITO\n"
        f"{SEP}\n"
        f"Contesto: {context}\n"
        f"Errore: {error[:300]}"
    )
    _send(text)

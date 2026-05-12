"""
Streamlit monitoring dashboard.
Run with: streamlit run dashboard.py

Reads positions.json and trades_history.json directly — no API keys required
for the basic view. Live prices are fetched from Kraken public API if available.
Refreshes automatically every 30 seconds.
"""

import json
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import streamlit as st

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Kraken Bot Monitor",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

POSITIONS_FILE = Path("positions.json")
TRADES_FILE = Path("trades_history.json")
LOG_FILE = Path("bot.log")
PAUSE_FILE = Path(".pause_until")

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
KRAKEN_PAIRS = {"BTC": "XXBTZEUR", "ETH": "XETHZEUR", "SOL": "SOLEUR", "XRP": "XXRPZEUR"}
ALLOCATIONS = {"BTC": 0.40, "ETH": 0.30, "SOL": 0.20, "XRP": 0.10}
ASSET_COLORS = {"BTC": "#F7931A", "ETH": "#627EEA", "SOL": "#9945FF", "XRP": "#346AA9"}

AUTO_REFRESH_SECONDS = 30


# ── Data loaders ─────────────────────────────────────────────────────────────

@st.cache_data(ttl=AUTO_REFRESH_SECONDS)
def load_positions() -> dict:
    if not POSITIONS_FILE.exists():
        return {}
    try:
        return json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


@st.cache_data(ttl=AUTO_REFRESH_SECONDS)
def load_trade_history() -> list[dict]:
    if not TRADES_FILE.exists():
        return []
    try:
        return json.loads(TRADES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


@st.cache_data(ttl=10)
def fetch_live_prices() -> dict[str, float]:
    pairs = ",".join(KRAKEN_PAIRS.values())
    try:
        r = requests.get(
            f"https://api.kraken.com/0/public/Ticker?pair={pairs}", timeout=5
        )
        data = r.json().get("result", {})
        prices = {}
        for asset, pair in KRAKEN_PAIRS.items():
            if pair in data:
                prices[asset] = float(data[pair]["c"][0])
        return prices
    except Exception:
        return {}


@st.cache_data(ttl=AUTO_REFRESH_SECONDS)
def load_log_tail(lines: int = 100) -> list[str]:
    if not LOG_FILE.exists():
        return []
    try:
        text = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        return text.splitlines()[-lines:]
    except Exception:
        return []


def is_paused() -> tuple[bool, Optional[datetime]]:
    if not PAUSE_FILE.exists():
        return False, None
    try:
        resume_at = datetime.fromisoformat(PAUSE_FILE.read_text().strip())
        if datetime.now(timezone.utc) < resume_at:
            return True, resume_at
    except Exception:
        pass
    return False, None


# ── Helper formatters ─────────────────────────────────────────────────────────

def _dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def pnl_color(value: float) -> str:
    return "color: #00c853" if value >= 0 else "color: #ff1744"


def pct_str(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def eur_str(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}€{abs(value):.2f}"


# ── CSS ───────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
[data-testid="stMetricValue"] { font-size: 1.6rem; font-weight: 700; }
[data-testid="stMetricLabel"] { font-size: 0.8rem; color: #aaa; }
.asset-badge {
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-weight: 700; font-size: 0.85rem; color: white; margin-right: 4px;
}
.status-live { color: #00c853; font-weight: 700; }
.status-paused { color: #ff9800; font-weight: 700; }
.status-paper { color: #2196f3; font-weight: 700; }
.log-box {
    background: #0e1117; color: #c8c8c8; font-family: monospace;
    font-size: 0.75rem; padding: 12px; border-radius: 8px;
    max-height: 300px; overflow-y: auto; white-space: pre-wrap;
}
</style>
""", unsafe_allow_html=True)


# ── Main layout ───────────────────────────────────────────────────────────────

def main():
    # Header
    col_title, col_refresh = st.columns([5, 1])
    with col_title:
        st.title("📈 Kraken Swing Bot — Monitor")
    with col_refresh:
        st.write("")
        if st.button("🔄 Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    positions = load_positions()
    trades = load_trade_history()
    prices = fetch_live_prices()
    paused, resume_at = is_paused()

    # ── Status bar ────────────────────────────────────────────────────────────
    env_paper = True
    try:
        from dotenv import dotenv_values
        env = dotenv_values(".env")
        env_paper = env.get("PAPER_TRADING", "true").lower() == "true"
        capital_cfg = float(env.get("CAPITALE_TOTALE", "100"))
    except Exception:
        capital_cfg = 100.0

    if paused:
        status_html = f'<span class="status-paused">⏸ PAUSA — riprende {resume_at.strftime("%d/%m %H:%M")} UTC</span>'
    elif env_paper:
        status_html = '<span class="status-paper">📄 PAPER TRADING</span>'
    else:
        status_html = '<span class="status-live">🟢 LIVE TRADING</span>'

    st.markdown(status_html, unsafe_allow_html=True)
    st.caption(f"Ultimo aggiornamento: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC  •  Auto-refresh ogni {AUTO_REFRESH_SECONDS}s")
    st.divider()

    # ── KPI metrics row ───────────────────────────────────────────────────────
    open_positions = {a: p for a, p in positions.items() if p.get("active")}
    closed_trades = trades  # full history

    total_pnl = sum(float(_dec(t["pnl"])) for t in closed_trades)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_trades = [t for t in closed_trades if t.get("closed_at", "").startswith(today_str)]
    today_pnl = sum(float(_dec(t["pnl"])) for t in today_trades)
    total_fees = sum(float(_dec(t["fee"])) for t in closed_trades)
    win_trades = [t for t in closed_trades if float(_dec(t["pnl"])) > 0]
    win_rate = len(win_trades) / len(closed_trades) * 100 if closed_trades else 0

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Capitale configurato", f"€{capital_cfg:.0f}")
    m2.metric(
        "P&L totale",
        eur_str(total_pnl),
        delta=pct_str(total_pnl / capital_cfg * 100) if capital_cfg else None,
        delta_color="normal",
    )
    m3.metric("P&L oggi", eur_str(today_pnl))
    m4.metric("Posizioni aperte", len(open_positions))
    m5.metric(
        "Win rate",
        f"{win_rate:.0f}%",
        delta=f"{len(win_trades)}/{len(closed_trades)} trade",
        delta_color="off",
    )

    st.divider()

    # ── Open positions ────────────────────────────────────────────────────────
    st.subheader("📍 Posizioni aperte")

    if not open_positions:
        st.info("Nessuna posizione aperta al momento.")
    else:
        cols = st.columns(len(open_positions))
        for col, (asset, pos) in zip(cols, open_positions.items()):
            with col:
                entry = float(_dec(pos["entry_price"]))
                sl = float(_dec(pos["sl"]))
                tp1 = float(_dec(pos["tp1"]))
                tp2 = float(_dec(pos["tp2"]))
                size_eur = float(_dec(pos["size_eur"]))
                size_asset = float(_dec(pos["size_asset"]))
                current = prices.get(asset, entry)
                pnl_pct = (current - entry) / entry * 100 if entry else 0
                pnl_eur = size_eur * pnl_pct / 100

                color = ASSET_COLORS.get(asset, "#888")
                st.markdown(
                    f'<span class="asset-badge" style="background:{color}">{asset}</span>',
                    unsafe_allow_html=True,
                )
                st.metric(
                    label=f"Prezzo attuale",
                    value=f"€{current:,.2f}",
                    delta=f"{pct_str(pnl_pct)}  ({eur_str(pnl_eur)})",
                )

                entry_time = pos.get("entry_time") or "—"
                if entry_time != "—":
                    try:
                        entry_time = datetime.fromisoformat(entry_time).strftime("%d/%m %H:%M")
                    except Exception:
                        pass

                tp1_hit = pos.get("tp1_hit", False)

                data = {
                    "": ["Entrata", "Quantità", "Valore", "Stop Loss", "TP1", "TP2", "Apertura"],
                    "Valore": [
                        f"€{entry:,.2f}",
                        f"{size_asset} {asset}",
                        f"€{size_eur:.2f}",
                        f"€{sl:,.2f}  (-3%)",
                        f"€{tp1:,.2f}  (+5%) {'✅' if tp1_hit else ''}",
                        f"€{tp2:,.2f}  (+10%)",
                        entry_time,
                    ],
                }
                st.dataframe(
                    pd.DataFrame(data).set_index(""),
                    use_container_width=True,
                    hide_index=False,
                )

                # Mini price bar
                if entry > 0:
                    progress = max(0.0, min(1.0, (current - sl) / (tp2 - sl)))
                    st.progress(progress, text=f"SL ← {'▶' if current > entry else '◀'} → TP2")

    st.divider()

    # ── Asset overview cards ───────────────────────────────────────────────────
    st.subheader("🔍 Stato asset")
    acols = st.columns(len(ASSETS))
    for col, asset in zip(acols, ASSETS):
        with col:
            pos = positions.get(asset)
            active = pos.get("active", False) if pos else False
            in_cooldown = False
            if pos and not active:
                close_time_str = pos.get("close_time")
                if close_time_str:
                    try:
                        close_time = datetime.fromisoformat(close_time_str)
                        if close_time.tzinfo is None:
                            close_time = close_time.replace(tzinfo=timezone.utc)
                        elapsed_h = (datetime.now(timezone.utc) - close_time).total_seconds() / 3600
                        in_cooldown = elapsed_h < 4
                    except Exception:
                        pass

            current_price = prices.get(asset)
            color = ASSET_COLORS.get(asset, "#888")
            badge = f'<span class="asset-badge" style="background:{color}">{asset}</span>'

            if active:
                state = "🟢 In posizione"
            elif in_cooldown:
                state = "⏸ Cooldown 4h"
            else:
                state = "👀 In ascolto"

            price_str = f"€{current_price:,.2f}" if current_price else "—"
            alloc_str = f"{ALLOCATIONS[asset]*100:.0f}% (€{capital_cfg * ALLOCATIONS[asset]:.0f})"

            st.markdown(badge, unsafe_allow_html=True)
            st.markdown(f"**{state}**")
            st.markdown(f"Prezzo: **{price_str}**")
            st.markdown(f"Allocazione: {alloc_str}")

    st.divider()

    # ── Trade history table ───────────────────────────────────────────────────
    st.subheader("📋 Storico trade")

    if not closed_trades:
        st.info("Nessun trade chiuso ancora.")
    else:
        rows = []
        for t in reversed(closed_trades):
            pnl_val = float(_dec(t["pnl"]))
            entry_p = float(_dec(t["entry_price"]))
            exit_p = float(_dec(t["exit_price"]))
            rows.append({
                "Data": t.get("closed_at", "")[:16].replace("T", " "),
                "Asset": t["asset"],
                "Entrata": f"€{entry_p:,.2f}",
                "Uscita": f"€{exit_p:,.2f}",
                "Quantità": f"{float(_dec(t['size_asset'])):.6g} {t['asset']}",
                "Motivo": t.get("reason", "—").replace("_", " ").title(),
                "P&L": pnl_val,
                "Fee": f"€{float(_dec(t['fee'])):.3f}",
            })

        df = pd.DataFrame(rows)

        def color_pnl(val):
            if isinstance(val, (int, float)):
                c = "#00c853" if val >= 0 else "#ff1744"
                return f"color: {c}; font-weight: bold"
            return ""

        df_display = df.copy()
        df_display["P&L"] = df_display["P&L"].apply(lambda v: eur_str(v))

        st.dataframe(
            df_display,
            use_container_width=True,
            hide_index=True,
        )

        # P&L cumulative chart
        if len(rows) > 1:
            st.subheader("📊 P&L cumulativo")
            pnl_series = pd.DataFrame({
                "Data": [r["Data"] for r in reversed(rows)],
                "P&L cumulativo (€)": pd.Series(
                    [float(_dec(t["pnl"])) for t in closed_trades]
                ).cumsum().tolist(),
            })
            st.line_chart(pnl_series.set_index("Data"), color="#00c853")

    st.divider()

    # ── Log viewer ────────────────────────────────────────────────────────────
    with st.expander("🪵 Log in tempo reale (ultime 100 righe)", expanded=False):
        log_lines = load_log_tail(100)
        if log_lines:
            # Color-code by level
            colored = []
            for line in log_lines:
                if "ERROR" in line or "CRITICAL" in line:
                    colored.append(f'<span style="color:#ff1744">{line}</span>')
                elif "WARNING" in line:
                    colored.append(f'<span style="color:#ff9800">{line}</span>')
                elif "SUCCESS" in line or "BUY" in line or "OPENED" in line:
                    colored.append(f'<span style="color:#00c853">{line}</span>')
                else:
                    colored.append(line)
            log_html = "\n".join(colored)
            st.markdown(f'<div class="log-box">{log_html}</div>', unsafe_allow_html=True)
        else:
            st.info("bot.log non trovato. Avvia il bot per generare i log.")

    # ── Auto-refresh countdown ────────────────────────────────────────────────
    st.caption(f"⏱ Prossimo refresh automatico tra {AUTO_REFRESH_SECONDS}s")
    time.sleep(AUTO_REFRESH_SECONDS)
    st.cache_data.clear()
    st.rerun()


if __name__ == "__main__":
    main()

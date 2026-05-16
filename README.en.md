# Kraken Swing Trading Bot

Autonomous Python bot that runs a swing trading strategy on Kraken with real orders,
Telegram notifications, and a web monitoring dashboard. Runs 24/7 with no human intervention.

---

## Table of Contents

1. [Telegram Setup](#1-telegram-setup)
2. [Kraken API Keys](#2-kraken-api-keys)
3. [Configuration](#3-configuration)
4. [Installation](#4-installation)
5. [Running the bot](#5-running-the-bot)
6. [Web dashboard](#6-web-dashboard)
7. [Linux systemd](#7-linux-systemd)
8. [Windows Task Scheduler](#8-windows-task-scheduler)
9. [If the bot stops](#9-if-the-bot-stops)
10. [Monitored assets](#10-monitored-assets)
11. [Conservative strategy](#11-conservative-strategy)
12. [Aggressive strategy](#12-aggressive-strategy)
13. [Strategy comparison](#13-strategy-comparison)
14. [Position management](#14-position-management)
15. [File structure](#15-file-structure)

---

## 1. Telegram Setup

**Create the bot:**
1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and choose a name and username
3. BotFather replies with your **Bot Token** (format: `123456789:AABBCCDDee...`)
4. Copy the token to `TELEGRAM_BOT_TOKEN` in your `.env` file

**Get your Chat ID:**
1. Search for **@userinfobot** on Telegram
2. It replies with your numeric user ID — copy it to `TELEGRAM_CHAT_ID`
3. Send at least one message to your new bot to enable notifications

---

## 2. Kraken API Keys

1. Log in at [kraken.com](https://www.kraken.com) → Account → Security → API
2. Click **Create API Key**
3. Enable **only the `Trade` permission** — never enable `Withdraw` or `Funding`
4. Copy the **API Key** and **Private Key (Secret)** to your `.env` file

> **Security tip:** restrict the API key to your server's IP address in Kraken's API settings.

---

## 3. Configuration

```bash
cp .env.example .env
# Edit .env with your credentials
```

All available options:

```env
# Kraken API
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Capital and risk
CAPITALE_TOTALE=100          # euros to trade with
RISCHIO_PER_TRADE=0.015

# Mode
PAPER_TRADING=true           # true = simulation, false = real orders
STRATEGIA=conservative       # conservative | aggressive
```

> Always start with `PAPER_TRADING=true` to verify everything works before going live.

---

## 4. Installation

Requires **Python 3.11+**.

```bash
pip install -r requirements.txt
```

---

## 5. Running the bot

```bash
python bot.py
```

The bot aligns itself to the next 4h candle boundary (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC),
then repeats every 4 hours. Logs are written to `bot.log` (daily rotation, 7-day retention).

On startup it sends a Telegram confirmation with the active strategy, capital, and next scan time.
The daily report is sent at 20:00 UTC.

---

## 6. Web dashboard

```bash
streamlit run dashboard.py
```

Opens `http://localhost:8501` with:
- Bot status (live / paper / paused) and active strategy
- KPIs: capital, total P&L, today's P&L, win rate
- Open positions with live prices and SL→TP progress bar
- Per-asset status (in position / cooldown / watching)
- Trade history with cumulative P&L chart
- Live log viewer with color-coded log levels

Auto-refreshes every 30 seconds. No API keys required to use the dashboard.

---

## 7. Linux systemd

```bash
sudo nano /etc/systemd/system/kraken-bot.service
```

```ini
[Unit]
Description=Kraken Swing Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_LINUX_USER
WorkingDirectory=/home/YOUR_LINUX_USER/kraken-bot
ExecStart=/usr/bin/python3 /home/YOUR_LINUX_USER/kraken-bot/bot.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal
EnvironmentFile=/home/YOUR_LINUX_USER/kraken-bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable kraken-bot
sudo systemctl start kraken-bot

sudo systemctl status kraken-bot   # check status
sudo journalctl -u kraken-bot -f   # live logs
```

---

## 8. Windows Task Scheduler

1. Open **Task Scheduler** → "Create Basic Task"
2. Trigger: **At system startup**
3. Action: **Start a program**
   - Program: `C:\Python311\python.exe`
   - Arguments: `C:\path\to\kraken-bot\bot.py`
   - Start in: `C:\path\to\kraken-bot`
4. Check "Run whether user is logged on or not"
5. Save and enter your Windows credentials when prompted

Alternatively, create a `start.bat` launcher:

```bat
@echo off
cd /d C:\path\to\kraken-bot
python bot.py >> bot_console.log 2>&1
```

---

## 9. If the bot stops

**Your funds are protected.**

Every time the bot opens a position it places on Kraken:
- **Native Kraken stop-loss order** — stored on Kraken's servers, not in the bot. Executes automatically even if the bot is offline.
- **Take-profit limit orders** — also on Kraken, independent of the bot.

On restart the bot re-reads `positions.json`, reconnects open orders, and resumes monitoring. No positions are lost.

> In paper trading no real orders exist on Kraken — stopping the bot has no impact on funds.

> To close manually in live mode: Kraken → Open Orders → Cancel / Close.

---

## 10. Monitored assets

10 assets on EUR pairs, selected for liquidity and volatility:

| Asset | Allocation | Min order | Kraken pair |
|-------|-----------|-----------|-------------|
| BTC/EUR | 20% | 0.0001 BTC | XXBTZEUR |
| ETH/EUR | 18% | 0.01 ETH | XETHZEUR |
| SOL/EUR | 12% | 0.5 SOL | SOLEUR |
| XRP/EUR | 8% | 10 XRP | XXRPZEUR |
| ADA/EUR | 8% | 5 ADA | ADAEUR |
| AVAX/EUR | 8% | 0.1 AVAX | AVAXEUR |
| DOT/EUR | 8% | 0.5 DOT | DOTEUR |
| LINK/EUR | 7% | 0.2 LINK | LINKEUR |
| LTC/EUR | 6% | 0.05 LTC | XLTCZEUR |
| ATOM/EUR | 5% | 0.5 ATOM | ATOMEUR |

Allocations are weighted by market cap and sum to 100%.

### Minimum recommended capital

With small capital some assets may fall below Kraken's minimum order size.
The bot handles this automatically: if the computed size is below the minimum but
the available balance covers it, it **scales up to the minimum** instead of skipping the trade.

Example with €100 and aggressive strategy (size×1.25):

| Asset | Allocation | Computed size | Kraken min | Behaviour |
|-------|-----------|--------------|------------|-----------|
| XRP | 8% → €10 | 7.9 XRP | 10 XRP | scaled up to 10 XRP (€12.60) |
| LTC | 6% → €7.50 | 0.15 LTC | 0.05 LTC | ok |
| ADA | 8% → €10 | 43 ADA | 5 ADA | ok |

> With €100 the bot is fully operational on all 10 assets.
> To eliminate any scaling, use a capital of ≥ €200.

---

## 11. Conservative strategy

Default setting. Favours strong signals and reduces false positives.

### Entry signal (all conditions must be true)

| Condition | Value |
|-----------|-------|
| RSI(14) | between 35 and 50 (accumulation zone) |
| EMA(50) | price above the fast moving average |
| EMA(200) | price above the slow moving average (macro uptrend) |
| MACD | histogram > 0 or bullish crossover on last candle |
| Volume | > 1.3× 20-period average |
| ADX(14) | > 20 (trending market, not sideways) |

### Exits

| Event | Action |
|-------|--------|
| ATR stop loss | entry − 2 × ATR(14), capped at −6% |
| TP1 +5% | close 50%, SL moved to breakeven |
| TP1 → trailing stop | SL trails price at −3% from the high |
| TP2 +10% | close remaining 30%, position closed |
| RSI > 72 | SELL signal → immediate close |
| Bearish MACD divergence | SELL signal → immediate close |
| Flat position > 96h | time exit, capital freed |

### Risk controls

- Max 4 simultaneous open positions
- 4h cooldown after every close
- 24h global pause after 2 consecutive stop-losses

---

## 12. Aggressive strategy

More entry opportunities, tighter stops, three take-profit levels.
Best suited for trending markets with high volume.

### Entry signal (all conditions must be true)

| Condition | Value |
|-----------|-------|
| RSI(14) | between 30 and 58 (wider range) |
| EMA(20) | price above the faster moving average |
| EMA(200) | filter disabled (can trade against the macro trend) |
| MACD | histogram > 0 or bullish crossover |
| Volume | > 1.1× 20-period average |
| ADX(14) | > 15 |

### Exits

| Event | Action |
|-------|--------|
| ATR stop loss | entry − 1.5 × ATR(14), capped at −3% |
| TP1 +3% | close 40%, SL moved to breakeven |
| TP1 → trailing stop | SL trails price at −2% from the high |
| TP2 +7% | close 35% |
| TP3 +15% | close remaining 25%, position closed |
| RSI > 78 | SELL signal → immediate close |
| Bearish MACD divergence | SELL signal → immediate close |
| Flat position > 48h | time exit, capital freed |

### Risk controls

- Max 6 simultaneous open positions
- Position size is 1.25× the standard allocation (capped at available balance)
- 1h cooldown after every close
- 24h global pause after 3 consecutive stop-losses

> **When to use Aggressive:** clear trending markets with high ADX and growing volume.
> The tight stop (ATR×1.5) limits damage on false breakouts; TP3 (+15%) captures extended moves.
> Not recommended during sideways or high-uncertainty phases.

---

## 13. Strategy comparison

| Parameter | Conservative | Aggressive |
|-----------|-------------|------------|
| RSI buy | 35–50 | 30–58 |
| EMA reference | EMA(50) | EMA(20) |
| EMA(200) filter | Yes | No |
| Volume threshold | >1.3× | >1.1× |
| ADX minimum | 20 | 15 |
| RSI sell | >72 | >78 |
| Stop loss | ATR×2.0 (max −6%) | ATR×1.5 (max −3%) |
| TP1 | +5% → 50% | +3% → 40% |
| TP2 | +10% → 30% | +7% → 35% |
| TP3 | — | +15% → 25% |
| Trailing stop | −3% from high | −2% from high |
| Time exit | 96h flat | 48h flat |
| Cooldown | 4h | 1h |
| Max positions | 4 | 6 |
| Size multiplier | 1× | 1.25× |
| Pause after N SL | 2 | 3 |

```env
STRATEGIA=conservative   # default
STRATEGIA=aggressive
```

---

## 14. Position management

### Position lifecycle

```
BUY signal (RSI + EMA + MACD + Volume + ADX + EMA200)
    │
    └─▶ Limit buy order placed (maker price)
            │
            └─▶ Poll every 60s (max 30 min)
                    │
                    ├─▶ [expired] ──────────────────▶ cancelled, retry at next scan
                    │
                    └─▶ [filled]
                            │
                            ├─▶ Native Kraken SL  (ATR × multiplier)
                            ├─▶ TP1 limit sell
                            ├─▶ TP2 limit sell
                            └─▶ TP3 limit sell  (aggressive only)
                                    │
                    ┌───────────────┼───────────────────┬──────────────┐
                    │               │                   │              │
                  TP1 hit        SL hit           SELL signal     Time exit
               close 40-50%    close all         (RSI/MACD)      (flat > N h)
               SL→breakeven    position closed   close all        close all
               trailing on          │                  │              │
                    │          consecutive SL?         │              │
                    │          ≥ threshold? → 24h pause│              │
                    │                                  │              │
               price rises                             │              │
                    │                                  │              │
               trailing_high updated                   │              │
                    │                                  │              │
               price < trailing_SL                     │              │
                    └─▶ trailing stop exit ────────────┘              │
                        close with profit                             │
                                                                      │
                            TP2 hit (and TP3 if present)              │
                            close remainder                           │
                            position closed ───────────────────────── ┘
```

**In paper trading** orders are simulated locally with no Kraken API calls.
Fills are instant; TP/SL exits are checked against the live price every 60 seconds.

### State files

| File | Contents |
|------|----------|
| `positions.json` | live state of all positions (entry, SL, TP, order IDs) |
| `trades_history.json` | full history of closed trades |
| `bot.log` | daily rotating log file |
| `.pause_until` | resume timestamp for the global pause |

### Telegram notifications

| Event | Message content |
|-------|----------------|
| Bot startup | strategy, capital, next scan time |
| 4h scan | signal per asset (RSI, EMA, MACD, Volume, ADX, EMA200) |
| Order sent | type, limit price, quantity, order ID |
| Position opened | entry, SL (ATR-based), TP1/2/3, trailing stop, indicators |
| TP1 hit | partial close, trailing stop activated |
| TP2 / TP3 hit | partial/final close, total P&L, ROI |
| Trailing stop | high reached, exit price, protected profit |
| Stop loss | loss amount, consecutive SL count |
| Signal exit | RSI/MACD close on open position |
| Time exit | flat position closed after timeout |
| Max positions | BUY skipped, limit reached |
| Pause activated | N consecutive SL, resume time |
| API error | attempt N/3, retry delay |
| Daily report | P&L, closed trades, open positions, fees paid |

---

## 15. File structure

```
kraken-bot/
├── bot.py                # Entry point, 4h loop, daily report
├── strategy.py           # Indicators (RSI, EMA, MACD, ADX, ATR, Volume) and signals
├── kraken_client.py      # Kraken API wrapper (prices, orders, balance, fees)
├── order_manager.py      # Order lifecycle: place → poll → fill → TP/SL/trailing/time exit
├── position_manager.py   # Thread-safe positions.json and trades_history.json
├── risk_manager.py       # Sizing, SL/TP levels, StrategyConfig, global pause
├── telegram_notify.py    # Formatting and sending all Telegram messages
├── dashboard.py          # Streamlit dashboard (streamlit run dashboard.py)
├── positions.json        # Auto-generated: live position state
├── trades_history.json   # Auto-generated: closed trade history
├── bot.log               # Auto-generated: daily rotating log
├── .pause_until          # Auto-generated: global pause resume timestamp
├── .env                  # Credentials and config (never commit this)
├── .env.example          # Configuration template
└── requirements.txt      # Python dependencies
```

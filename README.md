# Kraken Swing Trading Bot

Autonomous Python bot that runs a 4h swing trading strategy on Kraken with real orders
and Telegram notifications, 24/7 with no human intervention required.

---

## 1. Create the Telegram Bot

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts — choose a name and a username.
3. BotFather replies with your **Bot Token** (format: `123456789:AABBCCDDee...`).
4. Copy it to `TELEGRAM_BOT_TOKEN` in `.env`.

**Get your Chat ID:**
1. Start a conversation with **@userinfobot** on Telegram.
2. It replies with your numeric user ID — copy it to `TELEGRAM_CHAT_ID` in `.env`.
3. Send any message to your new bot so it can reach you first.

---

## 2. Generate Kraken API Keys

1. Log in at [kraken.com](https://www.kraken.com) → Account → Security → API.
2. Click **Create API Key**.
3. Enable **only the `Trade` permission** — never enable `Withdraw` or `Funding`.
4. Copy the **API Key** and **Private Key (Secret)** to `.env`.

> **Security tip:** restrict the key to your server IP address in the Kraken API settings.

---

## 3. Configure the bot

```bash
cp .env.example .env
# Edit .env with your credentials and settings
```

Start with `PAPER_TRADING=true` to verify everything works before going live.

---

## 4. Install dependencies

Requires **Python 3.11+**.

```bash
pip install -r requirements.txt
```

---

## 5. Run the bot

```bash
python bot.py
```

The bot aligns itself to the next 4h candle boundary (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC)
before running the first scan, then repeats every 4 hours.

Logs are written to `bot.log` (daily rotation, 7-day retention).

---

## 6. Run as a background service on Linux (systemd)

Create the service file:

```bash
sudo nano /etc/systemd/system/kraken-bot.service
```

Paste this content (adjust paths to your actual installation):

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

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable kraken-bot
sudo systemctl start kraken-bot

# Check status
sudo systemctl status kraken-bot

# Watch live logs
sudo journalctl -u kraken-bot -f
```

---

## 7. Run on Windows with Task Scheduler

1. Open **Task Scheduler** → "Create Basic Task".
2. Trigger: **At system startup** (or daily at a fixed time).
3. Action: **Start a program**
   - Program: `C:\Python311\python.exe`
   - Arguments: `C:\path\to\kraken-bot\bot.py`
   - Start in: `C:\path\to\kraken-bot`
4. Check "Run whether user is logged on or not".
5. Save and enter your Windows credentials when prompted.

Alternatively, create a `.bat` launcher:

```bat
@echo off
cd /d C:\path\to\kraken-bot
python bot.py >> bot_console.log 2>&1
```

And schedule the `.bat` file in Task Scheduler.

---

## 8. What happens if the bot stops?

**Your funds are protected.**

Every time the bot opens a position it places:
- A **native Kraken stop-loss order** — this is stored on Kraken's servers, not in the bot.
  If the bot goes offline, this order still executes automatically when the price hits your stop.
- **Take-profit limit orders** — also stored on Kraken, fire independently of the bot.

When the bot restarts it re-reads `positions.json`, reconnects to open orders,
and resumes monitoring. No positions are lost.

> If you want to manually close everything, go to Kraken → Open Orders and cancel/close there.

---

## Strategy overview

### Asset allocations

| Asset | Allocation | Min order |
|-------|-----------|-----------|
| BTC/EUR | 40% | 0.0001 BTC |
| ETH/EUR | 30% | 0.01 ETH |
| SOL/EUR | 20% | 0.5 SOL |
| XRP/EUR | 10% | 10 XRP |

### Selecting a strategy

Set `STRATEGIA` in `.env`:

```
STRATEGIA=conservative   # default — safer, fewer trades
STRATEGIA=aggressive     # more entries, higher upside, tighter stops
```

### Parameter comparison

| Parameter | Conservative | Aggressive |
|-----------|-------------|------------|
| RSI buy zone | 35–50 | 30–58 |
| EMA reference | EMA(50) | EMA(20) — faster signal |
| Volume threshold | >1.3× avg | >1.1× avg |
| RSI sell | >72 | >78 — lets winners run |
| Stop loss | −3% | −1.5% — cuts losses quicker |
| Take profit 1 | +5% → close 50% | +3% → close 40% |
| Take profit 2 | +10% → close 30% | +7% → close 35% |
| Take profit 3 | — | +15% → close 25% |
| Cooldown after close | 4h | 1h — re-enters faster |
| Pause after N SL | 2 consecutive | 3 consecutive |
| Position size | 1× allocation | 1.25× allocation |

### Conservative strategy

**Buy signal** (all must be true):
- RSI(14) between 35 and 50
- Close price above EMA(50)
- MACD histogram > 0 or bullish crossover on last candle
- Volume > 1.3× 20-period average

**Exit rules:**
- Stop loss at −3% (native Kraken order, active even if bot is offline)
- TP1 at +5% → close 50%, move SL to breakeven
- TP2 at +10% → close remaining 30%
- Sell signal: RSI > 72 or bearish MACD divergence

**Risk controls:**
- Max 1 open position per asset
- 4h cooldown after closing any position
- After 2 consecutive stop-losses → 24h global pause

### Aggressive strategy

**Buy signal** (all must be true):
- RSI(14) between 30 and 58
- Close price above EMA(20)
- MACD histogram > 0 or bullish crossover on last candle
- Volume > 1.1× 20-period average

**Exit rules:**
- Stop loss at −1.5% (native Kraken order)
- TP1 at +3% → close 40%, move SL to breakeven
- TP2 at +7% → close 35%
- TP3 at +15% → close remaining 25%
- Sell signal: RSI > 78 or bearish MACD divergence

**Risk controls:**
- Max 1 open position per asset
- 1h cooldown after closing any position
- Position size is 1.25× the standard allocation (capped at available balance)
- After 3 consecutive stop-losses → 24h global pause

> **When to use aggressive:** trending markets with high volume and clear momentum.
> The tighter stop loss (−1.5%) limits damage on false breakouts, while TP3 (+15%)
> captures extended moves. Not recommended during sideways/choppy conditions.

---

## File structure

```
kraken-bot/
├── bot.py              # Entry point, scheduling loop
├── strategy.py         # Indicators and signal generation
├── kraken_client.py    # Kraken API wrapper (prices, orders, balance)
├── order_manager.py    # Order lifecycle (place, poll, cancel, TP/SL)
├── position_manager.py # positions.json read/write with thread locking
├── risk_manager.py     # Position sizing, pause logic, Kraken minimums
├── telegram_notify.py  # All Telegram message formatting and sending
├── positions.json      # Auto-created: live position state
├── bot.log             # Auto-created: rotating daily log
├── .pause_until        # Auto-created when global pause is active
├── .env                # Your secrets (never commit this)
├── .env.example        # Template
└── requirements.txt
```

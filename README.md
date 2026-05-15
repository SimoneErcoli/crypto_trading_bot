# Kraken Swing Trading Bot

Bot Python autonomo che esegue una strategia di swing trading su Kraken con ordini reali,
notifiche Telegram e monitoring via dashboard web. Gira 24/7 senza intervento umano.

---

## Indice

1. [Setup Telegram](#1-setup-telegram)
2. [API Key Kraken](#2-api-key-kraken)
3. [Configurazione](#3-configurazione)
4. [Installazione](#4-installazione)
5. [Avvio](#5-avvio)
6. [Dashboard web](#6-dashboard-web)
7. [Linux systemd](#7-linux-systemd)
8. [Windows Task Scheduler](#8-windows-task-scheduler)
9. [Se il bot si ferma](#9-se-il-bot-si-ferma)
10. [Asset monitorati](#10-asset-monitorati)
11. [Strategia Conservative](#11-strategia-conservative)
12. [Strategia Aggressive](#12-strategia-aggressive)
13. [Confronto strategie](#13-confronto-strategie)
14. [Gestione posizioni](#14-gestione-posizioni)
15. [Struttura file](#15-struttura-file)

---

## 1. Setup Telegram

**Creare il bot:**
1. Apri Telegram e cerca **@BotFather**
2. Invia `/newbot` e scegli nome e username
3. BotFather risponde con il **Bot Token** (formato: `123456789:AABBCCDDee...`)
4. Copia il token in `TELEGRAM_BOT_TOKEN` nel file `.env`

**Ottenere il Chat ID:**
1. Cerca **@userinfobot** su Telegram
2. Risponde con il tuo ID numerico — copialo in `TELEGRAM_CHAT_ID`
3. Invia almeno un messaggio al tuo nuovo bot per abilitare le notifiche

---

## 2. API Key Kraken

1. Accedi su [kraken.com](https://www.kraken.com) → Account → Security → API
2. Clicca **Create API Key**
3. Abilita **solo il permesso `Trade`** — non abilitare mai `Withdraw` o `Funding`
4. Copia **API Key** e **Private Key (Secret)** nel file `.env`

> **Sicurezza:** limita la chiave al solo indirizzo IP del tuo server nelle impostazioni API di Kraken.

---

## 3. Configurazione

```bash
cp .env.example .env
# Modifica .env con le tue credenziali
```

Tutte le opzioni disponibili:

```env
# Kraken API
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# Capitale e rischio
CAPITALE_TOTALE=100          # euro da utilizzare
RISCHIO_PER_TRADE=0.015

# Modalità
PAPER_TRADING=true           # true = simulazione, false = ordini reali
STRATEGIA=conservative       # conservative | aggressive
```

> Inizia sempre con `PAPER_TRADING=true` per verificare il funzionamento prima di andare live.

---

## 4. Installazione

Richiede **Python 3.11+**.

```bash
pip install -r requirements.txt
```

---

## 5. Avvio

```bash
python bot.py
```

Il bot si allinea alla prossima candela 4h (00:00, 04:00, 08:00, 12:00, 16:00, 20:00 UTC),
poi ripete ogni 4 ore. I log vengono scritti su `bot.log` (rotazione giornaliera, 7 giorni).

---

## 6. Dashboard web

```bash
streamlit run dashboard.py
```

Apre `http://localhost:8501` con:
- Stato bot (live/paper/pausa) e strategia attiva
- KPI: capitale, P&L totale, P&L oggi, win rate
- Posizioni aperte con prezzi live e barra SL→TP2
- Stato di ogni asset (in posizione / cooldown / in ascolto)
- Storico trade con grafico P&L cumulativo
- Log in tempo reale con color-coding per livello

Auto-refresh ogni 30 secondi. Non richiede API key per funzionare.

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

sudo systemctl status kraken-bot   # controlla stato
sudo journalctl -u kraken-bot -f   # log in tempo reale
```

---

## 8. Windows Task Scheduler

1. Apri **Utilità di pianificazione** → "Crea attività di base"
2. Trigger: **All'avvio del sistema**
3. Azione: **Avvia programma**
   - Programma: `C:\Python311\python.exe`
   - Argomenti: `C:\path\to\kraken-bot\bot.py`
   - Inizia in: `C:\path\to\kraken-bot`
4. Spunta "Esegui che l'utente sia connesso o meno"
5. Salva e inserisci le credenziali Windows

In alternativa crea un file `start.bat`:

```bat
@echo off
cd /d C:\path\to\kraken-bot
python bot.py >> bot_console.log 2>&1
```

---

## 9. Se il bot si ferma

**I fondi sono protetti.**

Ad ogni apertura di posizione il bot piazza su Kraken:
- **Stop-loss nativo Kraken** — risiede sui server Kraken, non nel bot. Scatta automaticamente anche se il bot è offline.
- **Ordini take-profit limit** — anch'essi su Kraken, indipendenti dal bot.

Al riavvio il bot rilegge `positions.json`, riconnette gli ordini aperti e riprende il monitoring. Nessuna posizione va persa.

> Per chiudere manualmente: Kraken → Ordini aperti → Cancella/Chiudi.

---

## 10. Asset monitorati

10 asset su coppie EUR, selezionati per liquidità e volatilità:

| Asset | Allocazione | Min ordine | Pair Kraken |
|-------|------------|-----------|-------------|
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

Le allocazioni sono pesate per capitalizzazione di mercato e sommano al 100%.

---

## 11. Strategia Conservative

Impostazione predefinita. Privilegia segnali forti e riduce i falsi positivi.

### Segnale d'entrata (tutti devono essere veri)

| Condizione | Valore |
|-----------|--------|
| RSI(14) | tra 35 e 50 (zona di accumulo) |
| EMA(50) | prezzo sopra la media mobile veloce |
| EMA(200) | prezzo sopra la media mobile lenta (trend rialzista) |
| MACD | istogramma > 0 oppure crossover bullish sull'ultima candela |
| Volume | > 1.3× media 20 periodi |
| ADX(14) | > 20 (mercato in trend, non laterale) |

### Uscite

| Evento | Azione |
|--------|--------|
| Stop loss ATR | entry − 2 × ATR(14), max −6% di cap |
| TP1 +5% | chiude 50%, SL spostato al breakeven |
| TP1 → trailing stop | SL segue il prezzo a −3% dal massimo |
| TP2 +10% | chiude 30%, posizione chiusa |
| RSI > 72 | segnale SELL → chiusura immediata |
| Divergenza MACD bearish | segnale SELL → chiusura immediata |
| Posizione piatta > 96h | time exit, capitale liberato |

### Controlli di rischio

- Max 4 posizioni aperte simultanee
- Cooldown 4h dopo ogni chiusura
- Pausa globale 24h dopo 2 stop-loss consecutivi

---

## 12. Strategia Aggressive

Più opportunità di entrata, stop più stretti, tre livelli di take profit.
Indicata per mercati in trend con alto volume.

### Segnale d'entrata (tutti devono essere veri)

| Condizione | Valore |
|-----------|--------|
| RSI(14) | tra 30 e 58 (range più ampio) |
| EMA(20) | prezzo sopra la media mobile più reattiva |
| EMA(200) | filtro disabilitato (può operare contro il macro trend) |
| MACD | istogramma > 0 oppure crossover bullish |
| Volume | > 1.1× media 20 periodi |
| ADX(14) | > 15 |

### Uscite

| Evento | Azione |
|--------|--------|
| Stop loss ATR | entry − 1.5 × ATR(14), max −3% di cap |
| TP1 +3% | chiude 40%, SL spostato al breakeven |
| TP1 → trailing stop | SL segue il prezzo a −2% dal massimo |
| TP2 +7% | chiude 35% |
| TP3 +15% | chiude 25% restanti, posizione chiusa |
| RSI > 78 | segnale SELL → chiusura immediata |
| Divergenza MACD bearish | segnale SELL → chiusura immediata |
| Posizione piatta > 48h | time exit, capitale liberato |

### Controlli di rischio

- Max 6 posizioni aperte simultanee
- Size 1.25× l'allocazione standard (cappata al saldo disponibile)
- Cooldown 1h dopo ogni chiusura
- Pausa globale 24h dopo 3 stop-loss consecutivi

> **Quando usare Aggressive:** mercati con trend chiaro, ADX alto e volume crescente.
> Lo stop stretto (ATR×1.5) limita le perdite su falsi breakout; TP3 (+15%) cattura
> i movimenti estesi. Non consigliato in fasi laterali o di alta incertezza.

---

## 13. Confronto strategie

| Parametro | Conservative | Aggressive |
|-----------|-------------|------------|
| RSI buy | 35–50 | 30–58 |
| EMA riferimento | EMA(50) | EMA(20) |
| Filtro EMA(200) | Si | No |
| Volume soglia | >1.3× | >1.1× |
| ADX minimo | 20 | 15 |
| RSI sell | >72 | >78 |
| Stop loss | ATR×2.0 (max −6%) | ATR×1.5 (max −3%) |
| TP1 | +5% → 50% | +3% → 40% |
| TP2 | +10% → 30% | +7% → 35% |
| TP3 | — | +15% → 25% |
| Trailing stop | −3% dal max | −2% dal max |
| Time exit | 96h flat | 48h flat |
| Cooldown | 4h | 1h |
| Max posizioni | 4 | 6 |
| Size | 1× | 1.25× |
| Pause dopo N SL | 2 | 3 |

```env
STRATEGIA=conservative   # default
STRATEGIA=aggressive
```

---

## 14. Gestione posizioni

### Ciclo di vita di una posizione

```
BUY signal
    └─▶ Ordine limit buy piazzato
            └─▶ Attesa esecuzione (max 30 min)
                    ├─▶ [scaduto] → cancellato, retry al prossimo scan
                    └─▶ [eseguito] → SL nativo + TP1/TP2(/TP3) su Kraken
                                          │
                               ┌──────────┼──────────┐
                             TP1        TP2/3        SL
                          chiude 40-50%  chiude tot  chiude tot
                          trailing on   posizione    posizione
                               │         chiusa       chiusa
                          prezzo sale
                               │
                          trailing SL aggiornato
                               │
                          prezzo scende sotto trailing SL
                               └─▶ chiusura con profitto
```

### File di stato

| File | Contenuto |
|------|-----------|
| `positions.json` | stato live di tutte le posizioni (entry, SL, TP, order ID) |
| `trades_history.json` | storico completo dei trade chiusi |
| `bot.log` | log rotante giornaliero |
| `.pause_until` | timestamp di ripresa dopo pausa globale |

### Notifiche Telegram inviate

| Evento | Messaggio |
|--------|-----------|
| Avvio bot | strategia, capitale, prossimo scan |
| Scan 4h | segnale per ogni asset (RSI, EMA, MACD, Volume, ADX, EMA200) |
| Ordine inviato | tipo, prezzo limit, quantità, order ID |
| Posizione aperta | entrata, SL (ATR), TP1/2/3, trailing stop, indicatori |
| TP1 raggiunto | incasso parziale, trailing stop attivato |
| TP2/TP3 raggiunto | incasso parziale/finale, P&L totale, ROI |
| Trailing stop | massimo raggiunto, prezzo di uscita, profitto protetto |
| Stop loss | perdita, SL consecutivi |
| Segnale exit | chiusura su RSI/MACD in posizione aperta |
| Time exit | chiusura posizione piatta per scadenza |
| Max posizioni | BUY ignorato, limite raggiunto |
| Pausa attivata | N SL consecutivi, orario ripresa |
| Errore API | tentativo N/3, retry |
| Report giornaliero | P&L, trade chiusi, posizioni aperte, fee |

---

## 15. Struttura file

```
kraken-bot/
├── bot.py                # Entry point, loop 4h, report giornaliero
├── strategy.py           # Indicatori (RSI, EMA, MACD, ADX, ATR, Volume) e segnali
├── kraken_client.py      # Wrapper API Kraken (prezzi, ordini, saldo, fee)
├── order_manager.py      # Ciclo ordini: place → poll → fill → TP/SL/trailing/time exit
├── position_manager.py   # positions.json e trades_history.json thread-safe
├── risk_manager.py       # Sizing, livelli SL/TP, StrategyConfig, pausa globale
├── telegram_notify.py    # Formattazione e invio di tutti i messaggi Telegram
├── dashboard.py          # Dashboard Streamlit (streamlit run dashboard.py)
├── positions.json        # Auto-generato: stato posizioni live
├── trades_history.json   # Auto-generato: storico trade chiusi
├── bot.log               # Auto-generato: log rotante giornaliero
├── .pause_until          # Auto-generato: timestamp pausa globale
├── .env                  # Credenziali e configurazione (non committare)
├── .env.example          # Template configurazione
└── requirements.txt      # Dipendenze Python
```

# Kraken Trading Bot

Bot Python autonomo che esegue strategie di swing trading o scalping su Kraken con
ordini reali, notifiche Telegram e monitoring via dashboard web. Gira 24/7 senza
intervento umano.

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
12b. [Strategia Scalping](#12b-strategia-scalping)
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
STRATEGIA=conservative       # conservative | aggressive | scalping
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

All'avvio invia su Telegram un messaggio di conferma con strategia, capitale e orario del prossimo scan.
Il report giornaliero viene inviato alle 20:00 UTC.

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

> In paper trading non esistono ordini reali su Kraken — fermare il bot non ha conseguenze sui fondi.

> Per chiudere manualmente in live: Kraken → Ordini aperti → Cancella/Chiudi.

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

### Capitale minimo consigliato

Con capitali bassi alcuni asset potrebbero non raggiungere il minimo d'ordine Kraken.
Il bot gestisce questo caso automaticamente: se la size calcolata è sotto il minimo ma
il saldo disponibile lo copre, **scala al minimo** invece di saltare il trade.

Esempio con €100 e strategia aggressive (size×1.25):

| Asset | Allocazione | Size calcolata | Min Kraken | Comportamento |
|-------|------------|---------------|------------|---------------|
| XRP | 8% → €10 | 7.9 XRP | 10 XRP | scalato a 10 XRP (€12.60) |
| LTC | 6% → €7.50 | 0.15 LTC | 0.05 LTC | ok |
| ADA | 8% → €10 | 43 ADA | 5 ADA | ok |

> Con €100 il bot è pienamente operativo su tutti e 10 gli asset.
> Per eliminare qualsiasi scaling, usa un capitale ≥ €200.

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

## 12b. Strategia Scalping

Opera solo su **BTC** (la coppia EUR più liquida su Kraken: spread stretto e book
profondo, essenziali con target dell'1–2%; inoltre l'ordine minimo ~€10 la rende
adatta anche a capitali piccoli). Candele da **15 minuti**, scan ogni **5 minuti**,
una posizione alla volta, entrate in pullback dentro micro-trend rialzisti.

### Segnale d'entrata (tutti devono essere veri)

| Condizione | Valore |
|-----------|--------|
| EMA(9) > EMA(21) | micro-trend rialzista |
| EMA(50) | prezzo sopra (filtro di trend locale) |
| RSI(14) | tra 38 e 62 (pullback/momentum, non ipercomprato) |
| MACD | istogramma > 0 oppure crossover bullish |
| Volume | > 1.1× media 20 periodi |
| ADX(14) | > 15 (esclude il puro laterale) |

### Uscite

| Evento | Azione |
|--------|--------|
| Stop loss ATR | entry − 1.2 × ATR(14), max −1.6% di cap (fallback −0.8%) |
| TP1 +1.2% | chiude 50%, SL spostato al breakeven |
| TP1 → trailing stop | SL segue il prezzo a −0.8% dal massimo |
| TP2 +2.5% | chiude il restante 50%, posizione chiusa |
| RSI > 78 | segnale SELL → chiusura immediata |
| EMA(9) incrocia sotto EMA(21) | momentum perso → chiusura immediata |
| Posizione piatta > 6h | time exit, capitale liberato |

### Gestione fondi (specifica dello scalping)

- **Equity reale, non capitale statico:** in live la size si calcola su
  `saldo EUR + valore posizioni aperte` letti da Kraken — i profitti compongono,
  le perdite riducono automaticamente l'esposizione.
- **Sizing risk-based:** size = (equity × `RISCHIO_PER_TRADE`) / distanza SL,
  cappata all'intera equity (una sola posizione): con SL a −0.8% il rischio
  effettivo per trade è ~0.8% dell'equity.
- **Buffer fee:** l'1% del saldo disponibile resta sempre libero per le commissioni.
- I target sono dimensionati sulle fee Kraken: TP1 +1.2% copre con margine un
  round-trip maker (~0.32%).

### Controlli di rischio

- Una sola posizione aperta alla volta
- Cooldown 30 minuti dopo ogni chiusura
- Entry limit cancellata se non eseguita entro 5 minuti
- Monitor di uscita ogni 15 secondi (vs 60s dello swing)
- Pausa globale 24h dopo 3 stop-loss consecutivi
- Report Telegram solo su eventi reali (BUY/SELL/errori), niente spam ogni 5 min

> **Quando usare Scalping:** sessioni con volatilità e volume sostenuti
> (apertura USA, news). In mercati piatti l'ADX e il filtro volume tengono il bot
> fermo. Le fee incidono molto sui target piccoli: evitare di abbassare i TP.

---

## 13. Confronto strategie

| Parametro | Conservative | Aggressive | Scalping |
|-----------|-------------|------------|----------|
| Asset | 10 | 10 | solo BTC |
| Timeframe | 4h | 4h | 15m |
| Scan | ogni 4h | ogni 4h | ogni 5 min |
| RSI buy | 35–50 | 30–58 | 38–62 |
| EMA riferimento | EMA(50) | EMA(20) | EMA(9)/EMA(21) + EMA(50) |
| Filtro EMA(200) | Si | No | No |
| Volume soglia | >1.3× | >1.1× | >1.1× |
| ADX minimo | 20 | 15 | 15 |
| RSI sell | >72 | >78 | >78 |
| Stop loss | ATR×2.0 (max −6%) | ATR×1.5 (max −3%) | ATR×1.2 (max −1.6%) |
| TP1 | +5% → 50% | +3% → 40% | +1.2% → 50% |
| TP2 | +10% → 30% | +7% → 35% | +2.5% → 50% |
| TP3 | — | +15% → 25% | — |
| Trailing stop | −3% dal max | −2% dal max | −0.8% dal max |
| Time exit | 96h flat | 48h flat | 6h flat |
| Cooldown | 4h | 1h | 30 min |
| Max posizioni | 4 | 6 | 1 |
| Sizing | allocazioni fisse | allocazioni ×1.25 | risk-based su equity |
| Timeout entry | 30 min | 30 min | 5 min |
| Poll uscite | 60s | 60s | 15s |
| Pause dopo N SL | 2 | 3 | 3 |

```env
STRATEGIA=conservative   # default
STRATEGIA=aggressive
STRATEGIA=scalping
```

---

## 14. Gestione posizioni

### Ciclo di vita di una posizione

```
BUY signal (RSI + EMA + MACD + Volume + ADX + EMA200)
    │
    └─▶ Ordine limit buy piazzato (prezzo maker)
            │
            └─▶ Polling ogni 60s (max 30 min)
                    │
                    ├─▶ [scaduto] ──────────────────▶ cancellato, retry al prossimo scan
                    │
                    └─▶ [eseguito]
                            │
                            ├─▶ SL nativo Kraken  (ATR×moltiplicatore)
                            ├─▶ TP1 limit sell
                            ├─▶ TP2 limit sell
                            └─▶ TP3 limit sell  (solo aggressive)
                                    │
                    ┌───────────────┼───────────────────┬──────────────┐
                    │               │                   │              │
                  TP1 hit        SL hit           SELL signal     Time exit
               chiude 40-50%   chiude tutto      (RSI/MACD)      (piatta >N h)
               SL→breakeven    posizione chiusa  chiude tutto     chiude tutto
               trailing on          │                  │              │
                    │          consecutive SL?         │              │
                    │          ≥ soglia? → pausa 24h   │              │
                    │                                  │              │
               prezzo sale                             │              │
                    │                                  │              │
               trailing_high aggiornato                │              │
                    │                                  │              │
               prezzo < trailing_SL                    │              │
                    └─▶ trailing stop exit ────────────┘              │
                        chiude con profitto                           │
                                                                      │
                            TP2 hit (e TP3 se present)                │
                            chiude rimanente                          │
                            posizione chiusa ─────────────────────────┘
```

**In paper trading** gli ordini sono simulati localmente senza chiamate API Kraken.
I fill sono istantanei, le uscite TP/SL vengono verificate sul prezzo live ogni 60 secondi.

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

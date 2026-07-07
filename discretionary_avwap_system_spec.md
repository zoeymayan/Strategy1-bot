# Discretionary AVWAP Trigger + Position Lock — CC Spec (v2)

## Overview

A system with two parts:
1. **Pine Script indicator(s)** on TradingView — all control happens here (trend, pause/resume, entry triggers, exit level)
2. **Python service on AWS EC2** — receives TV webhooks, holds state, talks to Dhan API, runs position polling/rogue-detection, sends one-way Telegram notifications

**Core principle: TV is the only control surface. Telegram is read-only output — Shyam never types a command into Telegram. All interaction (trend, pause/resume, entries, exits) happens by toggling inputs / drawing on TradingView and letting its alert system fire webhooks.**

**This replaces `sohum.service` entirely.**

---

## Dhan Access Token Refresh

Dhan access tokens expire (check current Dhan API docs for exact validity window — historically ~24hrs for some token types). This needs a daily refresh mechanism before market open.

**CC should first check how sohum currently handles this** — if sohum already has a working token refresh (cron job, script, or manual process), reuse/adapt it rather than building fresh. If sohum has been running on a manually-refreshed token (i.e. Shyam logs in and copies a new token periodically), that gap should be flagged, since this new service should not depend on manual token refresh given it's meant to run unattended through the trading session.

**If a fresh refresh mechanism is needed:**
- Separate cron job (not inside the Flask service) that runs early morning (e.g. 8:45 AM IST, before market open at 9:15 AM)
- Calls Dhan's token refresh/generate endpoint, writes new token to `.env` or a token file the service reads from
- Service should pick up the refreshed token without requiring a manual restart if possible (e.g. re-read token file each time `dhan_client` makes a call, rather than caching it once at startup) — CC to confirm whether Dhan's Python SDK supports this pattern or whether a `systemctl restart discretionary` post-refresh is simpler and more reliable
- Telegram notification on refresh success/failure, since a failed refresh silently breaks the entire system for the day

---

## CC First Step (mandatory, before writing any code)

1. Clone the existing sohum git repo (ask Shyam for the repo URL/path if not obvious)
2. Read and map existing modules: Dhan API wrapper, Telegram notifier, Flask webhook server, systemd unit, any position/order utilities, **and specifically how/whether token refresh is currently handled**
3. Flag any drift between the repo and what's actually deployed on EC2 (ask Shyam to confirm, don't assume repo = deployed) — this includes checking if there's an existing cron job on EC2 for token refresh that isn't reflected in the repo
4. Reuse working components (Dhan client, Telegram sender, webhook server skeleton, systemd config pattern) wherever they fit — modify rather than rewrite from scratch
5. Build this as a clean fresh service (new directory, new systemd unit) rather than patching sohum in place, since the logic is substantially different — but built on reused, proven components

---

## Part 1: Pine Script (TradingView)

One indicator script with multiple inputs, applied to the Nifty chart. All inputs trigger their own alert when changed/crossed.

### Inputs (control panel, edited directly on chart)

| Input | Type | Purpose |
|---|---|---|
| `Trend` | Dropdown: Bull / Bear / None | Sets system trend |
| `System Active` | Toggle: On / Off | Pause/resume |
| `Exit Price` | Float (manually typed/dragged) | PT/SL level — plotted as a horizontal line |

### Plots

- AVWAP — Shyam places this manually as a separate study/drawing (existing workflow, unchanged)
- Exit line — plotted from the `Exit Price` input, so it visually behaves like a draggable horizontal line but is alert-compatible since it's now a calculated series, not a static drawing

### Alerts to configure (4 total, created once per session unless trend/exit changes)

**1. Trend/Control change alert**
- Condition: fires whenever `Trend` or `System Active` input changes (use `input change` triggered alert, or a simple "once per bar close" condition watching the input value vs previous bar)
- Message:
```json
{"action":"control","trend":"{{plot("Trend")}}","active":"{{plot("System Active")}}","secret":"WEBHOOK_SECRET"}
```
- *(CC to finalize exact Pine syntax for reading dropdown/toggle input state into alert message — likely via `plot()` of an internal state variable, since alert messages can't directly read input() values)*

**2. Entry — close cross**
- Condition: `close crosses above AVWAP` (bull) / `close crosses below AVWAP` (bear) — direction governed by current `Trend` input inside the script logic
- Message:
```json
{"signal":"entry","trigger":"close_cross","price":{{close}},"secret":"WEBHOOK_SECRET"}
```

**3. Entry — wick rejection**
- Condition: `low < AVWAP AND close > AVWAP` (bull) / `high > AVWAP AND close < AVWAP` (bear), evaluated **once per bar close** (critical — must not fire intrabar)
- Message:
```json
{"signal":"entry","trigger":"wick_rejection","price":{{close}},"secret":"WEBHOOK_SECRET"}
```

**4. Exit**
- Condition: `close crosses [Exit Price plot]`
- Message:
```json
{"signal":"exit","price":{{close}},"secret":"WEBHOOK_SECRET"}
```
- To move the exit level: Shyam edits the `Exit Price` input value directly (no alert recreation needed, since the alert watches the plot, not a static price)

All 4 alerts webhook to: `http://{EC2_IP}:5001/webhook`

---

## Part 2: Python Service (AWS EC2)

### File Structure

```
~/discretionary/
├── main.py              # Flask webhook server + polling loop
├── telegram_notify.py   # One-way Telegram sender (no command handling)
├── dhan_client.py        # Dhan API wrapper (order, positions, ATM fetch)
├── state.py              # In-memory state machine
├── config.py              # Env vars loader
├── requirements.txt
└── discretionary.service  # systemd unit file
```

### State Machine (`state.py`)

```
State:
  trend: "bull" | "bear" | None
  active: bool
  position:
    open: bool
    symbol: str
    qty: int
    entry_price: float
    order_id: str
  daily_pnl: float          # resets to 0 at start of each trading day
  trade_count_today: int    # resets to 0 at start of each trading day
```

All state changes originate from incoming webhooks only — no external command interface.

**Daily reset:** at first webhook received after 9:00 AM IST on a new calendar date (or via a simple date-check on each webhook), reset `daily_pnl=0` and `trade_count_today=0`. No separate cron needed — checked inline whenever a webhook arrives.

### Telegram Notifier (`telegram_notify.py`)

**Strictly outbound. No bot command handler, no listener, no inbound processing of any kind.**

Sends a message on:
- Control change received (trend set, paused, resumed)
- Order placed (entry)
- Order blocked (entry attempted while locked)
- Exit order placed
- Position auto-unlock detected (flat confirmed via polling)
- Rogue position detected + squared off
- Any Dhan API error

### Webhook Server (`main.py`)

`POST /webhook` — single endpoint, payload `action` or `signal` field determines routing.

**Control payload handling:**
```
if payload.action == "control":
    update state.trend, state.active
    Telegram: "🎛 Trend: {trend} | Active: {active}"
```

**Entry payload handling:**
```
1. If active=False → drop, Telegram: "⏸ Entry ignored — system paused"
2. If trend=None → drop, Telegram: "⚠️ Entry ignored — no trend set"
3. If position.open=True → drop, Telegram: "⚠️ BLOCKED: position already open, no new trade"
4. Else:
   a. Fetch ATM strike from Dhan based on current Nifty price
   b. CE if bull, PE if bear
   c. Place BUY order, configured qty
   d. On success → state.position.open=True, store details
      Telegram: "✅ ORDER PLACED: {symbol} | {qty} lot | {trigger} | ~{price}"
   e. On failure → Telegram: "❌ ORDER FAILED: {error}" (state stays unlocked)
```

**Exit payload handling:**
```
1. If position.open=False → drop, log only (no Telegram spam if accidental double-fire)
2. Place SELL/SQ order for position.symbol, qty
3. On success → Telegram: "🚪 EXIT PLACED: {symbol} | {qty} lot"
   (state.position.open unset by polling loop once confirmed flat, not immediately here)
4. On failure → Telegram: "❌ EXIT FAILED: {error} — manual intervention needed"
```

### Daily P&L Tracking

- On every confirmed flat (polling loop detects net_qty=0 after a position was open), fetch the realized P&L for that closed position from Dhan's trade book / order history (exact_price the SELL filled at minus entry_price, × qty × lot size)
- Add to `state.daily_pnl`, increment `state.trade_count_today`
- Telegram message on each close:
```
🔓 Position closed. P&L this trade: ₹{trade_pnl}
📊 Today's total: ₹{daily_pnl} across {trade_count_today} trade(s)
```
- This replaces the plain "System unlocked" message — same trigger point (polling detects flat), richer content

### Position Polling Loop (background thread, every 30s)

```
poll():
  positions = dhan_client.get_nifty_option_positions()

  IF state.position.open == True:
    if all positions net_qty == 0:
      trade_pnl = dhan_client.get_realized_pnl(state.position.symbol, state.position.order_id)
      state.daily_pnl += trade_pnl
      state.trade_count_today += 1
      state.position.open = False
      Telegram: "🔓 Position closed. P&L this trade: ₹{trade_pnl}\n📊 Today's total: ₹{daily_pnl} across {trade_count_today} trade(s)"
      return

    for pos in positions:
      if pos.symbol != state.position.symbol and pos.net_qty != 0:
        square_off(pos.symbol, pos.net_qty)
        Telegram: "🚨 ROGUE POSITION SQUARED OFF: {symbol} {qty} lots"

    system_pos = match state.position.symbol in positions
    if system_pos and system_pos.net_qty != state.position.qty:
      Telegram: "⚠️ Qty mismatch — expected {expected}, found {actual}. Check manually."

  IF state.position.open == False:
    for pos in positions:
      if pos.net_qty != 0:
        square_off(pos.symbol, pos.net_qty)
        Telegram: "🚨 ROGUE POSITION while FLAT — squared off: {symbol} {qty} lots"
```

### Dhan API Client (`dhan_client.py`)

- `get_atm_strike(index_price, option_type)` → nearest 50 strike, current weekly expiry, full symbol string
- `place_order(symbol, qty, transaction_type)` → MARKET, INTRADAY, returns order_id
- `get_nifty_option_positions()` → filter positions for NIFTY...CE/PE, return `{symbol, net_qty, avg_price}`
- `square_off(symbol, qty)` → SELL MARKET order
- `get_realized_pnl(symbol, order_id)` → fetch trade book / order history for the matching BUY+SELL pair, return realized P&L (₹) for that closed trade

### Config (`config.py`)

```
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
WEBHOOK_SECRET=
TRADE_QTY=1
POLL_INTERVAL=30
FLASK_PORT=5001
```

**`TRADE_QTY` (lots per trade) — fixed, no scaling.** This is the single number of lots placed on every entry, set once and read from `.env` at service start. No multi-lot logic anywhere in the code — this stays a hard constant per run, by design (the whole point of the system is preventing size creep).

**To change lot size:** SSH into EC2, edit `TRADE_QTY` in `.env`, then `sudo systemctl restart discretionary`. This is the only intended way to change position size — no remote/TV/Telegram control over this value, intentionally, so it can't be casually changed mid-session in the heat of a trade.

### systemd

Replace sohum.service:
```bash
sudo systemctl stop sohum
sudo systemctl disable sohum
```

New unit (`discretionary.service`):
```ini
[Unit]
Description=Discretionary AVWAP Lock Service
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/discretionary
EnvironmentFile=/home/ubuntu/discretionary/.env
ExecStart=/home/ubuntu/discretionary/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Full Flow Summary

1. Shyam sets `Trend` input on Pine indicator (Bull/Bear) and ensures `System Active` = On → control webhook fires → EC2 updates state → Telegram confirms
2. Shyam manually places AVWAP on chart (discretionary, unchanged)
3. Price crosses AVWAP (close-cross or wick-rejection, matching trend) → entry webhook fires
4. EC2 validates (active, trend set, not already in position) → places ATM option order via Dhan → locks → Telegram confirms
5. While locked: any further entry webhook dropped + Telegram alert. Parallel 30s polling loop watches actual Dhan positions — any stray manual order (Dhan web/app) gets squared off immediately + Telegram alert
6. Shyam sets/drags `Exit Price` input to desired PT/SL level (no alert recreation needed)
7. Price crosses exit line → exit webhook fires → EC2 squares off position via Dhan → Telegram confirms
8. Polling loop independently confirms flat → calculates trade P&L → updates running daily total → unlocks system → Telegram reports per-trade and daily P&L → ready for next trend/entry
9. To pause mid-session: Shyam toggles `System Active` = Off on the indicator → control webhook → entries ignored until toggled back on

---

## Out of Scope

- Multi-lot scaling
- P&L tracking
- Non-ATM strike selection
- BankNifty (Nifty only)
- EOD auto-exit
- Any Telegram inbound/command handling

---

## Open Item for CC to Resolve

Pine Script cannot natively embed `input()` dropdown/toggle values directly into alert `message` text in real time using `{{...}}` placeholders — alert messages support `{{close}}`, `{{plot_0}}` etc., but reading arbitrary input state into the message string requires either:
(a) plotting the input as a series and referencing `{{plot("Trend")}}`, or
(b) using `alertcondition()` with the input baked into the condition logic and a fixed message that CC infers from context, or
(c) separate dedicated alerts per trend value (e.g. "Bull Mode" alert and "Bear Mode" alert as two different alertcondition() calls, manually toggled on/off by Shyam instead of a dropdown)

CC should evaluate which approach is most reliable in current Pine v5/v6 and implement accordingly — flag the tradeoff to Shyam if (c) ends up simpler/more robust than (a).

---

## Build Order for CC

1. Read sohum repo, map reusable components, identify existing token refresh approach
2. Resolve token refresh — reuse sohum's if working, else build cron + refresh script
3. `config.py` + `.env` template
4. `state.py`
5. `dhan_client.py` (test against Dhan sandbox/paper if available, with fresh token)
6. `telegram_notify.py` (outbound only)
7. `main.py` (webhook server + polling loop)
8. `discretionary.service`
9. Pine Script indicator — resolve the input-to-alert-message question first, then build all 4 alert conditions
10. End-to-end test: simulate each payload type via curl → verify state transitions and Telegram messages → live test on TV paper/small qty before real capital

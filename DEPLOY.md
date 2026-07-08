# Deploy — Discretionary AVWAP Lock Service

## 1. Stop sohum

```bash
sudo systemctl stop sohum
sudo systemctl disable sohum
```

## 2. Copy files to EC2

```bash
scp -r "path/to/discretionary avwap set up/" ubuntu@<EC2_IP>:~/discretionary/
```

Or clone/pull from repo.

## 3. Set up Python env

```bash
cd ~/discretionary
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 4. Configure .env

```bash
cp .env.example .env
nano .env
# Fill in: DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET,
#          TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_SECRET, TRADE_QTY
```

Choose a random `WEBHOOK_SECRET` (e.g. `openssl rand -hex 16`). Paste the same
value into all 4 TradingView alert message JSONs (2 entry + 2 exit-line alerts).

## 5. Install systemd service

```bash
sudo cp ~/discretionary/discretionary.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable discretionary
sudo systemctl start discretionary
sudo systemctl status discretionary
```

Logs: `sudo journalctl -u discretionary -f`

Note: there is no state file. The service re-derives everything (lock,
position, P&L) from Dhan on every webhook/poll — restarts are always safe,
even mid-position.

## 6. Set up token refresh cron

```bash
crontab -e
```

Add (EC2 system clock is IST, so this is 08:45 IST):
```
45 8 * * 1-5 /home/ubuntu/discretionary/venv/bin/python /home/ubuntu/discretionary/refresh_token.py >> /home/ubuntu/discretionary/refresh.log 2>&1
```

## 7. Open firewall port

EC2 Security Group: inbound TCP 5001 from **TradingView webhook IPs only**.
TradingView's current webhook IP ranges are published in their docs.

## 8. TradingView setup (one-time)

TV alerts freeze indicator settings at creation — so nothing below is ever
recreated. All daily control is toggling alerts on/off and dragging lines.

### 8a. Entry alerts (2)

Pine Editor → paste `discretionary_avwap.pine` → Add to chart. In the
indicator settings, bind "AVWAP Source" to your AVWAP study's plot.

Create two alerts on the indicator, **frequency "Once Per Bar Close"**,
webhook URL `http://<EC2_IP>:5001/webhook`, using the built-in messages
(replace `WEBHOOK_SECRET` in the script before adding it to the chart):

| Alert | Condition | Fires on |
|---|---|---|
| Entry Bull (CE) | `Entry Bull (CE)` | close crosses above AVWAP |
| Entry Bear (PE) | `Entry Bear (PE)` | close crosses below AVWAP |

**Daily control = the alert panel:** enable exactly one of the two for your
bias; both off = paused. Never delete them — pause/resume only.

### 8b. Exit lines (2 drawings)

Draw two **horizontal rays**: one for PT, one for SL. Right-click each →
"Add alert", condition **Crossing**, webhook URL as above, and paste as the
message (adjust `"line"` label per drawing):

```json
{"signal":"exit","line":"PT","secret":"<YOUR_SECRET>"}
```
```json
{"signal":"exit","line":"SL","secret":"<YOUR_SECRET>"}
```

Drag the lines to move PT/SL — the alert follows the drawing automatically.
**Never delete the lines** (deleting a drawing kills its alert); park them
away from price when not in use. Crossing alerts fire intrabar, so the exit
acts like a real stop, not a close-confirmed one.

The server is idempotent about exits: when flat, exit signals are ignored
silently, and it only ever sells existing longs — a stray second exit signal
can never create a short.

## 9. Smoke test

```bash
# Entry (bull → buys ATM CE; run while flat, during market hours, tiny TRADE_QTY)
curl -X POST http://<EC2_IP>:5001/webhook \
  -H 'Content-Type: application/json' \
  -d '{"signal":"entry","dir":"bull","price":24500,"secret":"<YOUR_SECRET>"}'

# Duplicate entry (should be silently ignored — check journalctl, no Telegram)
curl -X POST http://<EC2_IP>:5001/webhook \
  -H 'Content-Type: application/json' \
  -d '{"signal":"entry","dir":"bull","price":24500,"secret":"<YOUR_SECRET>"}'

# Exit (sells the long; run again after flat → silently ignored, never shorts)
curl -X POST http://<EC2_IP>:5001/webhook \
  -H 'Content-Type: application/json' \
  -d '{"signal":"exit","line":"SL","secret":"<YOUR_SECRET>"}'
```

Check Telegram for: order placed → exit placed → position closed with P&L.

## Changing lot size

```bash
ssh ubuntu@<EC2_IP>
nano ~/discretionary/.env   # edit TRADE_QTY=
sudo systemctl restart discretionary
```

This is the only way to change position size — no in-session override by design.

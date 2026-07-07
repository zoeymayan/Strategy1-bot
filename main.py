"""
Discretionary AVWAP Lock Service — stateless core.

Dhan is the single source of truth:
  - the entry lock IS the live positions book (any open option = locked)
  - provenance IS the DISC correlationId in today's order book
  - P&L IS Dhan's realizedProfit, summed on demand

The only in-process memory is short-lived cooldowns and the poll loop's
previous snapshot — all safe to lose on a restart. There is no state file.

Direction arrives in each entry payload ("dir": "bull"/"bear") — trend and
pause live entirely in TradingView's alert on/off toggles, so there is no
control webhook and no trend/active state here.

Exits can never short: we only ever sell what the positions book shows long.
Blocked/duplicate/flat signals are dropped silently (log only, no Telegram).
"""
import hmac
import logging
import threading
import time
from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify

import config
import dhan_client
import telegram_notify as tg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

IST = ZoneInfo("Asia/Kolkata")

# NSE holidays — update each year
NSE_HOLIDAYS = {
    2026: {
        date(2026, 1, 26),
        date(2026, 2, 19),
        date(2026, 3, 20),
        date(2026, 3, 31),
        date(2026, 4, 2),
        date(2026, 4, 3),
        date(2026, 4, 14),
        date(2026, 5, 1),
        date(2026, 6, 27),
        date(2026, 8, 15),
        date(2026, 8, 17),
        date(2026, 9, 16),
        date(2026, 10, 2),
        date(2026, 10, 22),
        date(2026, 11, 11),
        date(2026, 11, 12),
        date(2026, 11, 25),
        date(2026, 12, 25),
    },
}


def _is_trading_day() -> bool:
    today = datetime.now(IST).date()
    if today.weekday() >= 5:
        return False
    return today not in NSE_HOLIDAYS.get(today.year, set())


def _is_market_hours() -> bool:
    # Runs until 15:40 so the poll sees the broker's 15:15 MIS square-off
    # and books the day's final P&L.
    now = datetime.now(IST).time()
    return dtime(9, 0) <= now <= dtime(15, 40)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    payload = request.get_json(silent=True) or {}

    secret_ok = hmac.compare_digest(
        str(payload.get("secret", "")),
        config.WEBHOOK_SECRET,
    )
    if not secret_ok:
        log.warning("Webhook received with bad secret")
        return jsonify({"error": "unauthorized"}), 401

    log.info("RAW payload: %s", payload)
    signal = payload.get("signal")

    if signal == "entry":
        return _handle_entry(payload)
    elif signal == "exit":
        return _handle_exit(payload)
    else:
        log.warning("Unknown payload: %s", payload)
        return jsonify({"error": "unknown payload"}), 400


# ---------------------------------------------------------------------------
# Entry — lock derived live from Dhan, direction from the payload
# ---------------------------------------------------------------------------

_entry_lock    = threading.Lock()
_last_entry_at = 0.0   # cooldown bridges order-placed → visible-in-positions


def _handle_entry(payload: dict):
    global _last_entry_at

    direction = str(payload.get("dir", "")).strip().lower()
    if direction not in ("bull", "bear"):
        log.warning("Entry with invalid dir: %r", payload.get("dir"))
        return jsonify({"error": "invalid dir"}), 400

    with _entry_lock:
        if time.time() - _last_entry_at < config.ENTRY_COOLDOWN:
            log.info("Entry ignored — within %ss cooldown of last entry", config.ENTRY_COOLDOWN)
            return jsonify({"status": "ignored", "reason": "cooldown"}), 200

        # The lock: any open option position (long OR short) blocks a new entry
        positions = dhan_client.get_nifty_option_positions()
        if any(p["net_qty"] != 0 for p in positions):
            log.info("Entry ignored — position already open (lock)")
            return jsonify({"status": "ignored", "reason": "position_open"}), 200

        # An entry placed seconds ago may not show in positions yet
        if dhan_client.has_pending_disc_buy():
            log.info("Entry ignored — DISC buy already pending")
            return jsonify({"status": "ignored", "reason": "pending_buy"}), 200

        _last_entry_at = time.time()   # reserve before the slow calls below

    try:
        nifty_ltp   = dhan_client.get_nifty_ltp()
        option_type = "CE" if direction == "bull" else "PE"
        opt = dhan_client.get_atm_option(nifty_ltp, option_type)
        qty = config.TRADE_QTY * config.LOT_SIZE
        order_id = dhan_client.place_buy_order(opt["security_id"], qty)
    except Exception as exc:
        _last_entry_at = 0.0   # release cooldown so the next signal can retry
        log.exception("Entry failed")
        tg.send(f"❌ <b>ORDER FAILED</b>: {exc}")
        return jsonify({"status": "error", "reason": str(exc)}), 200

    # Fill price for the Telegram receipt only (best effort)
    try:
        entry_price = dhan_client.get_ltp(opt["security_id"])
    except Exception:
        entry_price = float(payload.get("price") or 0)

    tg.send(
        f"✅ <b>ORDER PLACED</b>: {opt['symbol']}\n"
        f"Lots: {config.TRADE_QTY} | ~₹{entry_price:.1f}"
    )
    log.info("Entry placed: %s order_id=%s", opt["symbol"], order_id)
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Exit — sell only what Dhan shows long; can never create a short
# ---------------------------------------------------------------------------

_exit_lock        = threading.Lock()
_recent_sells: dict[str, float] = {}   # security_id → time of our last sell
EXIT_RESELL_GUARD = 20   # seconds; bridges sell-placed → visible-in-order-book


def _handle_exit(payload: dict):
    line = str(payload.get("line", "")).strip().upper() or "EXIT"

    with _exit_lock:
        positions = dhan_client.get_nifty_option_positions()
        longs = [p for p in positions if p["net_qty"] > 0]

        if not longs:
            # PT already flattened us and now SL fired (or vice versa) — no-op.
            log.info("Exit signal (%s) — already flat, ignoring", line)
            return jsonify({"status": "ignored", "reason": "flat"}), 200

        for p in longs:
            sec = p["security_id"]
            if time.time() - _recent_sells.get(sec, 0) < EXIT_RESELL_GUARD:
                log.info("Exit (%s) — sell for %s placed moments ago, skipping", line, p["symbol"])
                continue
            if dhan_client.has_pending_sell(sec):
                log.info("Exit (%s) — pending sell exists for %s, skipping", line, p["symbol"])
                continue
            try:
                dhan_client.place_sell_order(sec, p["net_qty"])
                _recent_sells[sec] = time.time()
                tg.send(f"🚪 <b>EXIT PLACED ({line})</b>: {p['symbol']} | qty {p['net_qty']}")
                log.info("Exit (%s) placed for %s qty=%s", line, p["symbol"], p["net_qty"])
            except Exception as exc:
                # No flag to reset — the next crossing simply retries.
                log.exception("Exit order failed")
                tg.send(f"❌ <b>EXIT FAILED</b>: {exc} — next cross retries; check manually")

    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Position polling loop (background thread)
# ---------------------------------------------------------------------------

_in_position_prev = False   # was a DISC long open at the last poll?
_pnl_baseline     = 0.0     # daily realized P&L at the moment the trade opened
_alerted_shorts: set[str] = set()


def _polling_loop():
    log.info("Polling loop started (interval=%ss)", config.POLL_INTERVAL)
    error_streak = 0
    while True:
        time.sleep(config.POLL_INTERVAL)
        try:
            _poll_once()
            error_streak = 0
        except Exception as exc:
            error_streak += 1
            log.exception("Polling loop error: %s", exc)
            # Alert on the first failure, then only every ~10 min of outage
            if error_streak == 1 or error_streak % 20 == 0:
                tg.send(f"⚠️ Polling error (x{error_streak}): {exc}")


def _poll_once():
    global _in_position_prev, _pnl_baseline

    if not _is_trading_day() or not _is_market_hours():
        return

    positions = dhan_client.get_nifty_option_positions()
    disc_secs, disc_buy_count = dhan_client.get_disc_buys_today()

    longs  = [p for p in positions if p["net_qty"] > 0]
    shorts = [p for p in positions if p["net_qty"] < 0]

    # Rogue longs: anything not bought via a DISC order today, or DISC qty
    # above the configured size (manual add-on)
    allowed = config.TRADE_QTY * config.LOT_SIZE
    for p in longs:
        if p["security_id"] not in disc_secs:
            _square_off_rogue(p)
        elif p["net_qty"] > allowed:
            _square_off_rogue({**p, "net_qty": p["net_qty"] - allowed})

    # Shorts: never auto-buy to cover (could double a fill mid-flight) — alert once
    for p in shorts:
        if p["security_id"] not in _alerted_shorts:
            _alerted_shorts.add(p["security_id"])
            tg.send(
                f"🚨 <b>SHORT position detected</b>: {p['symbol']} qty {p['net_qty']}\n"
                f"Not auto-covering — square off manually."
            )
    _alerted_shorts.intersection_update({p["security_id"] for p in shorts})

    # Open/flat transitions → P&L receipts
    disc_open = any(p["security_id"] in disc_secs for p in longs)
    if disc_open and not _in_position_prev:
        # Baseline excludes the open trade (its realized P&L is still 0)
        _pnl_baseline = dhan_client.get_daily_realized_pnl(positions)
        _in_position_prev = True
        log.info("Trade open observed; P&L baseline %.0f", _pnl_baseline)
    elif not disc_open and _in_position_prev:
        daily_pnl = dhan_client.get_daily_realized_pnl(positions)
        trade_pnl = daily_pnl - _pnl_baseline
        _in_position_prev = False
        sign = "+" if trade_pnl >= 0 else ""
        tg.send(
            f"🔓 Position closed. P&L this trade: <b>₹{sign}{trade_pnl:,.0f}</b>\n"
            f"📊 Today's total: <b>₹{daily_pnl:+,.0f}</b> across {disc_buy_count} trade(s)"
        )
        log.info("Position closed: pnl=%.0f daily=%.0f trades=%d",
                 trade_pnl, daily_pnl, disc_buy_count)


def _square_off_rogue(pos: dict):
    try:
        dhan_client.square_off(pos["security_id"], pos["symbol"], pos["net_qty"])
        tg.send(f"🚨 <b>ROGUE POSITION SQUARED OFF</b>: {pos['symbol']} qty={pos['net_qty']}")
        log.warning("Rogue squared off: %s qty=%s", pos["symbol"], pos["net_qty"])
    except Exception as exc:
        tg.send(f"❌ Failed to square off rogue {pos['symbol']}: {exc}")
        log.exception("Rogue square-off failed: %s", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    poll_thread = threading.Thread(target=_polling_loop, daemon=True)
    poll_thread.start()

    tg.send("🟢 Discretionary AVWAP service started (stateless)")
    log.info("Starting Flask on port %s", config.FLASK_PORT)
    app.run(host="0.0.0.0", port=config.FLASK_PORT, debug=False, use_reloader=False)

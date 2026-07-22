"""
Automated intraday short-straddle windows (MFE/MAE-verified pure time exits).

Window 915  (tag STR915,  every trading day):
  09:15:05  SELL ATM CE + PE, nearest weekly, MIS market, STRADDLE_LOTS lots
  10:15:05  BUY both legs back
Window 1400 (tag STR1400, EXPIRY DAYS ONLY — live check, no calendar):
  14:00:05  SELL ATM CE + PE of today's expiry, MIS market, STRADDLE_LOTS_1400 lots
  15:00:05  BUY both legs back
  Expiry day = Dhan's nearest expiry == today, asked at 14:00 — self-adapts
  to holiday shifts and weekday changes.

Stateless: today's order book (per-window correlationId) is the only record.
Re-runs are idempotent — entry refuses if any live order with its own tag
exists today; exit only buys back what its tag net-shows short, so it can
never create a long. The lock service un-nets ALL straddle tags from its
position view. If an exit ultimately fails, Dhan's MIS square-off (~15:15)
is the backstop — and Telegram screams first.

Cron (EC2 system clock is IST):
  15 9  * * 1-5  venv/bin/python straddle_915.py --enter               >> straddle.log 2>&1
  15 10 * * 1-5  venv/bin/python straddle_915.py --exit                >> straddle.log 2>&1
  25 10 * * 1-5  venv/bin/python straddle_915.py --exit                >> straddle.log 2>&1
  0 14  * * 1-5  venv/bin/python straddle_915.py --window 1400 --enter >> straddle.log 2>&1
  0 15  * * 1-5  venv/bin/python straddle_915.py --window 1400 --exit  >> straddle.log 2>&1
  5 15  * * 1-5  venv/bin/python straddle_915.py --window 1400 --exit  >> straddle.log 2>&1

Safety re-run lines are silent no-ops when flat. --force skips clock
alignment and the entry deadline (smoke tests), never the trading-day or
expiry-day guards.
"""
import argparse
import logging
import sys
import time
from datetime import datetime, time as dtime

import config
import dhan_client
import telegram_notify as tg
from market_calendar import IST, is_trading_day

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("straddle_915")

WINDOWS = {
    "915": {
        "tag":         "STR915",
        "label":       "9:15 STRADDLE",
        "entry_at":    dtime(9, 15, 5),
        "deadline":    dtime(9, 20, 0),
        "exit_at":     dtime(10, 15, 5),
        "expiry_only": False,
        "lots":        lambda: config.STRADDLE_LOTS,
    },
    "1400": {
        "tag":         "STR1400",
        "label":       "2PM EXPIRY STRADDLE",
        "entry_at":    dtime(14, 0, 5),
        "deadline":    dtime(14, 5, 0),
        "exit_at":     dtime(15, 0, 5),
        "expiry_only": True,
        "lots":        lambda: config.STRADDLE_LOTS_1400,
    },
}

FILL_TIMEOUT   = 10   # seconds to wait for a market order to show TRADED
FLATTEN_ROUNDS = 3    # exit attempts before escalating to Telegram


def _sleep_until(t: dtime) -> None:
    now = datetime.now(IST)
    target = now.replace(hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
    delay = (target - now).total_seconds()
    if delay > 0:
        log.info("Sleeping %.1fs until %s IST", delay, t)
        time.sleep(delay)


def _wait_for_fill(order_id: str) -> str:
    """Poll until the order is TRADED or dead. Returns the last status seen."""
    status = ""
    deadline = time.time() + FILL_TIMEOUT
    while time.time() < deadline:
        status = dhan_client.get_order_status(order_id)
        if status == "TRADED" or status in dhan_client.DEAD_STATUSES:
            return status
        time.sleep(1)
    return status


def _flatten(win: dict) -> bool:
    """Buy back whatever this window's orders net-show short. True when flat."""
    tag = win["tag"]
    for round_no in range(1, FLATTEN_ROUNDS + 1):
        summary = dhan_client.get_straddle_summary(tag)
        shorts = {sec: s for sec, s in summary.items() if s["net_qty"] < 0}
        if not shorts:
            return True
        for sec, s in shorts.items():
            if dhan_client.has_pending_straddle_buy(sec, tag):
                log.info("Buy-back for %s already pending, skipping", s["symbol"])
                continue
            try:
                dhan_client.place_buy_order(sec, -s["net_qty"], correlation=tag)
                log.info("Buy-back placed: %s qty=%d (round %d)",
                         s["symbol"], -s["net_qty"], round_no)
            except Exception:
                log.exception("Buy-back failed for %s", s["symbol"])
        time.sleep(8)
    summary = dhan_client.get_straddle_summary(tag)
    return all(s["net_qty"] >= 0 for s in summary.values())


def _is_expiry_today() -> bool:
    today = datetime.now(IST).date().isoformat()
    return dhan_client.get_nearest_expiry() == today


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def enter(win: dict, force: bool = False) -> None:
    label, tag = win["label"], win["tag"]
    if not is_trading_day():
        log.info("[%s] Not a trading day — no entry", tag)
        return
    if not force:
        _sleep_until(win["entry_at"])
        if datetime.now(IST).time() > win["deadline"]:
            tg.send(f"⚠️ <b>{label} SKIPPED</b> — entry script started after "
                    f"{win['deadline']} IST (late cron?). No trade today.")
            return
    if win["expiry_only"] and not _is_expiry_today():
        log.info("[%s] Not an expiry day — no entry", tag)
        return

    if dhan_client.get_straddle_summary(tag):
        log.info("[%s] Orders already exist today — entry refused (idempotency)", tag)
        return

    qty  = win["lots"]() * config.LOT_SIZE
    spot = dhan_client.get_nifty_ltp()
    legs = dhan_client.get_atm_straddle(spot)
    log.info("[%s] Entering straddle: spot=%.1f strike=%d exp=%s qty=%d",
             tag, spot, legs["strike"], legs["expiry"], qty)

    # Leg 1 — CE. Confirm it filled before committing to leg 2.
    ce_order = dhan_client.place_sell_order(legs["ce"]["security_id"], qty,
                                            correlation=tag)
    ce_status = _wait_for_fill(ce_order)
    if ce_status != "TRADED":
        flat = _flatten(win)   # cover any partial CE fill
        tg.send(f"❌ <b>{label} ABORTED</b> — CE sell status {ce_status or 'unknown'}. "
                f"{'Covered, flat.' if flat else '🚨 NOT FLAT — check Dhan now.'}")
        return

    # Leg 2 — PE. On any failure, cover the CE so we never carry a naked leg.
    try:
        pe_order = dhan_client.place_sell_order(legs["pe"]["security_id"], qty,
                                                correlation=tag)
        pe_status = _wait_for_fill(pe_order)
        if pe_status != "TRADED":
            raise RuntimeError(f"PE sell status {pe_status or 'unknown'}")
    except Exception as exc:
        log.exception("[%s] PE leg failed — unwinding CE", tag)
        flat = _flatten(win)
        tg.send(f"❌ <b>{label} ABORTED</b> — PE leg failed: {exc}. "
                f"{'CE covered, flat.' if flat else '🚨 NOT FLAT — check Dhan now.'}")
        return

    summary = dhan_client.get_straddle_summary(tag)
    credit = sum(s["sell_value"] for s in summary.values())
    per_unit = credit / qty if qty else 0.0
    ce_avg = summary.get(legs["ce"]["security_id"], {}).get("sell_value", 0.0) / qty
    pe_avg = summary.get(legs["pe"]["security_id"], {}).get("sell_value", 0.0) / qty
    tg.send(
        f"🟢 <b>{label} SOLD</b>: NIFTY {legs['expiry']} {legs['strike']}\n"
        f"CE ₹{ce_avg:.1f} + PE ₹{pe_avg:.1f} = ₹{per_unit:.1f}/unit "
        f"(₹{credit:,.0f} credit, {win['lots']()} lots)\n"
        f"Exit at {win['exit_at'].strftime('%H:%M')}."
    )
    log.info("[%s] Straddle on: credit=%.0f (%.1f/unit)", tag, credit, per_unit)


# ---------------------------------------------------------------------------
# Exit (idempotent; safe to re-run any time)
# ---------------------------------------------------------------------------

def do_exit(win: dict, force: bool = False) -> None:
    label, tag = win["label"], win["tag"]
    if not is_trading_day():
        log.info("[%s] Not a trading day — no exit", tag)
        return
    if not force:
        _sleep_until(win["exit_at"])

    summary = dhan_client.get_straddle_summary(tag)
    if not summary:
        log.info("[%s] No orders today — exit is a no-op", tag)
        return
    if all(s["net_qty"] >= 0 for s in summary.values()):
        log.info("[%s] Already flat — exit is a no-op", tag)
        return

    if not _flatten(win):
        summary = dhan_client.get_straddle_summary(tag)
        still = ", ".join(f"{s['symbol']} {s['net_qty']}"
                          for s in summary.values() if s["net_qty"] < 0)
        tg.send(f"🚨 <b>{label} EXIT FAILED</b> — still short: {still}\n"
                f"Safety cron retries; Dhan MIS square-off ~15:15 is the "
                f"backstop. Check Dhan now.")
        return

    summary = dhan_client.get_straddle_summary(tag)
    pnl = sum(s["pnl"] for s in summary.values() if s["net_qty"] == 0)
    qty = max((s["sell_qty"] for s in summary.values()), default=0)
    entry_pu = sum(s["sell_value"] for s in summary.values()) / qty if qty else 0.0
    exit_pu  = sum(s["buy_value"] for s in summary.values()) / qty if qty else 0.0
    tg.send(
        f"🔴 <b>{label} CLOSED</b>: P&L <b>₹{pnl:+,.0f}</b>\n"
        f"₹{entry_pu:.1f} → ₹{exit_pu:.1f}/unit ({win['lots']()} lots)"
    )
    log.info("[%s] Straddle closed: pnl=%.0f entry=%.1f exit=%.1f",
             tag, pnl, entry_pu, exit_pu)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automated intraday straddle windows")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enter", action="store_true", help="sell the straddle")
    mode.add_argument("--exit", action="store_true", help="buy it back")
    parser.add_argument("--window", choices=sorted(WINDOWS), default="915",
                        help="which straddle window (default 915)")
    parser.add_argument("--force", action="store_true",
                        help="skip clock alignment / entry deadline (smoke tests)")
    args = parser.parse_args()

    win = WINDOWS[args.window]
    label = f"{win['label']} {'ENTRY' if args.enter else 'EXIT'}"
    try:
        if args.enter:
            enter(win, force=args.force)
        else:
            do_exit(win, force=args.force)
    except Exception as exc:
        log.exception("%s crashed", label)
        # Best effort: never leave a naked leg behind an entry crash.
        flat = True
        if args.enter:
            try:
                flat = _flatten(win)
            except Exception:
                log.exception("Post-crash flatten also failed")
                flat = False
        tg.send(f"❌ <b>{label} CRASHED</b>: {exc}\n"
                + ("Safety cron re-runs the exit; MIS square-off is the backstop."
                   if args.exit else
                   ("Flattened any partial fills — flat." if flat else
                    "🚨 Could NOT confirm flat — check Dhan now.")))
        sys.exit(1)

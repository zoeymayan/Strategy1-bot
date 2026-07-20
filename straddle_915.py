"""
9:15 short straddle — fully automated intraday strategy.

Every trading day:
  09:15:05  SELL ATM CE + ATM PE, nearest weekly expiry, MIS market orders,
            STRADDLE_LOTS lots (10 lots = 650 qty) — correlationId STR915
  10:15:05  BUY both legs back — flat by ~10:16

Stateless like everything else here: today's order book (correlationId
STR915) is the only record. Re-runs are idempotent — entry refuses if any
live STR915 order already exists today; exit only buys back what STR915
orders net-show short, so it can never create a long. The lock service
un-nets STR915 qty from its position view, so these legs never trip its
entry lock, short alerts, or rogue policing.

If the exit ultimately fails, Dhan's MIS square-off (~15:15) is the
backstop — and Telegram screams so you can act before that.

Cron (EC2 system clock is IST):
  15 9  * * 1-5  venv/bin/python straddle_915.py --enter >> straddle.log 2>&1
  15 10 * * 1-5  venv/bin/python straddle_915.py --exit  >> straddle.log 2>&1
  25 10 * * 1-5  venv/bin/python straddle_915.py --exit  >> straddle.log 2>&1   # safety re-run; no-op when flat

Smoke test (market hours, tiny STRADDLE_LOTS): --enter --force / --exit --force
(--force skips the clock alignment and entry deadline, not the trading-day guard).
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

ENTRY_AT       = dtime(9, 15, 5)
ENTRY_DEADLINE = dtime(9, 20, 0)   # cron fired late (reboot etc.) → skip the day
EXIT_AT        = dtime(10, 15, 5)

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


def _flatten() -> bool:
    """Buy back whatever STR915 orders net-show short. True when flat."""
    for round_no in range(1, FLATTEN_ROUNDS + 1):
        summary = dhan_client.get_straddle_summary()
        shorts = {sec: s for sec, s in summary.items() if s["net_qty"] < 0}
        if not shorts:
            return True
        for sec, s in shorts.items():
            if dhan_client.has_pending_straddle_buy(sec):
                log.info("Buy-back for %s already pending, skipping", s["symbol"])
                continue
            try:
                dhan_client.place_buy_order(sec, -s["net_qty"],
                                            correlation=dhan_client.STRADDLE_TAG)
                log.info("Buy-back placed: %s qty=%d (round %d)",
                         s["symbol"], -s["net_qty"], round_no)
            except Exception:
                log.exception("Buy-back failed for %s", s["symbol"])
        time.sleep(8)
    summary = dhan_client.get_straddle_summary()
    return all(s["net_qty"] >= 0 for s in summary.values())


# ---------------------------------------------------------------------------
# Entry — 09:15:05
# ---------------------------------------------------------------------------

def enter(force: bool = False) -> None:
    if not is_trading_day():
        log.info("Not a trading day — no entry")
        return
    if not force:
        _sleep_until(ENTRY_AT)
        if datetime.now(IST).time() > ENTRY_DEADLINE:
            tg.send("⚠️ <b>9:15 STRADDLE SKIPPED</b> — entry script started after "
                    f"{ENTRY_DEADLINE} IST (late cron?). No trade today.")
            return

    if dhan_client.get_straddle_summary():
        log.info("STR915 orders already exist today — entry refused (idempotency)")
        return

    qty  = config.STRADDLE_LOTS * config.LOT_SIZE
    spot = dhan_client.get_nifty_ltp()
    legs = dhan_client.get_atm_straddle(spot)
    log.info("Entering straddle: spot=%.1f strike=%d exp=%s qty=%d",
             spot, legs["strike"], legs["expiry"], qty)

    # Leg 1 — CE. Confirm it filled before committing to leg 2.
    ce_order = dhan_client.place_sell_order(legs["ce"]["security_id"], qty,
                                            correlation=dhan_client.STRADDLE_TAG)
    ce_status = _wait_for_fill(ce_order)
    if ce_status != "TRADED":
        flat = _flatten()   # cover any partial CE fill
        tg.send(f"❌ <b>9:15 STRADDLE ABORTED</b> — CE sell status {ce_status or 'unknown'}. "
                f"{'Covered, flat.' if flat else '🚨 NOT FLAT — check Dhan now.'}")
        return

    # Leg 2 — PE. On any failure, cover the CE so we never carry a naked leg.
    try:
        pe_order = dhan_client.place_sell_order(legs["pe"]["security_id"], qty,
                                                correlation=dhan_client.STRADDLE_TAG)
        pe_status = _wait_for_fill(pe_order)
        if pe_status != "TRADED":
            raise RuntimeError(f"PE sell status {pe_status or 'unknown'}")
    except Exception as exc:
        log.exception("PE leg failed — unwinding CE")
        flat = _flatten()
        tg.send(f"❌ <b>9:15 STRADDLE ABORTED</b> — PE leg failed: {exc}. "
                f"{'CE covered, flat.' if flat else '🚨 NOT FLAT — check Dhan now.'}")
        return

    summary = dhan_client.get_straddle_summary()
    credit = sum(s["sell_value"] for s in summary.values())
    per_unit = credit / qty if qty else 0.0
    ce_avg = summary.get(legs["ce"]["security_id"], {}).get("sell_value", 0.0) / qty
    pe_avg = summary.get(legs["pe"]["security_id"], {}).get("sell_value", 0.0) / qty
    tg.send(
        f"🟢 <b>9:15 STRADDLE SOLD</b>: NIFTY {legs['expiry']} {legs['strike']}\n"
        f"CE ₹{ce_avg:.1f} + PE ₹{pe_avg:.1f} = ₹{per_unit:.1f}/unit "
        f"(₹{credit:,.0f} credit, {config.STRADDLE_LOTS} lots)\n"
        f"Exit at 10:15."
    )
    log.info("Straddle on: credit=%.0f (%.1f/unit)", credit, per_unit)


# ---------------------------------------------------------------------------
# Exit — 10:15:05 (idempotent; safe to re-run any time)
# ---------------------------------------------------------------------------

def do_exit(force: bool = False) -> None:
    if not is_trading_day():
        log.info("Not a trading day — no exit")
        return
    if not force:
        _sleep_until(EXIT_AT)

    summary = dhan_client.get_straddle_summary()
    if not summary:
        log.info("No STR915 orders today — exit is a no-op")
        return
    if all(s["net_qty"] >= 0 for s in summary.values()):
        log.info("Straddle already flat — exit is a no-op")
        return

    if not _flatten():
        summary = dhan_client.get_straddle_summary()
        still = ", ".join(f"{s['symbol']} {s['net_qty']}"
                          for s in summary.values() if s["net_qty"] < 0)
        tg.send(f"🚨 <b>9:15 STRADDLE EXIT FAILED</b> — still short: {still}\n"
                f"Retrying via 10:25 cron; Dhan MIS square-off ~15:15 is the "
                f"backstop. Check Dhan now.")
        return

    summary = dhan_client.get_straddle_summary()
    pnl = sum(s["pnl"] for s in summary.values() if s["net_qty"] == 0)
    qty = max((s["sell_qty"] for s in summary.values()), default=0)
    entry_pu = sum(s["sell_value"] for s in summary.values()) / qty if qty else 0.0
    exit_pu  = sum(s["buy_value"] for s in summary.values()) / qty if qty else 0.0
    tg.send(
        f"🔴 <b>9:15 STRADDLE CLOSED</b>: P&L <b>₹{pnl:+,.0f}</b>\n"
        f"₹{entry_pu:.1f} → ₹{exit_pu:.1f}/unit ({config.STRADDLE_LOTS} lots)"
    )
    log.info("Straddle closed: pnl=%.0f entry=%.1f exit=%.1f", pnl, entry_pu, exit_pu)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="9:15 short straddle automation")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enter", action="store_true", help="sell the straddle at 09:15:05")
    mode.add_argument("--exit", action="store_true", help="buy it back at 10:15:05")
    parser.add_argument("--force", action="store_true",
                        help="skip clock alignment / entry deadline (smoke tests)")
    args = parser.parse_args()

    label = "ENTRY" if args.enter else "EXIT"
    try:
        if args.enter:
            enter(force=args.force)
        else:
            do_exit(force=args.force)
    except Exception as exc:
        log.exception("%s crashed", label)
        # Best effort: never leave a naked leg behind an entry crash.
        flat = True
        if args.enter:
            try:
                flat = _flatten()
            except Exception:
                log.exception("Post-crash flatten also failed")
                flat = False
        tg.send(f"❌ <b>9:15 STRADDLE {label} CRASHED</b>: {exc}\n"
                + ("Exit re-runs via 10:25 cron; MIS square-off is the backstop."
                   if args.exit else
                   ("Flattened any partial fills — flat." if flat else
                    "🚨 Could NOT confirm flat — check Dhan now.")))
        sys.exit(1)

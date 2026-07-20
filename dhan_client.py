"""
Dhan API wrapper for the discretionary AVWAP service.

Token is reloaded from .env when the file changes on disk,
under a lock so concurrent threads never observe a mid-reload state.
"""
from __future__ import annotations

import importlib
import logging
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

DHAN_BASE = "https://api.dhan.co/v2"

NIFTY_UNDERLYING_SCRIP = "13"
NIFTY_UNDERLYING_SEG   = "IDX_I"
NIFTY_STRIKE_INTERVAL  = 50

_reload_lock  = threading.Lock()
_env_mtime    = 0.0
_cached_token = ""
_cached_cid   = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _credentials() -> tuple[str, str]:
    """Return (access_token, client_id), reloading config only when .env changes."""
    global _env_mtime, _cached_token, _cached_cid
    import config
    env_path = Path(__file__).parent / ".env"
    try:
        mtime = env_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _reload_lock:
        if mtime > _env_mtime:
            importlib.reload(config)
            _cached_token = config.DHAN_ACCESS_TOKEN
            _cached_cid   = config.DHAN_CLIENT_ID
            _env_mtime    = mtime
        return _cached_token, _cached_cid


def _headers() -> dict:
    token, cid = _credentials()
    return {
        "access-token": token,
        "client-id":    cid,
        "Content-Type": "application/json",
    }


def _get(path: str, params: dict | None = None, retries: int = 3) -> dict:
    url   = DHAN_BASE + path
    delay = 1
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_headers(), params=params, timeout=10)
            if r.status_code == 429:
                log.warning("Rate limited; sleeping %ss", delay)
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt == retries - 1:
                raise
            log.warning("GET %s attempt %d failed: %s", path, attempt + 1, exc)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"GET {path} failed after {retries} retries")


def _post(path: str, body: dict) -> dict:
    """No retries on write calls — prevents duplicate orders."""
    url = DHAN_BASE + path
    r = requests.post(url, headers=_headers(), json=body, timeout=10)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def get_ltp(security_id: str, exchange_segment: str = "NSE_FNO") -> float:
    url = DHAN_BASE + "/marketfeed/ltp"
    r = requests.post(url, headers=_headers(),
                      json={exchange_segment: [int(security_id)]}, timeout=10)
    r.raise_for_status()
    data = r.json()
    try:
        price = float(data["data"][exchange_segment][str(security_id)]["last_price"])
    except Exception as exc:
        raise RuntimeError(f"LTP parse failed for {security_id}: {exc}") from exc
    if price <= 0:
        raise RuntimeError(f"LTP returned zero/negative for {security_id}")
    return price


def get_nifty_ltp() -> float:
    url = DHAN_BASE + "/marketfeed/ltp"
    r = requests.post(url, headers=_headers(),
                      json={"IDX_I": [int(NIFTY_UNDERLYING_SCRIP)]}, timeout=10)
    r.raise_for_status()
    data = r.json()
    try:
        price = float(data["data"]["IDX_I"][NIFTY_UNDERLYING_SCRIP]["last_price"])
    except Exception as exc:
        raise RuntimeError(f"Nifty LTP parse failed: {exc}") from exc
    if price <= 0:
        raise RuntimeError("Nifty LTP returned zero/negative")
    return price


# ---------------------------------------------------------------------------
# Options helpers
# ---------------------------------------------------------------------------

def get_nearest_expiry() -> str:
    r = requests.post(DHAN_BASE + "/optionchain/expirylist", headers=_headers(), json={
        "UnderlyingScrip": int(NIFTY_UNDERLYING_SCRIP),
        "UnderlyingSeg":   NIFTY_UNDERLYING_SEG,
    }, timeout=10)
    r.raise_for_status()
    expiries = r.json().get("data", [])
    if not expiries:
        raise RuntimeError("No expiries returned from Dhan")
    return expiries[0]


def get_atm_straddle(index_price: float) -> dict:
    """Return both ATM legs for the nearest weekly expiry in one chain call.

    {"strike", "expiry", "ce": {symbol, security_id}, "pe": {symbol, security_id}}
    """
    strike = round(index_price / NIFTY_STRIKE_INTERVAL) * NIFTY_STRIKE_INTERVAL
    expiry = get_nearest_expiry()

    r = requests.post(DHAN_BASE + "/optionchain", headers=_headers(), json={
        "UnderlyingScrip": int(NIFTY_UNDERLYING_SCRIP),
        "UnderlyingSeg":   NIFTY_UNDERLYING_SEG,
        "Expiry":          expiry,
    }, timeout=10)
    r.raise_for_status()
    oc = r.json().get("data", {}).get("oc", {})

    ce_id = pe_id = ""
    for strike_key, legs in oc.items():
        if int(float(strike_key)) == strike:
            ce_id = str(legs.get("ce", {}).get("security_id") or "")
            pe_id = str(legs.get("pe", {}).get("security_id") or "")
            break

    if not ce_id or not pe_id:
        raise RuntimeError(f"straddle legs not found for NIFTY {strike} exp {expiry}")

    base = f"NIFTY{expiry.replace('-', '')}{strike}"
    return {
        "strike": strike,
        "expiry": expiry,
        "ce": {"symbol": base + "CE", "security_id": ce_id},
        "pe": {"symbol": base + "PE", "security_id": pe_id},
    }


def get_atm_option(index_price: float, option_type: str) -> dict:
    """Return {symbol, security_id, strike, expiry} for the ATM Nifty option."""
    strike = round(index_price / NIFTY_STRIKE_INTERVAL) * NIFTY_STRIKE_INTERVAL
    expiry = get_nearest_expiry()

    r = requests.post(DHAN_BASE + "/optionchain", headers=_headers(), json={
        "UnderlyingScrip": int(NIFTY_UNDERLYING_SCRIP),
        "UnderlyingSeg":   NIFTY_UNDERLYING_SEG,
        "Expiry":          expiry,
    }, timeout=10)
    r.raise_for_status()
    oc = r.json().get("data", {}).get("oc", {})

    security_id = None
    for strike_key, legs in oc.items():
        if int(float(strike_key)) == strike:
            leg = legs.get("ce" if option_type == "CE" else "pe", {})
            security_id = str(leg.get("security_id") or "")
            break

    if not security_id:
        raise RuntimeError(f"security_id not found for NIFTY {strike} {option_type} exp {expiry}")

    return {
        "symbol":      f"NIFTY{expiry.replace('-', '')}{strike}{option_type}",
        "security_id": security_id,
        "strike":      strike,
        "expiry":      expiry,
        "option_type": option_type,
    }


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def _place_order(side: str, security_id: str, qty: int,
                 correlation: str | None = None) -> str:
    """Place a market intraday order. side = 'BUY' or 'SELL'. Returns order_id."""
    _, cid = _credentials()
    body = {
        "dhanClientId":    cid,
        "transactionType": side,
        "exchangeSegment": "NSE_FNO",
        "productType":     "INTRADAY",
        "orderType":       "MARKET",
        "validity":        "DAY",
        "securityId":      security_id,
        "quantity":        qty,
        "price":           0,
        "correlationId":   correlation or CORRELATION_TAG,
    }
    resp = _post("/orders", body)
    order_id = str(resp.get("orderId") or resp.get("order_id") or "")
    if not order_id:
        raise RuntimeError(f"{side} order failed: {resp}")
    return order_id


def place_buy_order(security_id: str, qty: int, correlation: str | None = None) -> str:
    return _place_order("BUY", security_id, qty, correlation)


def place_sell_order(security_id: str, qty: int, correlation: str | None = None) -> str:
    return _place_order("SELL", security_id, qty, correlation)


def get_order_status(order_id: str) -> str:
    """Current status of a single order (e.g. TRADED, PENDING, REJECTED)."""
    data = _get(f"/orders/{order_id}")
    o = data.get("data", data) if isinstance(data, dict) else data
    if isinstance(o, list):
        o = o[0] if o else {}
    return str(o.get("orderStatus") or o.get("status") or "")


def square_off(security_id: str, symbol: str, qty: int) -> str:
    """Square off a rogue long position."""
    return place_sell_order(security_id, abs(qty))


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def get_nifty_option_positions() -> list[dict]:
    """Return merged list of today's MIS Nifty option positions (deduped by security_id).

    Scoped to productType INTRADAY (MIS) only — the short-straddle strategy
    trades NRML ("MARGIN") in the same account and must be invisible to this
    service: its legs must not trip the entry lock, the rogue policing, the
    short alerts, or the P&L reports. Dhan never nets MIS and NRML rows
    against each other, so this partition is safe even on identical strikes.

    Includes flat rows (net_qty == 0) — their realized_profit is how daily P&L
    is computed, so callers must filter on net_qty themselves.
    """
    data = _get("/positions")
    positions = data if isinstance(data, list) else data.get("data", [])

    merged: dict[str, dict] = {}
    for p in positions:
        if str(p.get("productType") or p.get("product_type") or "") != "INTRADAY":
            continue
        sym = str(p.get("tradingSymbol") or p.get("symbol") or "")
        if not ("NIFTY" in sym and ("CE" in sym or "PE" in sym)):
            continue
        sec_id = str(p.get("securityId") or p.get("security_id") or "")
        net_qty = int(p.get("netQty") or p.get("net_qty") or 0)
        avg_price = float(p.get("costPrice") or p.get("averagePrice") or 0)
        realized = float(p.get("realizedProfit") or p.get("realized_profit") or 0)
        if sec_id in merged:
            merged[sec_id]["net_qty"] += net_qty
            merged[sec_id]["realized_profit"] += realized
        else:
            merged[sec_id] = {"symbol": sym, "security_id": sec_id,
                               "net_qty": net_qty, "avg_price": avg_price,
                               "realized_profit": realized}

    # The 9:15 straddle (straddle_915.py) is MIS too, but must be invisible
    # here — un-net its order-book qty so its shorts never trip the entry
    # lock, the short alerts, or rogue policing. Degrades safely: if the
    # order book is unreachable the raw shorts only block entries / alert.
    try:
        for sec_id, s in get_straddle_summary().items():
            if s["net_qty"] and sec_id in merged:
                merged[sec_id]["net_qty"] -= s["net_qty"]
    except Exception as exc:
        log.warning("Straddle un-netting skipped: %s", exc)

    return list(merged.values())


# ---------------------------------------------------------------------------
# Order book — provenance and pending-order checks
# ---------------------------------------------------------------------------

# Dhan order statuses that mean "may still fill"
PENDING_STATUSES = {"TRANSIT", "PENDING", "PART_TRADED"}
DEAD_STATUSES    = {"REJECTED", "CANCELLED", "EXPIRED"}

CORRELATION_TAG = "DISC"     # discretionary AVWAP trades
STRADDLE_TAG    = "STR915"   # automated 9:15→10:15 short straddle (straddle_915.py)


def get_order_book() -> list[dict]:
    """Return today's orders (Dhan order book is today-only)."""
    data = _get("/orders")
    return data if isinstance(data, list) else data.get("data", [])


def _order_fields(o: dict) -> tuple[str, str, str, str]:
    """(security_id, side, status, correlation_id) with key-name tolerance."""
    return (
        str(o.get("securityId") or o.get("security_id") or ""),
        str(o.get("transactionType") or o.get("transaction_type") or ""),
        str(o.get("orderStatus") or o.get("status") or ""),
        str(o.get("correlationId") or o.get("correlation_id") or ""),
    )


def get_disc_buys_today() -> tuple[set[str], int]:
    """Security IDs bought via DISC-tagged orders today, and count of filled buys.

    Pending buys are included in the security set so an in-flight entry is
    already recognised as ours by the polling loop.
    """
    secs: set[str] = set()
    filled = 0
    for o in get_order_book():
        sec_id, side, status, corr = _order_fields(o)
        if corr != CORRELATION_TAG or side != "BUY" or status in DEAD_STATUSES:
            continue
        if sec_id:
            secs.add(sec_id)
        if status == "TRADED":
            filled += 1
    return secs, filled


def has_pending_disc_buy() -> bool:
    """True if a DISC entry order is placed but not yet filled/dead."""
    for o in get_order_book():
        sec_id, side, status, corr = _order_fields(o)
        if corr == CORRELATION_TAG and side == "BUY" and status in PENDING_STATUSES:
            return True
    return False


def _order_fill(o: dict) -> tuple[int, float]:
    """(filled_qty, avg_traded_price) with key-name tolerance."""
    filled = int(o.get("filledQty") or o.get("filled_qty")
                 or o.get("tradedQty") or o.get("traded_qty") or 0)
    status = str(o.get("orderStatus") or o.get("status") or "")
    if not filled and status == "TRADED":
        filled = int(o.get("quantity") or 0)
    price = float(o.get("averageTradedPrice") or o.get("average_traded_price")
                  or o.get("tradedPrice") or o.get("traded_price") or 0)
    return filled, price


def get_straddle_summary() -> dict[str, dict]:
    """Per-security fill summary of today's STR915 orders. Empty dict = no straddle today.

    net_qty is the position the straddle's own orders imply (short = negative);
    pnl (sell_value − buy_value) is only meaningful once net_qty is 0.
    A key exists as soon as any live STR915 order does, even unfilled — that's
    the entry script's ran-today guard.
    """
    out: dict[str, dict] = {}
    for o in get_order_book():
        sec_id, side, status, corr = _order_fields(o)
        if corr != STRADDLE_TAG or status in DEAD_STATUSES or not sec_id:
            continue
        filled, price = _order_fill(o)
        s = out.setdefault(sec_id, {
            "symbol": str(o.get("tradingSymbol") or o.get("trading_symbol") or ""),
            "net_qty": 0, "sell_qty": 0, "sell_value": 0.0,
            "buy_qty": 0, "buy_value": 0.0,
        })
        if side == "SELL":
            s["net_qty"]    -= filled
            s["sell_qty"]   += filled
            s["sell_value"] += filled * price
        else:
            s["net_qty"]   += filled
            s["buy_qty"]   += filled
            s["buy_value"] += filled * price
    for s in out.values():
        s["pnl"] = s["sell_value"] - s["buy_value"]
    return out


def has_pending_straddle_buy(security_id: str) -> bool:
    """True if a STR915 buy-back order for this security is still working."""
    for o in get_order_book():
        sec_id, side, status, corr = _order_fields(o)
        if (corr == STRADDLE_TAG and sec_id == security_id
                and side == "BUY" and status in PENDING_STATUSES):
            return True
    return False


def has_pending_sell(security_id: str) -> bool:
    """True if any MIS sell order for this security is still working.

    NRML orders excluded — a straddle order on the same strike must not
    block a discretionary exit.
    """
    for o in get_order_book():
        sec_id, side, status, _ = _order_fields(o)
        if (sec_id == security_id and side == "SELL" and status in PENDING_STATUSES
                and str(o.get("productType") or o.get("product_type") or "") == "INTRADAY"):
            return True
    return False


# ---------------------------------------------------------------------------
# P&L
# ---------------------------------------------------------------------------

def get_daily_realized_pnl(positions: list[dict] | None = None) -> float:
    """Today's realized P&L across all Nifty option positions, straight from Dhan.

    Account-level truth: includes any rogue trades the poll squared off.
    Resets naturally each day because Dhan's positions book is today-only.
    """
    if positions is None:
        positions = get_nifty_option_positions()
    total = sum(p["realized_profit"] for p in positions)

    # Keep the AVWAP receipts about AVWAP: back out the closed 9:15 straddle's
    # realized P&L (it reports its own). Open straddle legs carry no realized
    # P&L on Dhan's rows, so only closed (net 0) legs are subtracted.
    try:
        for s in get_straddle_summary().values():
            if s["net_qty"] == 0:
                total -= s["pnl"]
    except Exception as exc:
        log.warning("Straddle P&L exclusion skipped: %s", exc)

    return total

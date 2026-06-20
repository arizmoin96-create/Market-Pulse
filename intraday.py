"""
Intraday Nifty / Bank Nifty scalping cockpit.

Turns NSE's live option chain + market breadth into fast, actionable signals:
  - PCR (put/call OI ratio) and net OI build-up (who's writing — calls or puts)
  - Support / Resistance from the OI "walls" (max put OI / max call OI strikes)
  - Max pain, ATM implied volatility
  - Advances/declines breadth for NIFTY 50 and NIFTY BANK
  - A combined Bullish / Bearish / Neutral bias per index with the key trigger
    levels and a one-line read for a scalp call.

Data: NSE option-chain-v3 + allIndices (warmed cookie session, like app.py).
NSE blocks datacenter IPs, so this is LOCAL-only (empty on Render) — same
constraint as the NSE announcement feed. Educational, NOT investment advice.
"""

import time
from datetime import datetime, timedelta, timezone

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# symbol -> (NSE option symbol, allIndices breadth index name, lot/strike note)
SYMBOLS = {
    "NIFTY": {"idx": "NIFTY 50"},
    "BANKNIFTY": {"idx": "NIFTY BANK"},
}
IST = timezone(timedelta(hours=5, minutes=30))

# 2026 NSE trading holidays (same list as index.html). Weekend handled separately.
HOLIDAYS_2026 = {
    "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
    "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10", "2026-11-24",
    "2026-12-25",
}


# ---------------------------------------------------------------------------
# NSE session
# ---------------------------------------------------------------------------
_sess = None


def _warm():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    s.get("https://www.nseindia.com", timeout=15)
    s.get("https://www.nseindia.com/option-chain", timeout=15)
    return s


def _get(url, referer):
    """GET JSON from NSE, re-warming the cookie session on failure."""
    global _sess
    last = None
    for _ in range(2):
        try:
            if _sess is None:
                _sess = _warm()
            r = _sess.get(
                url,
                headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"},
                timeout=15,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            _sess = None
    raise RuntimeError(f"NSE fetch failed: {last}")


# ---------------------------------------------------------------------------
# Option chain
# ---------------------------------------------------------------------------
_OC_REF = "https://www.nseindia.com/option-chain"
_exp_cache = {}  # symbol -> (ts, nearest_expiry)


def _nearest_expiry(symbol):
    now = time.time()
    cached = _exp_cache.get(symbol)
    if cached and now - cached[0] < 600:
        return cached[1]
    d = _get(
        f"https://www.nseindia.com/api/option-chain-contract-info?symbol={symbol}",
        _OC_REF,
    )
    exp = (d.get("expiryDates") or [None])[0]
    if exp:
        _exp_cache[symbol] = (now, exp)
    return exp


def _fetch_chain(symbol):
    exp = _nearest_expiry(symbol)
    d = _get(
        f"https://www.nseindia.com/api/option-chain-v3"
        f"?type=Indices&symbol={symbol}&expiry={exp}",
        _OC_REF,
    )
    rec = d.get("records") or {}
    rows = [r for r in (rec.get("data") or []) if r.get("CE") or r.get("PE")]
    return {
        "spot": rec.get("underlyingValue"),
        "expiry": exp,
        "timestamp": rec.get("timestamp"),
        "rows": rows,
    }


def _strike_interval(strikes):
    diffs = sorted({round(b - a) for a, b in zip(strikes, strikes[1:]) if b > a})
    return diffs[0] if diffs else 50


def analyze(symbol):
    """Option-chain signals for one index."""
    ch = _fetch_chain(symbol)
    spot, rows = ch["spot"], ch["rows"]
    if not spot or not rows:
        raise RuntimeError("empty chain")

    strikes = sorted(r["strikePrice"] for r in rows)
    interval = _strike_interval(strikes)
    atm = min(strikes, key=lambda k: abs(k - spot))

    # ATM ±10 strikes is the band scalpers actually trade against.
    band = 10 * interval
    near = [r for r in rows if abs(r["strikePrice"] - atm) <= band]

    def ce(r):
        return r.get("CE") or {}

    def pe(r):
        return r.get("PE") or {}

    ce_oi = sum(ce(r).get("openInterest", 0) for r in near)
    pe_oi = sum(pe(r).get("openInterest", 0) for r in near)
    ce_chg = sum(ce(r).get("changeinOpenInterest", 0) for r in near)
    pe_chg = sum(pe(r).get("changeinOpenInterest", 0) for r in near)
    pcr = round(pe_oi / ce_oi, 2) if ce_oi else None

    # Walls: max OI strikes = where writers are parked.
    resistance = max(near, key=lambda r: ce(r).get("openInterest", 0))["strikePrice"]
    support = max(near, key=lambda r: pe(r).get("openInterest", 0))["strikePrice"]
    # Fresh build-up today (max change in OI).
    ce_build = max(near, key=lambda r: ce(r).get("changeinOpenInterest", 0))["strikePrice"]
    pe_build = max(near, key=lambda r: pe(r).get("changeinOpenInterest", 0))["strikePrice"]

    atm_row = next((r for r in rows if r["strikePrice"] == atm), {})
    ce_iv = ce(atm_row).get("impliedVolatility")
    pe_iv = pe(atm_row).get("impliedVolatility")

    # Max pain across the near band.
    max_pain = _max_pain(near, ce, pe)

    # Top OI walls for display.
    top_ce = sorted(near, key=lambda r: ce(r).get("openInterest", 0), reverse=True)[:3]
    top_pe = sorted(near, key=lambda r: pe(r).get("openInterest", 0), reverse=True)[:3]
    walls_ce = [{"strike": r["strikePrice"], "oi": ce(r).get("openInterest", 0),
                 "chg": ce(r).get("changeinOpenInterest", 0)} for r in top_ce]
    walls_pe = [{"strike": r["strikePrice"], "oi": pe(r).get("openInterest", 0),
                 "chg": pe(r).get("changeinOpenInterest", 0)} for r in top_pe]

    # OI bias score (-1..+1): put writing & high PCR = bullish.
    denom = abs(pe_chg) + abs(ce_chg) + 1
    chg_score = _clamp((pe_chg - ce_chg) / denom)
    pcr_score = _clamp(((pcr or 1) - 1) / 0.5)
    # Position in the OI range: near support = bullish lean, near resistance = bearish.
    pos_score = 0.0
    if resistance > support:
        r = (spot - support) / (resistance - support)
        pos_score = _clamp((0.5 - r) * 2)

    return {
        "symbol": symbol,
        "spot": round(spot, 2),
        "atm": atm,
        "expiry": ch["expiry"],
        "timestamp": ch["timestamp"],
        "interval": interval,
        "pcr": pcr,
        "ce_oi": ce_oi, "pe_oi": pe_oi,
        "ce_chg": ce_chg, "pe_chg": pe_chg,
        "support": support, "resistance": resistance,
        "ce_build": ce_build, "pe_build": pe_build,
        "max_pain": max_pain,
        "ce_iv": ce_iv, "pe_iv": pe_iv,
        "walls_ce": walls_ce, "walls_pe": walls_pe,
        "_scores": {"chg": chg_score, "pcr": pcr_score, "pos": pos_score},
    }


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def _max_pain(rows, ce, pe):
    strikes = [r["strikePrice"] for r in rows]
    best, best_pain = None, None
    for expiry_price in strikes:
        pain = 0.0
        for r in rows:
            k = r["strikePrice"]
            if expiry_price > k:
                pain += ce(r).get("openInterest", 0) * (expiry_price - k)
            if expiry_price < k:
                pain += pe(r).get("openInterest", 0) * (k - expiry_price)
        if best_pain is None or pain < best_pain:
            best_pain, best = pain, expiry_price
    return best


# ---------------------------------------------------------------------------
# Breadth (allIndices)
# ---------------------------------------------------------------------------
def fetch_breadth():
    d = _get(
        "https://www.nseindia.com/api/allIndices",
        "https://www.nseindia.com/market-data/live-market-indices",
    )
    out = {}
    wanted = {v["idx"] for v in SYMBOLS.values()}
    for r in d.get("data", []):
        if r.get("index") in wanted:
            out[r["index"]] = {
                "last": _num(r.get("last")),
                "pct": _num(r.get("percentChange")),
                "change": _num(r.get("variation")),
                # NSE returns advances/declines/unchanged as STRINGS — coerce.
                "adv": _int(r.get("advances")),
                "dec": _int(r.get("declines")),
                "unch": _int(r.get("unchanged")),
            }
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Combined snapshot + bias
# ---------------------------------------------------------------------------
def market_phase():
    now = datetime.now(IST)
    ymd = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or ymd in HOLIDAYS_2026:
        return "closed"
    mins = now.hour * 60 + now.minute
    if 540 <= mins < 555:
        return "pre-open"
    if 555 <= mins <= 930:
        return "live"
    return "closed"


def _bias(sig, breadth_row):
    adv = (breadth_row or {}).get("adv") or 0
    dec = (breadth_row or {}).get("dec") or 0
    breadth_score = _clamp((adv - dec) / (adv + dec)) if (adv + dec) else 0.0
    pct = (breadth_row or {}).get("pct") or 0
    mom_score = _clamp(pct / 0.5)

    s = sig["_scores"]
    score = (0.38 * s["chg"] + 0.20 * s["pcr"] + 0.15 * s["pos"]
             + 0.17 * breadth_score + 0.10 * mom_score)

    if score >= 0.18:
        label, klass = "BULLISH", "up"
    elif score <= -0.18:
        label, klass = "BEARISH", "down"
    else:
        label, klass = "NEUTRAL", "flat"
    if abs(score) >= 0.45 and klass != "flat":
        label = "STRONG " + label
    conf = "High" if abs(score) >= 0.4 else "Medium" if abs(score) >= 0.22 else "Low"

    read = _read(sig, label, klass, breadth_row)
    return {
        "label": label, "klass": klass, "score": round(score, 2),
        "confidence": conf, "breadth_score": round(breadth_score, 2),
        "read": read,
    }


def _read(sig, label, klass, breadth_row):
    sym = sig["symbol"]
    spot, R, S, MP = sig["spot"], sig["resistance"], sig["support"], sig["max_pain"]
    writing = ("Call writing dominant — upside likely capped near "
               f"{R}." if sig["ce_chg"] > sig["pe_chg"]
               else f"Put writing dominant — dips likely bought near {S}.")
    if klass == "up":
        trig = f"Hold above {S} favours longs; a break over {R} opens momentum upside."
    elif klass == "down":
        trig = f"Stay below {R} favours shorts; loss of {S} opens momentum downside."
    else:
        trig = f"Range {S}–{R}; trade the edges, breakout either side sets direction."
    pcr = sig["pcr"]
    return (f"{sym} {spot:g} · bias {label} (PCR {pcr}). "
            f"Resistance {R}, Support {S}, Max-pain {MP}. {writing} {trig}")


_SNAP_CACHE = {"ts": 0.0, "data": None}
_SNAP_TTL = 4  # seconds — matches the 5s front-end poll behind one cache


def snapshot(force=False):
    now = time.time()
    if not force and _SNAP_CACHE["data"] and now - _SNAP_CACHE["ts"] < _SNAP_TTL:
        return _SNAP_CACHE["data"]

    phase = market_phase()
    breadth, errs = {}, []
    try:
        breadth = fetch_breadth()
    except Exception as e:  # noqa: BLE001
        errs.append(f"breadth: {e}")

    indices = []
    for sym, meta in SYMBOLS.items():
        try:
            sig = analyze(sym)
            sig["bias"] = _bias(sig, breadth.get(meta["idx"]))
            sig["breadth"] = breadth.get(meta["idx"])
            sig.pop("_scores", None)
            indices.append(sig)
        except Exception as e:  # noqa: BLE001
            errs.append(f"{sym}: {e}")

    out = {
        "ok": bool(indices),
        "phase": phase,
        "asof": int(now * 1000),
        "indices": indices,
        "errors": errs,
    }
    if not indices and _SNAP_CACHE["data"]:
        return _SNAP_CACHE["data"]
    _SNAP_CACHE.update(ts=now, data=out)
    return out

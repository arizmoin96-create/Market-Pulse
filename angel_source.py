"""
Angel One SmartAPI data source for the Scalper's Cockpit (optional, free).

Why: NSE's public option chain refreshes OI only every ~3 minutes and blocks
datacenter IPs. Angel One's authenticated API gives seconds-fresh OI AND works
from the cloud (Render), since it's token-auth, not IP-gated.

SmartAPI has no ready-made option-chain endpoint, so we assemble the chain:
  1. log in (daily JWT via TOTP),
  2. download the instrument master once/day -> find NIFTY/BANKNIFTY option
     tokens for the nearest expiry,
  3. call the Quote API in FULL mode -> openInterest + LTP per strike,
returning the SAME dict shape intraday.analyze() already consumes.

We call the REST API directly (only `requests` + `pyotp`) rather than the
SmartAPI SDK, whose websocket/logzero deps we don't need.

Enabled when  OI_SOURCE=angel  and these env vars are set:
  ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET

Change-in-OI: FULL gives *current* OI, not NSE's day-change, so we baseline
against the first snapshot of the session (the intraday build-up -- what matters
for scalping). ATM IV is left blank for now (Angel exposes it via a separate
Option-Greek endpoint we can wire later).
"""

import datetime
import os
import time

import requests

BASE = "https://apiconnect.angelone.in"
LOGIN_URL = BASE + "/rest/auth/angelbroking/user/v1/loginByPassword"
QUOTE_URL = BASE + "/rest/secure/angelbroking/market/v1/quote/"
SCRIP_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/"
    "OpenAPIScripMaster.json"
)

# Underlying spot tokens in the NSE cash segment (well-known Angel index tokens).
INDEX = {
    "NIFTY":     {"name": "NIFTY",     "label": "NIFTY 50",   "spot_token": "99926000"},
    "BANKNIFTY": {"name": "BANKNIFTY", "label": "NIFTY BANK",  "spot_token": "99926009"},
}

ATM_WINDOW = 12   # strikes each side of ATM -> <=50 tokens -> one FULL batch


def _creds():
    return {
        "api_key": os.environ.get("ANGEL_API_KEY"),
        "client": os.environ.get("ANGEL_CLIENT_CODE"),
        "mpin": os.environ.get("ANGEL_MPIN"),
        "totp_secret": os.environ.get("ANGEL_TOTP_SECRET"),
    }


def available():
    """True only if all four credentials are present."""
    return all(_creds().values())


def _headers(api_key, jwt=None):
    # The IP/MAC headers are required by Angel but their values aren't validated.
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-UserType": "USER",
        "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1",
        "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "AA:BB:CC:DD:EE:FF",
        "X-PrivateKey": api_key,
    }
    if jwt:
        h["Authorization"] = f"Bearer {jwt}"
    return h


# ---------------------------------------------------------------------------
# Login (daily JWT via TOTP)
# ---------------------------------------------------------------------------
_jwt = None
_auth_day = None


def _login():
    global _jwt, _auth_day
    today = datetime.date.today().isoformat()
    if _jwt and _auth_day == today:
        return _jwt

    import pyotp  # lazy: only needed for this source
    c = _creds()
    body = {
        "clientcode": c["client"],
        "password": c["mpin"],
        "totp": pyotp.TOTP(c["totp_secret"]).now(),
    }
    r = requests.post(LOGIN_URL, json=body, headers=_headers(c["api_key"]), timeout=15)
    r.raise_for_status()
    d = r.json()
    if not d.get("status") or not (d.get("data") or {}).get("jwtToken"):
        raise RuntimeError(f"Angel login failed: {d.get('message') or d}")
    _jwt, _auth_day = d["data"]["jwtToken"], today
    return _jwt


# ---------------------------------------------------------------------------
# Quote (FULL mode -> OI + LTP), batched <=50 tokens
# ---------------------------------------------------------------------------
def _quote_full(exchange, tokens):
    if not tokens:
        return {}
    api_key = _creds()["api_key"]
    jwt = _login()
    out = {}
    for i in range(0, len(tokens), 50):
        batch = [str(t) for t in tokens[i:i + 50]]
        r = requests.post(
            QUOTE_URL,
            json={"mode": "FULL", "exchangeTokens": {exchange: batch}},
            headers=_headers(api_key, jwt),
            timeout=15,
        )
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
        for row in data.get("fetched", []) or []:
            out[str(row.get("symbolToken"))] = {
                "oi": row.get("opnInterest"),
                "ltp": row.get("ltp"),
                "close": row.get("close"),
            }
        if len(tokens) > 50:
            time.sleep(0.4)  # stay under the ~1 req/s quote limit
    return out


# Short cache of the index FULL quote so chain+breadth don't double-fetch it.
_idx_cache = {}  # symbol -> (ts, {"last","pct","change"})


def _index_quote(symbol):
    now = time.time()
    c = _idx_cache.get(symbol)
    if c and now - c[0] < 3:
        return c[1]
    tok = INDEX[symbol]["spot_token"]
    q = _quote_full("NSE", [tok]).get(tok) or {}
    last, close = q.get("ltp"), q.get("close")
    change = (last - close) if (last is not None and close) else None
    pct = (change / close * 100.0) if (change is not None and close) else None
    out = {
        "last": last,
        "change": round(change, 2) if change is not None else None,
        "pct": round(pct, 2) if pct is not None else None,
    }
    _idx_cache[symbol] = (now, out)
    return out


# ---------------------------------------------------------------------------
# Instrument master -> option tokens for the nearest expiry (cached per day)
# ---------------------------------------------------------------------------
_chain_meta = {}  # symbol -> (day, expiry_str, {strike: {"CE": token, "PE": token}})


def _parse_expiry(e):
    return datetime.datetime.strptime(e, "%d%b%Y").date()


def _option_tokens(symbol):
    today = datetime.date.today().isoformat()
    cached = _chain_meta.get(symbol)
    if cached and cached[0] == today:
        return cached[1], cached[2]

    name = INDEX[symbol]["name"]
    scrip = requests.get(SCRIP_URL, timeout=30).json()
    rows = [
        s for s in scrip
        if s.get("exch_seg") == "NFO"
        and s.get("name") == name
        and s.get("instrumenttype") == "OPTIDX"
        and s.get("expiry")
    ]
    if not rows:
        raise RuntimeError(f"no {name} option contracts in instrument master")

    today_d = datetime.date.today()
    expiries = sorted({s["expiry"] for s in rows}, key=_parse_expiry)
    future = [e for e in expiries if _parse_expiry(e) >= today_d]
    expiry = future[0] if future else expiries[-1]

    strikes = {}
    for s in rows:
        if s["expiry"] != expiry:
            continue
        try:
            strike = round(float(s["strike"]) / 100.0)  # scrip strike is in paise
        except (TypeError, ValueError):
            continue
        ot = (s.get("symbol") or "")[-2:].upper()
        if ot not in ("CE", "PE"):
            continue
        strikes.setdefault(strike, {})[ot] = s["token"]

    _chain_meta[symbol] = (today, expiry, strikes)
    return expiry, strikes


def _fmt_expiry(e):
    try:
        return _parse_expiry(e).strftime("%d-%b-%Y")
    except Exception:
        return e


# ---------------------------------------------------------------------------
# Change-in-OI baseline (first snapshot of the day -> intraday build-up)
# ---------------------------------------------------------------------------
_oi_base = {"day": None, "map": {}}


def _baseline():
    today = datetime.date.today().isoformat()
    if _oi_base["day"] != today:
        _oi_base.update(day=today, map={})
    return _oi_base["map"]


def _interval(strikes):
    diffs = sorted({b - a for a, b in zip(strikes, strikes[1:]) if b > a})
    return diffs[0] if diffs else 50


# ---------------------------------------------------------------------------
# Public API -- shapes match intraday.py's NSE path
# ---------------------------------------------------------------------------
def fetch_chain(symbol):
    expiry, strikes = _option_tokens(symbol)
    spot = _index_quote(symbol)["last"]

    sorted_strikes = sorted(strikes)
    interval = _interval(sorted_strikes)
    atm = (min(sorted_strikes, key=lambda k: abs(k - spot)) if spot
           else sorted_strikes[len(sorted_strikes) // 2])
    window = ATM_WINDOW * interval
    selected = [k for k in sorted_strikes if abs(k - atm) <= window]

    tokens, tok_meta = [], {}
    for k in selected:
        for ot in ("CE", "PE"):
            t = strikes[k].get(ot)
            if t:
                tokens.append(t)
                tok_meta[str(t)] = (k, ot)

    quotes = _quote_full("NFO", tokens)
    base = _baseline()

    by_strike = {}
    for tok, q in quotes.items():
        k, ot = tok_meta.get(tok, (None, None))
        if k is None:
            continue
        oi = q.get("oi") or 0
        prev = base.setdefault(tok, oi)  # first sight today = baseline
        by_strike.setdefault(k, {})[ot] = {
            "openInterest": oi,
            "changeinOpenInterest": oi - prev,
            "impliedVolatility": 0,   # wired via Option-Greek endpoint later
            "lastPrice": q.get("ltp") or 0,
        }

    rows = [{"strikePrice": k, **by_strike[k]} for k in sorted(by_strike)]
    ts = datetime.datetime.now().strftime("%d-%b-%Y %H:%M:%S")
    return {"spot": spot, "expiry": _fmt_expiry(expiry), "timestamp": ts, "rows": rows}


def fetch_breadth():
    """Index spot/%chg from Angel. Advances/declines aren't available here, so
    they're None (the bias just weights OI/PCR more heavily)."""
    out = {}
    for sym, meta in INDEX.items():
        q = _index_quote(sym)
        out[meta["label"]] = {
            "last": q["last"], "pct": q["pct"], "change": q["change"],
            "adv": None, "dec": None, "unch": None,
        }
    return out

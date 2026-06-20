"""
Global -> India morning-call engine.

Three jobs, all free and key-less:
  1. fetch_global()    - pull overnight global market cues (US indices, Asian
                         markets, crude, USD/INR, dollar index, US 10Y, gold)
                         from Yahoo Finance.
  2. fetch_world_news()- aggregate world equity-market headlines from public
                         RSS feeds (CNBC, MarketWatch, ET Markets).
  3. morning_call()    - turn the cues into a rule-based prediction of how the
                         Indian market (Nifty/Sensex) is likely to OPEN the next
                         morning: Gap Up / Flat / Gap Down + points range +
                         confidence + sector tilts + a plain-English rationale.

An optional AI layer (generate_ai_briefing) writes a strategist-style note, used
only when ANTHROPIC_API_KEY is set. Without it, the rule-based call works fully.

Why no GIFT Nifty: it is the single best predictor of the Nifty open, but there
is no free/keyless feed for it (NSE-IX blocks datacenter IPs, Yahoo has no
symbol). So we infer the open from the global basket instead.
"""

import concurrent.futures
import os
import re
import time
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Instrument registry
# ---------------------------------------------------------------------------
# group: us | asia | india | commodity | fx | rates   (drives UI grouping)
# unit : prefix shown before the value ("" / "$" / "Rs" handled in UI)
INSTRUMENTS = [
    {"key": "sp500",    "sym": "^GSPC",    "name": "S&P 500",      "group": "us"},
    {"key": "nasdaq",   "sym": "^IXIC",    "name": "Nasdaq",       "group": "us"},
    {"key": "dow",      "sym": "^DJI",     "name": "Dow Jones",    "group": "us"},
    {"key": "nikkei",   "sym": "^N225",    "name": "Nikkei 225",   "group": "asia"},
    {"key": "hangseng", "sym": "^HSI",     "name": "Hang Seng",    "group": "asia"},
    {"key": "nifty",    "sym": "^NSEI",    "name": "Nifty 50",     "group": "india"},
    {"key": "sensex",   "sym": "^BSESN",   "name": "Sensex",       "group": "india"},
    {"key": "crude",    "sym": "CL=F",     "name": "Crude (WTI)",  "group": "commodity", "unit": "$"},
    {"key": "gold",     "sym": "GC=F",     "name": "Gold",         "group": "commodity", "unit": "$"},
    {"key": "usdinr",   "sym": "INR=X",    "name": "USD/INR",      "group": "fx"},
    {"key": "dxy",      "sym": "DX-Y.NYB", "name": "Dollar Index", "group": "fx"},
    {"key": "ust10y",   "sym": "^TNX",     "name": "US 10Y",       "group": "rates", "unit": "%"},
]
_BY_KEY = {i["key"]: i for i in INSTRUMENTS}


# Yahoo now rate-limits keyless callers (HTTP 429) unless you carry session
# cookies. We keep one warmed session around and re-warm it on a 429, exactly
# like the NSE proxy in app.py. Hosts are rotated (query1/query2) on retry.
_session = None
_YHOSTS = ("query1", "query2")
# Circuit breaker: once Yahoo 429s us, every quote call short-circuits (no
# network) until this timestamp, so the endpoint stays snappy under a throttle
# instead of blocking on 12 slow retries. Auto-recovers after the cooldown.
_throttled_until = 0.0
_THROTTLE_COOLDOWN = 60  # seconds


def _warm_session():
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": UA,
            "Accept": "text/html,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )
    try:
        s.get("https://finance.yahoo.com", timeout=12)  # collect A1/A3 cookies
    except Exception:
        pass
    return s


def _yahoo_quote(inst):
    """Fetch one instrument from Yahoo's chart API -> normalized dict (or None).

    Retries across query1/query2, re-warming the cookie session on a 429.
    """
    global _session, _throttled_until
    if time.time() < _throttled_until:
        return None  # breaker open — skip the network entirely
    sym = inst["sym"]
    for attempt in range(2):
        try:
            if _session is None:
                _session = _warm_session()
            host = _YHOSTS[attempt % len(_YHOSTS)]
            r = _session.get(
                f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}",
                params={"range": "1d", "interval": "1d"},
                headers={"Referer": "https://finance.yahoo.com/"},
                timeout=10,
            )
            if r.status_code == 429:
                _throttled_until = time.time() + _THROTTLE_COOLDOWN  # trip breaker
                _session = None
                return None
            r.raise_for_status()
            res = (r.json().get("chart") or {}).get("result") or []
            if not res:
                return None
            m = res[0].get("meta") or {}
            last = m.get("regularMarketPrice")
            prev = m.get("chartPreviousClose") or m.get("previousClose")
            if last is None or prev is None or prev == 0:
                return None
            change = last - prev
            return {
                "key": inst["key"],
                "name": inst["name"],
                "group": inst["group"],
                "unit": inst.get("unit", ""),
                "last": round(last, 2),
                "prev": round(prev, 2),
                "change": round(change, 2),
                "pct": round(change / prev * 100.0, 2),
                "asof": m.get("regularMarketTime"),
                "tz": m.get("exchangeTimezoneName"),
            }
        except Exception:
            _session = None
    return None


# Tiny TTL cache so rapid polling / multiple clients don't multiply Yahoo calls.
_GLOBAL_CACHE = {"ts": 0.0, "data": None}
_GLOBAL_TTL = 45  # seconds — overnight cues move slowly; be kind to Yahoo


def fetch_global(force=False):
    """Return {ok, asof, cues:{key:{...}}} of all instruments.

    Low concurrency (4 workers) on purpose: a burst of keyless requests is what
    trips Yahoo's 429 limiter. Results are cached for _GLOBAL_TTL seconds, and
    a partial result is still cached/served (better a few tiles than none)."""
    now = time.time()
    if not force and _GLOBAL_CACHE["data"] and now - _GLOBAL_CACHE["ts"] < _GLOBAL_TTL:
        return _GLOBAL_CACHE["data"]

    # Sequential, one warmed session, lightly spaced. A *burst* of keyless
    # requests is what trips Yahoo's 429 limiter; 12 calls spread over ~2s with
    # shared cookies sails under it and is plenty fast behind the 45s cache.
    cues = {}
    for i, inst in enumerate(INSTRUMENTS):
        q = _yahoo_quote(inst)
        if q:
            cues[q["key"]] = q
        if i < len(INSTRUMENTS) - 1:
            time.sleep(0.12)

    # If a transient throttle wiped the pull, keep serving the last good cues.
    if not cues and _GLOBAL_CACHE["data"]:
        return _GLOBAL_CACHE["data"]

    out = {"ok": bool(cues), "asof": int(now * 1000), "cues": cues}
    _GLOBAL_CACHE.update(ts=now, data=out)
    return out


# ---------------------------------------------------------------------------
# World news (RSS)
# ---------------------------------------------------------------------------
RSS_FEEDS = [
    ("CNBC",        "https://www.cnbc.com/id/100003114/device/rss/rss.html"),   # US top news
    ("CNBC",        "https://www.cnbc.com/id/10000664/device/rss/rss.html"),    # markets
    ("CNBC",        "https://www.cnbc.com/id/100727362/device/rss/rss.html"),   # world
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("ET Markets",  "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s):
    return _WS_RE.sub(" ", _TAG_RE.sub("", s or "")).strip()


def _parse_pubdate(s):
    try:
        return int(parsedate_to_datetime(s).timestamp() * 1000)
    except Exception:
        return None


def _fetch_feed(source, url):
    items = []
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for it in root.iter("item"):
            def tx(tag):
                el = it.find(tag)
                return el.text if el is not None and el.text else ""
            title = _strip_html(tx("title"))
            if not title:
                continue
            items.append(
                {
                    "source": source,
                    "title": title,
                    "link": (tx("link") or "").strip(),
                    "summary": _strip_html(tx("description"))[:280],
                    "published": tx("pubDate").strip(),
                    "ts": _parse_pubdate(tx("pubDate")),
                }
            )
    except Exception:
        pass
    return items


_NEWS_CACHE = {"ts": 0.0, "data": None}
_NEWS_TTL = 120  # seconds


def fetch_world_news(limit=45, force=False):
    now = time.time()
    if not force and _NEWS_CACHE["data"] and now - _NEWS_CACHE["ts"] < _NEWS_TTL:
        return _NEWS_CACHE["data"]

    merged = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(RSS_FEEDS)) as ex:
        futs = [ex.submit(_fetch_feed, s, u) for s, u in RSS_FEEDS]
        for f in concurrent.futures.as_completed(futs):
            merged.extend(f.result())

    # Dedupe by title, newest first.
    seen, out = set(), []
    for it in sorted(merged, key=lambda x: x["ts"] or 0, reverse=True):
        k = it["title"].lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    out = out[:limit]

    res = {"ok": bool(out), "data": out}
    _NEWS_CACHE.update(ts=now, data=res)
    return res


# ---------------------------------------------------------------------------
# Rule-based prediction
# ---------------------------------------------------------------------------
# Each driver pushes the Indian open up (+1) or down (-1) per 1% of its own move,
# scaled by weight. Equity moves are larger in % than FX/rates, so they dominate
# naturally; weights only fine-tune relative influence.
DRIVERS = [
    {"key": "sp500",    "w": 1.0, "sign": +1, "label": "Wall Street (S&P 500)"},
    {"key": "nasdaq",   "w": 0.6, "sign": +1, "label": "Nasdaq (tech)"},
    {"key": "dow",      "w": 0.3, "sign": +1, "label": "Dow Jones"},
    {"key": "nikkei",   "w": 0.7, "sign": +1, "label": "Nikkei (Asia, live)"},
    {"key": "hangseng", "w": 0.7, "sign": +1, "label": "Hang Seng (Asia, live)"},
    {"key": "crude",    "w": 0.5, "sign": -1, "label": "Crude oil (import bill)"},
    {"key": "usdinr",   "w": 0.9, "sign": -1, "label": "Rupee (USD/INR)"},
    {"key": "dxy",      "w": 0.4, "sign": -1, "label": "Dollar index"},
    {"key": "ust10y",   "w": 0.3, "sign": -1, "label": "US 10Y yield"},
]
_K = 0.15          # raw-score -> predicted gap % calibration
_CLAMP = 4.0       # ignore the tail of any single absurd print (%)


def _clamp(x):
    return max(-_CLAMP, min(_CLAMP, x))


def morning_call(global_data=None):
    """Build the prediction dict from global cues."""
    g = global_data or fetch_global()
    cues = g.get("cues", {})

    drivers, raw, pos_w, neg_w = [], 0.0, 0.0, 0.0
    for d in DRIVERS:
        c = cues.get(d["key"])
        if not c or c.get("pct") is None:
            continue
        pct = c["pct"]
        contrib = d["w"] * d["sign"] * _clamp(pct)   # >0 = bullish for India
        raw += contrib
        if contrib > 0:
            pos_w += d["w"]
        elif contrib < 0:
            neg_w += d["w"]
        drivers.append(
            {
                "key": d["key"],
                "label": d["label"],
                "pct": pct,
                "impact": "bull" if contrib > 0 else "bear" if contrib < 0 else "flat",
                "weight": round(abs(contrib), 3),
            }
        )

    nifty = cues.get("nifty", {})
    nifty_last = nifty.get("last")

    gap_pct = round(raw * _K, 2)
    gap_pts = round(nifty_last * gap_pct / 100.0) if nifty_last else None

    # Direction + strength
    if gap_pct >= 0.25:
        direction, klass = ("GAP UP", "up")
    elif gap_pct <= -0.25:
        direction, klass = ("GAP DOWN", "down")
    else:
        direction, klass = ("FLAT / RANGE-BOUND", "flat")
    strong = abs(gap_pct) >= 0.75
    if klass != "flat":
        direction = ("STRONG " if strong else "MILD ") + direction

    # Confidence: how lopsided are the drivers, scaled by move size.
    total_w = pos_w + neg_w
    dominant = (max(pos_w, neg_w) / total_w) if total_w else 0.0
    mag = min(1.0, abs(gap_pct) / 0.8)
    conf_score = dominant * (0.5 + 0.5 * mag)
    confidence = "High" if conf_score >= 0.72 else "Medium" if conf_score >= 0.55 else "Low"

    # Predicted points band
    band = None
    if gap_pts is not None:
        spread = max(15, round(abs(gap_pts) * 0.4))
        band = [gap_pts - spread, gap_pts + spread]

    return {
        "ok": bool(drivers),
        "direction": direction,
        "klass": klass,
        "gap_pct": gap_pct,
        "gap_pts": gap_pts,
        "band": band,
        "confidence": confidence,
        "conf_score": round(conf_score, 2),
        "nifty_last": nifty_last,
        "drivers": sorted(drivers, key=lambda x: x["weight"], reverse=True),
        "sectors": _sector_tilts(cues),
        "rationale": _rationale(direction, klass, drivers, cues),
        "note": "Inferred from the global basket (no live GIFT Nifty feed). "
                "Educational, not investment advice.",
    }


def _pct(cues, key):
    c = cues.get(key)
    return c["pct"] if c and c.get("pct") is not None else None


def _tilt(score, up_reason, down_reason, flat="mixed cues"):
    if score is None:
        return None
    if score > 0.15:
        return {"dir": "up", "reason": up_reason}
    if score < -0.15:
        return {"dir": "down", "reason": down_reason}
    return {"dir": "flat", "reason": flat}


def _sector_tilts(cues):
    out = {}
    nasdaq, usdinr = _pct(cues, "nasdaq"), _pct(cues, "usdinr")
    hsi, dxy = _pct(cues, "hangseng"), _pct(cues, "dxy")
    crude, sp = _pct(cues, "crude"), _pct(cues, "sp500")
    y = _pct(cues, "ust10y")

    # IT: Nasdaq + a weak rupee (USD/INR up) help exporters.
    if nasdaq is not None or usdinr is not None:
        s = (nasdaq or 0) * 1.0 + (usdinr or 0) * 0.5
        out["IT / Tech"] = _tilt(s, "Nasdaq up / rupee soft", "Nasdaq weak / rupee firm")
    # Metals: China (Hang Seng) up + weaker dollar.
    if hsi is not None or dxy is not None:
        s = (hsi or 0) * 1.0 - (dxy or 0) * 0.6
        out["Metals & Mining"] = _tilt(s, "China firm / dollar soft", "China weak / dollar strong")
    # OMCs (oil marketers): cheaper crude is a tailwind.
    if crude is not None:
        out["Oil Marketers (OMCs)"] = _tilt(-crude, "crude lower", "crude higher")
    # Banks/Financials: risk-on + lower US yields.
    if sp is not None or y is not None:
        s = (sp or 0) * 0.5 - (y or 0) * 0.5
        out["Banks & Financials"] = _tilt(s, "risk-on / yields easing", "risk-off / yields rising")
    # Defensives: catch a bid when global risk is off.
    if sp is not None:
        out["Defensives (Pharma/FMCG)"] = _tilt(-sp * 0.5, "global risk-off", "global risk-on", "neutral")
    return {k: v for k, v in out.items() if v}


def _fmt(cues, key):
    c = cues.get(key)
    if not c:
        return None
    return f"{c['name']} {'+' if c['pct'] >= 0 else ''}{c['pct']:.2f}%"


def _rationale(direction, klass, drivers, cues):
    bull = sorted([d for d in drivers if d["impact"] == "bull"], key=lambda x: x["weight"], reverse=True)
    bear = sorted([d for d in drivers if d["impact"] == "bear"], key=lambda x: x["weight"], reverse=True)
    pos = ", ".join(filter(None, (_fmt(cues, d["key"]) for d in bull[:3])))
    neg = ", ".join(filter(None, (_fmt(cues, d["key"]) for d in bear[:3])))

    parts = []
    if pos:
        parts.append(f"Supportive: {pos}.")
    if neg:
        parts.append(f"Headwinds: {neg}.")
    if klass == "up":
        tail = "Net read: Indian indices likely to open firmer."
    elif klass == "down":
        tail = "Net read: a soft, lower start looks likely for Nifty/Sensex."
    else:
        tail = "Net read: a flat, range-bound open with no strong global lead."
    parts.append(tail)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Optional AI narrative (used only if ANTHROPIC_API_KEY is set)
# ---------------------------------------------------------------------------
_AI_CACHE = {"ts": 0.0, "text": None, "model": None}
_AI_TTL = 1800  # 30 min — AI notes are expensive; don't regenerate on every click.


def ai_available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def generate_ai_briefing(force=False):
    """Strategist-style pre-open note via Claude. Returns dict; cached 30 min."""
    if not ai_available():
        return {
            "ok": False,
            "error": "AI briefing is off. Set ANTHROPIC_API_KEY to enable it "
                     "(the rule-based call above always works without a key).",
        }
    now = time.time()
    if not force and _AI_CACHE["text"] and now - _AI_CACHE["ts"] < _AI_TTL:
        return {"ok": True, "text": _AI_CACHE["text"], "model": _AI_CACHE["model"], "cached": True}

    try:
        import anthropic
    except ImportError:
        return {"ok": False, "error": "The 'anthropic' package is not installed (pip install anthropic)."}

    g = fetch_global()
    call = morning_call(g)
    news = fetch_world_news(limit=10)

    cue_lines = [
        f"- {c['name']}: {c['last']} ({'+' if c['pct'] >= 0 else ''}{c['pct']:.2f}%)"
        for c in g.get("cues", {}).values()
    ]
    headlines = [f"- ({n['source']}) {n['title']}" for n in news.get("data", [])[:8]]

    user = (
        "Overnight global market cues:\n" + "\n".join(cue_lines) +
        "\n\nTop world headlines:\n" + "\n".join(headlines) +
        f"\n\nRule-based model says: {call['direction']} "
        f"(~{call['gap_pts']} pts, {call['confidence']} confidence).\n\n"
        "Write a tight pre-open note (max 5 sentences) for an Indian equity "
        "trader: (1) what drove global markets overnight, (2) the likely impact "
        "on Nifty/Sensex at the open, (3) one or two sectors to watch. "
        "Be specific and punchy. No disclaimers, no preamble."
    )

    model = os.environ.get("GLOBAL_AI_MODEL", "claude-haiku-4-5")
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model,
            max_tokens=420,
            system="You are a sharp sell-side markets strategist writing the "
                   "morning pre-open note for Indian equity traders.",
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        _AI_CACHE.update(ts=now, text=text, model=model)
        return {"ok": True, "text": text, "model": model, "cached": False}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"AI call failed: {e}"}

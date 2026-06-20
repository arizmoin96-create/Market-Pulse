# Market Pulse — Nifty / Bank Nifty

A standalone trading dashboard for the Indian market, split out from the
Corporate Announcement Pulse project. Two focused views:

### 🌍 Global → India Morning Call
Captures overnight world-market action (especially the US) and predicts how the
Indian market is likely to **open**:

- **Overnight Wrap** — live tiles for US indices (S&P 500 / Nasdaq / Dow), Asia
  (Nikkei / Hang Seng), crude, gold, USD/INR, dollar index, US 10Y, plus
  Nifty/Sensex last close (Yahoo Finance).
- **Morning Call** — a rule-based engine weighs those cues into a Gap Up / Flat /
  Gap Down call on the Nifty open, with a points range, confidence, drivers and
  sector tilts.
- **World Market News** — US & global headlines from public RSS, sentiment-tagged.
- **✨ AI Strategist Briefing** *(optional)* — a Claude-written pre-open note.
  Set `ANTHROPIC_API_KEY` to enable; the rule-based call always works without it.

### ⚡ Scalper's Cockpit
Fast (5s) intraday decision screen for **Nifty & Bank Nifty** options scalping,
built from the NSE live option chain + breadth:

- **Bias** — Bullish / Bearish / Neutral with confidence, from net OI build-up
  (call vs put writing), PCR, position-in-OI-range and advance/decline breadth.
- **Key levels gauge** — Support (max put OI) · Max-pain · Resistance (max call
  OI), with a live spot marker.
- **Metrics** — PCR, Call/Put OI change, ATM IV.
- **OI walls** — top call (resistance) and put (support) strikes.
- **Actionable read** — the levels, who's in control, and the triggers that flip
  the call.

## Setup & run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py        # http://127.0.0.1:5060
```

Optional AI briefing:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export GLOBAL_AI_MODEL=claude-haiku-4-5   # optional (default)
```

## Files

| File                | Purpose                                                  |
| ------------------- | -------------------------------------------------------- |
| `app.py`            | Flask server + API routes                                |
| `global_markets.py` | Global cues, world-news RSS, morning-call engine         |
| `intraday.py`       | NSE option-chain signals + breadth → intraday scalp bias |
| `index.html`        | The dashboard UI (two views)                             |

## Notes

- **NSE blocks datacenter IPs.** The Scalper's Cockpit (option chain + breadth)
  and the Indian leg of the morning call therefore work on a **local network
  only** — they will be empty on a cloud host like Render. The global cues
  (Yahoo) and world news (RSS) do work in the cloud.
- NSE only republishes **open-interest snapshots about every ~3 minutes**, so the
  OI-based levels move on that cadence while spot/breadth update faster.
- Educational tooling — **not investment advice**. OI levels are guidance, not
  guarantees. Manage your own risk.

"""
Market Pulse — Nifty / Bank Nifty trading dashboard.

A standalone Flask app with two views:
  - Global -> India Morning Call: overnight world-market cues + a next-morning
    prediction for the Indian open, plus a live world-news feed.
  - Scalper's Cockpit: intraday NSE option-chain signals (PCR, OI walls,
    max-pain, IV), breadth, and an actionable bias for Nifty/Bank Nifty.

Engines live in global_markets.py and intraday.py.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5060

NSE blocks datacenter IPs, so the Scalper's Cockpit (and parts of the morning
call) work on a local network only. Educational, not investment advice.
"""

import os

from flask import Flask, jsonify, request, send_from_directory

import global_markets as gm
import intraday as iq

# Serve files relative to THIS file, so the app works regardless of the CWD
# it is launched from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Global -> India Morning Call
# ---------------------------------------------------------------------------
@app.route("/api/global")
def api_global():
    try:
        return jsonify(gm.fetch_global())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e), "cues": {}}), 200


@app.route("/api/morningcall")
def api_morningcall():
    try:
        return jsonify(gm.morning_call())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200


@app.route("/api/worldnews")
def api_worldnews():
    try:
        return jsonify(gm.fetch_world_news())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e), "data": []}), 200


@app.route("/api/ai-briefing")
def api_ai_briefing():
    try:
        force = request.args.get("force") == "1"
        return jsonify(gm.generate_ai_briefing(force=force))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200


# ---------------------------------------------------------------------------
# Scalper's Cockpit (intraday)
# ---------------------------------------------------------------------------
@app.route("/api/intraday")
def api_intraday():
    try:
        return jsonify(iq.snapshot())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e), "indices": []}), 200


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


if __name__ == "__main__":
    # Port 5060 (corporate-pulse uses 5050) so both can run side by side.
    port = int(os.environ.get("PORT", 5060))
    app.run(host="0.0.0.0", port=port, debug=False)

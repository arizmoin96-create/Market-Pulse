"""
AI Co-pilot — answers a trader's questions over the LIVE cockpit state.

It snapshots the current option-chain signals (intraday.snapshot), the global
overnight cues, and the top headlines, then asks Claude to give a concise,
specific read that cites the actual levels/OI/PCR/bias.

Requires ANTHROPIC_API_KEY (same as the morning briefing). Educational — it is
told never to issue buy/sell orders, only to interpret the data. Without a key
the cockpit shows a friendly "set the key to enable" message.
"""

import os

import global_markets as gm
import intraday as iq


def available():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _context():
    """Compact, model-friendly snapshot of everything the co-pilot can see."""
    lines = []
    try:
        snap = iq.snapshot()
        lines.append(f"Market phase: {snap.get('phase')} | OI source: {snap.get('source')}")
        for s in snap.get("indices", []):
            b = s.get("bias", {})
            br = s.get("breadth") or {}
            lines.append(
                f"{s['symbol']}: spot {s['spot']} ({br.get('pct')}%) | "
                f"bias {b.get('label')} ({b.get('confidence')} conf) | PCR {s['pcr']} | "
                f"support {s['support']} | resistance {s['resistance']} | max-pain {s['max_pain']} | "
                f"Call OI Δ {s['ce_chg']} | Put OI Δ {s['pe_chg']} | "
                f"breadth adv/dec {br.get('adv')}/{br.get('dec')} | read: {b.get('read')}"
            )
    except Exception as e:  # noqa: BLE001
        lines.append(f"(option-chain snapshot unavailable: {e})")

    try:
        news = gm.fetch_world_news().get("data", [])[:6]
        if news:
            lines.append("Top headlines: " + " || ".join(f"({n['source']}) {n['title']}" for n in news))
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines)


SYSTEM = (
    "You are a sharp Indian-markets options-scalping assistant embedded in a live "
    "Nifty / Bank Nifty cockpit. Answer the trader's question using ONLY the live "
    "data provided — cite the actual numbers (spot, PCR, support/resistance, "
    "max-pain, OI build-up, bias, breadth, headlines). Be concise (max ~6 "
    "sentences), concrete and practical, and call out the trigger levels. If the "
    "market is closed, say so and treat the figures as the last session. Never "
    "issue a definitive buy/sell order — frame everything as the data's read and "
    "note the trader owns the decision. No preamble, no boilerplate disclaimers."
)


def answer(question):
    if not available():
        return {"ok": False, "error": "AI co-pilot is off — set ANTHROPIC_API_KEY on the server to enable it."}
    try:
        import anthropic
    except ImportError:
        return {"ok": False, "error": "The 'anthropic' package isn't installed (pip install anthropic)."}

    model = os.environ.get("COPILOT_MODEL", "claude-haiku-4-5")
    try:
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model,
            max_tokens=520,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": f"LIVE COCKPIT DATA:\n{_context()}\n\nTRADER'S QUESTION: {question}",
            }],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        return {"ok": True, "text": text, "model": model}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"AI call failed: {e}"}

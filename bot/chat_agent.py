"""The chat agent itself.

Polls Telegram for messages. For each message that ISN'T a `/approve` or
`/reject` (those still belong to the orchestrator's approval gate), sends
the text to Claude with the tools defined in `bot.tools`. Claude either
answers conversationally or asks to run a tool — we execute it and feed
the result back until Claude returns a final natural-language reply,
which we then send to Telegram.

Designed to be friendly + concise on Telegram (Chris's "keep it simple"
feedback). The system prompt keeps replies short and free of jargon.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import anthropic
import requests
from loguru import logger

from config import settings

from .tools import IMPLS, TOOLS, dispatch


TG_BASE = "https://api.telegram.org"


SYSTEM_PROMPT = """You are Chris's personal assistant for the clipfarmer system — a 24/7 clip-farming pipeline that scans Whop campaigns, finds source videos on YouTube, produces vertical clips, posts to YouTube/Instagram, and auto-submits to Whop for payouts.

Chris talks to you on Telegram in casual English. Match that tone:
- Reply in short sentences. Never use long paragraphs.
- Skip preamble. Don't say "Sure, let me help with that". Just do the thing.
- If a tool needs to run, run it without asking permission unless the action is destructive or expensive (a real money / live-post action — for those, confirm first).
- After running a tool, summarise the result in plain English. Don't dump raw output unless Chris asks.
- If you're not sure what Chris wants, ask one clarifying question.
- Use Telegram HTML formatting (<b>bold</b>, <i>italic</i>, <code>monospace</code>) sparingly for readability.
- Numbers and URLs go in <code> tags so they're tappable.

When Chris asks vague things like "how's it going" or "what's up", use system_status to give him a one-line snapshot.
When Chris wants to "run a clip" or "make some videos", default to format_mode='crop' which is the proven-good setting.
When listing campaigns, note which ones have a registered source (can run immediately) vs which need find_source first.
"""

MODEL = settings.anthropic_model


class ChatAgent:
    def __init__(self) -> None:
        self.bot_token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        if not (self.bot_token and self.chat_id):
            raise RuntimeError(
                "Telegram bot not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)."
            )
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._update_offset = 0
        self._seed_offset()

    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        logger.info("[bot] listening — message me anything on Telegram")
        self._send("👋 Bot is online. Ask me anything (e.g. <i>'how's it going?'</i>, <i>'show campaigns'</i>, <i>'find a source for 44'</i>).")
        while True:
            try:
                self._poll_and_handle()
            except KeyboardInterrupt:
                logger.info("[bot] stopping")
                self._send("👋 Bot offline.")
                return
            except Exception as e:
                logger.exception(f"[bot] loop error: {e}")
                time.sleep(5)

    # ------------------------------------------------------------------
    def _poll_and_handle(self) -> None:
        data = self._api("getUpdates", offset=self._update_offset, timeout=25)
        updates = data.get("result", [])
        for u in updates:
            self._update_offset = max(self._update_offset, u["update_id"] + 1)
            msg = u.get("message") or u.get("edited_message") or {}
            text = (msg.get("text") or "").strip()
            chat = msg.get("chat") or {}
            from_user = msg.get("from") or {}
            if not text:
                continue
            # Only respond in our configured chat
            if str(chat.get("id")) != str(self.chat_id):
                continue
            # Hand off /approve and /reject to the orchestrator via the shared
            # verdict file. The bot is the sole Telegram poller now (no
            # offset-stealing race with the orchestrator), so we have to relay
            # the decision rather than letting both pollers fight.
            if text.startswith("/approve") or text.startswith("/reject"):
                self._write_verdict(text)
                continue
            logger.info(f"[bot] msg from {from_user.get('username') or from_user.get('id')}: {text[:200]}")
            try:
                reply = self._answer(text)
            except Exception as e:
                logger.exception(f"[bot] answer crashed: {e}")
                reply = f"⚠️ I crashed processing that: <code>{_esc(str(e))}</code>"
            self._send(reply)

    # ------------------------------------------------------------------
    def _answer(self, text: str) -> str:
        """Run the agent loop: Claude → maybe tools → Claude → final reply."""
        messages: list[dict[str, Any]] = [{"role": "user", "content": text}]
        for hop in range(8):  # hard cap on tool-loop hops
            resp = self.client.messages.create(
                model=MODEL,
                max_tokens=2_000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
            # Did Claude ask for any tools?
            tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
            if not tool_uses:
                # Final natural-language reply
                text_parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
                return "\n".join(t for t in text_parts if t).strip() or "(no reply)"

            # Execute each tool and append results
            messages.append({"role": "assistant", "content": [_block_to_dict(b) for b in resp.content]})
            tool_results = []
            for tu in tool_uses:
                logger.info(f"[bot] tool: {tu.name}({tu.input})")
                self._typing()
                result_text = dispatch(tu.name, dict(tu.input or {}))
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result_text[:6000],   # Telegram-friendly cap
                })
            messages.append({"role": "user", "content": tool_results})
        return "⚠️ Hit the tool-loop limit. Try rephrasing more specifically."

    # ------------------------------------------------------------------
    # Telegram plumbing
    # ------------------------------------------------------------------
    def _seed_offset(self) -> None:
        try:
            data = self._api("getUpdates")
            updates = data.get("result", [])
            if updates:
                self._update_offset = max(u["update_id"] for u in updates) + 1
        except Exception as e:
            logger.warning(f"[bot] could not seed offset: {e}")

    def _api(self, method: str, **params) -> dict:
        url = f"{TG_BASE}/bot{self.bot_token}/{method}"
        r = requests.post(url, data=params, timeout=60)
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram {method} failed: {r.status_code} {r.text}")
        return r.json()

    def _send(self, text: str) -> None:
        # Telegram caps messages at 4096 chars
        if len(text) > 4000:
            text = text[:3900] + "\n…(truncated)"
        try:
            self._api(
                "sendMessage",
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview="true",
            )
        except Exception as e:
            logger.warning(f"[bot] send failed: {e}")

    def _typing(self) -> None:
        try:
            self._api("sendChatAction", chat_id=self.chat_id, action="typing")
        except Exception:
            pass

    def _write_verdict(self, text: str) -> None:
        """Relay /approve or /reject to the running orchestrator via a shared file.
        The orchestrator's wait_for_verdict polls this file instead of Telegram."""
        from publisher.telegram_gate import VERDICT_FILE
        import json as _json
        lower = text.strip().lower()
        if lower.startswith("/approve"):
            status = "APPROVED"
            note = lower.replace("/approve", "", 1).strip()
            ack = "✅ Approval forwarded to the pipeline."
        else:
            status = "REJECTED"
            note = lower.replace("/reject", "", 1).strip()
            ack = "🛑 Rejection forwarded — fresh moment incoming."
        try:
            VERDICT_FILE.parent.mkdir(parents=True, exist_ok=True)
            VERDICT_FILE.write_text(
                _json.dumps({"status": status, "note": note}), encoding="utf-8",
            )
            self._send(ack)
        except Exception as e:
            logger.warning(f"[bot] failed to write verdict file: {e}")


# ----------------------------------------------------------------------
def _block_to_dict(b) -> dict:
    """Convert an SDK content block back into the dict shape the API expects on retry."""
    t = getattr(b, "type", "")
    if t == "text":
        return {"type": "text", "text": b.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input or {})}
    # Fallback — best-effort
    return {"type": t, **{k: v for k, v in b.__dict__.items() if not k.startswith("_")}}


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

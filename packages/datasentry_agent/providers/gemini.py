"""Gemini-backed reasoner. The fallback when a ``GEMINI_API_KEY`` is present.

Google's ``generateContent`` API differs from the OpenAI shape in three ways
that matter to this codebase, and all three are handled here rather than
leaking into the nodes:

* **The system prompt is its own field**, ``systemInstruction``, not a message
  with a role. That is an improvement for our purposes: the standing rule that
  evidence is data cannot be displaced by a long user turn.
* **JSON is a response MIME type**, ``application/json``, set on
  ``generationConfig`` rather than on the request.
* **A response can be empty with a reason.** A ``finishReason`` of ``SAFETY``
  or ``MAX_TOKENS`` returns candidates with no text, so the empty case is
  logged as what it is instead of being parsed as malformed JSON.

The free tier is metered per minute and per day and is comfortable for an
investigation at a time. Flash handles triage and planning; the frontier tier
runs the reasoning tasks.
"""

from __future__ import annotations

import os
from typing import Any

from datasentry_agent.providers.hosted import HostedReasoner, RateLimited, retry_after_seconds

API_ROOT = "https://generativelanguage.googleapis.com/v1beta"

# Both are on the free tier. `ds doctor` verifies these against the live
# catalogue, because Google retires model ids on a published schedule.
CHEAP_MODEL = "gemini-2.0-flash"
FRONTIER_MODEL = "gemini-2.5-flash"


class GeminiReasoner(HostedReasoner):
    name = "gemini"
    api_key_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(self, config: Any = None, *, client: Any = None) -> None:
        super().__init__(config, client=client)
        self.api_key = self.settings.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")

    @classmethod
    def models(cls) -> tuple[str, str]:
        return CHEAP_MODEL, FRONTIER_MODEL

    def complete(self, *, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool) -> tuple[str, int, int]:
        import httpx

        config: dict[str, Any] = {"temperature": 0, "maxOutputTokens": max_tokens}
        if json_mode:
            config["responseMimeType"] = "application/json"

        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }

        response = httpx.post(
            API_ROOT + "/models/" + model + ":generateContent",
            # The key travels as a header, never in the query string, so it
            # does not land in a proxy access log.
            headers={"x-goog-api-key": self.api_key, "content-type": "application/json"},
            json=body,
            timeout=60.0,
        )
        if response.status_code == 429:
            raise RateLimited(retry_after_seconds(response.headers))
        response.raise_for_status()
        doc = response.json()

        candidates = doc.get("candidates") or []
        text = ""
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(str(part.get("text", "")) for part in parts)
            if not text:
                reason = str(candidates[0].get("finishReason", "unknown"))
                raise ValueError("gemini returned no text, finishReason=" + reason)

        usage = doc.get("usageMetadata") or {}
        return (
            text,
            int(usage.get("promptTokenCount", 0)),
            int(usage.get("candidatesTokenCount", 0)),
        )

    def health(self) -> dict[str, Any]:
        import httpx

        if not self.api_key:
            return {"provider": self.name, "ok": False, "reason": "GEMINI_API_KEY is not set"}
        try:
            response = httpx.get(
                API_ROOT + "/models",
                headers={"x-goog-api-key": self.api_key},
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "ok": False, "reason": type(exc).__name__}
        if response.status_code != 200:
            # Google answers a bad key with 400 INVALID_ARGUMENT, not 401.
            # Reporting that as "the models endpoint returned 400" sends
            # someone looking for a network problem when they have a typo,
            # so the reason string is read out of the body.
            detail = ""
            try:
                detail = str((response.json().get("error") or {}).get("message", ""))
            except Exception:  # noqa: BLE001 - an unparseable error body is still an error
                detail = ""
            if response.status_code in {401, 403} or "API key not valid" in detail:
                return {"provider": self.name, "ok": False,
                        "reason": "key rejected" + (" -- " + detail if detail else "")}
            return {"provider": self.name, "ok": False,
                    "reason": "models endpoint returned " + str(response.status_code)
                              + (": " + detail if detail else "")}
        # Ids come back as "models/gemini-2.0-flash"; compare on the bare name.
        available = {
            str(m.get("name", "")).split("/")[-1] for m in (response.json().get("models") or [])
        }
        return {
            "provider": self.name,
            "ok": True,
            "models": sorted(m for m in available if m.startswith("gemini"))[:40],
            "cheap": self.cheap_model,
            "frontier": self.frontier_model,
            "cheap_available": self.cheap_model in available,
            "frontier_available": self.frontier_model in available,
        }

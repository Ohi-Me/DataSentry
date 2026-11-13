"""Groq-backed reasoner. The default when a ``GROQ_API_KEY`` is present.

Groq serves open-weight models on its own inference hardware behind an
OpenAI-compatible Chat Completions API, and its free tier is generous enough
to run the whole RCA loop without a card on file. That combination is why it
is first in the auto-detection order: a fresh clone with one environment
variable gets real reasoning rather than the deterministic baseline.

Two-tier routing survives the move intact, which is the thing worth checking
when you swap providers. Triage and planning are classification and go to the
8B model; hypothesising, verifying and narrating go to the 70B one. The ratio
target in ADR-007 is unchanged because it is a property of the graph, not of
the vendor.

The rate limit is the real constraint here, not the price. A free key is
metered in requests per minute and tokens per day, and one investigation is a
burst of five calls. ``HostedReasoner`` retries a 429 once and then degrades,
so a limit costs you a blunter narrative rather than a failed run.
"""

from __future__ import annotations

import os
from typing import Any

from datasentry_agent.providers.hosted import HostedReasoner, RateLimited, retry_after_seconds

API_URL = "https://api.groq.com/openai/v1/chat/completions"
MODELS_URL = "https://api.groq.com/openai/v1/models"

# Both are on the free tier. Overridable with DS_LLM_CHEAP_MODEL and
# DS_LLM_FRONTIER_MODEL, and `ds doctor` checks the ids against the live
# catalogue rather than trusting these to stay current.
CHEAP_MODEL = "llama-3.1-8b-instant"
FRONTIER_MODEL = "llama-3.3-70b-versatile"


class GroqReasoner(HostedReasoner):
    name = "groq"
    api_key_env = ("GROQ_API_KEY",)

    def __init__(self, config: Any = None, *, client: Any = None) -> None:
        super().__init__(config, client=client)
        self.api_key = self.settings.groq_api_key or os.environ.get("GROQ_API_KEY", "")

    @classmethod
    def models(cls) -> tuple[str, str]:
        return CHEAP_MODEL, FRONTIER_MODEL

    def complete(self, *, model: str, system: str, user: str, max_tokens: int,
                 json_mode: bool) -> tuple[str, int, int]:
        import httpx

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            # Constrained decoding, not a request. Worth far more on an 8B
            # model than on a frontier one, which is exactly where it is used.
            body["response_format"] = {"type": "json_object"}

        response = httpx.post(
            API_URL,
            headers={
                "authorization": "Bearer " + self.api_key,
                "content-type": "application/json",
            },
            json=body,
            timeout=60.0,
        )
        if response.status_code == 429:
            raise RateLimited(retry_after_seconds(response.headers))
        response.raise_for_status()
        doc = response.json()
        choices = doc.get("choices") or []
        text = ""
        if choices:
            text = str((choices[0].get("message") or {}).get("content") or "")
        usage = doc.get("usage") or {}
        return text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))

    def health(self) -> dict[str, Any]:
        import httpx

        if not self.api_key:
            return {"provider": self.name, "ok": False, "reason": "GROQ_API_KEY is not set"}
        try:
            response = httpx.get(
                MODELS_URL,
                headers={"authorization": "Bearer " + self.api_key},
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            return {"provider": self.name, "ok": False, "reason": type(exc).__name__}
        if response.status_code == 401:
            return {"provider": self.name, "ok": False, "reason": "key rejected (401)"}
        if response.status_code != 200:
            return {"provider": self.name, "ok": False,
                    "reason": "models endpoint returned " + str(response.status_code)}
        available = {str(m.get("id")) for m in (response.json().get("data") or [])}
        return {
            "provider": self.name,
            "ok": True,
            "models": sorted(available)[:40],
            "cheap": self.cheap_model,
            "frontier": self.frontier_model,
            "cheap_available": self.cheap_model in available,
            "frontier_available": self.frontier_model in available,
        }

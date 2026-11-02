"""OpenCode Go chat client (OpenAI-compatible)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx

from cba_bot import __version__
from cba_bot.config import Settings


class LLMError(RuntimeError):
    pass


def _ids(payload: object) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    return [
        str(item["id"])
        for item in payload["data"]
        if isinstance(item, dict)
        and item.get("id")
        and item.get("type", "chat") == "chat"
    ]


async def list_models(
    settings: Settings, *, api_key: str | None = None, timeout: float = 10.0
) -> list[str]:
    base = settings.resolved_base_url()
    if not base:
        return []
    headers = {}
    key = api_key or settings.api_key
    if key:
        headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{base}/models", headers=headers)
        response.raise_for_status()
        return _ids(response.json())


class LLMClient:
    def __init__(self, settings: Settings, *, api_key: str | None = None):
        self.settings = settings
        self.model = settings.resolved_model()
        self.base_url = settings.resolved_base_url()
        self.api_key = api_key
        # OpenCode Go routes and prompt-caches per session; one client instance
        # is one conversation.
        self.session_id = uuid4().hex

    @property
    def label(self) -> str:
        if self.settings.provider == "mock":
            return "mock (retrieval only)"
        return f"{self.settings.provider} · {self.model}"

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        if self.settings.provider == "mock":
            return
        if not self.api_key:
            raise LLMError(
                "No OpenCode Go API key. Set OPENCODE_GO_API_KEY or run "
                "`cba-bot config --set-key`."
            )
        try:
            from openai import (
                APIConnectionError,
                APIStatusError,
                AsyncOpenAI,
                AuthenticationError,
                RateLimitError,
            )
        except ImportError as exc:  # pragma: no cover
            raise LLMError("The `openai` package is required.") from exc

        client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.settings.request_timeout,
            max_retries=2,
            default_headers={
                "User-Agent": f"cba-bot/{__version__}",
                "x-opencode-session": self.session_id,
            },
        )
        try:
            stream = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.settings.temperature,
                max_tokens=self.settings.max_tokens,
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                content = getattr(chunk.choices[0].delta, "content", None)
                if content:
                    yield content
        except AuthenticationError as exc:
            raise LLMError(
                "OpenCode Go rejected the key (401). Run `cba-bot config --set-key`."
            ) from exc
        except RateLimitError as exc:
            raise LLMError("Rate limited by OpenCode Go; retry shortly.") from exc
        except APIConnectionError as exc:
            raise LLMError(
                f"Could not reach {self.base_url}; check the network."
            ) from exc
        except APIStatusError as exc:
            raise LLMError(
                f"OpenCode Go returned HTTP {exc.status_code}: {exc.message}"
            ) from exc
        finally:
            await client.close()

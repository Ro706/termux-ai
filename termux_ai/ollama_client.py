"""Async Ollama REST client (httpx)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator

import httpx


@dataclass
class ModelInfo:
    name: str
    size_bytes: int
    parameter_size: str = ""
    quantization: str = ""

    @property
    def size_human(self) -> str:
        n = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
            n /= 1024
        return "?"


class OllamaError(Exception):
    pass


class OllamaClient:
    def __init__(self, host: str, timeout: float = 300.0):
        self.host = host.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.host, timeout=httpx.Timeout(timeout, connect=3.0))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def is_up(self) -> bool:
        try:
            r = await self._client.get("/api/version", timeout=2.0)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def version(self) -> str:
        try:
            r = await self._client.get("/api/version", timeout=2.0)
            return r.json().get("version", "?")
        except (httpx.HTTPError, ValueError):
            return "?"

    async def list_models(self) -> list[ModelInfo]:
        try:
            r = await self._client.get("/api/tags", timeout=5.0)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise OllamaError(f"cannot reach Ollama at {self.host}: {e}") from e
        out = []
        for m in r.json().get("models", []):
            d = m.get("details", {}) or {}
            out.append(ModelInfo(m.get("name", "?"), int(m.get("size", 0)), d.get("parameter_size", ""), d.get("quantization_level", "")))
        return out

    async def running_models(self) -> list[str]:
        try:
            r = await self._client.get("/api/ps", timeout=3.0)
            return [m.get("name", "?") for m in r.json().get("models", [])]
        except (httpx.HTTPError, ValueError):
            return []

    async def chat_stream(
        self, model: str, messages: list[dict], temperature: float = 0.4, num_ctx: int = 4096
    ) -> AsyncIterator[str]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode(errors="replace")[:300]
                    raise OllamaError(f"Ollama returned {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "error" in chunk:
                        raise OllamaError(chunk["error"])
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except httpx.HTTPError as e:
            raise OllamaError(f"connection problem: {e}") from e

    async def chat(self, model: str, messages: list[dict], **kw) -> str:
        return "".join([p async for p in self.chat_stream(model, messages, **kw)])

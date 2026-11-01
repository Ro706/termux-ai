import httpx

class OllamaClient:
    def __init__(self, base_url: str, timeout: int = 180):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def status(self):
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
            return r.json().get("models", [])

    async def chat(self, model: str, messages: list, on_token=None):
        payload = {"model": model, "messages": messages, "stream": True}
        output = []
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    import json
                    item = json.loads(line)
                    token = item.get("message", {}).get("content", "")
                    if token:
                        output.append(token)
                        if on_token:
                            on_token(token)
                    if item.get("done"):
                        break
        return "".join(output)

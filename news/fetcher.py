import asyncio
import feedparser
import httpx

async def fetch_news(feeds, limit=12):
    results = []
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        for url in feeds:
            try:
                response = await client.get(url)
                response.raise_for_status()
                parsed = feedparser.parse(response.text)
                for entry in parsed.entries[:5]:
                    results.append({
                        "title": entry.get("title", "Untitled"),
                        "link": entry.get("link", ""),
                        "source": parsed.feed.get("title", url),
                        "date": entry.get("published", "")
                    })
            except Exception:
                continue
    return results[:limit]

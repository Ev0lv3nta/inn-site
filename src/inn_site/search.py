import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx
from ddgs import DDGS

from inn_site.models import SearchHit


class ProviderError(RuntimeError):
    pass


class Cache:
    def __init__(self, directory: Path | None, ttl: int = 86400):
        self.directory, self.ttl = directory, ttl

    def get(self, key: str):
        if not self.directory:
            return None
        p = self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")
        try:
            obj = json.loads(p.read_text())
            if time.time() - obj["created"] < self.ttl:
                return obj["value"]
        except (OSError, ValueError, KeyError):
            pass
        return None

    def put(self, key: str, value):
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
            p = self.directory / (hashlib.sha256(key.encode()).hexdigest() + ".json")
            p.write_text(json.dumps({"created": time.time(), "value": value}, ensure_ascii=False))


def rpc_result(body: str) -> dict:
    """Exa отвечает JSON либо одним событием SSE с результатом JSON-RPC."""
    try:
        messages = [json.loads(body)]
    except json.JSONDecodeError:
        messages = []
        for event in body.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(
                line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")
            )
            if data and data != "[DONE]":
                try:
                    messages.append(json.loads(data))
                except json.JSONDecodeError:
                    continue
    for obj in messages:
        if "error" in obj:
            raise ProviderError("Ошибка JSON-RPC: " + str(obj["error"])[:300])
        if "result" in obj:
            if obj["result"].get("isError"):
                raise ProviderError("Поисковый инструмент вернул ошибку")
            return obj["result"]
    raise ProviderError("Не удалось прочитать ответ поискового инструмента")


def parse_hits(text: str) -> list[SearchHit]:
    hits, title, url, content = [], "", "", []
    for line in text.splitlines():
        if line.startswith("Title:"):
            if url:
                hits.append(SearchHit(url, title, "\n".join(content).strip()))
            title, url, content = line[6:].strip(), "", []
        elif line.startswith("URL:"):
            if url:
                hits.append(SearchHit(url, title, "\n".join(content).strip()))
            value = line[4:].strip().split()
            url = value[0] if value and value[0].startswith(("http://", "https://")) else ""
            content = []
        elif line.startswith(("Highlights:", "Text:", "Content:", "Markdown Content:")):
            content = [line.split(":", 1)[1].strip()]
        elif url:
            content.append(line)
        elif line.startswith("#"):
            title = line.lstrip("# ").strip()
    if url:
        hits.append(SearchHit(url, title, "\n".join(content).strip()))
    return hits


def target_error(text: str) -> bool:
    marker = "Warning: Target URL returned error "
    for line in text[:1000].splitlines():
        if line.startswith(marker):
            status = line[len(marker) :][:3]
            if status.isdigit() and 400 <= int(status) < 600:
                return True
    return False


class Search:
    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: Cache,
        provider: str = "searx",
        crw_url: str | None = None,
        searx_url: str = "http://127.0.0.1:8888",
    ):
        self.client, self.cache, self.provider = client, cache, provider
        self.crw_url = crw_url.rstrip("/") if crw_url else None
        self.searx_url = searx_url.rstrip("/")
        self.operations: list[dict] = []

    async def call_exa(self, tool: str, arguments: dict) -> str:
        key = "exa:" + tool + json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        cached = self.cache.get(key)
        self.operations.append(
            {"tool": tool, "arguments": arguments, "cache_hit": cached is not None}
        )
        if cached is not None:
            if target_error(cached):
                raise ProviderError("Удалённый кеш содержит ошибку целевой страницы")
            return cached
        try:
            r = await self.client.post(
                "https://mcp.exa.ai/mcp",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
                timeout=45,
            )
            r.raise_for_status()
            result = rpc_result(r.text)
            text = "\n".join(
                c["text"] for c in result.get("content", []) if c.get("type") == "text"
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"Exa недоступен: {type(e).__name__}") from e
        if not text:
            raise ProviderError("Пустой ответ Exa")
        # Ошибки и лимиты провайдера не превращаем в пустую поисковую выдачу.
        warning = text[:600].casefold().replace("_", " ").replace("-", " ")
        if any(
            s in warning
            for s in ["rate limit", "quota exceeded", "usage limit", "api key required"]
        ):
            raise ProviderError("Исчерпан бесплатный лимит Exa")
        self.cache.put(key, text)
        return text

    async def search(self, query: str, inn: str, limit: int = 12) -> list[SearchHit]:
        if self.provider == "searx":
            key = f"searx:{self.searx_url}:{limit}:{query}"
            data = self.cache.get(key)
            op = {"tool": "searx_search", "query": query, "cache_hit": data is not None}
            self.operations.append(op)
            if data is None:
                try:
                    response = await self.client.get(
                        self.searx_url + "/search",
                        params={
                            "q": query,
                            "format": "json",
                            "language": "ru",
                            "engines": "google cse,duckduckgo,bing,brave",
                        },
                        timeout=35,
                    )
                    response.raise_for_status()
                    obj = response.json()
                    data = obj.get("results", [])[:limit]
                    op["unresponsive_engines"] = obj.get("unresponsive_engines", [])
                    failed = {item[0] for item in op["unresponsive_engines"]}
                    if not data and {"google cse", "duckduckgo", "bing", "brave"}.issubset(failed):
                        if self.crw_url:
                            return await self.crw_search(query, inn, limit, "engines_unavailable")
                        raise ProviderError("Поисковые движки SearXNG недоступны")
                    self.cache.put(key, data)
                except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as e:
                    if self.crw_url:
                        return await self.crw_search(query, inn, limit, type(e).__name__)
                    raise ProviderError(f"SearXNG недоступен: {type(e).__name__}") from e
            if not data and self.crw_url:
                return await self.crw_search(query, inn, limit, "empty_result")
            try:
                if any(not isinstance(h.get("url"), str) for h in data):
                    raise ValueError("Нет URL результата")
                return [
                    SearchHit(h["url"], str(h.get("title") or ""), str(h.get("content") or ""))
                    for h in data
                ]
            except (ValueError, TypeError, AttributeError) as e:
                raise ProviderError("Некорректная выдача SearXNG") from e
        if self.provider == "ddgs":
            key = f"ddgs:{limit}:{query}"
            data = self.cache.get(key)
            self.operations.append(
                {"tool": "ddgs_search", "query": query, "cache_hit": data is not None}
            )
            if data is None:
                try:
                    data = await asyncio.wait_for(
                        asyncio.to_thread(
                            lambda: DDGS(timeout=12).text(
                                query,
                                region="ru-ru",
                                max_results=limit,
                                backend="google,brave,duckduckgo",
                            )
                        ),
                        35,
                    )
                    self.cache.put(key, data)
                except Exception as e:
                    raise ProviderError(f"Метапоиск недоступен: {type(e).__name__}") from e
            return [SearchHit(h["href"], h.get("title", ""), h.get("body", "")) for h in data]
        if self.provider == "crw":
            if not self.crw_url:
                raise ProviderError("Для CRW укажите CRW_URL")
            key = "crw:" + self.crw_url + query
            data = self.cache.get(key)
            self.operations.append(
                {"tool": "crw_search", "query": query, "cache_hit": data is not None}
            )
            if data is None:
                try:
                    r = await self.client.post(
                        self.crw_url + "/v1/search",
                        json={"query": query, "lang": "ru", "limit": limit},
                        timeout=45,
                    )
                    r.raise_for_status()
                    body = r.json()
                    if not body.get("success"):
                        raise ProviderError("CRW вернул ошибку поиска")
                    data = body.get("data", {}).get("results", [])
                    self.cache.put(key, data)
                except httpx.HTTPError as e:
                    raise ProviderError("CRW недоступен") from e
            return [SearchHit(h["url"], h.get("title", ""), h.get("snippet", "")) for h in data]
        text = await self.call_exa(
            "web_search_exa",
            {
                "query": query,
                "objective": (
                    f"Найти основной собственный сайт организации с ИНН {inn}. "
                    "Приоритет: реквизиты, контакты, оферта, политика конфиденциальности "
                    "самой организации. Карточки справочников помогают установить название, "
                    "но не являются её сайтом. Нужны точный ИНН и контекст принадлежности."
                ),
                "numResults": limit,
            },
        )
        hits = parse_hits(text)
        if not hits and "URL:" in text:
            raise ProviderError("Формат выдачи Exa изменился")
        return hits

    async def crw_search(self, query: str, inn: str, limit: int, reason: str) -> list[SearchHit]:
        fallback = Search(self.client, self.cache, "crw", self.crw_url)
        try:
            return await fallback.search(query, inn, limit)
        finally:
            for operation in fallback.operations:
                operation["fallback_reason"] = reason
            self.operations.extend(fallback.operations)

    async def fetch_remote(self, url: str) -> str | dict:
        if self.crw_url:
            self.operations.append({"tool": "crw_scrape", "url": url, "cache_hit": False})
            try:
                r = await self.client.post(
                    self.crw_url + "/v1/scrape",
                    json={
                        "url": url,
                        "formats": ["markdown", "rawHtml"],
                        "onlyMainContent": False,
                        "deadlineMs": 45000,
                    },
                    timeout=50,
                )
                r.raise_for_status()
                obj = r.json()
                if not obj.get("success"):
                    raise ProviderError("CRW не смог прочитать страницу")
                data = obj.get("data", {})
                metadata = data.get("metadata", {})
                if int(metadata.get("statusCode", 200)) >= 400:
                    raise ProviderError("CRW получил ошибку целевой страницы")
                text = data.get("markdown", "")
                if not text:
                    raise ProviderError("CRW вернул пустой текст")
                return {
                    "url": metadata.get("sourceURL") or url,
                    "text": text,
                    "html": data.get("rawHtml") or "",
                    "metadata": metadata,
                }
            except httpx.HTTPError as e:
                raise ProviderError("CRW не смог прочитать страницу") from e
        if self.provider == "exa":
            return await self.call_exa("web_fetch_exa", {"urls": [url], "maxCharacters": 14000})
        # Базовый Reader работает без ключа. Поиска через Jina здесь нет.
        key = "jina:" + url
        cached = self.cache.get(key)
        self.operations.append({"tool": "jina_reader", "url": url, "cache_hit": cached is not None})
        if cached is not None:
            if target_error(cached):
                raise ProviderError("Удалённый кеш содержит ошибку целевой страницы")
            return cached
        try:
            response = await self.client.get("https://r.jina.ai/" + url, timeout=40)
            response.raise_for_status()
            if target_error(response.text):
                raise ProviderError("Jina Reader получил ошибку целевой страницы")
            raw = response.text.replace("URL Source:", "URL:")
            self.cache.put(key, raw)
            return raw
        except httpx.HTTPError as e:
            raise ProviderError(f"Jina Reader недоступен: {type(e).__name__}") from e

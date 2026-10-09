import asyncio
import io
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from inn_site.rules import (
    excluded,
    host_of,
    normalize_text,
    public_url,
    site_domain,
)
from inn_site.search import Cache, ProviderError, Search, parse_hits

LINK_WORDS = re.compile(
    r"контакт|реквизит|оферт|политик|о компани|о нас|правов|сведения|"
    r"соглашен|условия|contact|requisit|legal|privacy|offer|about|terms",
    re.I,
)


@dataclass
class Page:
    url: str
    text: str
    kind: str
    links: list[str]
    hints: list[str] = field(default_factory=list)
    cache_hit: bool = False


class UnsafeURL(ValueError):
    pass


def legal_priority(label: str) -> int:
    if re.search(r"реквизит|оферт|requisit|legal|offer|terms", label, re.I):
        return 0
    return 1 if re.search(r"контакт|contact|политик|privacy", label, re.I) else 2


def website_hints(body: str, url: str) -> list[str]:
    """Ссылка «сайт компании» в карточке — подсказка для проверки, не доказательство."""
    soup = BeautifulSoup(body, "html.parser")
    hints = []
    for a in soup.find_all("a", href=True):
        target = urljoin(url, a["href"]).split("#")[0]
        label = a.get_text(" ", strip=True)
        nearby = a.parent.get_text(" ", strip=True)[:500]
        try:
            if (
                site_domain(target) != site_domain(url)
                and not excluded(target)
                and (re.search(r"сайт|website", nearby, re.I) or host_of(target) in label.lower())
                and target not in hints
            ):
                hints.append(target)
        except ValueError:
            continue
    return hints[:3]


def html_content(body: str, url: str) -> tuple[str, list[str]]:
    soup = BeautifulSoup(body, "html.parser")
    links = []
    priorities = {}
    for a in soup.find_all("a", href=True):
        target = urljoin(url, a["href"]).split("#")[0]
        if LINK_WORDS.search(a.get_text(" ", strip=True) + " " + a["href"]):
            try:
                if site_domain(target) == site_domain(url) and target not in links:
                    links.append(target)
                    label = a.get_text(" ", strip=True) + " " + a["href"]
                    priorities[target] = legal_priority(label)
            except ValueError:
                pass
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    return normalize_text(soup.get_text(" ", strip=True)), sorted(
        links, key=lambda u: priorities[u]
    )[:12]


def excerpts(text: str, inn: str, limit: int = 8500) -> str:
    """Начало страницы и контекст ИНН. Большую навигацию не отправляем в LLM."""
    text = normalize_text(text)
    if len(text) <= limit:
        return text
    edge = min(700, limit // 5)
    chunks = [text[:edge]]
    matches = list(re.finditer(rf"(?<!\d){inn}(?!\d)", text))
    selected = [matches[0], matches[-1]] if len(matches) > 1 else matches
    room = max(0, limit - 2 * edge - 130)
    window = room // max(1, len(selected))
    for match in selected:
        start = max(0, min(match.start() - window // 2, len(text) - window))
        chunks.append(text[start : start + window])
    chunks.append(text[-edge:])
    return "\n[... фрагмент пропущен ...]\n".join(dict.fromkeys(chunks))[:limit]


def inn_quote(text: str, inn: str) -> str:
    for fragment in text.split("[... фрагмент пропущен ...]"):
        fragment = normalize_text(fragment)
        match = re.search(rf"(?<!\d){inn}(?!\d)", fragment)
        if match:
            return fragment[max(0, match.start() - 160) : match.end() + 200].strip()
    return ""


class Fetcher:
    def __init__(
        self, client: httpx.AsyncClient, search: Search, cache: Cache, remote_fallback: bool = True
    ):
        self.client, self.search, self.cache = client, search, cache
        self.remote_fallback = remote_fallback
        self.robots: dict[str, RobotFileParser | None] = {}
        self.operations: list[dict] = []
        self.semaphore = asyncio.Semaphore(3)

    async def _safe_url(self, url: str):
        try:
            return await asyncio.wait_for(asyncio.to_thread(public_url, url), timeout=6)
        except ValueError as e:
            raise UnsafeURL(str(e)) from e

    async def _read(self, url: str, max_bytes: int = 3_000_000) -> tuple[str, bytes, str]:
        for _ in range(5):
            await self._safe_url(url)
            async with self.client.stream("GET", url, timeout=16, follow_redirects=False) as r:
                if r.is_redirect:
                    url = urljoin(url, r.headers["location"])
                    continue
                r.raise_for_status()
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("Страница превышает лимит размера")
                return str(r.url), bytes(data), r.headers.get("content-type", "")
        raise ValueError("Слишком много перенаправлений")

    async def _allowed(self, url: str) -> bool:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            rp = RobotFileParser()
            try:
                _, body, _ = await self._read(origin + "/robots.txt", 256_000)
                rp.parse(body.decode("utf-8", errors="replace").splitlines())
                self.robots[origin] = rp
            except httpx.HTTPStatusError as e:
                if e.response.status_code in {401, 403}:
                    rp.parse(["User-agent: *", "Disallow: /"])
                    self.robots[origin] = rp
                else:
                    self.robots[origin] = None
            except (httpx.HTTPError, ValueError, OSError, TimeoutError):
                self.robots[origin] = None
        parser = self.robots[origin]
        return parser is None or parser.can_fetch("InnSiteBot", url)

    async def fetch(self, url: str, inn: str) -> Page | None:
        async with self.semaphore:
            op = {"url": url, "cache_hit": False}
            self.operations.append(op)
            try:
                await self._safe_url(url)
                cached = self.cache.get("page:" + url)
                if cached:
                    op["cache_hit"] = True
                    return Page(**cached, cache_hit=True)
                if not await self._allowed(url):
                    op["error"] = "robots_denied"
                    return None
                final_url, body, content_type = await self._read(url)
                if host_of(final_url) != host_of(url) and not await self._allowed(final_url):
                    op["error"] = "redirect_robots_denied"
                    return None
                if "pdf" in content_type or body.startswith(b"%PDF-"):
                    reader = PdfReader(io.BytesIO(body))
                    text = "\n".join((p.extract_text() or "") for p in list(reader.pages)[:12])
                    kind, links, hints = "pdf", [], []
                elif "html" in content_type or body.lstrip().startswith(b"<"):
                    # HTTPX учитывает charset заголовка; meta charset читается BeautifulSoup.
                    encoding = BeautifulSoup(body, "html.parser").original_encoding or "utf-8"
                    html = body.decode(encoding, errors="replace")
                    text, links = html_content(html, final_url)
                    hints = website_hints(html, final_url)
                    kind = "html"
                else:
                    op["error"] = "unsupported_content"
                    return None
                if len(text) < 60:
                    raise ValueError("Пустой текст страницы")
                obj = {"url": final_url, "text": text, "kind": kind, "links": links, "hints": hints}
                self.cache.put("page:" + url, obj)
                op["kind"] = kind
                return Page(**obj)
            except UnsafeURL:
                op["error"] = "unsafe_url"
                return None
            except (httpx.HTTPError, ValueError, OSError, TimeoutError, PdfReadError) as e:
                op["error"] = type(e).__name__
            # Не передаём локальные адреса удалённому сервису даже при сбое DNS.
            try:
                await self._safe_url(url)
                if self.remote_fallback and await self._allowed(url):
                    raw = await self.search.fetch_remote(url)
                    hits = parse_hits(raw)
                    own = [h for h in hits if host_of(h.url) == host_of(url)]
                    text = own[0].text if own else ""
                    if len(text) > 60:
                        remote_url = own[0].url
                        await self._safe_url(remote_url)
                        if not await self._allowed(remote_url):
                            return None
                        links = []
                        for label, target in re.findall(
                            r"\[([^\]]+)\]\((https?://[^\s)]+)\)", text
                        ):
                            try:
                                if site_domain(target) == site_domain(url) and LINK_WORDS.search(
                                    label + target
                                ):
                                    links.append((legal_priority(label + target), target))
                            except ValueError:
                                continue
                        op["kind"] = "remote"
                        return Page(
                            remote_url,
                            normalize_text(text),
                            "remote",
                            list(dict.fromkeys(u for _, u in sorted(links)))[:12],
                        )
            except (ProviderError, httpx.HTTPError, ValueError, OSError, TimeoutError) as e:
                op["fallback_error"] = type(e).__name__
            return None

import asyncio
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

import httpx

from inn_site.fetch import LINK_WORDS, Fetcher, excerpts
from inn_site.llm import DEFAULT_MODEL, LLMError, Ollama, OpenRouter
from inn_site.models import Candidate, Evidence, Resolution, SearchHit
from inn_site.rules import (
    choose_domain,
    contact_domains,
    excluded,
    has_inn,
    host_of,
    site_domain,
    valid_inn,
    verified_name,
)
from inn_site.search import Cache, ProviderError, Search


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    llm_provider: str = "openrouter"
    ollama_url: str = "http://127.0.0.1:11434"
    search_provider: str = "searx"
    searx_url: str = "http://127.0.0.1:8888"
    crw_url: str | None = None
    cache_dir: Path | None = Path(".cache/inn-site")
    max_searches: int = 3
    max_pages: int = 12
    max_candidates: int = 6
    deadline_seconds: int = 240
    remote_fallback: bool = True
    budget_file: Path = Path(".cache/llm-spend.json")
    budget_usd: float = 0.25


class Finder:
    def __init__(self, search, fetcher, llm, settings: Settings):
        self.search, self.fetcher, self.llm, self.settings = search, fetcher, llm, settings
        self.candidates: list[Candidate] = []
        self.evidence: list[Evidence] = []
        self.read_urls: set[str] = set()
        self.rounds: list[dict] = []
        self.search_failures: list[str] = []
        self.discovered: list[SearchHit] = []
        self.metadata_reads = 0
        self.retired_ids: set[str] = set()

    def active_candidates(self):
        return [c for c in self.candidates if c.id not in self.retired_ids]

    def add_evidence(self, url: str, kind: str, text: str, candidate_id=None):
        e = Evidence(
            f"e{len(self.evidence) + 1}",
            url,
            kind,
            text,
            candidate_id,
            datetime.now(UTC).isoformat(),
        )
        self.evidence.append(e)

    def was_read(self, url: str) -> bool:
        def key(u):
            p = urlsplit(u)
            return host_of(u), p.path.rstrip("/"), p.query

        return any(key(url) == key(u) for u in self.read_urls)

    async def collect(self, hits, inn: str, turn: int):
        # Оставляем URL на уточняющие запросы, чтобы ранние ложные кандидаты
        # не исчерпали весь бюджет до появления страницы с реквизитами.
        reserve = 3 * (self.settings.max_searches - turn - 1)
        read_limit = max(len(self.read_urls), self.settings.max_pages - reserve)
        # Карточка нужна ради ссылки на сайт; её домен не становится ответом.
        for h in hits:
            try:
                is_card = excluded(h.url)
            except ValueError:
                continue
            if is_card and has_inn(h.title + h.text, inn) and self.metadata_reads < 2:
                if not self.was_read(h.url) and len(self.read_urls) < read_limit:
                    self.metadata_reads += 1
                    self.read_urls.add(h.url)
                    page = await self.fetcher.fetch(h.url, inn)
                    if page and has_inn(page.text, inn):
                        self.add_evidence(page.url, page.kind, excerpts(page.text, inn, 3500))
                        self.discovered.extend(
                            SearchHit(u, "Сайт из карточки организации", "") for u in page.hints
                        )
                        self.discovered.extend(
                            SearchHit("https://" + d + "/", "Контактный домен из карточки", "")
                            for d in contact_domains(page.text)
                        )
                    break
        hits = sorted(
            self.discovered + hits,
            key=lambda h: (not has_inn(h.title + h.text, inn), not LINK_WORDS.search(h.url)),
        )
        self.discovered = []
        pending = []
        per_domain = {}
        for h in hits:
            h.url = urldefrag(h.url)[0]
            self.add_evidence(h.url, "search", (h.title + "\n" + h.text)[:2000])
            try:
                if excluded(h.url):
                    continue
                domain = site_domain(h.url)
            except ValueError:
                continue
            c = next((x for x in self.candidates if x.domain == domain), None)
            if c is None:
                if len(self.active_candidates()) >= self.settings.max_candidates:
                    continue
                c = Candidate(f"c{len(self.candidates) + 1}", domain)
                self.candidates.append(c)
            elif c.id in self.retired_ids:
                if (
                    not has_inn(h.title + h.text, inn)
                    or len(self.active_candidates()) >= self.settings.max_candidates
                ):
                    continue
                self.retired_ids.remove(c.id)
            if h.url not in c.urls:
                c.urls.append(h.url)
            if (
                not self.was_read(h.url)
                and len(self.read_urls) < read_limit
                and per_domain.get(domain, 0) < 2
            ):
                self.read_urls.add(h.url)
                pending.append((c, h.url))
                per_domain[domain] = per_domain.get(domain, 0) + 1

        async def read(c, url):
            page = await self.fetcher.fetch(url, inn)
            return c, page

        def remember(c, page):
            if page:
                domain = site_domain(page.url)
                if excluded(page.url):
                    return
                if domain != c.domain:
                    c = next((x for x in self.candidates if x.domain == domain), None)
                    if c is None:
                        if len(self.active_candidates()) >= self.settings.max_candidates:
                            return
                        c = Candidate(f"c{len(self.candidates) + 1}", domain, [page.url])
                        self.candidates.append(c)
                self.add_evidence(page.url, page.kind, excerpts(page.text, inn, 5500), c.id)

        pages = await asyncio.gather(*(read(c, u) for c, u in pending))
        for depth in range(3):
            extra = []
            for c, p in pages:
                remember(c, p)
                if depth == 2 or not p or has_inn(p.text, inn):
                    continue
                for link in p.links[:2]:
                    if not self.was_read(link) and len(self.read_urls) < read_limit:
                        self.read_urls.add(link)
                        extra.append((c, link))
            pages = await asyncio.gather(*(read(c, u) for c, u in extra))

    def context(self, inn: str) -> list[Evidence]:
        # Сначала страницы с ИНН, затем сведения о компании и остальные страницы.
        ordered = sorted(
            [e for e in self.evidence if e.candidate_id not in self.retired_ids],
            key=lambda e: (e.kind == "search", not has_inn(e.text, inn), -int(e.id[1:])),
        )
        chosen, remaining = [], 16000
        for e in ordered:
            cap = 3000 if e.kind != "search" else 800
            if remaining < 500:
                break
            text = excerpts(e.text, inn, min(cap, remaining))
            chosen.append(Evidence(e.id, e.url, e.kind, text, e.candidate_id, e.fetched_at))
            remaining -= len(text)
        return chosen

    async def run(self, inn: str) -> tuple[str | None, str]:
        hints = []
        catalog_url = f"https://fbc.ru/inn/{inn}/"
        self.read_urls.add(catalog_url)
        catalog = await self.fetcher.fetch(catalog_url, inn)
        if catalog and has_inn(catalog.text, inn):
            self.add_evidence(catalog.url, "html", excerpts(catalog.text, inn, 3000))
            hints = [
                SearchHit("https://" + d + "/", "Контактный домен из каталога", "")
                for d in contact_domains(catalog.text)
            ]
            hints.extend(SearchHit(u, "Сайт из карточки", "") for u in catalog.hints)
        query = f"{inn} официальный сайт реквизиты"
        used_queries = set()
        status = "insufficient_evidence"
        for turn in range(self.settings.max_searches):
            if query.casefold() in used_queries:
                break
            used_queries.add(query.casefold())
            try:
                hits = await self.search.search(query, inn)
            except ProviderError as e:
                self.search_failures.append(str(e))
                if not hints and not self.candidates:
                    raise
                hits = []
            await self.collect(hints + hits, inn, turn)
            hints = []
            if not self.evidence:
                query = f'"{inn}" контакты организация'
                continue
            context = self.context(inn)
            active = self.active_candidates()
            review = await self.llm.review(inn, active, context)
            domain, status = choose_domain(inn, review, active, context)
            self.rounds.append(
                {
                    "query": query,
                    "review": review.model_dump(),
                    "status": status,
                    "domain": domain,
                    "context_evidence_ids": [e.id for e in context],
                }
            )
            if domain:
                return domain, status
            if status == "ambiguous":
                break
            self.retired_ids.update(
                v.candidate_id for v in review.verdicts if v.relation == "third_party"
            )
            name = verified_name(review.company_name, self.evidence, inn)
            city = verified_name(review.company_city, self.evidence, inn)
            if turn == 1 or (turn == 0 and self.candidates):
                likely = review.investigate_candidate_id or next(
                    (v.candidate_id for v in review.verdicts if v.relation == "owner"), None
                )
                candidate = next((c for c in self.candidates if c.id == likely), None)
                if candidate:
                    # Уже найденные страницы тоже остаются в очереди: первые две
                    # могут быть недоступны, а реквизиты находиться в следующей.
                    for url in candidate.urls:
                        if not self.was_read(url):
                            snippet = next(
                                (
                                    e.text
                                    for e in self.evidence
                                    if e.kind == "search" and e.url == url
                                ),
                                "",
                            )
                            hints.append(
                                SearchHit(url, "Найденная ранее страница кандидата", snippet)
                            )
                    query = f'site:{candidate.domain} "{inn}"'
                    if query.casefold() not in used_queries:
                        continue
                    query = f'site:{candidate.domain} "{inn}" filetype:pdf'
                    if query.casefold() not in used_queries:
                        continue
            if review.action == "finish" and not (turn == 0 and name):
                break
            if name:
                query = f'"{name}" {city + " " if city else ""}официальный сайт'
                if query.casefold() in used_queries:
                    query = f'"{name}" реквизиты контакты {inn}'
            else:
                query = f'"{inn}" контакты реквизиты -rusprofile.ru -list-org.com'
        return None, "search_error" if self.search_failures else status

    def trace(self) -> dict:
        return {
            "candidates": [asdict(c) for c in self.candidates],
            "evidence": [asdict(e) for e in self.evidence],
            "rounds": self.rounds,
            "search_operations": self.search.operations,
            "page_operations": self.fetcher.operations,
            "llm_operations": self.llm.operations,
            "search_failures": self.search_failures,
            "retired_candidate_ids": sorted(self.retired_ids),
        }


async def find_site(inn: str, settings: Settings | None = None) -> Resolution:
    settings = settings or Settings()
    started = time.monotonic()
    if not valid_inn(inn):
        return Resolution(None, "invalid_inn", {"status": "invalid_inn"})
    async with httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; InnSiteBot/0.1)",
            "Accept-Language": "ru,en;q=0.8",
        },
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
    ) as client:
        cache = Cache(settings.cache_dir)
        search = Search(
            client, cache, settings.search_provider, settings.crw_url, settings.searx_url
        )
        fetcher = Fetcher(
            client,
            search,
            cache,
            settings.remote_fallback,
        )
        llm = (
            OpenRouter(client, settings.model, settings.budget_file, settings.budget_usd)
            if settings.llm_provider == "openrouter"
            else Ollama(client, settings.ollama_url, settings.model)
        )
        finder = Finder(search, fetcher, llm, settings)
        error = None

        async def resolve():
            if isinstance(llm, OpenRouter):
                await llm.prepare()
            return await finder.run(inn)

        try:
            domain, status = await asyncio.wait_for(resolve(), settings.deadline_seconds)
            if status == "search_error":
                error = "; ".join(finder.search_failures)
        except TimeoutError:
            domain, status, error = None, "timeout", "Превышен общий лимит времени"
        except ProviderError as e:
            domain, status, error = None, "search_error", str(e)
        except LLMError as e:
            domain, status, error = None, "llm_error", str(e)
        trace = finder.trace()
        trace.update(
            {
                "inn": inn,
                "domain": domain,
                "status": status,
                "error": error,
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "settings": {
                    k: str(v) if isinstance(v, Path) else v for k, v in asdict(settings).items()
                },
                "run_at": datetime.now(UTC).isoformat(),
            }
        )
        return Resolution(domain, status, trace)

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urldefrag, urlsplit

import httpx

from inn_site.fetch import Fetcher, excerpts
from inn_site.llm import DEFAULT_MODEL, LLMError, Ollama, OpenRouter
from inn_site.models import Candidate, Evidence, Resolution, SearchHit
from inn_site.registry import URL as REGISTRY_URL
from inn_site.registry import identify
from inn_site.rules import (
    choose_domain,
    excluded,
    has_inn,
    host_of,
    ownership_evidence,
    site_domain,
    valid_inn,
)
from inn_site.search import Cache, ProviderError, Search


@dataclass
class Settings:
    model: str = DEFAULT_MODEL
    llm_provider: str = "openrouter"
    ollama_url: str = "http://127.0.0.1:11434"
    search_provider: str = "crw"
    searx_url: str = "http://127.0.0.1:8888"
    crw_url: str | None = "http://127.0.0.1:3080"
    cache_dir: Path | None = Path(".cache/inn-site")
    max_searches: int = 4
    max_steps: int = 9
    max_pages: int = 12
    max_candidates: int = 6
    deadline_seconds: int = 300
    remote_fallback: bool = True
    identity_lookup: bool = True
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
        self.registry_operations: list[dict] = []
        self.confirmed_ownership: dict[str, str] = {}

    def active_candidates(self):
        return [c for c in self.candidates if c.id not in self.retired_ids]

    def add_evidence(self, url: str, kind: str, text: str, candidate_id=None, links=None):
        e = Evidence(
            f"e{len(self.evidence) + 1}",
            url,
            kind,
            text,
            candidate_id,
            datetime.now(UTC).isoformat(),
            links or [],
        )
        self.evidence.append(e)

    def was_read(self, url: str) -> bool:
        def key(u):
            p = urlsplit(u)
            return host_of(u), p.path.rstrip("/"), p.query

        return any(key(url) == key(u) for u in self.read_urls)

    async def collect(self, hits, inn: str, turn: int, *, selected: bool = False):
        read_limit = self.settings.max_pages
        hits = sorted(hits, key=lambda h: not has_inn(h.title + h.text, inn))
        pending, per_domain = [], {}
        for hit in hits:
            url = urldefrag(hit.url)[0]
            try:
                is_card, domain = excluded(url), site_domain(url)
            except ValueError:
                continue
            if not selected:
                self.add_evidence(url, "search", (hit.title + "\n" + hit.text)[:2000])
            candidate = next((c for c in self.candidates if c.domain == domain), None)
            if is_card:
                if not selected or self.metadata_reads >= 3:
                    continue
                candidate = None
            elif candidate is None:
                if len(self.active_candidates()) >= self.settings.max_candidates:
                    continue
                candidate = Candidate(f"c{len(self.candidates) + 1}", domain)
                self.candidates.append(candidate)
            elif candidate.id in self.retired_ids:
                if not selected and not has_inn(hit.title + hit.text, inn):
                    continue
                self.retired_ids.remove(candidate.id)
            if candidate and url not in candidate.urls:
                candidate.urls.append(url)
            if not selected:
                continue
            if self.was_read(url) or len(self.read_urls) >= read_limit:
                continue
            if not selected and per_domain.get(domain, 0) >= 2:
                continue
            self.read_urls.add(url)
            if is_card:
                self.metadata_reads += 1
            per_domain[domain] = per_domain.get(domain, 0) + 1
            pending.append((candidate, url))

        async def read(candidate, url):
            page = await self.fetcher.fetch(url, inn)
            if candidate:
                (candidate.read_urls if page else candidate.failed_urls).append(url)
            if not page:
                return
            options = list(dict.fromkeys(page.links + page.hints))
            links = [{"url": u, "label": page.link_labels.get(u, "")} for u in options]
            if excluded(page.url):
                self.add_evidence(page.url, page.kind, excerpts(page.text, inn, 3500), links=links)
                return
            domain = site_domain(page.url)
            if not candidate or candidate.domain != domain:
                candidate = next((c for c in self.candidates if c.domain == domain), None)
                if candidate is None:
                    if len(self.active_candidates()) >= self.settings.max_candidates:
                        return
                    candidate = Candidate(f"c{len(self.candidates) + 1}", domain, [page.url])
                    self.candidates.append(candidate)
            self.add_evidence(
                page.url, page.kind, excerpts(page.text, inn, 5500), candidate.id, links
            )
            for u in page.links:
                if u not in candidate.urls:
                    candidate.urls.append(u)

        await asyncio.gather(*(read(c, u) for c, u in pending))

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
            chosen.append(
                Evidence(e.id, e.url, e.kind, text, e.candidate_id, e.fetched_at, e.links)
            )
            remaining -= len(text)
        return chosen

    async def run(self, inn: str) -> tuple[str | None, str]:
        query, action, requested = f'"{inn}"', "search", []
        for source in self.evidence:
            if source.kind != "registry":
                continue
            for record in json.loads(source.text).get("records", []):
                name = record.get("c") or record.get("n")
                if record.get("i") == inn and name:
                    query = f"{name} {record.get('rn') or ''} официальный сайт"
                    break
        used_queries, searches = set(), 0
        status = "insufficient_evidence"
        repair_used = False
        primary_check = None
        current_owners = set()
        for step in range(self.settings.max_steps):
            if action == "search":
                if searches >= self.settings.max_searches or query.casefold() in used_queries:
                    break
                searches += 1
                used_queries.add(query.casefold())
                try:
                    hits = await self.search.search(query, inn)
                except ProviderError as error:
                    self.search_failures.append(str(error))
                    if not self.evidence and not self.candidates:
                        raise
                    hits = []
                await self.collect(hits, inn, searches - 1)
            elif action == "read":
                known = {u for c in self.candidates for u in c.urls}
                known.update(link["url"] for e in self.evidence for link in e.links)
                known.update(e.url for e in self.evidence if e.kind == "search")
                requested = [u for u in requested if u in known and not self.was_read(u)]
                if not requested:
                    break
                await self.collect(
                    [SearchHit(u, "Выбрано моделью для чтения", "") for u in requested],
                    inn,
                    searches - 1,
                    selected=True,
                )
            context, active = self.context(inn), self.active_candidates()
            review = await self.llm.review(
                inn,
                active,
                context,
                state={
                    "used_queries": sorted(used_queries),
                    "searches_remaining": self.settings.max_searches - searches,
                    "steps_remaining": self.settings.max_steps - step - 1,
                    "visited_urls": sorted(self.read_urls),
                    "urls_remaining": self.settings.max_pages - len(self.read_urls),
                    "search_errors": self.search_failures,
                    "page_errors": [o for o in self.fetcher.operations if o.get("error")][-8:],
                    "validation_error": (
                        "Проверка свидетельств отклонила ответ: источник с ИНН либо "
                        "сравнительная цитата не подтверждены. Цитата должна быть дословной, "
                        "без склеивания фрагментов и добавленных многоточий. Исправь её "
                        "по тексту источника или продолжи проверку."
                        if status == "invalid_model_evidence"
                        else None
                    ),
                    "primary_check": primary_check,
                    "confirmed_ownership": self.confirmed_ownership,
                },
            )
            domain, status = choose_domain(inn, review, active, context)
            current_owners = set()
            for verdict in review.verdicts:
                source = ownership_evidence(inn, verdict, active, context)
                if source:
                    self.confirmed_ownership[verdict.candidate_id] = source.id
                    current_owners.add(verdict.candidate_id)
            self.rounds.append(
                {
                    "operation": action,
                    "query": query if action == "search" else None,
                    "read_urls": requested if action == "read" else [],
                    "review": review.model_dump(),
                    "status": status,
                    "domain": domain,
                    "context_evidence_ids": [e.id for e in context],
                }
            )
            if (
                domain
                and review.action == "finish"
                and f'"{inn}"' not in used_queries
                and searches < self.settings.max_searches
            ):
                # До окончательного выбора проверяем другие сайты того же субъекта.
                action, query, status = "search", f'"{inn}"', "insufficient_evidence"
                continue
            if domain and review.action == "finish" and primary_check is None:
                primary_check = {
                    "proposed_domain": domain,
                    "comparison_sources": [s.model_dump() for s in review.comparison_sources],
                }
                action, status = "plan", "ambiguous"
                continue
            if domain and review.action == "finish":
                return domain, status
            if review.subject_kind == "private_person" and len(inn) == 12:
                identity = [
                    e
                    for e in context
                    if e.id in review.subject_evidence_ids
                    and has_inn(e.text, inn)
                    and e.kind != "registry"
                ]
                no_entity = any(
                    e.kind == "registry" and json.loads(e.text).get("records") == []
                    for e in context
                )
                if (
                    identity
                    and no_entity
                    and not any(v.relation == "owner" for v in review.verdicts)
                ):
                    return None, "outside_scope"
            self.retired_ids.update(
                v.candidate_id for v in review.verdicts if v.relation == "third_party"
            )
            self.retired_ids.update(c.id for c in active if c.id in review.dismiss_candidate_ids)
            # Подтверждённые реквизиты остаются в контексте для сравнения доменов.
            self.retired_ids.difference_update(self.confirmed_ownership)
            if review.action == "finish" and status == "invalid_model_evidence" and not repair_used:
                repair_used, action = True, "plan"
            elif review.action == "read":
                action, requested = "read", review.read_urls
            elif review.action == "search" and review.next_query:
                action, query = "search", review.next_query.strip()
            elif searches == 0:
                # Отсутствие записи ФНС не заменяет поиск сайта в интернете.
                action, query = "search", f'"{inn}"'
            else:
                break
        # Неоднозначность уже установлена прочитанными собственными сайтами.
        # Ошибка дополнительного поиска сохраняется, но не отменяет эти факты.
        if status == "ambiguous" and len(current_owners) > 1:
            return None, status
        return None, "search_error" if self.search_failures else status

    def trace(self) -> dict:
        return {
            "candidates": [asdict(c) for c in self.candidates],
            "evidence": [asdict(e) for e in self.evidence],
            "rounds": self.rounds,
            "registry_operations": self.registry_operations,
            "search_operations": self.search.operations,
            "page_operations": self.fetcher.operations,
            "llm_operations": self.llm.operations,
            "search_failures": self.search_failures,
            "retired_candidate_ids": sorted(self.retired_ids),
            "confirmed_ownership": self.confirmed_ownership,
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
            if settings.identity_lookup:
                identity, operation = await identify(client, cache, inn)
                finder.registry_operations.append(operation)
                if identity:
                    finder.add_evidence(
                        REGISTRY_URL, "registry", json.dumps(identity, ensure_ascii=False)
                    )
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

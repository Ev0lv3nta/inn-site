from inn_site.fetch import Page
from inn_site.models import ComparisonSource, Review, SearchHit, Verdict
from inn_site.pipeline import Finder, Settings, find_site
from inn_site.search import ProviderError

INN = "7721581040"
OWNER = "Наш сервис. Реквизиты ООО «Дейта Кью»: ИНН 7721581040. Мы оказываем услуги."


class SearchFixture:
    def __init__(self, own_on_second=True):
        self.operations = []
        self.own_on_second = own_on_second

    async def search(self, query, inn):
        self.operations.append({"query": query})
        if len(self.operations) == 1 or not self.own_on_second:
            return [SearchHit("https://fbc.ru/inn/7721581040", "ООО «Дейта Кью»", OWNER)]
        return [SearchHit("https://example.ru/legal", "Реквизиты", OWNER)]


class FetchFixture:
    operations = []

    async def fetch(self, url, inn):
        return Page(url, OWNER, "html", [])


class ModelFixture:
    operations = []

    async def review(self, inn, candidates, evidence, state=None):
        page = next(
            (
                e
                for e in evidence
                if e.kind in {"html", "pdf"} and e.candidate_id and OWNER in e.text
            ),
            None,
        )
        verdicts = []
        if page:
            verdicts.append(
                Verdict(
                    candidate_id=page.candidate_id,
                    relation="owner",
                    main_site=True,
                    evidence_id=page.id,
                    quote=OWNER,
                    reason="Реквизиты исполнителя",
                )
            )
        visited = set((state or {}).get("visited_urls", []))
        links = [link["url"] for e in evidence for link in e.links if link["url"] not in visited]
        unread = [
            u for c in candidates for u in c.urls if u not in c.read_urls and u not in c.failed_urls
        ]
        search_urls = [e.url for e in evidence if e.kind == "search" and e.url not in visited]
        urls = list(dict.fromkeys(links + unread + search_urls))[:1]
        return Review(
            company_name="Дейта Кью",
            verdicts=verdicts,
            action="finish" if page else "read" if urls else "search",
            read_urls=urls if not page else [],
            next_query=None if page or urls else f'"Дейта Кью" "{inn}" официальный сайт',
            reason="Нужна страница сайта",
        )


async def test_agent_refines_query_using_grounded_company_name():
    search = SearchFixture()
    finder = Finder(search, FetchFixture(), ModelFixture(), Settings())
    assert await finder.run(INN) == ("example.ru", "found")
    assert len(search.operations) == 2
    assert "Дейта Кью" in search.operations[1]["query"]


async def test_agent_obeys_search_budget_and_does_not_return_catalog():
    search = SearchFixture(own_on_second=False)
    finder = Finder(search, FetchFixture(), ModelFixture(), Settings(max_searches=2))
    domain, _ = await finder.run(INN)
    assert domain is None
    assert len(search.operations) == 2
    assert finder.read_urls == {"https://fbc.ru/inn/7721581040"}


async def test_provider_failure_has_distinct_status(monkeypatch):
    class BrokenSearch:
        operations = []

        def __init__(self, *args):
            pass

        async def search(self, query, inn):
            raise ProviderError("quota")

    monkeypatch.setattr("inn_site.pipeline.Search", BrokenSearch)

    class NoFetch:
        operations = []

        def __init__(self, *args):
            pass

        async def fetch(self, url, inn):
            return None

    monkeypatch.setattr("inn_site.pipeline.Fetcher", NoFetch)
    result = await find_site(
        INN, Settings(cache_dir=None, llm_provider="ollama", identity_lookup=False)
    )
    assert result.public() == {"domain": None}
    assert result.status == "search_error"
    assert result.trace["error"] == "quota"


async def test_invalid_input_does_not_use_network():
    result = await find_site("7721581041")
    assert result.status == "invalid_inn"


async def test_main_site_proposal_does_not_cancel_requested_investigation():
    class ContinueChecking(ModelFixture):
        async def review(self, inn, candidates, evidence, state=None):
            result = await super().review(inn, candidates, evidence, state)
            if result.verdicts and len(state["used_queries"]) == 2:
                result.action, result.next_query = "search", "Другие бренды организации"
            return result

    search = SearchFixture()
    finder = Finder(search, FetchFixture(), ContinueChecking(), Settings())
    assert await finder.run(INN) == ("example.ru", "found")
    assert len(search.operations) == 3


async def test_confirmed_source_is_not_lost_when_model_dismisses_old_candidate():
    class FollowUp(ModelFixture):
        async def review(self, inn, candidates, evidence, state=None):
            page = next((e for e in evidence if e.kind == "html" and e.candidate_id), None)
            if page and not state["confirmed_ownership"]:
                result = await super().review(inn, candidates, evidence, state)
                result.verdicts[0].main_site = False
                result.action, result.next_query = "search", "Другие домены компании"
                result.dismiss_candidate_ids = [page.candidate_id]
                return result
            if state["confirmed_ownership"]:
                assert any(e.id in state["confirmed_ownership"].values() for e in evidence)
                assert candidates
            return await super().review(inn, candidates, evidence, state)

    finder = Finder(SearchFixture(), FetchFixture(), FollowUp(), Settings())
    assert await finder.run(INN) == ("example.ru", "found")


async def test_claimed_hierarchy_gets_checked_before_returning_a_domain():
    class TwoSites:
        operations = []

        async def search(self, query, inn):
            return [
                SearchHit("https://one.ru/legal", "Бренд 1", ""),
                SearchHit("https://two.ru/legal", "Бренд 2", ""),
            ]

    class Compare(ModelFixture):
        async def review(self, inn, candidates, evidence, state=None):
            pages = [e for e in evidence if e.kind == "html"]
            if not pages:
                return Review(
                    company_name=None,
                    verdicts=[],
                    action="read",
                    reason="Проверка",
                    read_urls=[c.urls[0] for c in candidates],
                )
            checking = bool(state["primary_check"])
            verdicts = [
                Verdict(
                    candidate_id=e.candidate_id,
                    relation="owner",
                    main_site=i == 0 and not checking,
                    evidence_id=e.id,
                    quote=OWNER,
                    reason="Оператор",
                )
                for i, e in enumerate(pages)
            ]
            return Review(
                company_name=None,
                verdicts=verdicts,
                action="finish",
                reason="Бренды",
                primary_basis="uncertain" if checking else "hierarchy",
                comparison_sources=[ComparisonSource(evidence_id=e.id, quote=OWNER) for e in pages],
            )

    finder = Finder(TwoSites(), FetchFixture(), Compare(), Settings())
    assert await finder.run(INN) == (None, "ambiguous")
    assert finder.rounds[-1]["operation"] == "plan"


async def test_personal_inn_needs_both_registry_result_and_identity_evidence():
    personal_inn = "500100732259"

    class PersonalSearch:
        operations = []

        async def search(self, query, inn):
            return [
                SearchHit(
                    "https://audit-it.ru/person",
                    "Руководитель компании",
                    "Физическое лицо, ИНН " + inn,
                )
            ]

    class PersonalModel:
        operations = []

        async def review(self, inn, candidates, evidence, state=None):
            source = next((e for e in evidence if e.kind == "search"), None)
            return Review(
                company_name=None,
                verdicts=[],
                action="finish",
                reason="Физлицо",
                subject_kind="private_person",
                subject_evidence_ids=[source.id] if source else [],
            )

    finder = Finder(PersonalSearch(), FetchFixture(), PersonalModel(), Settings())
    finder.add_evidence(
        "https://egrul.nalog.ru/", "registry", '{"requested_inn":"500100732259","records":[]}'
    )
    assert await finder.run(personal_inn) == (None, "outside_scope")


async def test_catalog_hint_survives_a_later_search_failure():
    class FailingAfterFirst:
        def __init__(self):
            self.operations = []

        async def search(self, query, inn):
            self.operations.append({"query": query})
            if len(self.operations) > 1:
                raise ProviderError("search temporarily blocked")
            return [SearchHit("https://fbc.ru/inn/7721581040", "Дейта Кью", OWNER)]

    class WithHints:
        operations = []

        async def fetch(self, url, inn):
            if "fbc.ru" in url:
                return Page(url, OWNER, "html", [], ["https://example.ru/legal"])
            return Page(url, OWNER, "html", [])

    class RetryThenRead(ModelFixture):
        async def review(self, inn, candidates, evidence, state=None):
            if len(state["used_queries"]) == 1 and any(e.kind != "search" for e in evidence):
                return Review(
                    company_name="Дейта Кью",
                    verdicts=[],
                    action="search",
                    next_query=f'"Дейта Кью" "{inn}"',
                    reason="Уточним сайт",
                )
            return await super().review(inn, candidates, evidence, state)

    finder = Finder(FailingAfterFirst(), WithHints(), RetryThenRead(), Settings())
    assert await finder.run(INN) == ("example.ru", "found")
    assert finder.search_failures == ["search temporarily blocked"]
    assert finder.evidence[-1].candidate_id is not None


async def test_nested_legal_document_is_read():
    class NestedFetch:
        operations = []

        async def fetch(self, url, inn):
            if "fbc.ru" in url:
                return None
            if url.endswith("/legal"):
                return Page(url, "Юридические документы компании", "html", [url + "/offer"])
            if url.endswith("/offer"):
                return Page(url, OWNER, "html", [])
            return Page(url, "Наш сервис", "html", [url + "legal"])

    class RootSearch:
        operations = []

        async def search(self, query, inn):
            return [SearchHit("https://example.ru/", "Наш сервис", "")]

    finder = Finder(RootSearch(), NestedFetch(), ModelFixture(), Settings())
    assert await finder.run(INN) == ("example.ru", "found")


async def test_rejected_directory_does_not_crowd_out_new_candidate():
    class ChangingSearch:
        operations = []

        async def search(self, query, inn):
            self.operations.append({"query": query})
            domain = "directory.ru" if len(self.operations) == 1 else "example.ru"
            return [SearchHit("https://" + domain + "/legal", "Дейта Кью", OWNER)]

    class RejectFirst(ModelFixture):
        async def review(self, inn, candidates, evidence, state=None):
            if candidates[0].domain == "directory.ru":
                return Review(
                    company_name="Дейта Кью",
                    verdicts=[
                        Verdict(
                            candidate_id=candidates[0].id,
                            relation="third_party",
                            main_site=False,
                            evidence_id=None,
                            quote="",
                            reason="Каталог",
                        )
                    ],
                    action="search",
                    next_query=f'"Дейта Кью" "{inn}"',
                    reason="Нужен собственный сайт",
                )
            return await super().review(inn, candidates, evidence)

    finder = Finder(ChangingSearch(), FetchFixture(), RejectFirst(), Settings(max_candidates=1))
    assert await finder.run(INN) == ("example.ru", "found")
    assert {c.id for c in finder.candidates} == {"c1", "c2"}


async def test_unread_search_hit_is_read_without_repeating_search():
    class ThreePages:
        operations = []

        async def search(self, query, inn):
            self.operations.append({"query": query})
            if len(self.operations) > 1:
                raise ProviderError("temporary failure")
            return [
                SearchHit("https://example.ru/" + path, "Дейта Кью", OWNER)
                for path in ["page1", "page2", "page3"]
            ]

    class LastPage:
        operations = []

        async def fetch(self, url, inn):
            text = (
                OWNER if url.endswith("page3") or "fbc.ru" in url else "Наш сервис без реквизитов"
            )
            return Page(url, text, "html", [])

    search = ThreePages()
    finder = Finder(search, LastPage(), ModelFixture(), Settings())
    assert await finder.run(INN) == ("example.ru", "found")
    assert not finder.search_failures
    assert len(search.operations) == 1


async def test_secondary_project_does_not_consume_next_search():
    class ProjectSearch:
        operations = []

        async def search(self, query, inn):
            self.operations.append({"query": query})
            return [SearchHit("https://project.ru/legal", "Дейта Кью", OWNER)]

    class ProjectModel:
        operations = []

        async def review(self, inn, candidates, evidence, state=None):
            page = next((e for e in evidence if e.candidate_id and e.kind == "html"), None)
            if not page:
                return await ModelFixture().review(inn, candidates, evidence, state)
            return Review(
                company_name="Дейта Кью",
                verdicts=[
                    Verdict(
                        candidate_id=page.candidate_id,
                        relation="owner",
                        main_site=False,
                        evidence_id=page.id,
                        quote=OWNER,
                        reason="Отдельный продукт, основной сайт ещё не найден",
                    )
                ],
                action="search",
                investigate_candidate_id=page.candidate_id,
                reason="Нужно найти основной сайт",
                next_query=f'"Дейта Кью" "{inn}" официальный сайт',
            )

    search = ProjectSearch()
    finder = Finder(search, FetchFixture(), ProjectModel(), Settings(max_searches=2))
    assert await finder.run(INN) == (None, "insufficient_evidence")
    assert '"Дейта Кью"' in search.operations[1]["query"]
    assert "site:project.ru" not in search.operations[1]["query"]


async def test_pdf_search_is_not_blocked_by_unread_html_backlog():
    class PdfSearch:
        def __init__(self):
            self.operations = []

        async def search(self, query, inn):
            self.operations.append({"query": query})
            if "filetype:pdf" in query:
                return [SearchHit("https://example.ru/offer.pdf", "Реквизиты", OWNER)]
            return [
                SearchHit(f"https://example.ru/legal/{n}", "Реквизиты", OWNER) for n in range(8)
            ]

    class PdfFetch:
        operations = []

        async def fetch(self, url, inn):
            if url.endswith(".pdf"):
                return Page(url, OWNER, "pdf", [])
            return None  # HTML закрыт; поиск продолжает находить такие страницы.

    class PdfModel(ModelFixture):
        async def review(self, inn, candidates, evidence, state=None):
            page = next((e for e in evidence if e.kind == "pdf"), None)
            pdf_urls = [
                u for c in candidates for u in c.urls if u.endswith(".pdf") and u not in c.read_urls
            ]
            if not page and pdf_urls:
                return Review(
                    company_name=None,
                    verdicts=[],
                    action="read",
                    read_urls=pdf_urls[:1],
                    reason="Проверим найденный PDF",
                )
            return Review(
                company_name=None,
                verdicts=[
                    Verdict(
                        candidate_id=candidates[0].id,
                        relation="owner" if page else "unclear",
                        main_site=bool(page),
                        evidence_id=page.id if page else None,
                        quote=OWNER if page else "",
                        reason="Реквизиты продавца в PDF" if page else "Нужен документ",
                    )
                ],
                action="finish" if page else "search",
                investigate_candidate_id=candidates[0].id,
                reason="Проверка реквизитов",
                next_query=None
                if page
                else f'site:example.ru "{inn}"'
                + (" filetype:pdf" if len(state["used_queries"]) > 1 else ""),
            )

    search = PdfSearch()
    finder = Finder(search, PdfFetch(), PdfModel(), Settings())
    assert await finder.run(INN) == ("example.ru", "found")
    assert len(search.operations) == 3
    assert "https://example.ru/offer.pdf" in finder.read_urls


async def test_irrelevant_first_search_does_not_end_lookup():
    class NoisySearch:
        def __init__(self):
            self.operations = []

        async def search(self, query, inn):
            self.operations.append({"query": query})
            if len(self.operations) == 1:
                return [SearchHit("https://directory.ru/", "Проверка по ИНН", "Сервис проверки")]
            return [SearchHit("https://example.ru/legal", "Реквизиты", OWNER)]

    class NoisyFetch:
        operations = []

        async def fetch(self, url, inn):
            if "fbc.ru" in url:
                return None
            return Page(url, OWNER if "example.ru" in url else "Сервис проверки", "html", [])

    class NoisyModel(ModelFixture):
        async def review(self, inn, candidates, evidence, state=None):
            if not any(c.domain == "example.ru" for c in candidates):
                return Review(
                    company_name=None,
                    verdicts=[],
                    action="search",
                    next_query=f'"{inn}" официальный сайт',
                    reason="Название компании не найдено",
                )
            return await super().review(
                inn, candidates, [e for e in evidence if "example.ru" in e.url]
            )

    search = NoisySearch()
    finder = Finder(search, NoisyFetch(), NoisyModel(), Settings())
    assert await finder.run(INN) == ("example.ru", "found")
    assert search.operations[0]["query"].startswith(f'"{INN}"')
    assert search.operations[1]["query"] == f'"{INN}" официальный сайт'


async def test_model_cannot_fetch_a_url_that_was_not_observed():
    fetched = []

    class RecordingFetch(FetchFixture):
        async def fetch(self, url, inn):
            fetched.append(url)
            return await super().fetch(url, inn)

    class InventedRead:
        operations = []

        async def review(self, inn, candidates, evidence, state=None):
            return Review(
                company_name=None,
                verdicts=[],
                action="read",
                read_urls=["http://127.0.0.1/secret"],
                reason="Недоверенное действие",
            )

    finder = Finder(SearchFixture(), RecordingFetch(), InventedRead(), Settings())
    assert (await finder.run(INN))[0] is None
    assert "http://127.0.0.1/secret" not in fetched

from inn_site.fetch import Page
from inn_site.models import Review, SearchHit, Verdict
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

    async def review(self, inn, candidates, evidence):
        page = next((e for e in evidence if e.kind == "html" and e.candidate_id), None)
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
        return Review(
            company_name="Дейта Кью",
            verdicts=verdicts,
            action="finish" if page else "search",
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
    assert finder.read_urls == {"https://fbc.ru/inn/7721581040/"}


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
    result = await find_site(INN, Settings(cache_dir=None, llm_provider="ollama"))
    assert result.public() == {"domain": None}
    assert result.status == "search_error"
    assert result.trace["error"] == "quota"


async def test_invalid_input_does_not_use_network():
    result = await find_site("7721581041")
    assert result.status == "invalid_inn"


async def test_catalog_hint_is_verified_when_search_is_unavailable():
    class BrokenSearch:
        operations = []

        async def search(self, query, inn):
            raise ProviderError("search temporarily blocked")

    class WithHints:
        operations = []

        async def fetch(self, url, inn):
            if "fbc.ru" in url:
                return Page(url, OWNER, "html", [], ["https://example.ru/legal"])
            return Page(url, OWNER, "html", [])

    finder = Finder(BrokenSearch(), WithHints(), ModelFixture(), Settings())
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
        async def review(self, inn, candidates, evidence):
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
                    reason="Нужен собственный сайт",
                )
            return await super().review(inn, candidates, evidence)

    finder = Finder(ChangingSearch(), FetchFixture(), RejectFirst(), Settings(max_candidates=1))
    assert await finder.run(INN) == ("example.ru", "found")
    assert {c.id for c in finder.candidates} == {"c1", "c2"}


async def test_unread_search_hit_is_used_when_next_search_fails():
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

    finder = Finder(ThreePages(), LastPage(), ModelFixture(), Settings())
    assert await finder.run(INN) == ("example.ru", "found")
    assert finder.search_failures == ["temporary failure"]

import json

import httpx
import pytest

from inn_site.fetch import Fetcher, UnsafeURL, excerpts, html_content, website_hints
from inn_site.search import Cache, ProviderError, Search, parse_hits, rpc_result


def test_exa_sse_and_json():
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"content": []}}
    assert rpc_result(json.dumps(payload)) == {"content": []}
    assert rpc_result("event: message\ndata: " + json.dumps(payload) + "\n\n") == {"content": []}


def test_exa_error_is_not_an_empty_result():
    with pytest.raises(ProviderError):
        rpc_result('{"result":{"isError":true,"content":[]}}')
    with pytest.raises(ProviderError):
        rpc_result("<html>Captcha</html>")


def test_search_format():
    text = "Title: Реквизиты\nURL: https://example.ru/legal\nHighlights:\nИНН 7721581040"
    hits = parse_hits(text)
    assert len(hits) == 1
    assert hits[0].url == "https://example.ru/legal"
    assert hits[0].text == "ИНН 7721581040"


def test_remote_fetch_markdown_format():
    hits = parse_hits("# Реквизиты\nURL: https://example.ru/legal\n\nООО Пример ИНН 7721581040")
    assert hits[0].text == "ООО Пример ИНН 7721581040"


def test_reader_url_is_not_page_evidence():
    hits = parse_hits(
        "Title: Документ\nURL: https://example.ru/7721581040\n"
        "Markdown Content:\nТекст без реквизитов"
    )
    assert hits[0].text == "Текст без реквизитов"


async def test_reader_target_error_is_not_an_evidence_page():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                text="Title: Error\nURL Source: https://example.ru/\n"
                "Warning: Target URL returned error 401: Unauthorized\nMarkdown Content:\n",
            )
        )
    ) as client:
        search = Search(client, Cache(None))
        with pytest.raises(ProviderError, match="ошибку целевой страницы"):
            await search.fetch_remote("https://example.ru/")


def test_html_contacts_and_no_script_text():
    text, links = html_content(
        """<script>fake INN</script><p>Наша компания</p>
        <a href="/legal">Реквизиты</a><a href="https://other.ru/contacts">Партнёр</a>""",
        "https://example.ru/",
    )
    assert "fake INN" not in text
    assert links == ["https://example.ru/legal"]


def test_inn_after_large_navigation_is_kept():
    text = "Каталог товаров. " * 5000 + "ООО Пример, ИНН 7721581040. Это наши реквизиты."
    assert "ООО Пример, ИНН 7721581040" in excerpts(text, "7721581040", 5000)


def test_footer_is_kept_with_inn_near_start():
    text = "ООО Пример ИНН 7721581040. " + "Каталог. " * 5000 + "Футер: это сайт партнёра."
    compact = excerpts(text, "7721581040", 3000)
    assert "7721581040" in compact
    assert "Футер: это сайт партнёра." in compact


async def test_http_429_is_reported():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(429, text="rate limit"))
    ) as client:
        search = Search(client, Cache(None), provider="exa")
        with pytest.raises(ProviderError):
            await search.search("query", "7721581040")


@pytest.mark.parametrize(
    "body", ["<html>Captcha</html>", '{"results":null}', '{"results":[{"title":"Нет URL"}]}']
)
async def test_malformed_searx_response_is_a_provider_error(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=body))
    ) as client:
        with pytest.raises(ProviderError):
            await Search(client, Cache(None)).search("query", "7721581040")


async def test_redirect_cannot_reach_private_network(monkeypatch):
    called = []

    def respond(request):
        called.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        f = Fetcher(client, Search(client, Cache(None)), Cache(None))

        async def check(url):
            if "127.0.0.1" in url:
                raise ValueError("private")
            return url

        monkeypatch.setattr(f, "_safe_url", check)
        with pytest.raises(ValueError):
            await f._read("https://example.ru/")
        assert called == ["https://example.ru/"]


async def test_robots_denial_does_not_trigger_remote_fetch(monkeypatch):
    async with httpx.AsyncClient() as client:
        f = Fetcher(client, Search(client, Cache(None)), Cache(None))

        async def safe(url):
            return url

        async def denied(url):
            return False

        monkeypatch.setattr(f, "_safe_url", safe)
        monkeypatch.setattr(f, "_allowed", denied)
        assert await f.fetch("https://example.ru/", "7721581040") is None
        assert not f.search.operations


def test_catalog_links_are_discovery_hints():
    html = '<p>Сайт компании: <a href="https://owner.ru/">Перейти</a></p>'
    html += '<a href="https://vk.com/owner">Соцсеть</a>'
    assert website_hints(html, "https://catalog.ru/company/1") == ["https://owner.ru/"]


async def test_unsafe_redirect_does_not_trigger_external_reader(monkeypatch):
    async with httpx.AsyncClient() as client:
        f = Fetcher(client, Search(client, Cache(None)), Cache(None))

        async def safe(url):
            return url

        async def allowed(url):
            return True

        async def unsafe(url, *args):
            raise UnsafeURL("private redirect")

        monkeypatch.setattr(f, "_safe_url", safe)
        monkeypatch.setattr(f, "_allowed", allowed)
        monkeypatch.setattr(f, "_read", unsafe)
        assert await f.fetch("https://example.ru/", "7721581040") is None
        assert f.operations[-1]["error"] == "unsafe_url"
        assert not f.search.operations

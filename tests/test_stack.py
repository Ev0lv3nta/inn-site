import importlib.util
import json
from pathlib import Path

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "search_gateway", Path(__file__).resolve().parents[1] / "stack/search_gateway.py"
)
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


@pytest.mark.parametrize("engine", ["tavily", "brave"])
def test_remote_search_uses_real_search_endpoint_and_preserves_source(engine):
    def respond(request):
        if engine == "tavily":
            assert request.url == "https://api.tavily.com/search"
            assert request.headers["Authorization"] == "Bearer secret"
            body = json.loads(request.content)
            assert body["search_depth"] == "basic"
            assert not body["include_answer"] and not body["include_raw_content"]
            data = {
                "results": [
                    {
                        "url": "https://example.ru/legal",
                        "title": "Company",
                        "content": "Search snippet",
                    }
                ]
            }
        else:
            assert request.url.host == "api.search.brave.com"
            assert request.url.path == "/res/v1/web/search"
            assert request.headers["X-Subscription-Token"] == "secret"
            data = {
                "web": {
                    "results": [
                        {
                            "url": "https://example.ru/legal",
                            "title": "Company",
                            "description": "Search snippet",
                        }
                    ]
                }
            }
        return httpx.Response(200, json=data)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = gateway.remote_search(client, engine, "company", "secret")
    assert result[0]["url"] == "https://example.ru/legal"
    assert result[0]["content"] == "Search snippet"


def test_search_gateway_does_not_call_remote_api_without_explicit_selection(monkeypatch):
    monkeypatch.setenv("REMOTE_SEARCH", "none")
    monkeypatch.setenv("TAVILY_API_KEY", "available-but-not-authorized")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "available-but-not-authorized")
    original_client = httpx.Client
    calls = []

    def respond(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"results": [], "unresponsive_engines": []})

    monkeypatch.setattr(
        gateway.httpx,
        "Client",
        lambda **kw: original_client(transport=httpx.MockTransport(respond), **kw),
    )
    assert gateway.search("company")["results"] == []
    assert calls == ["127.0.0.1"]

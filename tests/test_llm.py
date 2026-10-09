import json

import httpx
import pytest

from inn_site.llm import DEFAULT_MODEL, LLMError, OpenRouter, review_input
from inn_site.models import Candidate, Evidence


@pytest.mark.parametrize(
    "model,pricing",
    [
        ("vendor/paid", {"prompt": "0", "completion": "0"}),
        (DEFAULT_MODEL, {"prompt": "0.001", "completion": "0"}),
        (DEFAULT_MODEL, {"prompt": "0", "completion": "0", "request": "1"}),
        (DEFAULT_MODEL, {"prompt": "0"}),
    ],
)
async def test_unauthorized_or_expensive_model_cannot_reach_completion(monkeypatch, model, pricing):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(
            200, json={"data": [{"id": model, "pricing": pricing, "context_length": 32000}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        llm = OpenRouter(client, model)
        with pytest.raises(LLMError):
            await llm.prepare()
    assert "/api/v1/chat/completions" not in calls


def test_quote_schema_contains_only_source_fragments():
    text = "Реквизиты исполнителя ООО «Пример»: ИНН 7721581040. Наши услуги."
    schema, messages = review_input(
        "7721581040",
        [Candidate("c1", "example.ru")],
        [Evidence("e1", "https://example.ru/legal", "html", text, "c1")],
    )
    assert set(schema["$defs"]["Verdict"]["properties"]["quote"]["enum"]) == {"", text}
    assert json.loads(messages[1]["content"])["evidence"][0]["inn_quote"] == text


async def test_request_disables_fallbacks_and_plugins(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    content = {"company_name": None, "verdicts": [], "action": "finish", "reason": "Нет данных"}

    def respond(request):
        if request.url.path.endswith("models"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": DEFAULT_MODEL,
                            "pricing": {"prompt": "0", "completion": "0"},
                            "context_length": 32000,
                        }
                    ]
                },
            )
        body = json.loads(request.content)
        assert body["model"] == DEFAULT_MODEL
        assert body["reasoning"]["effort"] == "high"
        assert body["provider"]["allow_fallbacks"] is False
        assert body["plugins"] == []
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(content)}}], "usage": {"cost": 0}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        llm = OpenRouter(client, budget_file=tmp_path / "budget.json")
        assert (await llm.review("7721581040", [], [])).action == "finish"
        assert llm.operations[0]["cost"] == 0

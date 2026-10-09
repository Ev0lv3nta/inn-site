import httpx

from inn_site.registry import identify
from inn_site.search import Cache


async def test_only_the_subjects_own_inn_can_identify_it():
    def respond(request):
        if request.method == "POST":
            return httpx.Response(200, json={"t": "token", "captchaRequired": False})
        return httpx.Response(
            200,
            json={
                "rows": [
                    {"i": "7721581040", "c": "ООО «Пример»", "k": "ul"},
                    {"i": "500100732259", "c": "ИП Иванов Иван Иванович", "k": "fl"},
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        data, operation = await identify(client, Cache(None), "500100732259")
    assert len(data["records"]) == 1
    assert data["records"][0]["k"] == "fl"
    assert operation["exact_matches"] == 1


async def test_registry_captcha_is_an_optional_source_error():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"captchaRequired": True}))
    ) as client:
        data, operation = await identify(client, Cache(None), "7721581040")
    assert data is None
    assert operation["error"] == "captcha_required"


async def test_empty_registry_is_not_a_claim_that_no_website_exists():
    def respond(request):
        return httpx.Response(
            200, json={"t": "token"} if request.method == "POST" else {"rows": []}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        data, operation = await identify(client, Cache(None), "500100732259")
    assert data == {"requested_inn": "500100732259", "records": []}
    assert not operation.get("error")

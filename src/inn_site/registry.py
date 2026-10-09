"""Идентификация субъекта в публичном сервисе ЕГРЮЛ/ЕГРИП ФНС."""

import httpx

from inn_site.search import Cache

URL = "https://egrul.nalog.ru/"


async def identify(client: httpx.AsyncClient, cache: Cache, inn: str) -> tuple[dict | None, dict]:
    key = "registry:" + inn
    data = cache.get(key)
    operation = {"tool": "fns_identity", "cache_hit": data is not None}
    if data is not None:
        return data, operation
    try:
        response = await client.post(URL, data={"query": inn, "region": ""}, timeout=15)
        response.raise_for_status()
        token = response.json()
        if token.get("captchaRequired"):
            operation["error"] = "captcha_required"
            return None, operation
        if not isinstance(token.get("t"), str):
            raise ValueError("Нет токена результата")
        result = await client.get(URL + "search-result/" + token["t"], timeout=15)
        result.raise_for_status()
        rows = result.json().get("rows")
        if not isinstance(rows, list):
            raise ValueError("Нет списка результатов")
        exact = [row for row in rows if row.get("i") == inn]
        data = {
            "requested_inn": inn,
            "records": [
                {field: row.get(field) for field in ["i", "k", "n", "c", "o", "rn"]}
                for row in exact
            ],
        }
        cache.put(key, data)
        operation["exact_matches"] = len(exact)
        return data, operation
    except (httpx.HTTPError, ValueError, TypeError, AttributeError) as error:
        operation["error"] = type(error).__name__
        return None, operation

"""SearXNG-compatible endpoint для CRW; необязательный резерв Tavily/Brave."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx


def remote_search(client, engine, query, key, limit=10):
    if engine == "tavily":
        response = client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": "Bearer " + key},
            json={
                "query": query,
                "search_depth": "basic",
                "max_results": limit,
                "include_answer": False,
                "include_raw_content": False,
                "auto_parameters": False,
                "language": "ru",
            },
        )
        response.raise_for_status()
        return [
            {
                "url": item["url"],
                "title": item.get("title", ""),
                "content": item.get("content", ""),
                "engines": ["tavily"],
            }
            for item in response.json()["results"]
        ]
    response = client.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": key},
        params={"q": query, "count": limit, "search_lang": "ru", "country": "RU"},
    )
    response.raise_for_status()
    return [
        {
            "url": item["url"],
            "title": item.get("title", ""),
            "content": item.get("description", ""),
            "engines": ["brave-api"],
        }
        for item in response.json().get("web", {}).get("results", [])
    ]


def search(query):
    with httpx.Client(timeout=25) as client:
        try:
            response = client.get(
                os.getenv("SEARX_URL", "http://127.0.0.1:8890") + "/search",
                params={"q": query, "format": "json", "language": "ru"},
            )
            response.raise_for_status()
            result = response.json()
            if result.get("results"):
                return result
        except (httpx.HTTPError, ValueError):
            result = {"results": [], "unresponsive_engines": [["searxng", "unavailable"]]}
        engine = os.getenv("REMOTE_SEARCH", "none")
        key = os.getenv("TAVILY_API_KEY" if engine == "tavily" else "BRAVE_SEARCH_API_KEY", "")
        if engine in {"tavily", "brave"} and key:
            results = remote_search(client, engine, query, key)
            return {
                "results": results,
                "unresponsive_engines": [],
                "number_of_results": len(results),
            }
        return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        route = urlsplit(self.path)
        status = 200
        if route.path == "/healthz":
            result = {"status": "ok"}
        elif route.path == "/search":
            query = parse_qs(route.query).get("q", [""])[0]
            if not query or len(query) > 1000:
                status, result = 400, {"error": "invalid_query"}
            else:
                try:
                    result = search(query)
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    status, result = 503, {"error": "search_unavailable"}
        else:
            status, result = 404, {"error": "not_found"}
        data = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    ThreadingHTTPServer(
        ("127.0.0.1", int(os.getenv("SEARCH_GATEWAY_PORT", "8891"))), Handler
    ).serve_forever()

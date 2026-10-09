import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from inn_site.llm import DEFAULT_MODEL
from inn_site.pipeline import Settings, find_site


def main() -> int:
    parser = argparse.ArgumentParser(description="Найти основной сайт организации по ИНН")
    parser.add_argument("--inn", help="ИНН; без параметра читается JSON из stdin")
    parser.add_argument("--trace", type=Path, help="Сохранить источники и причины решения")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--search", choices=["searx", "ddgs", "exa", "crw"], default="searx")
    parser.add_argument("--llm", choices=["openrouter", "ollama"], default="openrouter")
    parser.add_argument("--model")
    args = parser.parse_args()
    try:
        if args.inn is not None:
            inn = args.inn
        else:
            obj = json.load(sys.stdin)
            if not isinstance(obj, dict) or set(obj) != {"inn"}:
                raise ValueError('Ожидается объект {"inn": "..."}')
            inn = obj["inn"]
        result = asyncio.run(
            find_site(
                inn,
                Settings(
                    model=args.model
                    or (
                        os.getenv("OLLAMA_MODEL", "qwen3.5:4b")
                        if args.llm == "ollama"
                        else DEFAULT_MODEL
                    ),
                    llm_provider=args.llm,
                    ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434"),
                    search_provider=args.search,
                    searx_url=os.getenv("SEARX_URL", "http://127.0.0.1:8888"),
                    crw_url=os.getenv("CRW_URL"),
                    budget_file=Path(os.getenv("BUDGET_FILE", ".cache/llm-spend.json")),
                    budget_usd=float(os.getenv("LLM_BUDGET_USD", "0.25")),
                    cache_dir=None
                    if args.no_cache
                    else Path(os.getenv("CACHE_DIR", ".cache/inn-site")),
                ),
            )
        )
    except (ValueError, json.JSONDecodeError) as e:
        print(json.dumps({"domain": None}))
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(result.public(), ensure_ascii=False))
    if args.trace:
        args.trace.parent.mkdir(parents=True, exist_ok=True)
        args.trace.write_text(json.dumps(result.trace, ensure_ascii=False, indent=2))
    if result.status == "invalid_inn":
        print("Некорректный ИНН", file=sys.stderr)
        return 2
    if result.status in {"search_error", "llm_error", "timeout"}:
        print(result.trace["error"], file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())

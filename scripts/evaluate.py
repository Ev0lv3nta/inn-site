"""Живой прогон. Ответы из cases.json не передаются пайплайну."""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import statistics
from datetime import UTC, datetime
from pathlib import Path

import httpx

from inn_site.llm import DEFAULT_MODEL, OpenRouter
from inn_site.pipeline import Settings, find_site

ROOT = Path(__file__).resolve().parents[1]


def summarize(rows):
    positives = [r for r in rows if r["expected_domains"]]
    answered = [r for r in rows if r["domain"] is not None]
    correct_answers = [r for r in answered if r["ok"]]
    return {
        "cases": len(rows),
        "correct": sum(r["ok"] for r in rows),
        "returned_domains": len(answered),
        "wrong_domains": len(answered) - len(correct_answers),
        "precision_on_returned": len(correct_answers) / len(answered) if answered else None,
        "positive_recall": sum(r["ok"] for r in positives) / len(positives) if positives else None,
        "median_seconds": statistics.median(r["seconds"] for r in rows) if rows else None,
        "errors": sum(r["status"] in {"search_error", "llm_error", "timeout"} for r in rows),
    }


async def run(args):
    cases = json.loads((ROOT / "tests/cases.json").read_text())
    if args.split != "all":
        cases = [c for c in cases if c["split"] == args.split]
    if args.inn:
        cases = [c for c in cases if c["inn"] in args.inn]
    output = args.output or ROOT / f"reports/live-{args.search}-{args.split}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    trace_dir = output.parent / "private" / output.stem
    trace_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        llm_provider=args.llm,
        model=args.model
        or (os.getenv("OLLAMA_MODEL", "qwen3.5:4b") if args.llm == "ollama" else DEFAULT_MODEL),
        search_provider=args.search,
        searx_url=args.searx_url,
        crw_url=args.crw_url,
        cache_dir=None if args.no_cache else ROOT / ".cache/inn-site",
        budget_file=ROOT / ".cache/llm-spend.json",
    )
    async with httpx.AsyncClient() as client:
        if args.llm == "ollama":
            model_info = (await client.get(settings.ollama_url + "/api/tags")).json()
        else:
            verifier = OpenRouter(client, settings.model)
            await verifier.prepare()
            model_info = verifier.metadata
    rows = []
    report = {
        "run_at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "model_info": model_info,
        "search_provider": args.search,
        "source_sha256": hashlib.sha256(
            b"".join(
                p.relative_to(ROOT).as_posix().encode() + p.read_bytes()
                for p in sorted((ROOT / "src").rglob("*.py"))
            )
        ).hexdigest(),
        "rows": rows,
    }
    for case in cases:
        result = await find_site(case["inn"], settings)
        ok = (
            result.domain in case["expected_domains"]
            if case["expected_domains"]
            else result.domain is None
            and result.status not in {"search_error", "llm_error", "timeout"}
        )
        rows.append(
            {
                **case,
                "domain": result.domain,
                "status": result.status,
                "ok": ok,
                "seconds": result.trace.get("elapsed_seconds", 0),
                "search_calls": sum(
                    o["tool"] in {"web_search_exa", "crw_search", "ddgs_search", "searx_search"}
                    for o in result.trace.get("search_operations", [])
                ),
                "read_urls": len(result.trace.get("page_operations", [])),
                "cache_hits": sum(
                    o.get("cache_hit", False)
                    for o in result.trace.get("search_operations", [])
                    + result.trace.get("page_operations", [])
                ),
                "llm_calls": len(result.trace.get("llm_operations", [])),
                "llm_cost_usd": round(
                    sum(o.get("cost") or 0 for o in result.trace.get("llm_operations", [])), 8
                ),
                "search_failures": len(result.trace.get("search_failures", [])),
            }
        )
        (trace_dir / f"{case['inn']}.json").write_text(
            json.dumps(result.trace, ensure_ascii=False, indent=2)
        )
        report["summary"] = summarize(rows)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    parser.add_argument("--search", choices=["searx", "ddgs", "exa", "crw"], default="searx")
    parser.add_argument("--searx-url", default=os.getenv("SEARX_URL", "http://127.0.0.1:8888"))
    parser.add_argument("--crw-url")
    parser.add_argument("--llm", choices=["openrouter", "ollama"], default="openrouter")
    parser.add_argument("--model")
    parser.add_argument("--inn", nargs="*")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-cache", action="store_true")
    asyncio.run(run(parser.parse_args()))

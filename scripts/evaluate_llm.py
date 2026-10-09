import argparse
import asyncio
import json
from pathlib import Path

import httpx

from inn_site.llm import Ollama, OpenRouter
from inn_site.models import Candidate, Evidence
from inn_site.rules import choose_domain, has_inn

ROOT = Path(__file__).resolve().parents[1]


async def main(args):
    cases = json.loads((ROOT / "tests/llm_cases.json").read_text())
    output = args.output or ROOT / "reports/llm-regression.json"
    previous = json.loads(output.read_text()) if args.append and output.exists() else {}
    rows = previous.get("rows", [])
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    cases = [c for c in cases if c["id"] not in {r["id"] for r in rows}]
    async with httpx.AsyncClient() as client:
        llm = (
            OpenRouter(
                client, budget_file=ROOT / ".cache/llm-spend.json", budget_usd=args.budget_usd
            )
            if args.llm == "openrouter"
            else Ollama(client)
        )
        if isinstance(llm, OpenRouter):
            await llm.prepare()
        for case in cases:
            inn = case.get("inn", "7721581040")
            candidates = [
                Candidate(**c)
                for c in case.get("candidates", [{"id": "c1", "domain": "example.ru"}])
            ]
            evidence = [
                Evidence(**e)
                for e in case.get(
                    "evidence",
                    [
                        {
                            "id": "e1",
                            "url": "https://example.ru/legal",
                            "kind": case.get("kind", "html"),
                            "text": case["text"],
                            "candidate_id": "c1",
                        }
                    ],
                )
            ]
            review = await llm.review(inn, candidates, evidence)
            domain, status = choose_domain(inn, review, candidates, evidence)
            baseline = (
                "example.ru"
                if evidence[0].kind != "search" and has_inn(evidence[0].text, inn)
                else None
            )
            rows.append(
                {
                    "id": case["id"],
                    "inn": inn,
                    "expected": case["expected"],
                    "domain": domain,
                    "status": status,
                    "ok": domain == case["expected"],
                    "review": review.model_dump(),
                    "exact_inn_baseline": baseline,
                    "baseline_ok": baseline == case["expected"],
                }
            )
            print(case["id"], domain, rows[-1]["ok"], flush=True)
    output.write_text(
        json.dumps(
            {
                "model": llm.model,
                "rows": rows,
                "operations": previous.get("operations", []) + llm.operations,
                "summary": {
                    "cases": len(rows),
                    "llm_correct": sum(r["ok"] for r in rows),
                    "baseline_correct": sum(r["baseline_ok"] for r in rows),
                    "llm_false_domains": sum(r["domain"] is not None and not r["ok"] for r in rows),
                    "baseline_false_domains": sum(
                        r["exact_inn_baseline"] is not None and not r["baseline_ok"] for r in rows
                    ),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not all(r["ok"] for r in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Проверить LLM на сохранённых фрагментах")
    parser.add_argument("--case", nargs="*")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--budget-usd", type=float, default=0.25)
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--llm", choices=["openrouter", "ollama"], default="openrouter")
    asyncio.run(main(parser.parse_args()))

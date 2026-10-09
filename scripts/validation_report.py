"""Собрать отчёт из сохранённых прогонов, без новых сетевых запросов."""
# ruff: noqa: E501

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGES = [
    ("exploration/live-holdout", "Первый контроль с первоначальным эталоном", 3),
    ("exploration/live-oss-final", "Первый полный прогон CRW", 18),
    ("exploration/live-oss-fix-check", "Проверка исправлений чтения и анализа", 5),
    ("live-oss-release", "Заключительный полный прогон", 18),
    ("live-oss-release-targets", "Повтор четырёх затронутых случаев", 4),
]


def load_results():
    stages = []
    for stem, label, expected in STAGES:
        report = json.loads((ROOT / "reports" / (stem + ".json")).read_text())
        if len(report["rows"]) != expected:
            raise ValueError(f"Прогон {stem} ещё не завершён")
        stages.append((stem, label, report))
    latest = {}
    for stem, _, report in stages:
        for row in report["rows"]:
            latest[row["inn"]] = {**row, "stage": stem, "source_sha256": report.get("source_sha256")}
    cases = json.loads((ROOT / "tests/cases.json").read_text())
    if set(latest) != {c["inn"] for c in cases}:
        raise ValueError("Проверены не все заданные входы")
    rows = [latest[c["inn"]] for c in cases]
    regression = json.loads((ROOT / "reports/llm-oss-final.json").read_text())
    return stages, rows, regression


def metric(rows):
    positives = [r for r in rows if r["expected_domains"]]
    returned = [r for r in rows if r["domain"]]
    return (
        sum(r["ok"] for r in positives),
        len(positives),
        sum(r["ok"] for r in returned),
        len(returned),
        sum(r["status"] in {"search_error", "llm_error", "timeout"} for r in rows),
    )


def decisions(rows):
    items = []
    for row in rows:
        trace = json.loads(
            (ROOT / "reports/private" / row["stage"] / (row["inn"] + ".json")).read_text()
        )
        item = {
            "inn": row["inn"],
            "domain": row["domain"],
            "status": row["status"],
            "gold_source": row["gold_source"],
            "accepted_evidence": [],
        }
        if row["domain"]:
            sources = {e["id"]: e for e in trace["evidence"]}
            candidates = {c["id"]: c for c in trace["candidates"]}
            for verdict in trace["rounds"][-1]["review"]["verdicts"]:
                source = sources.get(verdict["evidence_id"])
                candidate = candidates.get(verdict["candidate_id"])
                if not (
                    verdict["relation"] == "owner" and verdict["main_site"] and source and candidate
                ):
                    continue
                if candidate["domain"] != row["domain"] or row["inn"] not in verdict["quote"]:
                    continue
                words = verdict["quote"].split()
                pos = next((i for i, w in enumerate(words) if row["inn"] in w), 0)
                start = max(0, pos - 10)
                item["accepted_evidence"].append(
                    {
                        "url": source["url"],
                        "kind": source["kind"],
                        "quote_excerpt": " ".join(words[start : start + 22]),
                        "quote_sha256": hashlib.sha256(verdict["quote"].encode()).hexdigest(),
                        "reason": verdict["reason"],
                    }
                )
        items.append(item)
    return items


def main():
    stages, rows, regression = load_results()
    final = stages[-1][2]
    audit = json.loads((ROOT / "reports/llm-saved-audit.json").read_text())
    h, p, c, n, errors = metric(rows)
    stats = regression["summary"]
    cost = sum(r["llm_cost_usd"] for r in rows)
    text = f"""# Проверка

Последние живые результаты по ИНН: {sum(r["ok"] for r in rows)}/{len(rows)} правильных результатов.
Найдены {h}/{p} основных сайтов, ложных доменов {n - c}, технических сбоев {errors}.
Остальные два входа — неоднозначный основной бренд и некорректный ИНН.

Дата: `{final["run_at"]}`. Модель — `{final["model_info"]["id"]}`, reasoning `high`;
Python — {final["python"]}. Поиск — CRW REST + SearXNG. Чтение — HTTP/PDF,
при необходимости Lightpanda и Chromium через Playwright. Платные поисковые API
не использовались. Версии и проверки стека — `reports/stack-check.json`.

Проведён полный запуск 18 входов с `--no-cache`, затем повторены четыре пропуска после
доработки. Таблица объединяет последние ответы каждого ИНН, а не изображает один
безошибочный запуск. Текущая LLM дополнительно подтвердила {audit["summary"]["correct"]}/{audit["summary"]["cases"]}
остальных сайтов на сохранённых свидетельствах (`llm-saved-audit.json`), без нового поиска.
Эталоны и первичные ссылки
находятся в `tests/cases.json`; алгоритм получает только ИНН и настройки.
Это известные входы после разработки, а не новый слепой контроль. Качество на ИП,
случайной выборке и организациях без найденного сайта отдельно не измерялось.
Результаты нельзя переносить на весь ЕГРЮЛ.

Стоимость LLM в выбранных живых результатах — ${cost:.4f}.
Тариф на момент запуска: $0.10/M вход, $0.50/M выход. Общий журнал ограничивает бюджет.

| Организация | ИНН | Ответ | Статус | Время, с |
| --- | --- | --- | --- | --- |
"""
    for row in rows:
        text += f"| {row['name']} | {row['inn']} | {row['domain'] or 'null'} | {row['status']} | {row['seconds']} |\n"
    text += f"""
## Роль организации

На {stats["cases"]} синтетических фрагментах сравниваются те же свидетельства
и два способа принятия решения. Среди отрицательных примеров — клиент, банк в счёте,
чужой реестр, личный ИНН директора, бывший владелец, равноправные бренды и инструкция
внутри страницы. Это проверка выбранных ошибок, а не оценка их частоты.

| Подход | Правильные ответы | Ложные домены |
| --- | --- | --- |
| Только точный ИНН на странице | {stats["baseline_correct"]}/{stats["cases"]} | {stats["baseline_false_domains"]} |
| LLM и проверка источника | {stats["llm_correct"]}/{stats["cases"]} | {stats["llm_false_domains"]} |

Тексты — `tests/llm_cases.json`, ответы — `reports/llm-oss-final.json`.
Офлайн-тесты защищают от выдуманного источника, приватных адресов, нарушения robots.txt,
неверного ИНН, превышения бюджета и потери подтверждённых свидетельств.

## Исправления и предыдущие результаты

Первый контроль содержал ошибочный эталон Фотоэксперта: исходный `netprint.ru`
не устанавливал основной бренд. Аудит первичных страниц обнаружил несколько
собственных сайтов, поэтому ожидаемый ответ стал `null`. Исходный результат 2/3
сохранён. Затем были отложены ЛитРес, АШАН и Спортмастер; теперь их результаты
использованы в доработке, и слепым контролем они не называются.

Exa и DDGS упирались в квоты, CAPTCHA и нестабильную выдачу. Обновлённый SearXNG
восстановил поиск по названию. После перехода на CRW обнаружились потеря уже найденного
сайта, неточное цитирование и необоснованный выбор бренда по широте услуг.
Доказательства теперь сохраняются в состоянии, цитату прикрепляет программа,
а спорная иерархия проходит дополнительную проверку. Банковские документы потребовали
отдельного CA bundle; проверка TLS оставалась включённой.

| Этап | Найдены / положительные | Ложные домены | Сбои |
| --- | --- | --- | --- |
"""
    for _, label, report in stages:
        hh, pp, cc, nn, ee = metric(report["rows"])
        text += f"| {label} | {hh}/{pp} | {nn - cc} | {ee} |\n"
    text += f"""
Результаты всех этапов, включая неудачные, находятся в `reports/exploration`.
Техническая ошибка не засчитывается как правильный отказ. Нет ручных поправок
к итоговым ответам и подстановок из эталона. Короткие свидетельства выданных
доменов — `reports/decisions.json`; полные тексты страниц остаются в приватных трассах.

SHA-256 кода `src/` на начало заключительного запуска:
`{final["source_sha256"]}`.

Для более широкой оценки нужны независимые разметчики, новые организации,
отдельные сегменты ИП и групп, поздние данные и интервалы неопределённости.
Бесплатный поиск зависит от индекса и доступности движков; один успешный прогон
не гарантирует следующий.
"""
    (ROOT / "reports/validation.md").write_text(text)
    (ROOT / "reports/latest.json").write_text(
        json.dumps(
            {
                "source_sha256": final["source_sha256"],
                "rows": rows,
                "summary": {
                    "cases": len(rows), "correct": sum(r["ok"] for r in rows),
                    "returned_domains": n, "wrong_domains": n - c,
                    "positive_recall": h / p, "errors": errors,
                    "kind": "latest per-INN results across full run and targeted recheck",
                },
                "llm_cost_usd": round(cost, 8),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    (ROOT / "reports/decisions.json").write_text(
        json.dumps(decisions(rows), ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()

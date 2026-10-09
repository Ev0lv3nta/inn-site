"""Собрать отчёт из сохранённых прогонов, без новых сетевых запросов."""
# ruff: noqa: E501

import hashlib
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGES = [
    ("live-final-dev", "Первый прогон Luna 6", 12),
    ("live-dev-recheck", "Проверка обхода и чтения", 7),
    ("live-dev-final-check", "Проверка адресного поиска", 3),
    ("live-dev-budget-check", "Проверка бюджета чтения", 2),
    ("live-frozen-dev", "Свежий прогон финальной версии", 12),
    ("live-holdout", "Первый контроль, до разбора неоднозначности", 3),
    ("live-ambiguity-check", "Проверка отказа для нескольких брендов", 1),
    ("live-final-holdout", "Новый отложенный контроль", 3),
    ("live-network-recheck", "Проверка другого сетевого адреса", 4),
    ("live-cse-recheck", "Проверка Google CSE", 4),
    ("live-final-target-check", "Проверка точного ИНН внутри домена", 1),
    ("live-frontier-check", "Проверка очереди найденных URL", 1),
    ("live-final", "Заключительный прогон всех входов", 18),
]


def load_results():
    stages, latest = [], {}
    for stem, label, expected in STAGES:
        report = json.loads((ROOT / "reports" / (stem + ".json")).read_text())
        if len(report["rows"]) != expected:
            raise ValueError(f"Прогон {stem} ещё не завершён")
        stages.append((stem, label, report))
        for row in report["rows"]:
            latest[row["inn"]] = {**row, "stage": stem}
    cases = json.loads((ROOT / "tests/cases.json").read_text())
    if set(latest) != {c["inn"] for c in cases}:
        raise ValueError("Проверены не все заданные входы")
    rows = [{**latest[c["inn"]], "split": c["split"]} for c in cases]
    regression = json.loads((ROOT / "reports/llm-regression.json").read_text())
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
        path = ROOT / "reports/private" / row["stage"] / (row["inn"] + ".json")
        trace = json.loads(path.read_text())
        row["llm_cost_usd"] = round(
            sum(o.get("cost") or 0 for o in trace.get("llm_operations", [])), 8
        )
        row["search_failures"] = len(trace.get("search_failures", []))
        item = {
            "inn": row["inn"],
            "domain": row["domain"],
            "status": row["status"],
            "stage": row["stage"],
            "gold_source": row["gold_source"],
            "accepted_evidence": [],
        }
        if row["domain"]:
            by_evidence = {e["id"]: e for e in trace["evidence"]}
            by_candidate = {c["id"]: c for c in trace["candidates"]}
            for verdict in trace["rounds"][-1]["review"]["verdicts"]:
                e = by_evidence.get(verdict["evidence_id"])
                candidate = by_candidate.get(verdict["candidate_id"])
                if not (
                    verdict["relation"] == "owner" and verdict["main_site"] and e and candidate
                ):
                    continue
                if candidate["domain"] != row["domain"] or row["inn"] not in verdict["quote"]:
                    continue
                words = verdict["quote"].split()
                pos = next((i for i, w in enumerate(words) if row["inn"] in w), 0)
                start = max(0, pos - 10)
                item["accepted_evidence"].append(
                    {
                        "url": e["url"],
                        "kind": e["kind"],
                        "quote_excerpt": " ".join(words[start : start + 22]),
                        "quote_sha256": hashlib.sha256(verdict["quote"].encode()).hexdigest(),
                        "reason": verdict["reason"],
                    }
                )
        items.append(item)
    return items


def main():
    stages, rows, regression = load_results()
    accepted = decisions(rows)
    dev = [r for r in rows if r["split"] == "dev"]
    holdout = [r for r in rows if r["split"] == "holdout"]
    hits, positives, correct, returned, errors = metric(rows)
    stats = regression["summary"]
    model_info = stages[-1][2]["model_info"]
    text = f"""# Проверка

Дата — 9 октября 2026 года. LLM — `{model_info["id"]}`, reasoning `high`,
OpenRouter. Python — {stages[-1][2]["python"]}. Поиск — собственный SearXNG
в Docker; обычное чтение HTTP/PDF, при необходимости бесплатный Jina Reader.
В финальной конфигурации включены Google CSE (публичный поисковый элемент без ключа), DuckDuckGo и Yahoo. Идентификатор образа и настройки движков сохранены в `compose.yaml` и `search/settings.yml`.

## Живые запросы

Начальная разработка: 11 организаций и неверный вход. Первый контроль из трёх
других организаций выявил ошибку выбора основного бренда. После разбора он
переведён в данные разработки; для окончательного контроля отложены ещё три
организации, результаты которых до этого прогона не использовались для исправлений. Эталоны и первичные ссылки — `tests/cases.json`.
Пайплайн получает только ИНН и настройки, а не ожидаемые домены или эталонные URL.
Контрольный прогон выполнен с `--no-cache`.

| Этап | Найдены / положительные случаи | Ложные домены | Сбои инструментов |
| --- | --- | --- | --- |
"""
    for _, label, report in stages:
        h, p, c, n, e = metric(report["rows"])
        text += f"| {label} | {h} / {p} | {n - c} | {e} |\n"
    dh, dp, dc, dn, de = metric(dev)
    hh, hp, hc, hn, he = metric(holdout)
    text += f"""
Последние результаты разработки: {dh}/{dp} подтверждённых сайтов, ложных доменов
{dn - dc}. Контрольный набор: {hh}/{hp}, ложных доменов {hn - hc}. Это отдельные
показатели. Объединение последних результатов по ИНН даёт {hits}/{positives},
среди выданных доменов правильны {correct}/{returned}; технических ошибок {errors}.
Итоговые строки ниже получены в одном заключительном прогоне всех 18 входов, без локального кеша. Это уже известные входы после разработки; такой прогон проверяет итоговое поведение и не является новым слепым контролем.
Медиана времени по положительным случаям — {statistics.median(r["seconds"] for r in rows if r["expected_domains"]):.1f} с.
Число наблюдений мало, а выборка не случайная: эти доли нельзя переносить на весь ЕГРЮЛ.

| Организация | ИНН | Ответ | Статус | Время, с |
| --- | --- | --- | --- | --- |
"""
    for r in rows:
        text += f"| {r['name']} | {r['inn']} | {r['domain'] or 'null'} | {r['status']} | {r['seconds']} |\n"
    text += """
В начале были пропуски из-за каталогов в выдаче и недостаточного обхода
юридических документов. Из карточек добавлены только неподтверждённые ссылки;
решение по-прежнему требует страницы самого сайта. Модель выбирает вероятный
домен для адресного поиска; PDF помогает, когда основная HTML-страница закрыта.
Часть бюджета чтения оставлена на уточняющие запросы, чтобы ранние неподходящие
кандидаты не заняли все URL. Повторно проверялись именно затронутые случаи.

Прогоны Exa и локальной 4B-модели сохранены отдельно в `reports/exploration`.
Квота Exa исчерпалась, а публичная выдача DDGS оказалась нестабильной. Обычный Google и Brave ограничивали запросы. Штатный модуль Google CSE даёт другой бесплатный путь к индексу Google; он проверен отдельно на прежних пропусках. Поэтому
основной вариант использует собственный SearXNG и Luna 6; локальная 4B-модель
не участвует в итоговых метриках. SearXNG тоже зависит от индекса, CAPTCHA и IP:
список недоступных движков есть в трассировках, ошибки не маскируются под «сайта нет».

## Ошибка эталона и выбора основного бренда

Первоначально для ИНН 7724709637 был размечен только netprint.ru. Пайплайн
вернул fotoproekt.ru. Аудит первичных страниц подтвердил, что ООО «Фотоэксперт»
ведёт также Фотопроект и printbook.ru. Исходный эталон не устанавливал, какой
бренд главный. Самой модели тоже не хватало основания выбрать Фотопроект
лишь по широте услуг. Исходный результат 2/3 сохранён без исправления задним числом.

Случай перемечен как неоднозначный основной домен с ожидаемым `null`, а не как
«организация без сайта». В правила добавлен отказ при нескольких собственных
доменах с проверенными цитатами, включая те, которые LLM назвала узкими.
Это сознательно консервативное ограничение прототипа. Проверка этого исправления
отдельно записана в `live-ambiguity-check.json`. Новые входы контроля: ЛитРес,
АШАН и Спортмастер. Модель, промпт и критерий точного ИНН при этом не заменялись.

## Для чего нужна LLM

"""
    text += f"""На {stats["cases"]} синтетических фрагментах сравниваются одни и те же
свидетельства и два решения. В положительном примере организация ведёт сайт;
в отрицательных встречаются клиент, банк в платёжных реквизитах, бывший владелец,
чужой реестр, каталог, узкий проект и инструкция внутри веб-страницы.

| Подход | Правильные ответы | Ложные домены |
| --- | --- | --- |
| Только точный ИНН на странице | {stats["baseline_correct"]} / {stats["cases"]} | {stats["baseline_false_domains"]} |
| LLM и проверка свидетельства | {stats["llm_correct"]} / {stats["cases"]} | {stats["llm_false_domains"]} |

Это целевая проверка ошибок, а не оценка их частоты в интернете. Тексты —
`tests/llm_cases.json`, ответы — `reports/llm-regression.json`. Офлайн-тесты
проверяют выдуманные цитаты, неоднозначность, границы ИНН, приватные адреса,
robots.txt, бюджет расходов, вложенные документы и поведение при сбое поиска.

## Расходы и границы оценки

Тариф из каталога на момент запуска: $0.10/M вход, $0.50/M выход. Стоимость
переданных LLM запросов сохраняется в отчётах и журнале расходов. Сумма для
последних решений по ИНН — ${sum(r["llm_cost_usd"] for r in rows):.5f}; она не
включает предыдущие попытки. Общий журнал учитывает и эксперименты; неизвестная
стоимость незавершённого запроса остаётся зарезервированной. Ни SearXNG,
ни базовый Reader не списывают поисковый API-бюджет; инфраструктура имеет стоимость.

Нет репрезентативной проверки ИП, организаций без подтверждённого сайта, групп
и сайтов без реквизитов. Нет оценки массового сбора и промышленного SLA.
Отказ на положительном эталоне считается пропуском, даже если причина — источник.
Неверный ИНН не считается отрицательным примером компании. `null` при техническом
сбое не засчитывается как правильный ответ.

Первичные ссылки и короткие подтверждения находятся в `reports/decisions.json`.
Полные трассы создаются `--trace` и не включаются в Git. Повторная проверка правил на 14 сохранённых решениях LLM изменила только неоднозначный случай Фотоэксперта; результат — `reports/rule-replay.json`. JSON каждого этапа
содержит время запуска, модель, тариф и SHA-256 исходников, поэтому результаты
можно связать с проверенной версией кода.
"""
    (ROOT / "reports/validation.md").write_text(text)
    (ROOT / "reports/decisions.json").write_text(json.dumps(accepted, ensure_ascii=False, indent=2))
    (ROOT / "reports/latest.json").write_text(
        json.dumps(
            {
                "rows": rows,
                "note": "Один заключительный прогон всех 18 известных входов без локального кеша; ранние контрольные результаты сохранены отдельно.",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("reports/validation.md")


if __name__ == "__main__":
    main()

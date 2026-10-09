"""Собрать ответы в один PDF. При публикации достаточно передать GitHub URL."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from validation_report import load_results, metric

ROOT = Path(__file__).resolve().parents[1]


def register_fonts():
    choices = [
        (
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        ),
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ]
    for regular, bold in choices:
        if Path(regular).exists() and Path(bold).exists():
            pdfmetrics.registerFont(TTFont("Body", regular))
            pdfmetrics.registerFont(TTFont("BodyBold", bold))
            pdfmetrics.registerFontFamily(
                "Body", normal="Body", bold="BodyBold", italic="Body", boldItalic="BodyBold"
            )
            return
    raise RuntimeError("Нужен шрифт Arial или DejaVu Sans с поддержкой кириллицы")


def markup(text):
    value = MarkdownIt("commonmark", {"html": False}).renderInline(text)
    return (
        value.replace("<a href=", '<a color="#235b83" href=')
        .replace("<code>", '<font color="#29475b">')
        .replace("</code>", "</font>")
    )


def results_text():
    stages, rows, regression = load_results()
    h, p, c, n, _errors = metric(rows)
    live = (
        f"В последних результатах для каждого ИНН подтверждено {h} из {p} сайтов, "
        f"ложных доменов: {n - c}. В случае равноправных брендов получен null, "
        "неверный ИНН отклонён до обращения к сети. Это итог нескольких прогонов. "
        "В полном прогоне правильными были 14 из 18 ответов; после исправлений "
        "четыре проблемных случая проверены заново, все четыре прошли. "
        "Остальные 13 сайтов дополнительно проверены текущей моделью на сохранённых "
        "страницах, без нового поиска. Полного повторного прогона финальной версии "
        "по всем 18 входам не было. Неудачные результаты также сохранены."
    )
    stats = regression["summary"]
    reg = (
        f"Отдельно модель проверена на {stats['cases']} подготовленных фрагментах "
        "с неоднозначными упоминаниями ИНН. "
        f"Правильных ответов: {stats['llm_correct']}, "
        f"ложных доменов: {stats['llm_false_domains']}. "
        "Простая проверка наличия ИНН на тех же текстах дала "
        f"{stats['baseline_correct']} правильных ответа и "
        f"{stats['baseline_false_domains']} ложных доменов. "
        "На этих примерах чтение контекста помогает отличить владельца сайта "
        "от стороннего упоминания. Тексты и ответы есть в репозитории."
    )
    return live, reg


def paragraphs(text, styles):
    story, buffer = [], []

    def flush():
        if buffer:
            story.append(KeepTogether([Paragraph(markup(" ".join(buffer)), styles["Text"])]))
            buffer.clear()

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            flush()
        elif line == "<!-- pagebreak -->":
            flush()
            story.append(PageBreak())
        elif line.startswith("# "):
            flush()
            story.extend(
                [
                    Paragraph(markup(line[2:]), styles["Title"]),
                    HRFlowable(width="100%", thickness=1, color=colors.HexColor("#a8bac7")),
                    Spacer(1, 12),
                ]
            )
        elif line.startswith("## "):
            flush()
            story.append(Paragraph(markup(line[3:]), styles["Heading"]))
        elif line.startswith("| "):
            flush()
            table_rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                raw = lines[i].strip()
                if not all(c in "| :-" for c in raw):
                    cells = [
                        Paragraph(markup(c.strip()), styles["Cell"])
                        for c in raw.strip("|").split("|")
                    ]
                    table_rows.append(cells)
                i += 1
            widths = [126, 176, 181] if "Источник" in str(lines[i - len(table_rows) - 1]) else None
            if len(table_rows[0]) == 3 and "Организация" in table_rows[0][0].text:
                widths = [175, 86, 222]
            table = Table(
                table_rows,
                colWidths=widths or [483 / len(table_rows[0])] * len(table_rows[0]),
                repeatRows=1,
                hAlign="LEFT",
            )
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eaf0f4")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#a8bac7")),
                        ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor("#dce3e8")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.extend([table, Spacer(1, 10)])
            continue
        elif line.partition(". ")[0].isdigit() and ". " in line:
            flush()
            story.append(Paragraph(markup(line), styles["Item"]))
        else:
            buffer.append(line)
        i += 1
    flush()
    return story


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Body", 8)
    canvas.setFillColor(colors.HexColor("#637585"))
    canvas.drawString(56, 30, "DaData · Тестовое задание AI/LLM инженера · 10.10.2026")
    canvas.drawRightString(A4[0] - 56, 30, str(doc.page))
    canvas.restoreState()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github-url")
    parser.add_argument("--output", type=Path, default=ROOT.parent / "dadata-solution.pdf")
    args = parser.parse_args()
    if args.github_url:
        parsed = urlsplit(args.github_url)
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or parsed.query
            or parsed.fragment
            or len(parts) != 2
            or any(
                not part
                or any(
                    c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                    for c in part
                )
                for part in parts
            )
        ):
            parser.error("Нужен URL репозитория GitHub")
    repo = (
        f"[GitHub]({args.github_url})"
        if args.github_url
        else "подготовлен локально; GitHub-ссылка будет добавлена после публикации"
    )
    register_fonts()
    base = getSampleStyleSheet()
    styles = {
        "Text": ParagraphStyle(
            "Text", fontName="Body", fontSize=10.1, leading=13.8, spaceAfter=6, alignment=TA_LEFT
        ),
        "Title": ParagraphStyle(
            "TitleRu",
            parent=base["Title"],
            fontName="BodyBold",
            fontSize=19,
            leading=24,
            alignment=TA_LEFT,
            spaceAfter=10,
        ),
        "Heading": ParagraphStyle(
            "HeadingRu",
            fontName="BodyBold",
            fontSize=12.1,
            leading=16,
            spaceBefore=10,
            spaceAfter=7,
            keepWithNext=True,
        ),
        "Cell": ParagraphStyle("Cell", fontName="Body", fontSize=9.1, leading=12.5),
        "Item": ParagraphStyle(
            "Item", fontName="Body", fontSize=10.1, leading=13.8, spaceAfter=5, leftIndent=4
        ),
    }
    theory = (ROOT / "docs/theory.md").read_text()
    practice = (ROOT / "docs/practice.template.md").read_text()
    live, regression = results_text()
    practice = practice.replace("{{REPOSITORY}}", repo).replace("{{LIVE_RESULTS}}", live)
    practice = practice.replace("{{REGRESSION_RESULTS}}", regression)
    checks = json.loads((ROOT / "reports/checks.json").read_text())
    practice = practice.replace("{{OFFLINE_TESTS}}", str(checks["pytest"]["passed"]))
    if "{{" in practice:
        raise ValueError("Не все поля PDF заполнены")
    (ROOT / "docs/practice.md").write_text(practice)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(args.output),
        pagesize=A4,
        leftMargin=50,
        rightMargin=50,
        topMargin=38,
        bottomMargin=42,
        title="DaData: сайты организаций",
        author="",
        subject="Тестовое задание AI/LLM инженера",
    )
    story = paragraphs(theory, styles) + [PageBreak()] + paragraphs(practice, styles)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(args.output)


if __name__ == "__main__":
    main()

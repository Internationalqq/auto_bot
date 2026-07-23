from __future__ import annotations

import ast
import re
from pathlib import Path

from jinja2 import Environment


module = ast.parse(Path("autobot/web_ui.py").read_text(encoding="utf-8"))
template_source = next(
    ast.literal_eval(node.value)
    for node in module.body
    if isinstance(node, ast.Assign)
    and len(node.targets) == 1
    and isinstance(node.targets[0], ast.Name)
    and node.targets[0].id == "INDEX_TEMPLATE"
)
template = Environment(autoescape=True).from_string(template_source)

cards = []
for has_estimate, has_merge_report, has_svodka in (
    (True, True, True),
    (True, False, False),
    (False, False, False),
):
    cards.append(
        {
            "tender_id": str(len(cards) + 1),
            "display_title": "Тестовая закупка",
            "region": "Регион",
            "eis_url": "https://example.com",
            "has_report": has_estimate,
            "has_display_data": has_estimate,
            "has_estimate": has_estimate,
            "has_merge_report": has_merge_report,
            "has_svodka": has_svodka,
            "report_file": "x",
            "stage_open": True,
            "stage_display": "Подача заявок",
            "estimate_rows": 12 if has_estimate else 0,
            "publish_date": "21.07.2026",
        }
    )

coverage = {
    "tender_count": 3,
    "tenders_missing_merge_html": 2,
    "merge_html_among_tenders": 1,
    "svodka_xlsx_count": 1,
    "missing_no_svodka": 1,
    "missing_no_estimate": 1,
    "missing_no_html": 0,
}
rendered = template.render(
    grouped=[("Регион", cards)],
    rebuild_options=cards,
    coverage=coverage,
    show_all=False,
    sort_mode="publish_desc",
    visible_count=3,
    tender_count=3,
    display_report_count=2,
    report_count=2,
)

ids = re.findall(r'\bid="([^"]+)"', rendered)
assert len(ids) == len(set(ids))
assert "showParseLaunchFeedback()" in rendered
assert 'role="status" aria-live="polite"' in rendered
assert ".page > .action-hub { order: 1; }" in rendered
assert rendered.count("<details") == rendered.count("</details>")
assert rendered.count("<section") == rendered.count("</section>")

print("web_ui_render_ok", f"html={len(rendered)}", f"ids={len(ids)}")

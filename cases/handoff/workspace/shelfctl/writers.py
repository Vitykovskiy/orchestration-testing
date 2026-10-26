"""Existing public renderers; their bytes form part of the CLI contract."""

import json


def render_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, separators=(",", ":")) + "\n"


def render_text(report: dict) -> str:
    lines = ["Склад\tSKU\tВ наличии\tРезерв\tДоступно"]
    for row in report["rows"]:
        lines.append("\t".join(str(row[key]) for key in ("warehouse", "sku", "on_hand", "reserved", "available")))
    return "\n".join(lines) + "\n"

"""Validate a snapshot and combine repeated inventory positions."""

from __future__ import annotations

import json
from pathlib import Path


class InventoryError(ValueError):
    pass


def load_report(path: Path, warehouse: str | None = None) -> dict:
    try:
        source = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryError("invalid snapshot") from error
    if not isinstance(source, dict) or not isinstance(source.get("snapshot"), str) or not source["snapshot"]:
        raise InventoryError("snapshot must have a nonempty name")
    if not isinstance(source.get("items"), list):
        raise InventoryError("items must be an array")
    combined = {}
    for item in source["items"]:
        if not isinstance(item, dict):
            raise InventoryError("item must be an object")
        if any(not isinstance(item.get(key), str) or not item[key] for key in ("warehouse", "sku")):
            raise InventoryError("warehouse and sku must be nonempty strings")
        if any(type(item.get(key)) is not int or item[key] < 0 for key in ("on_hand", "reserved")):
            raise InventoryError("quantities must be nonnegative integers")
        if item["reserved"] > item["on_hand"]:
            raise InventoryError("reserved exceeds on_hand")
        key = (item["warehouse"], item["sku"])
        if key not in combined:
            combined[key] = {"warehouse": key[0], "sku": key[1], "on_hand": 0, "reserved": 0}
        combined[key]["on_hand"] += item["on_hand"]
        combined[key]["reserved"] += item["reserved"]
    rows = []
    for item in combined.values():
        if warehouse is None or item["warehouse"] == warehouse:
            rows.append(dict(item, available=item["on_hand"] - item["reserved"]))
    totals = {key: sum(row[key] for row in rows) for key in ("on_hand", "reserved", "available")}
    return {"snapshot": source["snapshot"], "rows": rows, "totals": totals}

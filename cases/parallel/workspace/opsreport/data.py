"""Existing input contract shared by independently registered reports."""

from datetime import datetime, timezone
import json
from pathlib import Path


def load_events(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise ValueError("input must contain an events array")
    identifiers = set()
    result = []
    for raw in payload["events"]:
        if not isinstance(raw, dict):
            raise ValueError("event must be an object")
        event = dict(raw)
        for key in ("id", "service", "timestamp", "route"):
            if not isinstance(event.get(key), str) or not event[key]:
                raise ValueError(f"{key} must be a nonempty string")
        if not event["route"].startswith("/"):
            raise ValueError("route must begin with /")
        if event["id"] in identifiers:
            raise ValueError("event ids must be unique")
        identifiers.add(event["id"])
        if event.get("status") not in ("ok", "error"):
            raise ValueError("status must be ok or error")
        for key in ("duration_ms", "bytes"):
            if type(event.get(key)) is not int or event[key] < 0:
                raise ValueError(f"{key} must be a nonnegative integer")
        try:
            stamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("invalid timestamp") from error
        if stamp.tzinfo is None or stamp.utcoffset() is None or stamp.microsecond:
            raise ValueError("timestamp must have timezone and whole seconds")
        event["at"] = stamp.astimezone(timezone.utc)
        result.append(event)
    return result

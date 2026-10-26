import argparse
import json
import sys

from .data import load_events
from .reports import REGISTRY


def main():
    parser = argparse.ArgumentParser(description="Operational event reports")
    parser.add_argument("--input")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("reports")
    commands.add_parser("check")
    report = commands.add_parser("report")
    report.add_argument("name", choices=(*REGISTRY, "all"))
    args = parser.parse_args()
    if args.command == "reports":
        print(json.dumps(list(REGISTRY)))
        return 0
    if not args.input:
        parser.error("--input is required")
    try:
        events = load_events(args.input)
        if args.command == "check":
            value = {"events": len(events), "services": len({event["service"] for event in events})}
        elif args.name == "all":
            value = {name: generate(events) for name, generate in REGISTRY.items()}
        else:
            value = REGISTRY[args.name](events)
        if args.command == "report" and args.name == "audit":
            sys.stdout.write(value)
        else:
            print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, ValueError, NotImplementedError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2

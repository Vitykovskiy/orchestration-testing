"""Command line interface for inventory snapshot reports."""

import argparse
import json
from pathlib import Path
import sys

from .inventory import InventoryError, load_report
from .writers import render_json, render_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shelfctl")
    parser.add_argument("--input", required=True, type=Path)
    subcommands = parser.add_subparsers(dest="command", required=True)
    report = subcommands.add_parser("report")
    report.add_argument("--format", choices=("json", "text"), default="json")
    report.add_argument("--warehouse")
    args = parser.parse_args(argv)
    try:
        data = load_report(args.input, args.warehouse)
    except InventoryError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    renderer = {"json": render_json, "text": render_text}[args.format]
    print(renderer(data), end="")
    return 0

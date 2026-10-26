"""Command-line boundary for stockctl."""

import argparse
import json
import sqlite3
import sys

from . import service
from .exporters import movements_csv, stock_json
from .repository import connect


def build_parser():
    parser = argparse.ArgumentParser(prog="stockctl")
    parser.add_argument("--db", required=True, help="SQLite database path")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")

    stock = commands.add_parser("stock").add_subparsers(dest="stock_command", required=True)
    add = stock.add_parser("add")
    add.add_argument("--warehouse", required=True)
    add.add_argument("--sku", required=True)
    add.add_argument("--qty", required=True, type=int)
    listing = stock.add_parser("list")
    listing.add_argument("--warehouse", required=True)
    listing.add_argument("--format", choices=("text", "json"), default="text")

    reserve = commands.add_parser("reserve")
    reserve.add_argument("--warehouse", required=True)
    reserve.add_argument("--sku", required=True)
    reserve.add_argument("--qty", required=True, type=int)

    for name in ("cancel", "ship"):
        commands.add_parser(name).add_argument("id", type=int)

    exports = commands.add_parser("export").add_subparsers(dest="export_command", required=True)
    exports.add_parser("stock").add_argument("--format", choices=("json",), required=True)
    exports.add_parser("movements").add_argument("--format", choices=("csv",), required=True)
    return parser


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def execute(args, connection):
    if args.command == "init":
        return json_text({"status": "initialized"})
    if args.command == "stock":
        if args.stock_command == "add":
            return json_text(service.add_stock(connection, args.warehouse, args.sku, args.qty))
        items = service.list_stock(connection, args.warehouse)
        if args.format == "json":
            return json_text(items)
        lines = ["Склад\tSKU\tНа складе\tЗарезервировано\tДоступно"]
        lines.extend(
            f"{item['warehouse']}\t{item['sku']}\t{item['on_hand']}\t"
            f"{item['reserved']}\t{item['available']}"
            for item in items
        )
        return "\n".join(lines)
    if args.command == "reserve":
        return json_text(service.reserve(connection, args.warehouse, args.sku, args.qty))
    if args.command == "cancel":
        return json_text(service.cancel(connection, args.id))
    if args.command == "ship":
        return json_text(service.ship(connection, args.id))
    if args.export_command == "stock":
        return stock_json.export(connection)
    return movements_csv.export(connection)


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        with connect(args.db) as connection:
            output = execute(args, connection)
    except (ValueError, NotImplementedError, sqlite3.Error) as error:
        print(json_text({"error": str(error)}), file=sys.stderr)
        return 2
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0

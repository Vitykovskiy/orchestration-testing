"""SQLite persistence and the original database format."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS stock (
    warehouse TEXT NOT NULL,
    sku TEXT NOT NULL,
    on_hand INTEGER NOT NULL,
    reserved INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (warehouse, sku)
);
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse TEXT NOT NULL,
    sku TEXT NOT NULL,
    qty INTEGER NOT NULL,
    status TEXT NOT NULL,
    FOREIGN KEY (warehouse, sku) REFERENCES stock (warehouse, sku)
);
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER,
    warehouse TEXT NOT NULL,
    sku TEXT NOT NULL,
    delta INTEGER NOT NULL,
    kind TEXT NOT NULL,
    FOREIGN KEY (reservation_id) REFERENCES reservations (id)
);
"""


@contextmanager
def connect(path: str | Path):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.executescript(SCHEMA)
        with connection:
            yield connection
    finally:
        connection.close()


def stock_record(connection, warehouse: str, sku: str):
    row = connection.execute(
        "SELECT warehouse, sku, on_hand, reserved FROM stock "
        "WHERE warehouse = ? AND sku = ?",
        (warehouse, sku),
    ).fetchone()
    if row is None:
        raise ValueError("Stock item does not exist")
    return stock_payload(row)


def stock_payload(row):
    result = dict(row)
    result["available"] = result["on_hand"] - result["reserved"]
    return result


def reservation_record(connection, reservation_id: int):
    row = connection.execute(
        "SELECT id, warehouse, sku, qty, status FROM reservations WHERE id = ?",
        (reservation_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Reservation does not exist")
    return dict(row)


def add_movement(connection, warehouse, sku, delta, kind, reservation_id=None):
    connection.execute(
        "INSERT INTO movements (reservation_id, warehouse, sku, delta, kind) "
        "VALUES (?, ?, ?, ?, ?)",
        (reservation_id, warehouse, sku, delta, kind),
    )

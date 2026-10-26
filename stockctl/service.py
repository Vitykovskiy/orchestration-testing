"""Inventory commands, independent of the command-line presentation."""

from .repository import (
    add_movement,
    reservation_record,
    stock_payload,
    stock_record,
)


def require_positive(qty: int):
    if qty <= 0:
        raise ValueError("Quantity must be positive")


def require_identity(warehouse: str, sku: str):
    if not warehouse or not sku:
        raise ValueError("Warehouse and SKU must not be empty")


def add_stock(connection, warehouse: str, sku: str, qty: int):
    require_positive(qty)
    require_identity(warehouse, sku)
    connection.execute(
        "INSERT INTO stock (warehouse, sku, on_hand, reserved) VALUES (?, ?, ?, 0) "
        "ON CONFLICT (warehouse, sku) DO UPDATE SET on_hand = on_hand + excluded.on_hand",
        (warehouse, sku, qty),
    )
    add_movement(connection, warehouse, sku, qty, "add")
    return stock_record(connection, warehouse, sku)


def list_stock(connection, warehouse: str):
    rows = connection.execute(
        "SELECT warehouse, sku, on_hand, reserved FROM stock ORDER BY warehouse, sku"
    ).fetchall()
    return [stock_payload(row) for row in rows]


def reserve(connection, warehouse: str, sku: str, qty: int):
    require_positive(qty)
    require_identity(warehouse, sku)
    item = stock_record(connection, warehouse, sku)
    if item["available"] < qty:
        raise ValueError("Insufficient available stock")
    cursor = connection.execute(
        "INSERT INTO reservations (warehouse, sku, qty, status) VALUES (?, ?, ?, 'active')",
        (warehouse, sku, qty),
    )
    reservation_id = cursor.lastrowid
    connection.execute(
        "UPDATE stock SET reserved = reserved + ? WHERE warehouse = ? AND sku = ?",
        (qty, warehouse, sku),
    )
    add_movement(connection, warehouse, sku, 0, "reserve", reservation_id)
    return reservation_record(connection, reservation_id)


def cancel(connection, reservation_id: int):
    reservation = reservation_record(connection, reservation_id)
    if reservation["status"] == "shipped":
        raise ValueError("A shipped reservation cannot be cancelled")
    connection.execute(
        "UPDATE stock SET reserved = reserved - ? WHERE warehouse = ? AND sku = ?",
        (reservation["qty"], reservation["warehouse"], reservation["sku"]),
    )
    connection.execute(
        "UPDATE reservations SET status = 'cancelled' WHERE id = ?", (reservation_id,)
    )
    add_movement(
        connection, reservation["warehouse"], reservation["sku"], 0, "cancel", reservation_id
    )
    return reservation_record(connection, reservation_id)


def ship(connection, reservation_id: int):
    reservation = reservation_record(connection, reservation_id)
    if reservation["status"] == "shipped":
        return reservation
    if reservation["status"] != "active":
        raise ValueError("Only an active reservation can be shipped")
    connection.execute(
        "UPDATE stock SET on_hand = on_hand - ?, reserved = reserved - ? "
        "WHERE warehouse = ? AND sku = ?",
        (
            reservation["qty"],
            reservation["qty"],
            reservation["warehouse"],
            reservation["sku"],
        ),
    )
    connection.execute(
        "UPDATE reservations SET status = 'shipped' WHERE id = ?", (reservation_id,)
    )
    add_movement(
        connection,
        reservation["warehouse"],
        reservation["sku"],
        -reservation["qty"],
        "ship",
        reservation_id,
    )
    return reservation_record(connection, reservation_id)

"""Synthetic Northstar Outfitters warehouse in SQLite.

Every row is generated from a fixed seed. There is no live store, no real
customer, and no employer dataset behind it. The shape mirrors what a
local analytics mirror of a commerce platform looks like: order facts,
a separate refund ledger, daily ad spend, daily sessions, and a record of
how far each sync has actually reached.

Two details are deliberate because they are where real dashboards lie:

* `orders.refunded_snapshot` is the refund total *as of when the order was
  last synced*. Refunds issued weeks later never reach it. The `refunds`
  ledger is complete. Net revenue must come from the ledger.
* Each source has its own `synced_through` date. A dashboard that sums
  "last 7 days" across sources that stopped at different days is comparing
  a full week of spend with five days of revenue.
"""

from __future__ import annotations

import random
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta

START = date(2026, 1, 1)
AS_OF = date(2026, 9, 28)

PRODUCTS = {
    "trail-flask": 34.0,
    "alpine-shell": 189.0,
    "summit-pack": 149.0,
    "ridge-headlamp": 42.0,
}
CHANNELS = ("search", "social", "email", "direct", "affiliate")
PAID_CHANNELS = ("search", "social", "affiliate")

# How far each sync reached. Ads lag a day behind the store on purpose.
SYNCED_THROUGH = {
    "orders": AS_OF - timedelta(days=1),
    "refunds": AS_OF - timedelta(days=1),
    "ad_spend": AS_OF - timedelta(days=2),
    "sessions": AS_OF - timedelta(days=1),
}

SCHEMA = """
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    first_order_date TEXT NOT NULL
);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date TEXT NOT NULL,
    channel TEXT NOT NULL,
    product TEXT NOT NULL,
    gross REAL NOT NULL,
    is_first_order INTEGER NOT NULL,
    refunded_snapshot REAL NOT NULL DEFAULT 0
);
CREATE TABLE refunds (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    refund_date TEXT NOT NULL,
    amount REAL NOT NULL
);
CREATE TABLE ad_spend (
    day TEXT NOT NULL,
    channel TEXT NOT NULL,
    product TEXT NOT NULL,
    spend REAL NOT NULL,
    PRIMARY KEY (day, channel, product)
);
CREATE TABLE sessions (
    day TEXT NOT NULL,
    channel TEXT NOT NULL,
    sessions INTEGER NOT NULL,
    PRIMARY KEY (day, channel)
);
CREATE TABLE sync_state (
    source TEXT PRIMARY KEY,
    synced_through TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX orders_date ON orders(order_date);
CREATE INDEX refunds_order ON refunds(order_id);
"""


class Warehouse:
    """One connection, one lock. SQLite is the demo; the contract is the point."""

    def __init__(self, path: str = ":memory:", *, seed: int = 7) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        if not self._has_schema():
            self._conn.executescript(SCHEMA)
            _seed(self._conn, seed)

    def _has_schema(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_state'"
        ).fetchone()
        return row is not None

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            finally:
                cur.close()

    def query(self, sql: str, params: list[object]) -> list[sqlite3.Row]:
        with self.cursor() as cur:
            return cur.execute(sql, params).fetchall()

    def sync_state(self) -> dict[str, dict[str, object]]:
        rows = self.query("SELECT source, synced_through, generation FROM sync_state", [])
        return {
            row["source"]: {
                "synced_through": row["synced_through"],
                "generation": row["generation"],
            }
            for row in rows
        }

    def record_sync(self, source: str, synced_through: date) -> int:
        """A successful sync advances the generation. Caches keyed on it go dark."""
        with self.cursor() as cur:
            cur.execute(
                "UPDATE sync_state SET synced_through = ?, generation = generation + 1 "
                "WHERE source = ?",
                [synced_through.isoformat(), source],
            )
            row = cur.execute(
                "SELECT generation FROM sync_state WHERE source = ?", [source]
            ).fetchone()
        if row is None:
            raise KeyError(source)
        return int(row["generation"])

    def add_refund(self, order_id: int, refund_date: date, amount: float) -> None:
        """A late refund lands in the ledger only, exactly like production."""
        with self.cursor() as cur:
            cur.execute(
                "INSERT INTO refunds (order_id, refund_date, amount) VALUES (?, ?, ?)",
                [order_id, refund_date.isoformat(), amount],
            )


def _seed(conn: sqlite3.Connection, seed: int) -> None:
    rng = random.Random(seed)
    customers: list[tuple[int, str]] = []
    orders: list[tuple[object, ...]] = []
    refunds: list[tuple[object, ...]] = []
    ads: list[tuple[object, ...]] = []
    sessions: list[tuple[object, ...]] = []
    order_id = 0

    day = START
    last_order_day = SYNCED_THROUGH["orders"]
    while day <= last_order_day:
        stamp = day.isoformat()
        weekend = day.weekday() >= 5
        season = 1.0 + 0.35 * (day.month in (8, 9))
        for channel in CHANNELS:
            base = {"search": 18, "social": 14, "email": 9, "direct": 11, "affiliate": 5}[channel]
            count = max(0, int(rng.gauss(base * season * (0.75 if weekend else 1.0), 2.5)))
            for _ in range(count):
                product = rng.choices(list(PRODUCTS), weights=(5, 2, 2, 4))[0]
                gross = round(PRODUCTS[product] * rng.choice((1, 1, 1, 2)), 2)
                returning = customers and rng.random() < 0.32
                if returning:
                    customer_id = rng.choice(customers)[0]
                    first = False
                else:
                    customer_id = len(customers) + 1
                    customers.append((customer_id, stamp))
                    first = True
                order_id += 1
                snapshot = 0.0
                if rng.random() < 0.06:
                    lag = rng.randint(0, 45)
                    refund_day = day + timedelta(days=lag)
                    if refund_day <= SYNCED_THROUGH["refunds"]:
                        amount = gross if rng.random() < 0.7 else round(gross / 2, 2)
                        refunds.append((order_id, refund_day.isoformat(), amount))
                        # Refunds within 3 days were caught by the order sync;
                        # later ones exist only in the ledger.
                        if lag <= 3:
                            snapshot = amount
                orders.append((order_id, customer_id, stamp, channel, product, gross, int(first), snapshot))
            sessions.append((stamp, channel, int(count * rng.uniform(38, 52)) + 40))
        if day <= SYNCED_THROUGH["ad_spend"]:
            for channel in PAID_CHANNELS:
                for product, price in PRODUCTS.items():
                    spend = round(price * rng.uniform(0.6, 1.4) * (3 if channel == "search" else 2), 2)
                    ads.append((stamp, channel, product, spend))
        day += timedelta(days=1)

    conn.executemany("INSERT INTO customers VALUES (?, ?)", customers)
    conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", orders)
    conn.executemany(
        "INSERT INTO refunds (order_id, refund_date, amount) VALUES (?, ?, ?)", refunds
    )
    conn.executemany("INSERT INTO ad_spend VALUES (?, ?, ?, ?)", ads)
    conn.executemany("INSERT INTO sessions VALUES (?, ?, ?)", sessions)
    conn.executemany(
        "INSERT INTO sync_state (source, synced_through) VALUES (?, ?)",
        [(source, through.isoformat()) for source, through in SYNCED_THROUGH.items()],
    )
    conn.commit()

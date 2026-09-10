"""Availability tracking for Hot Aisle shapes: collect ``/available/`` listings
into a SQLite history and produce "when is this shape free" summaries.

Read-only by design: this module never creates, deletes or reserves anything. It
only reads the available listings and records what it saw.

Schema
------
    shapes   : one row per stable shape (kind + canonical specs). A shape that
               disappears from listings is *kept* here so its history stays
               continuous; it's just recorded with quantity 0 once absent.
    samples  : one row per (shape, timestamp) observation, with the raw quantity
               and the on-demand price in cents when the API provided one.

Stdlib only (``sqlite3``, ``time``, ``json``) -- no third-party deps.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .models import AvailableType

DEFAULT_DB = "~/.local/state/hotaisle/availability.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shapes (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,              -- 'vm' | 'bm'
    label       TEXT NOT NULL,              -- human readable shape label
    cpu_cores   INTEGER,
    ram_bytes   INTEGER,
    disk_bytes  INTEGER,
    gpu_models  TEXT,                    -- JSON list, e.g. ["8x NVIDIA MI300X"]
    gpu_count   INTEGER,
    min_reservation_minutes INTEGER,
    UNIQUE (kind, label)
);

CREATE TABLE IF NOT EXISTS samples (
    id           INTEGER PRIMARY KEY,
    shape_id     INTEGER NOT NULL REFERENCES shapes(id),
    ts           REAL NOT NULL,            -- unix epoch seconds
    quantity     INTEGER NOT NULL,         -- 0 when absent from the listing
    price_cents  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_shape_ts ON samples(shape_id, ts);
"""


def default_db_path() -> str:
    from pathlib import Path

    return str(Path(DEFAULT_DB).expanduser())


def shape_key_of(avail: AvailableType, kind: str) -> Dict[str, Any]:
    """Canonical identifying fields for a shape, used as its stable identity."""
    s = avail.specs
    gpus = [
        {"count": g.count, "manufacturer": g.manufacturer, "model": g.model}
        for g in s.gpus
    ]
    return {
        "kind": kind,
        "label": avail.label or s.full_label,
        "cpu_cores": s.cpu_cores,
        "ram_bytes": s.ram_capacity,
        "disk_bytes": s.disk_capacity,
        "gpu_models": json.dumps(gpus, sort_keys=True),
        "gpu_count": s.gpu_count,
        "min_reservation_minutes": avail.minimum_reservation_minutes,
    }


class AvailabilityDB:
    """Wraps a SQLite connection with a small, safe public surface.

    Single-writer / single-process by default; a coarse lock makes concurrent reads
    (e.g. from the HTTP view while ``watch`` writes) safe.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = os.path.expanduser(path) if path else default_db_path()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            yield self.conn
            self.conn.commit()

    def _upsert_shape(self, key: Dict[str, Any]) -> int:
        with self._write() as conn:
            row = conn.execute(
                "SELECT id FROM shapes WHERE kind=? AND label=?",
                (key["kind"], key["label"]),
            ).fetchone()
            if row:
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO shapes (kind, label, cpu_cores, ram_bytes, disk_bytes,"
                " gpu_models, gpu_count, min_reservation_minutes)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (key["kind"], key["label"], key["cpu_cores"], key["ram_bytes"],
                 key["disk_bytes"], key["gpu_models"], key["gpu_count"],
                 key["min_reservation_minutes"]),
            )
            return int(cur.lastrowid)

    def record(
        self,
        kind: str,
        listings: Iterable[AvailableType],
        ts: Optional[float] = None,
    ) -> int:
        """Record a full ``/available/`` listing snapshot.

        Shapes already known from prior sweeps but *absent* this sweep are recorded
        with quantity 0 so their availability windows stay continuous.
        """
        ts = ts if ts is not None else time.time()
        seen_ids: List[int] = []
        for avail in listings:
            key = shape_key_of(avail, kind)
            sid = self._upsert_shape(key)
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO samples (shape_id, ts, quantity, price_cents)"
                    " VALUES (?,?,?,?)",
                    (sid, ts, int(avail.quantity or 0),
                     avail.on_demand_price),
                )
            seen_ids.append(sid)
        # Any shape recorded for this kind before but absent now -> quantity 0.
        with self._write() as conn:
            known = conn.execute(
                "SELECT id FROM shapes WHERE kind=?", (kind,)
            ).fetchall()
            for row in known:
                if int(row["id"]) not in seen_ids:
                    conn.execute(
                        "INSERT INTO samples (shape_id, ts, quantity, price_cents)"
                        " VALUES (?,?,?,NULL)",
                        (int(row["id"]), ts, 0),
                    )
        return len(seen_ids)

    def prune(self, keep_seconds: float) -> int:
        """Delete samples older than ``keep_seconds``; return rows removed."""
        cutoff = time.time() - keep_seconds
        with self._write() as conn:
            cur = conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
            return cur.rowcount

    # ------------------------------------------------------------------ query

    def shapes(self, kind: Optional[str] = None) -> List[sqlite3.Row]:
        q = "SELECT * FROM shapes"
        args: tuple = ()
        if kind:
            q += " WHERE kind=?"
            args = (kind,)
        with self.lock:
            return list(self.conn.execute(q + " ORDER BY kind, label", args))

    def sample_times(self, since: float) -> List[float]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT ts FROM samples WHERE ts >= ? ORDER BY ts", (since,)
            ).fetchall()
            return [float(r["ts"]) for r in rows]

    def series(self, shape_id: int, since: float) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, quantity, price_cents FROM samples"
                " WHERE shape_id=? AND ts >= ? ORDER BY ts",
                (shape_id, since),
            ).fetchall()
            return [{"ts": float(r["ts"]), "quantity": int(r["quantity"]),
                     "price_cents": r["price_cents"]} for r in rows]

    def last_sampled(self) -> Optional[float]:
        with self.lock:
            row = self.conn.execute("SELECT MAX(ts) AS m FROM samples").fetchone()
            return float(row["m"]) if row and row["m"] is not None else None

    def __enter__(self) -> "AvailabilityDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


@dataclass
class ShapeStats:
    shape_id: int
    kind: str
    label: str
    gpu_count: int
    avails: int
    observations: int
    availability_pct: float
    first_seen: Optional[float]
    last_seen: Optional[float]
    last_quantity: int
    max_quantity: int


def summarize(db: AvailabilityDB, since: float, kind: Optional[str] = None
              ) -> List[ShapeStats]:
    """Aggregate observations per shape into per-shape availability stats."""
    out: List[ShapeStats] = []
    for shape in db.shapes(kind=kind):
        sid = int(shape["id"])
        rows = db.series(sid, since)
        if not rows:
            continue
        avails = sum(1 for r in rows if r["quantity"] > 0)
        out.append(ShapeStats(
            shape_id=sid,
            kind=shape["kind"],
            label=shape["label"],
            gpu_count=shape["gpu_count"] or 0,
            avails=avails,
            observations=len(rows),
            availability_pct=100.0 * avails / len(rows) if rows else 0.0,
            first_seen=rows[0]["ts"],
            last_seen=rows[-1]["ts"],
            last_quantity=rows[-1]["quantity"],
            max_quantity=max(r["quantity"] for r in rows),
        ))
    return out

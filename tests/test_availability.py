"""Offline tests for the availability tracker (SQLite history + summary).

Uses a temp DB; never touches the real API, no API key, no network.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hotaisle.availability import AvailabilityDB, ShapeStats, default_db_path, summarize  # noqa: E402
from hotaisle.models import AvailableType  # noqa: E402


def _avail(label, quantity, gpu_count=0, cpu=2, ram=2 ** 30, disk=10 * 2 ** 30,
           price=100, min_res=30, gpu_model=None):
    specs = {"cpu_cores": cpu, "ram_capacity": ram, "disk_capacity": disk,
             "gpus": [{"count": gpu_count, "manufacturer": "AMD",
                       "model": gpu_model or ("MI300X" if gpu_count else "CPU")}]
             if gpu_count else []}
    return AvailableType.from_dict({
        "Quantity": quantity, "OnDemandPrice": price,
        "MinimumReservationMinutes": min_res, "Specs": specs,
        "Name": "test-%s" % label,
    })


class AvailabilityTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "avail.db")
        self.db = AvailabilityDB(self.db_path)

    def tearDown(self):
        self.db.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_schema_init_creates_tables(self):
        # Closing and reopening yields the same schema without error.
        self.db.close()
        self.db = AvailabilityDB(self.db_path)
        shapes = self.db.shapes()
        self.assertEqual(list(shapes), [])

    def test_record_creates_shape_and_sample(self):
        n = self.db.record("vm", [_avail("a", 2)], ts=1000.0)
        self.assertEqual(n, 1)
        shapes = self.db.shapes()
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0]["kind"], "vm")
        rows = self.db.series(int(shapes[0]["id"]), since=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["quantity"], 2)

    def test_shape_upsert_is_stable_for_same_label(self):
        # Recording the same shape twice must reuse the same shape_id, not duplicate.
        self.db.record("vm", [_avail("a", 1)], ts=1000.0)
        self.db.record("vm", [_avail("a", 3)], ts=1001.0)
        shapes = self.db.shapes()
        self.assertEqual(len(shapes), 1)
        rows = self.db.series(int(shapes[0]["id"]), since=0)
        self.assertEqual([r["quantity"] for r in rows], [1, 3])

    def test_absent_shape_recorded_as_zero(self):
        # A shape seen before but missing from this sweep -> quantity 0, not dropped.
        self.db.record("bm", [_avail("node", 2)], ts=1000.0)
        self.db.record("bm", [], ts=1005.0)  # node vanished
        shapes = self.db.shapes()
        self.assertEqual(len(shapes), 1)
        rows = self.db.series(int(shapes[0]["id"]), since=0)
        self.assertEqual([(r["ts"], r["quantity"]) for r in rows],
                         [(1000.0, 2), (1005.0, 0)])

    def test_vm_and_bm_are_separate_kinds(self):
        self.db.record("vm", [_avail("same", 1)], ts=1000.0)
        self.db.record("bm", [_avail("same", 5)], ts=1000.0)
        shapes = self.db.shapes()
        self.assertEqual(len(shapes), 2)
        self.assertEqual({s["kind"] for s in shapes}, {"vm", "bm"})

    def test_prune_removes_old_only(self):
        self.db.record("vm", [_avail("a", 1)], ts=time.time() - 40000)
        self.db.record("vm", [_avail("a", 2)], ts=time.time() - 100)
        removed = self.db.prune(keep_seconds=3600)
        self.assertEqual(removed, 1)
        shapes = self.db.shapes()
        rows = self.db.series(int(shapes[0]["id"]), since=0)
        self.assertEqual(len(rows), 1)

    def test_summary_computes_pct_and_rare(self):
        # Two sweeps. In both, "common" is present; "rare" only in the first.
        self.db.record("vm", [_avail("common", 3), _avail("rare", 1)],
                      ts=time.time() - 600)
        self.db.record("vm", [_avail("common", 2)],
                      ts=time.time())
        stats = summarize(self.db, since=time.time() - 3600)
        by_label = {s.label: s for s in stats}
        self.assertIn("test-common", by_label)
        self.assertIn("test-rare", by_label)
        # common appears in both sweeps (rare absent -> 0 handled); rare only saw
        # quantity once.
        self.assertEqual(by_label["test-common"].availability_pct, 100.0)
        self.assertEqual(by_label["test-rare"].availability_pct, 50.0)

    def test_kind_filter_in_summary(self):
        self.db.record("vm", [_avail("a", 1)], ts=time.time())
        self.db.record("bm", [_avail("b", 1)], ts=time.time())
        stats = summarize(self.db, since=0, kind="vm")
        self.assertEqual([s.label for s in stats], ["test-a"])
        stats = summarize(self.db, since=0, kind="bm")
        self.assertEqual([s.label for s in stats], ["test-b"])

    def test_last_sampled(self):
        self.assertIsNone(self.db.last_sampled())
        self.db.record("vm", [_avail("a", 1)], ts=1234.0)
        self.assertEqual(self.db.last_sampled(), 1234.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

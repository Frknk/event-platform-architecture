from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from app.core.problem import ProblemException
from app.core.security import generate_uuidv7
from app.db.database import DEFAULT_EVENT_ID, DEFAULT_ORG_ID, ParticipationDatabase
from app.services.audit_service import AuditService
from app.services.participation_service import ParticipationService


class EventReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ParticipationDatabase(
            db_path=str(Path(self.temp_dir.name) / "participation.db")
        )
        self.service = ParticipationService(self.db, AuditService())
        self.aggregate_id = generate_uuidv7()

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def _envelope(self, version: int, *, event_id: str | None = None) -> dict:
        return {
            "message_id": generate_uuidv7(),
            "domain_event_id": event_id or generate_uuidv7(),
            "topic": "configuration.event.release_published.v1",
            "schema_version": 1,
            "producer": "event-configuration",
            "organization_id": DEFAULT_ORG_ID,
            "business_event_id": DEFAULT_EVENT_ID,
            "occurred_at": "2026-11-11T00:00:00Z",
            "aggregate": {
                "type": "event",
                "id": self.aggregate_id,
                "version": version,
            },
            "data": {"capacity": 7, "waitlist_enabled": True},
        }

    def _inbox_versions(self) -> list[int]:
        with self.db.read_cursor() as cur:
            cur.execute(
                "SELECT aggregate_version FROM inbox WHERE consumer_name = 'participation' "
                "ORDER BY aggregate_version"
            )
            return [row["aggregate_version"] for row in cur.fetchall()]

    def test_release_sequence_gap_is_persisted_then_resolved_by_missing_version(self) -> None:
        first = self._envelope(1)
        self.assertEqual(self.service.ingest_release_published(first)["status"], "materialized")
        gap_event = self._envelope(3)

        with self.assertRaises(ProblemException) as raised:
            self.service.ingest_release_published(gap_event)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(raised.exception.code, "release_gap")
        self.assertEqual(self._inbox_versions(), [1])

        with self.db.read_cursor() as cur:
            cur.execute("SELECT * FROM event_gaps")
            gap = dict(cur.fetchone())
        self.assertEqual(gap["expected_version"], 2)
        self.assertEqual(gap["observed_version"], 3)
        self.assertEqual(gap["state"], "open")

        second = self._envelope(2)
        self.assertEqual(self.service.ingest_release_published(second)["status"], "materialized")
        self.assertEqual(self.service.ingest_release_published(gap_event)["status"], "materialized")
        self.assertEqual(self._inbox_versions(), [1, 2, 3])

        with self.db.read_cursor() as cur:
            cur.execute("SELECT state, resolved_at FROM event_gaps")
            resolved = cur.fetchone()
        self.assertEqual(resolved["state"], "resolved")
        self.assertIsNotNone(resolved["resolved_at"])

    def test_concurrent_duplicate_event_is_applied_once(self) -> None:
        envelope = self._envelope(1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                self.service.ingest_release_published,
                (envelope, envelope),
            ))
        self.assertEqual(
            sorted(result["status"] for result in results),
            ["duplicate_ignored", "materialized"],
        )
        self.assertEqual(self._inbox_versions(), [1])

    def test_invalid_or_unversioned_envelopes_are_not_fabricated(self) -> None:
        incomplete = self._envelope(1)
        del incomplete["domain_event_id"]
        with self.assertRaises(ProblemException):
            self.service.ingest_release_published(incomplete)

        unversioned = self._envelope(1)
        del unversioned["aggregate"]["version"]
        with self.assertRaises(ProblemException):
            self.service.ingest_release_published(unversioned)
        self.assertEqual(self._inbox_versions(), [])

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
from contextlib import redirect_stderr
from io import StringIO
import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from app.core.security import generate_uuidv7
from app.db.database import DEFAULT_EVENT_ID, DEFAULT_ORG_ID, ParticipationDatabase
from app.events.worker import main as worker_main
from app.services.audit_service import AuditContext, AuditService
from app.services.event_delivery_service import (
    DeliveryReceipt,
    OutboxDeliveryService,
    TransportError,
    TransportUnavailable,
)
from app.services.participation_service import ParticipationService


class AcceptingTransport:
    def __init__(self) -> None:
        self.messages: list[tuple[dict, dict]] = []

    def publish(self, envelope, *, headers):
        self.messages.append((dict(envelope), dict(headers)))
        return DeliveryReceipt(message_id=envelope["message_id"], accepted=True)


class FailingTransport:
    def __init__(self, *, error_code: str = "transport_timeout", retryable: bool = True) -> None:
        self.error_code = error_code
        self.retryable = retryable

    def publish(self, envelope, *, headers):
        raise TransportError(self.error_code, retryable=self.retryable)


class TimeoutTransport:
    def publish(self, envelope, *, headers):
        raise TimeoutError("adapter deadline exceeded")


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=5)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class EventDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = ParticipationDatabase(
            db_path=str(Path(self.temp_dir.name) / "participation.db")
        )
        self.service = ParticipationService(self.db, audit_service=AuditService())
        with self.db.transaction() as cur:
            self.event = self.service._emit_outbox_event(
                cur,
                organization_id=DEFAULT_ORG_ID,
                business_event_id=DEFAULT_EVENT_ID,
                topic="participation.registration.created.v1",
                aggregate_type="registration",
                aggregate_id=generate_uuidv7(),
                aggregate_version=1,
                data={"state": "confirmed"},
            )
        with self.db.read_cursor() as cur:
            cur.execute(
                "SELECT delivery_id FROM outbox_deliveries WHERE message_id = ?",
                (self.event["message_id"],),
            )
            self.delivery_id = cur.fetchone()["delivery_id"]

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def _delivery(self) -> dict:
        with self.db.read_cursor() as cur:
            cur.execute(
                "SELECT * FROM outbox_deliveries WHERE delivery_id = ?",
                (self.delivery_id,),
            )
            return dict(cur.fetchone())

    def test_worker_without_transport_exits_before_database_access(self) -> None:
        output = StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(output):
            exit_code = worker_main(["--organization-id", DEFAULT_ORG_ID])
        self.assertEqual(exit_code, 2)
        self.assertIn("No rows were claimed or changed", output.getvalue())

    def test_worker_does_not_create_missing_sqlite_database(self) -> None:
        missing_path = str(Path(self.temp_dir.name) / "not-created.db")
        output = StringIO()
        with patch.dict(
            os.environ,
            {
                "PARTICIPATION_EVENT_TRANSPORT": "unused:factory",
                "PARTICIPATION_SQLITE_PATH": missing_path,
            },
            clear=True,
        ), redirect_stderr(output):
            exit_code = worker_main(["--organization-id", DEFAULT_ORG_ID])
        self.assertEqual(exit_code, 2)
        self.assertFalse(Path(missing_path).exists())
        self.assertIn("never creates or migrates", output.getvalue())

    def test_outbox_is_pending_until_transport_acknowledges(self) -> None:
        with self.db.read_cursor() as cur:
            cur.execute(
                "SELECT published_at, attempts FROM outbox WHERE message_id = ?",
                (self.event["message_id"],),
            )
            outbox_row = cur.fetchone()
        self.assertIsNone(outbox_row["published_at"])
        self.assertEqual(outbox_row["attempts"], 0)
        self.assertEqual(self._delivery()["state"], "pending")

        with self.assertRaises(TransportUnavailable):
            OutboxDeliveryService(self.db, None).process_once(
                organization_id=DEFAULT_ORG_ID
            )

        self.assertEqual(self._delivery()["state"], "pending")
        self.assertEqual(self._delivery()["attempts"], 0)

        transport = AcceptingTransport()
        result = OutboxDeliveryService(self.db, transport).process_once(
            organization_id=DEFAULT_ORG_ID
        )
        self.assertEqual(result, {"claimed": 1, "published": 1, "retrying": 0, "dead_lettered": 0})
        self.assertEqual(self._delivery()["state"], "published")
        self.assertEqual(self._delivery()["attempts"], 1)
        self.assertIsNotNone(self._delivery()["published_at"])
        self.assertEqual(transport.messages[0][0]["message_id"], self.event["message_id"])

    def test_corrupt_envelope_goes_to_dlq_without_poisoning_the_batch(self) -> None:
        with self.db.transaction() as cur:
            cur.execute(
                "UPDATE outbox_deliveries SET envelope_json = ? WHERE delivery_id = ?",
                ("{not-json", self.delivery_id),
            )

        transport = AcceptingTransport()
        result = OutboxDeliveryService(self.db, transport).process_once(
            organization_id=DEFAULT_ORG_ID
        )

        self.assertEqual(result["dead_lettered"], 1)
        self.assertEqual(result["published"], 0)
        self.assertEqual(transport.messages, [])
        self.assertEqual(self._delivery()["state"], "dead_letter")
        with self.db.read_cursor() as cur:
            cur.execute(
                "SELECT error_code FROM event_dead_letters WHERE delivery_id = ?",
                (self.delivery_id,),
            )
            self.assertEqual(cur.fetchone()["error_code"], "invalid_envelope")

    def test_unexpected_transport_exception_is_classified_and_retried(self) -> None:
        result = OutboxDeliveryService(
            self.db,
            TimeoutTransport(),
            jitter=lambda cap: 0.0,
        ).process_once(organization_id=DEFAULT_ORG_ID)

        self.assertEqual(result["retrying"], 1)
        delivery = self._delivery()
        self.assertEqual(delivery["state"], "pending")
        self.assertEqual(delivery["last_error_code"], "transport_timeout")

    def test_existing_outbox_rows_are_not_assumed_delivered_or_requeued(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        legacy_id = generate_uuidv7()
        legacy_message_id = generate_uuidv7()
        legacy_domain_event_id = generate_uuidv7()
        conn = sqlite3.connect(legacy_path)
        try:
            conn.execute(
                """
                CREATE TABLE outbox (
                    id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL UNIQUE,
                    domain_event_id TEXT NOT NULL UNIQUE,
                    organization_id TEXT NOT NULL,
                    business_event_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    aggregate_type TEXT NOT NULL,
                    aggregate_id TEXT NOT NULL,
                    aggregate_version INTEGER NOT NULL,
                    partition_key TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    published_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                INSERT INTO outbox VALUES (
                    ?, ?, ?, ?, ?, ?, 1, 'registration', ?, 1, ?, '{}',
                    '2025-01-01T00:00:00Z', '2025-01-01T00:00:01Z', 1
                )
                """,
                (
                    legacy_id,
                    legacy_message_id,
                    legacy_domain_event_id,
                    DEFAULT_ORG_ID,
                    DEFAULT_EVENT_ID,
                    "participation.registration.created.v1",
                    generate_uuidv7(),
                    f"{DEFAULT_ORG_ID}:{DEFAULT_EVENT_ID}",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        legacy_db = ParticipationDatabase(db_path=str(legacy_path))
        try:
            with legacy_db.read_cursor() as cur:
                cur.execute(
                    "SELECT published_at, attempts FROM outbox WHERE id = ?",
                    (legacy_id,),
                )
                legacy = cur.fetchone()
                cur.execute("SELECT COUNT(*) AS count FROM outbox_deliveries")
                deliveries = cur.fetchone()["count"]
            self.assertEqual(legacy["published_at"], "2025-01-01T00:00:01Z")
            self.assertEqual(legacy["attempts"], 1)
            self.assertEqual(deliveries, 0)
        finally:
            legacy_db.close()

    def test_retries_are_limited_and_dlq_contains_only_classified_metadata(self) -> None:
        clock = MutableClock()
        processor = OutboxDeliveryService(
            self.db,
            FailingTransport(),
            max_attempts=2,
            base_backoff_seconds=1,
            max_backoff_seconds=10,
            clock=clock,
            jitter=lambda cap: cap,
        )

        first = processor.process_once(organization_id=DEFAULT_ORG_ID)
        self.assertEqual(first["retrying"], 1)
        after_first = self._delivery()
        self.assertEqual(after_first["state"], "pending")
        self.assertEqual(after_first["attempts"], 1)
        self.assertEqual(after_first["last_error_code"], "transport_timeout")
        self.assertEqual(
            after_first["next_attempt_at"],
            (clock.current + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        )
        self.assertEqual(
            processor.process_once(organization_id=DEFAULT_ORG_ID)["claimed"],
            0,
        )

        clock.advance(1)
        second = processor.process_once(organization_id=DEFAULT_ORG_ID)
        self.assertEqual(second["dead_lettered"], 1)
        dead = self._delivery()
        self.assertEqual(dead["state"], "dead_letter")
        self.assertEqual(dead["attempts"], 2)

        with self.db.read_cursor() as cur:
            cur.execute("SELECT * FROM event_dead_letters")
            rows = [dict(row) for row in cur.fetchall()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["error_code"], "transport_timeout")
        self.assertEqual(rows[0]["domain_event_id"], self.event["domain_event_id"])
        self.assertNotIn("envelope_json", rows[0])

    def test_permanent_failure_goes_to_dlq_without_exhausting_retries(self) -> None:
        result = OutboxDeliveryService(
            self.db,
            FailingTransport(error_code="invalid_envelope", retryable=False),
        ).process_once(organization_id=DEFAULT_ORG_ID)
        self.assertEqual(result["dead_lettered"], 1)
        self.assertEqual(self._delivery()["attempts"], 1)
        self.assertEqual(self._delivery()["state"], "dead_letter")

    def test_concurrent_processors_claim_a_delivery_only_once(self) -> None:
        entered_transport = Event()
        release_transport = Event()

        class BlockingTransport(AcceptingTransport):
            def publish(self, envelope, *, headers):
                entered_transport.set()
                if not release_transport.wait(timeout=5):
                    raise TimeoutError("test transport timed out")
                return super().publish(envelope, headers=headers)

        transport = BlockingTransport()
        first_result: list[dict] = []
        first = Thread(
            target=lambda: first_result.append(
                OutboxDeliveryService(self.db, transport).process_once(
                    organization_id=DEFAULT_ORG_ID
                )
            )
        )
        first.start()
        self.assertTrue(entered_transport.wait(timeout=5))

        second = OutboxDeliveryService(self.db, transport).process_once(
            organization_id=DEFAULT_ORG_ID
        )
        self.assertEqual(second["claimed"], 0)
        release_transport.set()
        first.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertEqual(first_result[0]["published"], 1)
        self.assertEqual(len(transport.messages), 1)

    def test_replay_preserves_domain_event_and_occurrence_and_is_audited(self) -> None:
        OutboxDeliveryService(
            self.db,
            FailingTransport(error_code="transport_rejected", retryable=False),
        ).process_once(organization_id=DEFAULT_ORG_ID)
        source = self._delivery()
        original_envelope = json.loads(source["envelope_json"])
        processor = OutboxDeliveryService(self.db, None)
        context = AuditContext(
            request_id=generate_uuidv7(),
            actor_type="service",
            actor_id="verified-test-service",
        )

        replay = processor.replay_dead_letter(
            organization_id=DEFAULT_ORG_ID,
            delivery_id=self.delivery_id,
            idempotency_key="replay-request-0001",
            audit_context=context,
        )
        duplicate = processor.replay_dead_letter(
            organization_id=DEFAULT_ORG_ID,
            delivery_id=self.delivery_id,
            idempotency_key="replay-request-0001",
            audit_context=context,
        )
        self.assertEqual(duplicate["status"], "already_scheduled")
        self.assertEqual(duplicate["message_id"], replay["message_id"])
        self.assertNotEqual(replay["message_id"], self.event["message_id"])

        with self.db.read_cursor() as cur:
            cur.execute(
                "SELECT * FROM outbox_deliveries WHERE delivery_id = ?",
                (replay["delivery_id"],),
            )
            replayed = dict(cur.fetchone())
            cur.execute("SELECT action, target_id FROM audit_logs")
            audit_row = cur.fetchone()
        replay_envelope = json.loads(replayed["envelope_json"])
        self.assertEqual(replay_envelope["domain_event_id"], original_envelope["domain_event_id"])
        self.assertEqual(replay_envelope["occurred_at"], original_envelope["occurred_at"])
        self.assertIsNone(replay_envelope["published_at"])
        self.assertEqual(audit_row["action"], "replay_event")
        self.assertEqual(audit_row["target_id"], self.delivery_id)

        transport = AcceptingTransport()
        delivered = OutboxDeliveryService(self.db, transport).process_once(
            organization_id=DEFAULT_ORG_ID
        )
        self.assertEqual(delivered["published"], 1)
        self.assertEqual(transport.messages[0][1], {"replay": "true"})

    def test_replay_requires_attributed_context(self) -> None:
        OutboxDeliveryService(
            self.db,
            FailingTransport(retryable=False),
        ).process_once(organization_id=DEFAULT_ORG_ID)
        with self.assertRaises(ValueError):
            OutboxDeliveryService(self.db, None).replay_dead_letter(
                organization_id=DEFAULT_ORG_ID,
                delivery_id=self.delivery_id,
                idempotency_key="replay-request-0002",
                audit_context=AuditContext(generate_uuidv7(), None, None),
            )

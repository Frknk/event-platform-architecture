from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from app.db.database import DEFAULT_ORG_ID, ParticipationDatabase


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class SQLiteAuditRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = str(Path(self.temp_dir.name) / "participation.db")
        self.db = ParticipationDatabase(db_path=db_path)

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def _insert_audit_log(self, *, created_at: str, target_id: str | None = None) -> str:
        audit_id = str(uuid.uuid4())
        with self.db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO audit_logs (
                    id, organization_id, actor, action, target_type, target_id,
                    reason, payload_json, created_at
                ) VALUES (?, ?, 'unattributed', 'privacy_request', 'privacy_request',
                          ?, 'not_recorded', NULL, ?)
                """,
                (audit_id, DEFAULT_ORG_ID, target_id or str(uuid.uuid4()), created_at),
            )
        return audit_id

    def _insert_hold_event(
        self,
        *,
        hold_id: str,
        target_id: str,
        event_type: str,
        digest: str,
    ) -> str:
        event_id = str(uuid.uuid4())
        with self.db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO audit_legal_hold_events (
                    event_id, hold_id, organization_id, target_type, target_id,
                    event_type, authority_ref_digest
                ) VALUES (?, ?, ?, 'privacy_request', ?, ?, ?)
                """,
                (event_id, hold_id, DEFAULT_ORG_ID, target_id, event_type, digest),
            )
        return event_id

    def _assert_delete_rejected(self, audit_id: str, expected_message: str) -> None:
        with self.assertRaisesRegex(sqlite3.IntegrityError, expected_message):
            with self.db.transaction() as cur:
                cur.execute("DELETE FROM audit_logs WHERE id = ?", (audit_id,))

    def test_insert_is_allowed_but_update_is_rejected(self) -> None:
        created_at = _utc_iso(datetime.now(timezone.utc))
        audit_id = self._insert_audit_log(created_at=created_at)

        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with self.db.transaction() as cur:
                cur.execute(
                    "UPDATE audit_logs SET action = 'changed' WHERE id = ?",
                    (audit_id,),
                )

        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "retention period has not expired"
        ):
            with self.db.transaction() as cur:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO audit_logs (
                        id, organization_id, actor, action, target_type, target_id,
                        reason, payload_json, created_at
                    ) VALUES (?, ?, 'unattributed', 'changed', 'privacy_request',
                              ?, 'not_recorded', NULL, ?)
                    """,
                    (
                        audit_id,
                        DEFAULT_ORG_ID,
                        str(uuid.uuid4()),
                        created_at,
                    ),
                )

        with self.db.read_cursor() as cur:
            cur.execute("SELECT action FROM audit_logs WHERE id = ?", (audit_id,))
            self.assertEqual(cur.fetchone()["action"], "privacy_request")

    def test_delete_before_two_years_is_rejected(self) -> None:
        recent_id = self._insert_audit_log(
            created_at=_utc_iso(datetime.now(timezone.utc) - timedelta(days=365))
        )

        self._assert_delete_rejected(recent_id, "retention period has not expired")

    def test_expired_delete_remains_disabled_without_maintenance_authority(self) -> None:
        old_id = self._insert_audit_log(
            created_at=_utc_iso(datetime.now(timezone.utc) - timedelta(days=3 * 365))
        )

        self._assert_delete_rejected(old_id, "audit purge is disabled")

    def test_active_hold_blocks_delete_and_hold_events_are_append_only(self) -> None:
        target_id = str(uuid.uuid4())
        old_id = self._insert_audit_log(
            created_at=_utc_iso(datetime.now(timezone.utc) - timedelta(days=3 * 365)),
            target_id=target_id,
        )
        hold_id = str(uuid.uuid4())
        placed_event_id = self._insert_hold_event(
            hold_id=hold_id,
            target_id=target_id,
            event_type="placed",
            digest="a" * 64,
        )

        self._assert_delete_rejected(old_id, "protected by legal hold")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with self.db.transaction() as cur:
                cur.execute(
                    "UPDATE audit_legal_hold_events SET event_type = 'released' WHERE event_id = ?",
                    (placed_event_id,),
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with self.db.transaction() as cur:
                cur.execute(
                    "DELETE FROM audit_legal_hold_events WHERE event_id = ?",
                    (placed_event_id,),
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
            with self.db.transaction() as cur:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO audit_legal_hold_events (
                        event_id, hold_id, organization_id, target_type, target_id,
                        event_type, authority_ref_digest
                    ) VALUES (?, ?, ?, 'privacy_request', ?, 'placed', ?)
                    """,
                    (
                        placed_event_id,
                        hold_id,
                        DEFAULT_ORG_ID,
                        target_id,
                        "d" * 64,
                    ),
                )

        self._insert_hold_event(
            hold_id=hold_id,
            target_id=target_id,
            event_type="released",
            digest="b" * 64,
        )
        self._assert_delete_rejected(old_id, "audit purge is disabled")

    def test_release_without_active_hold_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "cannot release an inactive audit legal hold"
        ):
            self._insert_hold_event(
                hold_id=str(uuid.uuid4()),
                target_id=str(uuid.uuid4()),
                event_type="released",
                digest="c" * 64,
            )

    def test_demo_reset_preserves_audit_history(self) -> None:
        audit_id = self._insert_audit_log(
            created_at=_utc_iso(datetime.now(timezone.utc))
        )

        self.db.reset_demo_state()

        with self.db.read_cursor() as cur:
            cur.execute("SELECT COUNT(*) AS count FROM audit_logs WHERE id = ?", (audit_id,))
            self.assertEqual(cur.fetchone()["count"], 1)


class PostgreSQLAuditRetentionTests(unittest.TestCase):
    def test_migration_guards_uuid_and_text_target_ids(self) -> None:
        database_url = os.environ.get("PARTICIPATION_TEST_POSTGRES_URL")
        if not database_url:
            self.skipTest("PARTICIPATION_TEST_POSTGRES_URL is not configured.")

        try:
            import psycopg
            from psycopg import sql
        except ImportError:
            self.skipTest("psycopg is not installed.")

        conn = psycopg.connect(database_url, autocommit=True)
        schema_names: list[str] = []
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "003_audit_retention.sql"
        )
        migration_sql = migration_path.read_text(encoding="utf-8")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                if not cur.fetchone()[0].endswith("_test"):
                    self.skipTest(
                        "PostgreSQL retention tests require a disposable database "
                        "whose name ends in '_test'."
                    )

            organization_id = DEFAULT_ORG_ID
            for target_type in ("uuid", "text"):
                schema_name = f"audit_retention_test_{uuid.uuid4().hex}"
                schema_names.append(schema_name)
                target_id = str(uuid.uuid4())
                audit_id = str(uuid.uuid4())
                recent_audit_id = str(uuid.uuid4())
                recent_target_id = str(uuid.uuid4())
                hold_id = str(uuid.uuid4())
                placed_id = str(uuid.uuid4())
                released_id = str(uuid.uuid4())

                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
                    )
                    cur.execute(
                        sql.SQL("SET search_path TO {}").format(
                            sql.Identifier(schema_name)
                        )
                    )
                    cur.execute(
                        f"""
                        CREATE TABLE audit_logs (
                            id uuid PRIMARY KEY,
                            organization_id uuid NOT NULL,
                            actor text NOT NULL,
                            action text NOT NULL,
                            target_type text NOT NULL,
                            target_id {target_type} NOT NULL,
                            reason text NOT NULL,
                            payload_json jsonb,
                            created_at timestamptz NOT NULL
                        )
                        """
                    )
                    cur.execute(migration_sql)
                    cur.execute(
                        "SELECT set_config('app.current_organization_id', %s, false)",
                        (organization_id,),
                    )
                    cur.execute(
                        """
                        SELECT c.relrowsecurity, c.relforcerowsecurity
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = %s AND c.relname = 'audit_legal_hold_events'
                        """,
                        (schema_name,),
                    )
                    self.assertEqual(cur.fetchone(), (True, True))

                    cur.execute(
                        """
                        INSERT INTO audit_logs (
                            id, organization_id, actor, action, target_type,
                            target_id, reason, created_at
                        ) VALUES (%s, %s, 'unattributed', 'privacy_request',
                                  'privacy_request', %s, 'not_recorded', %s)
                        """,
                        (
                            audit_id,
                            organization_id,
                            target_id,
                            datetime.now(timezone.utc) - timedelta(days=3 * 365),
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO audit_logs (
                            id, organization_id, actor, action, target_type,
                            target_id, reason, created_at
                        ) VALUES (%s, %s, 'unattributed', 'privacy_request',
                                  'privacy_request', %s, 'not_recorded', %s)
                        """,
                        (
                            recent_audit_id,
                            organization_id,
                            recent_target_id,
                            datetime.now(timezone.utc),
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO audit_legal_hold_events (
                            event_id, hold_id, organization_id, target_type,
                            target_id, event_type, authority_ref_digest
                        ) VALUES (%s, %s, %s, 'privacy_request', %s, 'placed', %s)
                        """,
                        (placed_id, hold_id, organization_id, target_id, "a" * 64),
                    )
                    with self.assertRaisesRegex(psycopg.Error, "protected by legal hold"):
                        cur.execute(
                            "DELETE FROM audit_logs WHERE id = %s",
                            (audit_id,),
                        )
                    with self.assertRaisesRegex(psycopg.Error, "append-only"):
                        cur.execute(
                            "UPDATE audit_legal_hold_events SET event_type = 'released' "
                            "WHERE event_id = %s",
                            (placed_id,),
                        )
                    cur.execute(
                        """
                        INSERT INTO audit_legal_hold_events (
                            event_id, hold_id, organization_id, target_type,
                            target_id, event_type, authority_ref_digest
                        ) VALUES (%s, %s, %s, 'privacy_request', %s, 'released', %s)
                        """,
                        (released_id, hold_id, organization_id, target_id, "b" * 64),
                    )

                    with self.assertRaisesRegex(psycopg.Error, "append-only"):
                        cur.execute(
                            "UPDATE audit_logs SET action = 'changed' WHERE id = %s",
                            (audit_id,),
                        )
                    with self.assertRaisesRegex(psycopg.Error, "append-only"):
                        cur.execute("TRUNCATE audit_logs")
                    with self.assertRaisesRegex(psycopg.Error, "append-only"):
                        cur.execute("TRUNCATE audit_legal_hold_events")
                    with self.assertRaisesRegex(
                        psycopg.Error, "retention period has not expired"
                    ):
                        cur.execute(
                            "DELETE FROM audit_logs WHERE id = %s",
                            (recent_audit_id,),
                        )
                    with self.assertRaisesRegex(psycopg.Error, "purge is disabled"):
                        cur.execute("DELETE FROM audit_logs WHERE id = %s", (audit_id,))
                    with self.assertRaisesRegex(psycopg.Error, "append-only"):
                        cur.execute(
                            "DELETE FROM audit_legal_hold_events WHERE event_id = %s",
                            (placed_id,),
                        )
        finally:
            with conn.cursor() as cur:
                for schema_name in reversed(schema_names):
                    cur.execute(
                        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                            sql.Identifier(schema_name)
                        )
                    )
            conn.close()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from app.db.database import ParticipationDatabase


AUDIT_COLUMNS = {"actor_type", "actor_id", "result", "request_id"}

LEGACY_AUDIT_ROW = {
    "id": "0192f000-0000-7000-8000-000000000701",
    "organization_id": "0192f000-0000-7000-8000-000000000001",
    "actor": "manager",
    "action": "correct_registration",
    "target_type": "registration",
    "target_id": "0192f000-0000-7000-8000-000000000702",
    "reason": "legacy reason",
    "payload_json": '{"updated_fields":["phone"]}',
    "created_at": "2026-01-02T03:04:05Z",
}

CREATE_LEGACY_SQLITE_AUDIT_TABLE = """
CREATE TABLE audit_logs (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
)
"""


class SQLiteAuditMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "participation.db")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _open_database(self) -> ParticipationDatabase:
        return ParticipationDatabase(db_path=self.db_path)

    def _columns(self, db: ParticipationDatabase) -> set[str]:
        with db.read_cursor() as cur:
            cur.execute("PRAGMA table_info(audit_logs)")
            return {row["name"] for row in cur.fetchall()}

    def _assert_nullable_columns(self, db: ParticipationDatabase) -> None:
        with db.read_cursor() as cur:
            cur.execute("PRAGMA table_info(audit_logs)")
            columns = {row["name"]: row for row in cur.fetchall()}
        for column in AUDIT_COLUMNS:
            self.assertEqual(columns[column]["notnull"], 0)

    def _assert_legacy_row_unchanged(self, db: ParticipationDatabase) -> None:
        with db.read_cursor() as cur:
            cur.execute("SELECT * FROM audit_logs WHERE id = ?", (LEGACY_AUDIT_ROW["id"],))
            row = dict(cur.fetchone())

        for key, value in LEGACY_AUDIT_ROW.items():
            self.assertEqual(row[key], value)
        for column in AUDIT_COLUMNS:
            self.assertIsNone(row[column])

    def test_migrates_empty_database(self) -> None:
        db = self._open_database()
        try:
            self.assertTrue(AUDIT_COLUMNS.issubset(self._columns(db)))
            self._assert_nullable_columns(db)
            with db.read_cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS count FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'audit_logs'"
                )
                self.assertEqual(cur.fetchone()["count"], 1)
        finally:
            db._conn.close()

    def test_preserves_legacy_row_and_adds_nullable_columns(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(CREATE_LEGACY_SQLITE_AUDIT_TABLE)
            conn.execute(
                """
                INSERT INTO audit_logs (
                    id, organization_id, actor, action, target_type, target_id,
                    reason, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(LEGACY_AUDIT_ROW.values()),
            )
            conn.commit()
        finally:
            conn.close()

        db = self._open_database()
        try:
            self.assertTrue(AUDIT_COLUMNS.issubset(self._columns(db)))
            self._assert_nullable_columns(db)
            self._assert_legacy_row_unchanged(db)
            db.init_schema()
            self._assert_legacy_row_unchanged(db)
        finally:
            db._conn.close()


class PostgreSQLAuditMigrationTests(unittest.TestCase):
    def _run_migration(self, *, existing_table: bool) -> None:
        database_url = os.environ.get("PARTICIPATION_TEST_POSTGRES_URL")
        if not database_url:
            self.skipTest("PARTICIPATION_TEST_POSTGRES_URL is not configured.")

        try:
            import psycopg
            from psycopg import sql
        except ImportError:
            self.skipTest("psycopg is not installed.")

        conn = psycopg.connect(database_url, autocommit=True)
        schema_name = f"audit_migration_test_{uuid.uuid4().hex}"
        schema_created = False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                if not cur.fetchone()[0].endswith("_test"):
                    self.skipTest(
                        "PostgreSQL migration tests require a disposable database "
                        "whose name ends in '_test'."
                    )

            migration_path = (
                Path(__file__).resolve().parents[1]
                / "migrations"
                / "002_audit_logs_enrichment.sql"
            )
            migration_sql = migration_path.read_text(encoding="utf-8")

            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
                    )
                    schema_created = True
                    cur.execute(
                        sql.SQL("SET LOCAL search_path TO {}").format(
                            sql.Identifier(schema_name)
                        )
                    )

                    if existing_table:
                        cur.execute(
                            """
                            CREATE TABLE audit_logs (
                                id uuid PRIMARY KEY,
                                organization_id uuid NOT NULL,
                                actor text NOT NULL,
                                action text NOT NULL,
                                target_type text NOT NULL,
                                target_id text NOT NULL,
                                reason text NOT NULL,
                                payload_json jsonb,
                                created_at timestamptz NOT NULL
                            )
                            """
                        )
                        cur.execute(
                            """
                            INSERT INTO audit_logs (
                                id, organization_id, actor, action, target_type,
                                target_id, reason, payload_json, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                            """,
                            (
                                LEGACY_AUDIT_ROW["id"],
                                LEGACY_AUDIT_ROW["organization_id"],
                                LEGACY_AUDIT_ROW["actor"],
                                LEGACY_AUDIT_ROW["action"],
                                LEGACY_AUDIT_ROW["target_type"],
                                LEGACY_AUDIT_ROW["target_id"],
                                LEGACY_AUDIT_ROW["reason"],
                                LEGACY_AUDIT_ROW["payload_json"],
                                LEGACY_AUDIT_ROW["created_at"],
                            ),
                        )

                    cur.execute(migration_sql)
                    cur.execute(
                        """
                        SELECT column_name, is_nullable
                        FROM information_schema.columns
                        WHERE table_schema = %s AND table_name = 'audit_logs'
                        """,
                        (schema_name,),
                    )
                    column_rows = {row[0]: row[1] for row in cur.fetchall()}
                    columns = set(column_rows)
                    self.assertTrue(AUDIT_COLUMNS.issubset(columns))
                    for column in AUDIT_COLUMNS:
                        self.assertEqual(column_rows[column], "YES")

                    cur.execute(
                        "SELECT set_config('app.current_organization_id', %s, true)",
                        (LEGACY_AUDIT_ROW["organization_id"],),
                    )
                    cur.execute("SELECT COUNT(*) FROM audit_logs")
                    expected_count = 1 if existing_table else 0
                    self.assertEqual(cur.fetchone()[0], expected_count)

                    if existing_table:
                        cur.execute(
                            """
                            SELECT id, organization_id, actor, action, target_type,
                                   target_id, reason, payload_json, created_at,
                                   actor_type, actor_id, result, request_id
                            FROM audit_logs WHERE id = %s
                            """,
                            (LEGACY_AUDIT_ROW["id"],),
                        )
                        row = cur.fetchone()
                        for index, key in enumerate(LEGACY_AUDIT_ROW):
                            value = row[index]
                            expected = LEGACY_AUDIT_ROW[key]
                            if isinstance(value, uuid.UUID):
                                value = str(value)
                            elif key == "payload_json":
                                value = json.dumps(value, separators=(",", ":"))
                            elif key == "created_at":
                                value = value.isoformat().replace("+00:00", "Z")
                            self.assertEqual(value, expected)
                        self.assertEqual(row[9:], (None, None, None, None))
        finally:
            if schema_created:
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                            sql.Identifier(schema_name)
                        )
                    )
            conn.close()

    def test_migrates_empty_database(self) -> None:
        self._run_migration(existing_table=False)

    def test_preserves_existing_database_rows(self) -> None:
        self._run_migration(existing_table=True)


if __name__ == "__main__":
    unittest.main()

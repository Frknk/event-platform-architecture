"""
Transactional Database Layer for Team 3 — Participation.
Supports both:
- PostgreSQL 16+ (Production / Docker Compose environment with ConnectionPool and RLS)
- SQLite (Local development / in-memory unit tests with thread locks)

Enforces:
- Atomic capacity locking (row-level locking / CHECK constraints)
- State machine CHECK constraints identical to 001_initial_participation_schema.sql
- Tenant isolation by organization_id (AD-12 with FORCE ROW LEVEL SECURITY)
- Default seeded release for IncubaUNAS 2026 so the demo works out-of-the-box.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional
import uuid

from app.core.security import utc_now_iso

logger = logging.getLogger("participation.db")

DEFAULT_ORG_ID = "0192f000-0000-7000-8000-000000000001"
DEFAULT_SITE_SLUG = "incuba-unas"
DEFAULT_EVENT_ID = "0192f000-0000-7000-8000-000000000101"
DEFAULT_EVENT_SLUG = "congreso-innovacion-demo-day-2026"
DEFAULT_PUBLICATION_ID = "0192f000-0000-7000-8000-000000000201"
DEFAULT_FORM_VERSION_ID = "0192f000-0000-7000-8000-000000000301"
DEFAULT_CONSENT_VERSION = "consent-unas-v1.0"
DEFAULT_POSTGRES_URL = "postgresql://participation_app:participation_local_secret@127.0.0.1:5434/participation_db"


class PostgresRow(dict):
    """Dictionary subclass providing dict/sqlite3.Row compatible item access."""
    pass


def _format_pg_val(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


def _format_pg_row(row: Optional[dict]) -> Optional[PostgresRow]:
    if row is None:
        return None
    return PostgresRow({k: _format_pg_val(v) for k, v in row.items()})


def _adapt_sql_for_pg(sql: str) -> str:
    parts = sql.split("'")
    for i in range(0, len(parts), 2):
        parts[i] = parts[i].replace("?", "%s")
    sql = "'".join(parts)
    if "INSERT OR IGNORE INTO" in sql:
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        if "ON CONFLICT" not in sql:
            sql += " ON CONFLICT DO NOTHING"
    return sql


class PostgresCursorWrapper:
    """Wraps a psycopg cursor to provide unified DBAPI and Row interfaces."""

    def __init__(self, cur: Any, org_id: str = DEFAULT_ORG_ID) -> None:
        self._cur = cur
        self.org_id = org_id

    def execute(self, query: str, params: Any = None) -> PostgresCursorWrapper:
        clean_q = query.strip()
        if clean_q.upper().startswith("PRAGMA") or clean_q.upper().startswith("BEGIN IMMEDIATE"):
            return self
        adapted_sql = _adapt_sql_for_pg(query)
        if params is not None:
            if isinstance(params, (tuple, list)):
                self._cur.execute(adapted_sql, params)
            else:
                self._cur.execute(adapted_sql, (params,))
        else:
            self._cur.execute(adapted_sql)
        return self

    def fetchone(self) -> Optional[PostgresRow]:
        row = self._cur.fetchone()
        return _format_pg_row(row) if row is not None else None

    def fetchall(self) -> list[PostgresRow]:
        rows = self._cur.fetchall()
        return [_format_pg_row(r) for r in rows]

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def close(self) -> None:
        self._cur.close()

    def __iter__(self) -> Iterator[PostgresRow]:
        for r in self._cur:
            yield _format_pg_row(r)


class ParticipationDatabase:
    """Thread-safe transactional database engine supporting PostgreSQL and SQLite."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        db_url: Optional[str] = None,
    ) -> None:
        target = (
            db_url
            or db_path
            or os.environ.get("DATABASE_URL")
            or os.environ.get("PARTICIPATION_DB_URL")
        )

        # 1. Explicit sqlite path (e.g. unit tests or .db file)
        if target and (target.endswith(".db") or target.startswith("sqlite:") or target.startswith("sqlite:///")):
            self.backend = "sqlite"
            self.db_path = target.replace("sqlite:///", "").replace("sqlite:", "")
            self._init_sqlite()
        # 2. Explicit PostgreSQL URL
        elif target and (target.startswith("postgres://") or target.startswith("postgresql://")):
            self.backend = "postgres"
            self.db_url = target
            self._init_postgres()
        # 3. Explicit SQLite environment override
        elif os.environ.get("PARTICIPATION_SQLITE_PATH"):
            self.backend = "sqlite"
            self.db_path = os.environ["PARTICIPATION_SQLITE_PATH"]
            self._init_sqlite()
        # 4. Default: Prefer PostgreSQL container on 5434, fallback to SQLite if offline
        else:
            pg_url = os.environ.get("DATABASE_URL", DEFAULT_POSTGRES_URL)
            try:
                self.backend = "postgres"
                self.db_url = pg_url
                self._init_postgres()
            except Exception as e:
                logger.warning(
                    "PostgreSQL not available at %s (%s). Falling back to local SQLite.",
                    pg_url,
                    e,
                )
                self.backend = "sqlite"
                self.db_path = str(Path(tempfile.gettempdir()) / "participation_service.db")
                self._init_sqlite()

        self.init_schema()
        self.seed_default_release()

    def _init_sqlite(self) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row

    def _init_postgres(self) -> None:
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as err:
            raise ImportError("psycopg and psycopg_pool must be installed for PostgreSQL backend.") from err

        url = self.db_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]

        self._dict_row = dict_row
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=20,
            timeout=10.0,
            open=True,
        )
        # Test connection
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")

    @contextmanager
    def transaction(self, org_id: str = DEFAULT_ORG_ID) -> Iterator[Any]:
        """Acquire atomic transactional lock with tenant isolation (RLS in Postgres, BEGIN IMMEDIATE in SQLite)."""
        if self.backend == "postgres":
            with self._pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor(row_factory=self._dict_row) as cur:
                        cur.execute(
                            "SELECT set_config('app.current_organization_id', %s, true);",
                            (org_id,),
                        )
                        yield PostgresCursorWrapper(cur, org_id=org_id)
        else:
            with self._lock:
                cursor = self._conn.cursor()
                cursor.execute("BEGIN IMMEDIATE")
                try:
                    yield cursor
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise
                finally:
                    cursor.close()

    @contextmanager
    def read_cursor(self, org_id: str = DEFAULT_ORG_ID) -> Iterator[Any]:
        if self.backend == "postgres":
            with self._pool.connection() as conn:
                with conn.cursor(row_factory=self._dict_row) as cur:
                    cur.execute(
                        "SELECT set_config('app.current_organization_id', %s, true);",
                        (org_id,),
                    )
                    yield PostgresCursorWrapper(cur, org_id=org_id)
        else:
            with self._lock:
                cursor = self._conn.cursor()
                try:
                    yield cursor
                finally:
                    cursor.close()

    def init_schema(self) -> None:
        if self.backend == "postgres":
            with self.transaction() as cur:
                cur.execute("SELECT to_regclass('release_projections');")
                res = cur.fetchone()
                if not res or not res.get("to_regclass"):
                    migration_file = (
                        Path(__file__).resolve().parent.parent.parent
                        / "migrations"
                        / "001_initial_participation_schema.sql"
                    )
                    if migration_file.exists():
                        cur.execute(migration_file.read_text(encoding="utf-8"))
        else:
            with self.transaction() as cur:
                cur.executescript(
                    """
                    PRAGMA foreign_keys = ON;

                    CREATE TABLE IF NOT EXISTS release_projections (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        site_slug TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        event_slug TEXT NOT NULL,
                        event_title TEXT NOT NULL,
                        publication_id TEXT NOT NULL UNIQUE,
                        release_version INTEGER NOT NULL CHECK (release_version >= 1),
                        state TEXT NOT NULL CHECK (state IN ('pending', 'active', 'failed', 'superseded')),
                        binding_purpose TEXT NOT NULL CHECK (binding_purpose IN ('registration', 'application')),
                        binding_state TEXT NOT NULL CHECK (binding_state IN ('scheduled', 'open', 'closed', 'cancelled')),
                        form_version_id TEXT NOT NULL,
                        consent_version TEXT NOT NULL,
                        schema_hash TEXT NOT NULL,
                        form_schema_json TEXT NOT NULL,
                        ui_schema_json TEXT NOT NULL,
                        consent_text TEXT NOT NULL,
                        requires_approval INTEGER NOT NULL DEFAULT 0,
                        requires_email_verification INTEGER NOT NULL DEFAULT 0,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS capacity_counters (
                        event_id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        publication_id TEXT NOT NULL,
                        capacity INTEGER NOT NULL CHECK (capacity >= 0),
                        confirmed_count INTEGER NOT NULL DEFAULT 0 CHECK (confirmed_count >= 0 AND confirmed_count <= capacity),
                        waitlist_enabled INTEGER NOT NULL DEFAULT 1,
                        waitlist_count INTEGER NOT NULL DEFAULT 0 CHECK (waitlist_count >= 0),
                        next_waitlist_seq INTEGER NOT NULL DEFAULT 1 CHECK (next_waitlist_seq >= 1),
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS registrations (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        publication_id TEXT NOT NULL,
                        form_version_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN (
                                'pending_verification',
                                'pending_review',
                                'waitlisted',
                                'confirmed',
                                'rejected',
                                'cancelled'
                            )
                        ),
                        email_hmac TEXT NOT NULL,
                        email_masked TEXT NOT NULL,
                        full_name_masked TEXT NOT NULL,
                        pii_ciphertext TEXT NOT NULL,
                        pii_key_id TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'public_web',
                        cancellation_reason TEXT,
                        rejection_reason TEXT,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        confirmed_at TEXT,
                        cancelled_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS registration_answers (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        registration_id TEXT NOT NULL UNIQUE REFERENCES registrations(id),
                        form_version_id TEXT NOT NULL,
                        answers_ciphertext TEXT NOT NULL,
                        key_id TEXT NOT NULL,
                        schema_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS consents (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        subject_type TEXT NOT NULL CHECK (subject_type IN ('registration', 'application')),
                        subject_id TEXT NOT NULL,
                        consent_version TEXT NOT NULL,
                        consent_hash TEXT NOT NULL,
                        accepted_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS waitlist_entries (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        registration_id TEXT NOT NULL UNIQUE REFERENCES registrations(id),
                        position INTEGER NOT NULL CHECK (position >= 1),
                        state TEXT NOT NULL CHECK (state IN ('waiting', 'promoted', 'cancelled', 'expired')),
                        promoted_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (organization_id, event_id, position)
                    );

                    CREATE TABLE IF NOT EXISTS tickets (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        registration_id TEXT NOT NULL REFERENCES registrations(id),
                        jti TEXT NOT NULL UNIQUE,
                        state TEXT NOT NULL CHECK (state IN ('active', 'revoked', 'expired', 'replaced')),
                        version INTEGER NOT NULL DEFAULT 1,
                        key_id TEXT NOT NULL,
                        audience TEXT NOT NULL DEFAULT 'onsite',
                        scope TEXT NOT NULL DEFAULT 'general',
                        ticket_jws TEXT NOT NULL,
                        issued_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        revoked_at TEXT,
                        revision INTEGER NOT NULL DEFAULT 1
                    );

                    CREATE TABLE IF NOT EXISTS ticket_revocations (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        ticket_id TEXT NOT NULL REFERENCES tickets(id),
                        jti TEXT NOT NULL UNIQUE,
                        reason_code TEXT NOT NULL,
                        revoked_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS ventures (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        name TEXT NOT NULL,
                        track TEXT NOT NULL,
                        leader_email_hmac TEXT NOT NULL,
                        leader_pii_ciphertext TEXT NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS team_memberships (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        venture_id TEXT NOT NULL REFERENCES ventures(id),
                        email_hmac TEXT NOT NULL,
                        email_masked TEXT NOT NULL,
                        role TEXT NOT NULL CHECK (role IN ('leader', 'co_founder', 'member')),
                        state TEXT NOT NULL CHECK (state IN ('active', 'left', 'removed')),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS team_invitations (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        venture_id TEXT NOT NULL REFERENCES ventures(id),
                        email_hmac TEXT NOT NULL,
                        email_masked TEXT NOT NULL,
                        role TEXT NOT NULL,
                        token_hash TEXT NOT NULL UNIQUE,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'accepted', 'declined', 'expired', 'revoked')),
                        expires_at TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS applications (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        venture_id TEXT NOT NULL REFERENCES ventures(id),
                        form_version_id TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (
                            state IN (
                                'draft',
                                'submitted',
                                'needs_changes',
                                'resubmitted',
                                'admitted',
                                'rejected',
                                'withdrawn'
                            )
                        ),
                        current_revision_number INTEGER NOT NULL DEFAULT 1,
                        draft_answers_ciphertext TEXT NOT NULL,
                        snapshot_hash TEXT,
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        submitted_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS application_revisions (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        application_id TEXT NOT NULL REFERENCES applications(id),
                        revision_number INTEGER NOT NULL,
                        snapshot_ciphertext TEXT NOT NULL,
                        snapshot_hash TEXT NOT NULL,
                        submitted_at TEXT NOT NULL,
                        UNIQUE (application_id, revision_number)
                    );

                    CREATE TABLE IF NOT EXISTS application_files (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        application_id TEXT NOT NULL REFERENCES applications(id),
                        field_name TEXT NOT NULL,
                        file_name TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        sha256_hash TEXT NOT NULL,
                        state TEXT NOT NULL CHECK (state IN ('uploading', 'scanning', 'ready', 'quarantined', 'deleted')),
                        created_at TEXT NOT NULL,
                        scanned_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS idempotency_keys (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        status_code INTEGER NOT NULL,
                        response_body_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        UNIQUE (organization_id, operation, idempotency_key)
                    );

                    CREATE TABLE IF NOT EXISTS inbox (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        consumer_name TEXT NOT NULL,
                        domain_event_id TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        aggregate_version INTEGER NOT NULL,
                        processed_at TEXT NOT NULL,
                        UNIQUE (consumer_name, domain_event_id)
                    );

                    CREATE TABLE IF NOT EXISTS outbox (
                        id TEXT PRIMARY KEY,
                        message_id TEXT NOT NULL UNIQUE,
                        domain_event_id TEXT NOT NULL UNIQUE,
                        organization_id TEXT NOT NULL,
                        business_event_id TEXT NOT NULL,
                        topic TEXT NOT NULL,
                        schema_version INTEGER NOT NULL DEFAULT 1,
                        aggregate_type TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        aggregate_version INTEGER NOT NULL,
                        partition_key TEXT NOT NULL,
                        envelope_json TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        published_at TEXT,
                        attempts INTEGER NOT NULL DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS privacy_requests (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        email_hmac TEXT NOT NULL,
                        request_type TEXT NOT NULL CHECK (request_type IN ('access', 'rectification', 'erasure')),
                        legal_hold INTEGER NOT NULL DEFAULT 0,
                        state TEXT NOT NULL CHECK (state IN ('pending', 'completed', 'rejected')),
                        tombstone_receipt TEXT,
                        evidence_json TEXT,
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    );

                    CREATE TABLE IF NOT EXISTS audit_logs (
                        id TEXT PRIMARY KEY,
                        organization_id TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        target_type TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        payload_json TEXT,
                        created_at TEXT NOT NULL
                    );
                    """
                )

    def seed_default_release(self, default_capacity: int = 5) -> Dict[str, Any]:
        """Ensure the default published release for IncubaUNAS 2026 exists."""
        form_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "Formulario Oficial de Inscripción — IncubaUNAS 2026",
            "type": "object",
            "required": ["track", "role_profile", "modality"],
            "properties": {
                "track": {
                    "type": "string",
                    "title": "Track o Programa Principal",
                    "enum": [
                        "Agrotech & Bioeconomía Amazónica",
                        "IA Aplicada & Transformación Digital",
                        "Demo Day de Startups & Emprendimiento",
                    ],
                },
                "role_profile": {
                    "type": "string",
                    "title": "Perfil del Participante",
                    "enum": [
                        "Estudiante UNAS",
                        "Investigador / Docente",
                        "Líder de Emprendimiento",
                        "Inversionista / Invitado Externo",
                    ],
                },
                "modality": {
                    "type": "string",
                    "title": "Modalidad de Participación",
                    "enum": ["Presencial — Auditorio Central UNAS", "Presencial + Talleres Prácticos"],
                },
                "dietary_or_accessibility": {
                    "type": "string",
                    "title": "Requerimiento de accesibilidad (Opcional)",
                },
            },
        }
        ui_schema = {
            "ui:order": ["track", "role_profile", "modality", "dietary_or_accessibility"]
        }
        consent_text = (
            "Autorizo a IncubaUNAS y la Universidad Nacional Agraria de la Selva (UNAS) "
            "el tratamiento de mis datos personales exclusivamente para la gestión operativa del evento, "
            "control de asistencia y emisión de certificados conforme a la Ley N° 29733."
        )
        schema_hash = hashlib.sha256(
            json.dumps(form_schema, sort_keys=True).encode("utf-8")
        ).hexdigest()

        now = utc_now_iso()
        with self.transaction() as cur:
            cur.execute(
                "SELECT id FROM release_projections WHERE event_id = ? AND state = 'active'",
                (DEFAULT_EVENT_ID,),
            )
            row = cur.fetchone()
            if not row:
                cur.execute(
                    """
                    INSERT INTO release_projections (
                        id, organization_id, site_slug, event_id, event_slug, event_title,
                        publication_id, release_version, state, binding_purpose, binding_state,
                        form_version_id, consent_version, schema_hash, form_schema_json,
                        ui_schema_json, consent_text, requires_approval, requires_email_verification,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 'registration', 'open', ?, ?, ?, ?, ?, ?, false, false, 1, ?, ?)
                    ON CONFLICT (publication_id) DO NOTHING
                    """,
                    (
                        "0192f000-0000-7000-8000-000000000401",
                        DEFAULT_ORG_ID,
                        DEFAULT_SITE_SLUG,
                        DEFAULT_EVENT_ID,
                        DEFAULT_EVENT_SLUG,
                        "Congreso Nacional de Innovación & Demo Day IncubaUNAS 2026",
                        DEFAULT_PUBLICATION_ID,
                        1,
                        DEFAULT_FORM_VERSION_ID,
                        DEFAULT_CONSENT_VERSION,
                        schema_hash,
                        json.dumps(form_schema, ensure_ascii=False),
                        json.dumps(ui_schema, ensure_ascii=False),
                        consent_text,
                        now,
                        now,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO capacity_counters (
                        event_id, organization_id, publication_id, capacity,
                        confirmed_count, waitlist_enabled, waitlist_count, next_waitlist_seq,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 0, true, 0, 1, 1, ?, ?)
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    (
                        DEFAULT_EVENT_ID,
                        DEFAULT_ORG_ID,
                        DEFAULT_PUBLICATION_ID,
                        default_capacity,
                        now,
                        now,
                    ),
                )
        return {
            "organization_id": DEFAULT_ORG_ID,
            "site_slug": DEFAULT_SITE_SLUG,
            "event_id": DEFAULT_EVENT_ID,
            "event_slug": DEFAULT_EVENT_SLUG,
        }

    def reset_demo_state(self, capacity: int = 5, waitlist_enabled: bool = True) -> None:
        """Reset registrations, waitlist, tickets, and outbox for live classroom demonstrations."""
        with self.transaction() as cur:
            cur.execute("DELETE FROM ticket_revocations")
            cur.execute("DELETE FROM tickets")
            cur.execute("DELETE FROM waitlist_entries")
            cur.execute("DELETE FROM registration_answers")
            cur.execute("DELETE FROM consents")
            cur.execute("DELETE FROM registrations")
            cur.execute("DELETE FROM application_revisions")
            cur.execute("DELETE FROM applications")
            cur.execute("DELETE FROM team_invitations")
            cur.execute("DELETE FROM team_memberships")
            cur.execute("DELETE FROM ventures")
            cur.execute("DELETE FROM idempotency_keys")
            cur.execute("DELETE FROM inbox")
            cur.execute("DELETE FROM outbox")
            cur.execute("DELETE FROM capacity_counters")
            cur.execute("DELETE FROM release_projections")
        self.seed_default_release(default_capacity=capacity)
        with self.transaction() as cur:
            cur.execute(
                "UPDATE capacity_counters SET capacity = ?, waitlist_enabled = ? WHERE event_id = ?",
                (capacity, bool(waitlist_enabled), DEFAULT_EVENT_ID),
            )

    def close(self) -> None:
        if self.backend == "postgres" and hasattr(self, "_pool"):
            self._pool.close()
        elif self.backend == "sqlite" and hasattr(self, "_conn"):
            self._conn.close()

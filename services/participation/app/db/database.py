"""
Transactional Database Layer for Team 3 — Participation.
Enforces:
- Atomic capacity locking (BEGIN IMMEDIATE + mutex lock for local SQLite / row-level lock semantics)
- State machine CHECK constraints identical to 001_initial_participation_schema.sql
- Tenant isolation by organization_id (AD-12)
- Default seeded release for IncubaUNAS 2026 so the demo works out-of-the-box.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator

from app.core.security import utc_now_iso

DEFAULT_ORG_ID = "0192f000-0000-7000-8000-000000000001"
DEFAULT_SITE_SLUG = "incuba-unas"
DEFAULT_EVENT_ID = "0192f000-0000-7000-8000-000000000101"
DEFAULT_EVENT_SLUG = "congreso-innovacion-demo-day-2026"
DEFAULT_PUBLICATION_ID = "0192f000-0000-7000-8000-000000000201"
DEFAULT_FORM_VERSION_ID = "0192f000-0000-7000-8000-000000000301"
DEFAULT_CONSENT_VERSION = "consent-unas-v1.0"


class ParticipationDatabase:
    """Thread-safe transactional database engine for Participation Service."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.environ.get(
            "PARTICIPATION_SQLITE_PATH", "/tmp/participation_service.db"
        )
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # Explicit transaction control
        )
        self._conn.row_factory = sqlite3.Row
        self.init_schema()
        self.seed_default_release()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        """Acquire atomic transactional lock (simulating SELECT ... FOR UPDATE on capacity_counters)."""
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
    def read_cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self._conn.cursor()
            try:
                yield cursor
            finally:
                cursor.close()

    def init_schema(self) -> None:
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 'registration', 'open', ?, ?, ?, ?, ?, ?, 0, 0, 1, ?, ?)
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
                    INSERT OR IGNORE INTO capacity_counters (
                        event_id, organization_id, publication_id, capacity,
                        confirmed_count, waitlist_enabled, waitlist_count, next_waitlist_seq,
                        revision, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 0, 1, 0, 1, 1, ?, ?)
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
                (capacity, 1 if waitlist_enabled else 0, DEFAULT_EVENT_ID),
            )

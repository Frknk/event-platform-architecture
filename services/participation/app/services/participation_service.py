"""
Domain Service for Team 3 — Participation.
Implements User Stories:
- PA-001: Load active published form & availability summary
- PA-002: Public registration with Idempotency-Key, AES-256-GCM PII encryption & consent record
- PA-004: Duplicate detection via normalized email HMAC blind index without PII enumeration leak
- PA-006: Self-service registration status lookup via signed token
- PA-007: Idempotent cancellation + ticket revocation + atomic waitlist promotion
- PA-008 & PA-015: Asymmetric JWS QR Ticket issuance, verification, rotation & revocation list
- PA-010: Release materialization from configuration.event.release_published.v1 via consumer inbox
- PA-011 & PA-012: Strict atomic capacity counter (confirmed <= capacity) & monotonic waitlist ordering
- PA-013: Automatic FIFO promotion from waitlist within the same transaction when capacity frees up
- PA-014: Manager confirmation/rejection for approval-gated registrations
- PA-020, PA-021, PA-025: Venture creation, team member invitations & immutable application submission
- PA-030: Backoffice listing with cursor, email HMAC search, and role-based PII masking
- PA-041: High-concurrency load simulation (3x peak) proving zero overbooking
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple
import uuid

from app.core.problem import ProblemException
from app.core.security import (
    generate_uuidv7,
    mask_email,
    mask_name,
    normalize_email,
    security_manager,
    utc_now_iso,
)
from app.db.database import (
    DEFAULT_CONSENT_VERSION,
    DEFAULT_EVENT_ID,
    DEFAULT_EVENT_SLUG,
    DEFAULT_FORM_VERSION_ID,
    DEFAULT_ORG_ID,
    DEFAULT_SITE_SLUG,
    ParticipationDatabase,
)


class ParticipationService:
    """Core transactional engine for Team 3 — Participation."""

    def __init__(self, db: ParticipationDatabase) -> None:
        self.db = db

    # -------------------------------------------------------------------------
    # Transactional Outbox & Inbox Helpers (EVENT-CONTRACTS.md)
    # -------------------------------------------------------------------------
    def _emit_outbox_event(
        self,
        cur: Any,
        *,
        organization_id: str,
        business_event_id: str,
        topic: str,
        aggregate_type: str,
        aggregate_id: str,
        aggregate_version: int,
        data: Dict[str, Any],
        actor_type: str = "user",
        actor_id: str = "anonymous",
        correlation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write a canonical v1 domain event envelope into the transactional outbox (zero PII)."""
        now = utc_now_iso()
        message_id = generate_uuidv7()
        domain_event_id = generate_uuidv7()
        corr_id = correlation_id or generate_uuidv7()
        partition_key = f"{organization_id}:{business_event_id}"

        envelope = {
            "message_id": message_id,
            "domain_event_id": domain_event_id,
            "topic": topic,
            "schema_version": 1,
            "producer": "participation",
            "organization_id": organization_id,
            "business_event_id": business_event_id,
            "occurred_at": now,
            "published_at": now,
            "actor": {"type": actor_type, "id": actor_id},
            "traceparent": f"00-{corr_id.replace('-', '')}-0192f00000007000-01",
            "correlation_id": corr_id,
            "causation_id": corr_id,
            "aggregate": {
                "type": aggregate_type,
                "id": aggregate_id,
                "version": aggregate_version,
            },
            "partition_key": partition_key,
            "data": data,
        }
        cur.execute(
            """
            INSERT INTO outbox (
                id, message_id, domain_event_id, organization_id, business_event_id,
                topic, schema_version, aggregate_type, aggregate_id, aggregate_version,
                partition_key, envelope_json, occurred_at, published_at, attempts
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                generate_uuidv7(),
                message_id,
                domain_event_id,
                organization_id,
                business_event_id,
                topic,
                aggregate_type,
                aggregate_id,
                aggregate_version,
                partition_key,
                json.dumps(envelope, ensure_ascii=False),
                now,
                now,
            ),
        )
        return envelope

    def _compute_public_availability_state(
        self, capacity: int, confirmed_count: int, waitlist_enabled: bool, binding_state: str
    ) -> str:
        if binding_state != "open":
            return "closed"
        remaining = max(0, capacity - confirmed_count)
        if remaining == 0:
            return "waitlist_only" if waitlist_enabled else "closed"
        if remaining <= max(1, int(capacity * 0.2)):
            return "limited"
        return "available"

    def _check_or_prepare_idempotency(
        self,
        cur: Any,
        *,
        organization_id: str,
        operation: str,
        idempotency_key: str,
        request_payload: Dict[str, Any],
    ) -> Optional[Tuple[int, Dict[str, Any]]]:
        """
        Verify Idempotency-Key (API-CONTRACTS.md):
        - Same key + same request hash -> return stored (status_code, response_body)
        - Same key + different request hash -> raise 409 idempotency_key_reused
        """
        if not idempotency_key or len(idempotency_key.strip()) < 8:
            raise ProblemException(
                status=400,
                code="validation_error",
                title="Missing or invalid Idempotency-Key",
                detail="Header 'Idempotency-Key' (min 8 chars) is mandatory for command endpoints.",
            )
        req_hash = hashlib.sha256(
            json.dumps(request_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

        cur.execute(
            """
            SELECT request_hash, status_code, response_body_json
            FROM idempotency_keys
            WHERE organization_id = ? AND operation = ? AND idempotency_key = ?
            """,
            (organization_id, operation, idempotency_key),
        )
        row = cur.fetchone()
        if row:
            if row["request_hash"] != req_hash:
                raise ProblemException(
                    status=409,
                    code="idempotency_key_reused",
                    title="Idempotency-Key reused with different payload",
                    detail="The provided Idempotency-Key was already used with a different request payload.",
                )
            return int(row["status_code"]), json.loads(row["response_body_json"])
        return None

    def _save_idempotency_result(
        self,
        cur: Any,
        *,
        organization_id: str,
        operation: str,
        idempotency_key: str,
        request_payload: Dict[str, Any],
        status_code: int,
        response_body: Dict[str, Any],
    ) -> None:
        req_hash = hashlib.sha256(
            json.dumps(request_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        now = utc_now_iso()
        cur.execute(
            """
            INSERT INTO idempotency_keys (
                id, organization_id, operation, idempotency_key,
                request_hash, status_code, response_body_json, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generate_uuidv7(),
                organization_id,
                operation,
                idempotency_key,
                req_hash,
                status_code,
                json.dumps(response_body, ensure_ascii=False),
                now,
                now,
            ),
        )

    # -------------------------------------------------------------------------
    # PA-010: Materialize Release from Event Configuration
    # -------------------------------------------------------------------------
    def ingest_release_published(self, event_envelope: Dict[str, Any]) -> Dict[str, Any]:
        """Consume configuration.event.release_published.v1 idempotently via inbox (PA-010)."""
        domain_event_id = event_envelope.get("domain_event_id") or generate_uuidv7()
        org_id = event_envelope.get("organization_id", DEFAULT_ORG_ID)
        event_id = event_envelope.get("business_event_id", DEFAULT_EVENT_ID)
        agg = event_envelope.get("aggregate", {})
        agg_version = int(agg.get("version", 1))
        data = event_envelope.get("data", {})

        with self.db.transaction() as cur:
            cur.execute(
                "SELECT id FROM inbox WHERE consumer_name = 'participation' AND domain_event_id = ?",
                (domain_event_id,),
            )
            if cur.fetchone():
                return {"status": "duplicate_ignored", "domain_event_id": domain_event_id}

            now = utc_now_iso()
            cur.execute(
                """
                INSERT INTO inbox (
                    id, organization_id, consumer_name, domain_event_id,
                    topic, aggregate_id, aggregate_version, processed_at
                ) VALUES (?, ?, 'participation', ?, ?, ?, ?, ?)
                """,
                (
                    generate_uuidv7(),
                    org_id,
                    domain_event_id,
                    event_envelope.get("topic", "configuration.event.release_published.v1"),
                    event_id,
                    agg_version,
                    now,
                ),
            )

            new_capacity = int(data.get("capacity", 5))
            waitlist_enabled = bool(data.get("waitlist_enabled", True))

            cur.execute(
                "SELECT confirmed_count FROM capacity_counters WHERE event_id = ?",
                (event_id,),
            )
            cap_row = cur.fetchone()
            if cap_row:
                if new_capacity < int(cap_row["confirmed_count"]):
                    raise ProblemException(
                        status=409,
                        code="invalid_state_transition",
                        title="Operational capacity conflict",
                        detail=(
                            f"Cannot reduce capacity ({new_capacity}) below already confirmed "
                            f"registrations ({cap_row['confirmed_count']})."
                        ),
                    )
                cur.execute(
                    """
                    UPDATE capacity_counters
                    SET capacity = ?, waitlist_enabled = ?, revision = revision + 1, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (new_capacity, waitlist_enabled, now, event_id),
                )

            return {
                "status": "materialized",
                "domain_event_id": domain_event_id,
                "event_id": event_id,
                "capacity": new_capacity,
                "waitlist_enabled": bool(waitlist_enabled),
            }

    # -------------------------------------------------------------------------
    # PA-001: Get Active Public Form
    # -------------------------------------------------------------------------
    def get_public_form(self, site_slug: str, event_slug: str) -> Dict[str, Any]:
        with self.db.read_cursor() as cur:
            cur.execute(
                """
                SELECT r.*, c.capacity, c.confirmed_count, c.waitlist_enabled, c.waitlist_count
                FROM release_projections r
                JOIN capacity_counters c ON c.event_id = r.event_id
                WHERE r.site_slug = ? AND r.event_slug = ? AND r.state = 'active'
                """,
                (site_slug, event_slug),
            )
            row = cur.fetchone()
            if not row:
                raise ProblemException(
                    status=404,
                    code="publication_incomplete",
                    title="Published event form not found",
                    detail=f"No active published release found for {site_slug}/{event_slug}.",
                )

            capacity = int(row["capacity"])
            confirmed = int(row["confirmed_count"])
            waitlist_enabled = bool(row["waitlist_enabled"])
            waitlist_count = int(row["waitlist_count"])
            remaining = max(0, capacity - confirmed)
            public_state = self._compute_public_availability_state(
                capacity, confirmed, waitlist_enabled, row["binding_state"]
            )

            return {
                "site_slug": row["site_slug"],
                "event_slug": row["event_slug"],
                "event_id": row["event_id"],
                "event_title": row["event_title"],
                "organization_id": row["organization_id"],
                "publication_id": row["publication_id"],
                "form_version_id": row["form_version_id"],
                "consent_version": row["consent_version"],
                "schema_hash": row["schema_hash"],
                "binding_state": row["binding_state"],
                "requires_approval": bool(row["requires_approval"]),
                "capacity_summary": {
                    "capacity": capacity,
                    "confirmed_count": confirmed,
                    "remaining": remaining,
                    "waitlist_enabled": waitlist_enabled,
                    "waitlist_count": waitlist_count,
                    "public_state": public_state,
                },
                "schema": json.loads(row["form_schema_json"]),
                "ui_schema": json.loads(row["ui_schema_json"]),
                "consent_text": row["consent_text"],
            }

    # -------------------------------------------------------------------------
    # PA-002, PA-004, PA-008, PA-011, PA-012, PA-015: Create Registration
    # -------------------------------------------------------------------------
    def create_registration(
        self,
        *,
        site_slug: str,
        event_slug: str,
        payload: Dict[str, Any],
        idempotency_key: str,
    ) -> Tuple[int, Dict[str, Any]]:
        with self.db.transaction() as cur:
            cur.execute(
                """
                SELECT r.*, c.capacity, c.confirmed_count, c.waitlist_enabled,
                       c.waitlist_count, c.next_waitlist_seq
                FROM release_projections r
                JOIN capacity_counters c ON c.event_id = r.event_id
                WHERE r.site_slug = ? AND r.event_slug = ? AND r.state = 'active'
                """,
                (site_slug, event_slug),
            )
            rel = cur.fetchone()
            if not rel:
                raise ProblemException(
                    status=404,
                    code="publication_incomplete",
                    title="Event release not active",
                    detail="Cannot accept registrations until release is completely materialized.",
                )

            org_id = rel["organization_id"]
            event_id = rel["event_id"]

            # 1. Idempotency check (PA-002)
            cached = self._check_or_prepare_idempotency(
                cur,
                organization_id=org_id,
                operation=f"create_registration:{event_id}",
                idempotency_key=idempotency_key,
                request_payload=payload,
            )
            if cached is not None:
                return cached

            # 2. Binding & version validations
            if rel["binding_state"] != "open":
                raise ProblemException(
                    status=409,
                    code="binding_not_open",
                    title="Registration window is not open",
                    detail="The registration form binding for this event is currently closed.",
                )

            if payload.get("form_version_id") != rel["form_version_id"]:
                raise ProblemException(
                    status=409,
                    code="form_version_mismatch",
                    title="Form version mismatch",
                    detail="Submitted form_version_id does not match the active published form version.",
                )

            if payload.get("consent_version") != rel["consent_version"] or not payload.get(
                "consent_accepted"
            ):
                raise ProblemException(
                    status=409,
                    code="consent_version_mismatch",
                    title="Explicit consent required",
                    detail="Valid acceptance of the current privacy consent version is required.",
                )

            # 3. Validate required JSON Schema fields server-side
            schema = json.loads(rel["form_schema_json"])
            required_fields = schema.get("required", [])
            answers = payload.get("answers") or {}
            missing = [f for f in required_fields if not answers.get(f)]
            if missing:
                raise ProblemException(
                    status=422,
                    code="validation_error",
                    title="Missing required form answers",
                    detail=f"Missing required schema fields: {', '.join(missing)}",
                    errors=[
                        {"pointer": f"/answers/{f}", "code": "required", "message": "Field is required"}
                        for f in missing
                    ],
                )

            attendee = payload.get("attendee") or {}
            raw_email = attendee.get("email", "")
            raw_name = attendee.get("full_name", "")
            if "@" not in raw_email or len(raw_name.strip()) < 2:
                raise ProblemException(
                    status=422,
                    code="validation_error",
                    title="Invalid attendee identity",
                    detail="Valid attendee full_name and email are required.",
                )

            # 4. Duplicate detection via tenant-scoped HMAC blind index (PA-004)
            email_hmac = security_manager.compute_email_hmac(org_id, raw_email)
            cur.execute(
                """
                SELECT id, state FROM registrations
                WHERE organization_id = ? AND event_id = ? AND email_hmac = ?
                  AND state NOT IN ('cancelled', 'rejected')
                """,
                (org_id, event_id, email_hmac),
            )
            existing = cur.fetchone()
            if existing:
                raise ProblemException(
                    status=409,
                    code="already_registered",
                    title="Duplicate registration detected",
                    detail="An active registration already exists for the normalized participant identity in this event.",
                )

            # 5. Atomic capacity decision (PA-011 & PA-012)
            capacity = int(rel["capacity"])
            confirmed_count = int(rel["confirmed_count"])
            waitlist_enabled = bool(rel["waitlist_enabled"])
            waitlist_count = int(rel["waitlist_count"])
            next_waitlist_seq = int(rel["next_waitlist_seq"])

            registration_id = generate_uuidv7()
            now = utc_now_iso()
            waitlist_position: Optional[int] = None
            ticket_id: Optional[str] = None
            confirmed_at: Optional[str] = None

            if bool(rel["requires_approval"]):
                state = "pending_review"
            elif confirmed_count < capacity:
                state = "confirmed"
                confirmed_at = now
                # Atomic counter update: safe under PostgreSQL READ COMMITTED
                # (never overwrite with a stale absolute value).
                cur.execute(
                    """
                    UPDATE capacity_counters
                    SET confirmed_count = confirmed_count + 1, revision = revision + 1, updated_at = ?
                    WHERE event_id = ? AND confirmed_count < capacity
                    """,
                    (now, event_id),
                )
                if cur.rowcount != 1:
                    raise ProblemException(
                        status=409,
                        code="capacity_exhausted",
                        title="Concurrent capacity exhausted",
                        detail="Capacity was reached concurrently.",
                    )
                confirmed_count += 1
            elif waitlist_enabled:
                state = "waitlisted"
                waitlist_position = next_waitlist_seq
                waitlist_count += 1
                cur.execute(
                    """
                    UPDATE capacity_counters
                    SET waitlist_count = ?, next_waitlist_seq = ?, revision = revision + 1, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (waitlist_count, next_waitlist_seq + 1, now, event_id),
                )
            else:
                raise ProblemException(
                    status=409,
                    code="capacity_exhausted",
                    title="Event capacity exhausted",
                    detail="All spots are taken and waitlist is disabled for this event.",
                )

            # 6. Encrypt PII at rest with AES-256-GCM (AD-8)
            aad = f"{org_id}:{event_id}:{registration_id}"
            pii_ciphertext = security_manager.encrypt_pii(
                {
                    "full_name": raw_name.strip(),
                    "email": normalize_email(raw_email),
                    "document_number": attendee.get("document_number", ""),
                    "phone": attendee.get("phone", ""),
                    "institution": attendee.get("institution", "UNAS"),
                },
                associated_data=aad,
            )
            answers_ciphertext = security_manager.encrypt_pii(answers, associated_data=aad)

            cur.execute(
                """
                INSERT INTO registrations (
                    id, organization_id, event_id, publication_id, form_version_id,
                    state, email_hmac, email_masked, full_name_masked,
                    pii_ciphertext, pii_key_id, source, revision,
                    created_at, updated_at, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'public_web', 1, ?, ?, ?)
                """,
                (
                    registration_id,
                    org_id,
                    event_id,
                    rel["publication_id"],
                    rel["form_version_id"],
                    state,
                    email_hmac,
                    mask_email(raw_email),
                    mask_name(raw_name),
                    pii_ciphertext,
                    security_manager.pii_key_id,
                    now,
                    now,
                    confirmed_at,
                ),
            )

            cur.execute(
                """
                INSERT INTO registration_answers (
                    id, organization_id, registration_id, form_version_id,
                    answers_ciphertext, key_id, schema_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generate_uuidv7(),
                    org_id,
                    registration_id,
                    rel["form_version_id"],
                    answers_ciphertext,
                    security_manager.pii_key_id,
                    rel["schema_hash"],
                    now,
                    now,
                ),
            )

            consent_hash = hashlib.sha256(rel["consent_text"].encode("utf-8")).hexdigest()
            cur.execute(
                """
                INSERT INTO consents (
                    id, organization_id, subject_type, subject_id,
                    consent_version, consent_hash, accepted_at
                ) VALUES (?, ?, 'registration', ?, ?, ?, ?)
                """,
                (
                    generate_uuidv7(),
                    org_id,
                    registration_id,
                    rel["consent_version"],
                    consent_hash,
                    now,
                ),
            )

            # 7. Emit domain events & issue Ticket JWS if confirmed
            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.registration.created.v1",
                aggregate_type="registration",
                aggregate_id=registration_id,
                aggregate_version=1,
                data={
                    "registration_id": registration_id,
                    "state": state,
                    "source": "public_web",
                    "form_version_id": rel["form_version_id"],
                },
            )

            if state == "confirmed":
                ticket_record = self._issue_ticket_internal(
                    cur,
                    organization_id=org_id,
                    event_id=event_id,
                    registration_id=registration_id,
                )
                ticket_id = ticket_record["id"]
                self._emit_outbox_event(
                    cur,
                    organization_id=org_id,
                    business_event_id=event_id,
                    topic="participation.registration.confirmed.v1",
                    aggregate_type="registration",
                    aggregate_id=registration_id,
                    aggregate_version=1,
                    data={
                        "registration_id": registration_id,
                        "event_id": event_id,
                        "ticket_id": ticket_id,
                        "ticket_version": 1,
                        "confirmed_at": now,
                    },
                )
            elif state == "waitlisted" and waitlist_position is not None:
                cur.execute(
                    """
                    INSERT INTO waitlist_entries (
                        id, organization_id, event_id, registration_id,
                        position, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'waiting', ?, ?)
                    """,
                    (
                        generate_uuidv7(),
                        org_id,
                        event_id,
                        registration_id,
                        waitlist_position,
                        now,
                        now,
                    ),
                )
                self._emit_outbox_event(
                    cur,
                    organization_id=org_id,
                    business_event_id=event_id,
                    topic="participation.registration.waitlisted.v1",
                    aggregate_type="registration",
                    aggregate_id=registration_id,
                    aggregate_version=1,
                    data={
                        "registration_id": registration_id,
                        "event_id": event_id,
                        "position": waitlist_position,
                        "policy_version": 1,
                    },
                )

            # Availability changed event for Team 1 Content Projection
            public_state = self._compute_public_availability_state(
                capacity, confirmed_count, waitlist_enabled, rel["binding_state"]
            )
            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.availability.changed.v1",
                aggregate_type="availability",
                aggregate_id=event_id,
                aggregate_version=confirmed_count + waitlist_count,
                data={
                    "event_id": event_id,
                    "public_state": public_state,
                    "remaining": max(0, capacity - confirmed_count),
                    "capacity": capacity,
                    "waitlist_count": waitlist_count,
                    "as_of": now,
                },
            )

            self_token = security_manager.issue_self_access_token(
                organization_id=org_id,
                registration_id=registration_id,
            )

            receipt = {
                "id": registration_id,
                "organization_id": org_id,
                "event_id": event_id,
                "state": state,
                "waitlist_position": waitlist_position,
                "ticket_id": ticket_id,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
                "self_access_token": self_token,
            }

            self._save_idempotency_result(
                cur,
                organization_id=org_id,
                operation=f"create_registration:{event_id}",
                idempotency_key=idempotency_key,
                request_payload=payload,
                status_code=201,
                response_body=receipt,
            )
            return 201, receipt

    def _issue_ticket_internal(
        self,
        cur: Any,
        *,
        organization_id: str,
        event_id: str,
        registration_id: str,
        version: int = 1,
    ) -> Dict[str, Any]:
        ticket_id = generate_uuidv7()
        jti = generate_uuidv7()
        jws_compact, claims, issued_at, expires_at = security_manager.issue_ticket_jws(
            jti=jti,
            organization_id=organization_id,
            event_id=event_id,
            registration_id=registration_id,
            scope="general",
            version=version,
        )
        cur.execute(
            """
            INSERT INTO tickets (
                id, organization_id, event_id, registration_id, jti,
                state, version, key_id, audience, scope, ticket_jws,
                issued_at, expires_at, revision
            ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, 'onsite', 'general', ?, ?, ?, 1)
            """,
            (
                ticket_id,
                organization_id,
                event_id,
                registration_id,
                jti,
                version,
                security_manager.ticket_key_id,
                jws_compact,
                issued_at,
                expires_at,
            ),
        )
        self._emit_outbox_event(
            cur,
            organization_id=organization_id,
            business_event_id=event_id,
            topic="participation.ticket.issued.v1",
            aggregate_type="ticket",
            aggregate_id=ticket_id,
            aggregate_version=version,
            data={
                "ticket_id": ticket_id,
                "registration_id": registration_id,
                "event_id": event_id,
                "scope": "general",
                "version": version,
                "key_id": security_manager.ticket_key_id,
                "expires_at": expires_at,
            },
        )
        return {
            "id": ticket_id,
            "jti": jti,
            "ticket_jws": jws_compact,
            "claims": claims,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }

    # -------------------------------------------------------------------------
    # PA-006 & PA-008: Self-Service Registration & Ticket Lookup
    # -------------------------------------------------------------------------
    def get_registration_self(self, registration_id: str) -> Dict[str, Any]:
        with self.db.read_cursor() as cur:
            cur.execute("SELECT * FROM registrations WHERE id = ?", (registration_id,))
            reg = cur.fetchone()
            if not reg:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Registration not found",
                    detail="The requested registration does not exist.",
                )

            org_id = reg["organization_id"]
            event_id = reg["event_id"]
            aad = f"{org_id}:{event_id}:{registration_id}"
            if reg["full_name_masked"] == "ANONIMIZADO":
                pii = {
                    "full_name": "ANONIMIZADO",
                    "email": "anonimizado@tombstone.internal",
                    "document_number": "***",
                    "phone": "***",
                    "institution": "***",
                }
            else:
                pii = security_manager.decrypt_pii(reg["pii_ciphertext"], associated_data=aad)

            cur.execute(
                "SELECT answers_ciphertext FROM registration_answers WHERE registration_id = ?",
                (registration_id,),
            )
            ans_row = cur.fetchone()
            answers = (
                security_manager.decrypt_pii(ans_row["answers_ciphertext"], associated_data=aad)
                if ans_row
                else {}
            )

            cur.execute(
                "SELECT position, state, promoted_at FROM waitlist_entries WHERE registration_id = ?",
                (registration_id,),
            )
            wl_row = cur.fetchone()

            cur.execute(
                "SELECT id, state, jti, version, issued_at, revoked_at FROM tickets WHERE registration_id = ? ORDER BY version DESC LIMIT 1",
                (registration_id,),
            )
            tkt_row = cur.fetchone()

            return {
                "id": reg["id"],
                "organization_id": org_id,
                "event_id": event_id,
                "state": reg["state"],
                "revision": int(reg["revision"]),
                "attendee": pii,
                "answers": answers,
                "waitlist": dict(wl_row) if wl_row else None,
                "ticket_summary": dict(tkt_row) if tkt_row else None,
                "created_at": reg["created_at"],
                "updated_at": reg["updated_at"],
                "confirmed_at": reg["confirmed_at"],
                "cancelled_at": reg["cancelled_at"],
            }

    def get_registration_ticket(self, registration_id: str) -> Dict[str, Any]:
        """Return QR Ticket JWS metadata (PA-008: only confirmed has active ticket; shows revocation)."""
        with self.db.read_cursor() as cur:
            cur.execute("SELECT id, state FROM registrations WHERE id = ?", (registration_id,))
            reg = cur.fetchone()
            if not reg:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Registration not found",
                    detail="Registration not found.",
                )

            cur.execute(
                """
                SELECT * FROM tickets
                WHERE registration_id = ?
                ORDER BY version DESC LIMIT 1
                """,
                (registration_id,),
            )
            tkt = cur.fetchone()
            if not tkt:
                raise ProblemException(
                    status=409,
                    code="waitlisted" if reg["state"] == "waitlisted" else "invalid_state_transition",
                    title="Ticket not available for current registration state",
                    detail=f"Registration is in state '{reg['state']}' and does not have an issued ticket.",
                )

            header, claims = security_manager.verify_jws(tkt["ticket_jws"], expected_aud="onsite")
            return {
                "id": tkt["id"],
                "registration_id": tkt["registration_id"],
                "event_id": tkt["event_id"],
                "organization_id": tkt["organization_id"],
                "state": tkt["state"],
                "version": int(tkt["version"]),
                "key_id": tkt["key_id"],
                "audience": tkt["audience"],
                "scope": tkt["scope"],
                "jti": tkt["jti"],
                "ticket_jws": tkt["ticket_jws"],
                "decoded_header": header,
                "decoded_claims": claims,
                "issued_at": tkt["issued_at"],
                "expires_at": tkt["expires_at"],
                "revoked_at": tkt["revoked_at"],
            }

    # -------------------------------------------------------------------------
    # PA-007 & PA-013: Cancel Registration + Revoke Ticket + Promote Waitlist
    # -------------------------------------------------------------------------
    def cancel_registration(
        self,
        *,
        registration_id: str,
        reason: str,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM registrations WHERE id = ?", (registration_id,))
            reg = cur.fetchone()
            if not reg:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Registration not found",
                    detail="Registration not found.",
                )

            org_id = reg["organization_id"]
            event_id = reg["event_id"]
            payload = {"registration_id": registration_id, "reason": reason}

            cached = self._check_or_prepare_idempotency(
                cur,
                organization_id=org_id,
                operation=f"cancel_registration:{registration_id}",
                idempotency_key=idempotency_key,
                request_payload=payload,
            )
            if cached is not None:
                return cached[1]

            prior_state = reg["state"]
            now = utc_now_iso()

            # Idempotent if already cancelled (PA-007)
            if prior_state == "cancelled":
                result = {
                    "id": registration_id,
                    "state": "cancelled",
                    "prior_state": "cancelled",
                    "ticket_revoked": False,
                    "promoted_registration_id": None,
                    "cancelled_at": reg["cancelled_at"],
                }
                self._save_idempotency_result(
                    cur,
                    organization_id=org_id,
                    operation=f"cancel_registration:{registration_id}",
                    idempotency_key=idempotency_key,
                    request_payload=payload,
                    status_code=200,
                    response_body=result,
                )
                return result

            if prior_state not in ("confirmed", "waitlisted", "pending_review", "pending_verification"):
                raise ProblemException(
                    status=409,
                    code="invalid_state_transition",
                    title="Invalid state transition",
                    detail=f"Cannot cancel registration from state '{prior_state}'.",
                )

            new_revision = int(reg["revision"]) + 1
            cur.execute(
                """
                UPDATE registrations
                SET state = 'cancelled', cancellation_reason = ?, cancelled_at = ?,
                    revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (reason, now, new_revision, now, registration_id),
            )

            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.registration.cancelled.v1",
                aggregate_type="registration",
                aggregate_id=registration_id,
                aggregate_version=new_revision,
                data={
                    "registration_id": registration_id,
                    "prior_state": prior_state,
                    "cancelled_at": now,
                },
            )

            ticket_revoked = False
            promoted_reg_id: Optional[str] = None
            promoted_ticket_id: Optional[str] = None

            if prior_state == "waitlisted":
                cur.execute(
                    """
                    UPDATE waitlist_entries
                    SET state = 'cancelled', updated_at = ?
                    WHERE registration_id = ? AND state = 'waiting'
                    """,
                    (now, registration_id),
                )
                cur.execute(
                    """
                    UPDATE capacity_counters
                    SET waitlist_count = MAX(0, waitlist_count - 1), revision = revision + 1, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (now, event_id),
                )

            elif prior_state == "confirmed":
                # 1. Revoke active ticket (PA-007, PA-015)
                cur.execute(
                    """
                    SELECT id, jti, version FROM tickets
                    WHERE registration_id = ? AND state = 'active'
                    """,
                    (registration_id,),
                )
                tkt = cur.fetchone()
                if tkt:
                    ticket_revoked = True
                    cur.execute(
                        """
                        UPDATE tickets
                        SET state = 'revoked', revoked_at = ?, revision = revision + 1
                        WHERE id = ?
                        """,
                        (now, tkt["id"]),
                    )
                    cur.execute(
                        """
                        INSERT INTO ticket_revocations (
                            id, organization_id, event_id, ticket_id, jti, reason_code, revoked_at
                        ) VALUES (?, ?, ?, ?, ?, 'registration_cancelled', ?)
                        """,
                        (
                            generate_uuidv7(),
                            org_id,
                            event_id,
                            tkt["id"],
                            tkt["jti"],
                            now,
                        ),
                    )
                    self._emit_outbox_event(
                        cur,
                        organization_id=org_id,
                        business_event_id=event_id,
                        topic="participation.ticket.revoked.v1",
                        aggregate_type="ticket",
                        aggregate_id=tkt["id"],
                        aggregate_version=int(tkt["version"]) + 1,
                        data={
                            "ticket_id": tkt["id"],
                            "event_id": event_id,
                            "version": int(tkt["version"]),
                            "reason_code": "registration_cancelled",
                            "revoked_at": now,
                        },
                    )

                # 2. Release 1 spot and check for automatic FIFO waitlist promotion in SAME transaction (PA-013)
                cur.execute(
                    """
                    SELECT id, registration_id, position
                    FROM waitlist_entries
                    WHERE organization_id = ? AND event_id = ? AND state = 'waiting'
                    ORDER BY position ASC
                    LIMIT 1
                    """,
                    (org_id, event_id),
                )
                next_in_line = cur.fetchone()

                if next_in_line:
                    # Promote automatically without dropping confirmed_count
                    promoted_reg_id = next_in_line["registration_id"]
                    cur.execute(
                        """
                        UPDATE waitlist_entries
                        SET state = 'promoted', promoted_at = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, next_in_line["id"]),
                    )
                    cur.execute(
                        """
                        UPDATE registrations
                        SET state = 'confirmed', confirmed_at = ?, revision = revision + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, now, promoted_reg_id),
                    )
                    cur.execute(
                        """
                        UPDATE capacity_counters
                        SET waitlist_count = MAX(0, waitlist_count - 1), revision = revision + 1, updated_at = ?
                        WHERE event_id = ?
                        """,
                        (now, event_id),
                    )
                    new_tkt = self._issue_ticket_internal(
                        cur,
                        organization_id=org_id,
                        event_id=event_id,
                        registration_id=promoted_reg_id,
                    )
                    promoted_ticket_id = new_tkt["id"]
                    self._emit_outbox_event(
                        cur,
                        organization_id=org_id,
                        business_event_id=event_id,
                        topic="participation.registration.promoted.v1",
                        aggregate_type="registration",
                        aggregate_id=promoted_reg_id,
                        aggregate_version=2,
                        data={
                            "registration_id": promoted_reg_id,
                            "prior_state": "waitlisted",
                            "waitlist_position": int(next_in_line["position"]),
                            "ticket_id": promoted_ticket_id,
                            "ticket_version": 1,
                            "promoted_at": now,
                        },
                    )
                else:
                    # No one waiting: decrement confirmed_count
                    cur.execute(
                        """
                        UPDATE capacity_counters
                        SET confirmed_count = MAX(0, confirmed_count - 1), revision = revision + 1, updated_at = ?
                        WHERE event_id = ?
                        """,
                        (now, event_id),
                    )

            # Emit updated availability
            cur.execute(
                "SELECT capacity, confirmed_count, waitlist_enabled, waitlist_count FROM capacity_counters WHERE event_id = ?",
                (event_id,),
            )
            cap = cur.fetchone()
            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.availability.changed.v1",
                aggregate_type="availability",
                aggregate_id=event_id,
                aggregate_version=1,
                data={
                    "event_id": event_id,
                    "public_state": self._compute_public_availability_state(
                        int(cap["capacity"]),
                        int(cap["confirmed_count"]),
                        bool(cap["waitlist_enabled"]),
                        "open",
                    ),
                    "remaining": max(0, int(cap["capacity"]) - int(cap["confirmed_count"])),
                    "capacity": int(cap["capacity"]),
                    "waitlist_count": int(cap["waitlist_count"]),
                    "as_of": now,
                },
            )

            result = {
                "id": registration_id,
                "state": "cancelled",
                "prior_state": prior_state,
                "ticket_revoked": ticket_revoked,
                "promoted_registration_id": promoted_reg_id,
                "promoted_ticket_id": promoted_ticket_id,
                "cancelled_at": now,
            }
            self._save_idempotency_result(
                cur,
                organization_id=org_id,
                operation=f"cancel_registration:{registration_id}",
                idempotency_key=idempotency_key,
                request_payload=payload,
                status_code=200,
                response_body=result,
            )
            return result

    # -------------------------------------------------------------------------
    # PA-D: Backoffice & Admissibility (PA-030 .. PA-035)
    # -------------------------------------------------------------------------
    def list_event_registrations(
        self,
        *,
        event_id: str,
        state: Optional[str] = None,
        email_query: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        cursor: Optional[str] = None,
        reveal_pii: bool = False,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """PA-030: Cursor-paginated listing with email HMAC filter and role-based PII masking."""
        with self.db.read_cursor() as cur:
            query = """
                SELECT r.*, w.position AS waitlist_position, w.state AS waitlist_state,
                       t.id AS ticket_id, t.state AS ticket_state, t.jti AS ticket_jti
                FROM registrations r
                LEFT JOIN waitlist_entries w ON w.registration_id = r.id
                LEFT JOIN tickets t ON t.registration_id = r.id
                WHERE r.event_id = ?
            """
            params: List[Any] = [event_id]

            if state:
                query += " AND r.state = ?"
                params.append(state)

            if email_query:
                email_hmac = security_manager.compute_email_hmac(DEFAULT_ORG_ID, email_query)
                query += " AND r.email_hmac = ?"
                params.append(email_hmac)

            if from_date:
                query += " AND r.created_at >= ?"
                params.append(from_date)

            if to_date:
                query += " AND r.created_at <= ?"
                params.append(to_date)

            if cursor:
                if "|" in cursor:
                    cursor_created_at, cursor_id = cursor.split("|", 1)
                    query += " AND (r.created_at > ? OR (r.created_at = ? AND r.id > ?))"
                    params.extend([cursor_created_at, cursor_created_at, cursor_id])
                else:
                    query += " AND r.created_at > ?"
                    params.append(cursor)

            query += " ORDER BY r.created_at ASC LIMIT ?"
            fetch_limit = min(100, max(1, limit))
            params.append(fetch_limit)

            cur.execute(query, tuple(params))
            rows = cur.fetchall()

            items = []
            for r in rows:
                org_id = r["organization_id"]
                reg_id = r["id"]
                aad = f"{org_id}:{event_id}:{reg_id}"
                if reveal_pii:
                    pii = security_manager.decrypt_pii(r["pii_ciphertext"], associated_data=aad)
                    display_name = pii.get("full_name", r["full_name_masked"])
                    display_email = pii.get("email", r["email_masked"])
                    institution = pii.get("institution", "UNAS")
                    phone = pii.get("phone", "***")
                    document_number = pii.get("document_number", "***")
                else:
                    display_name = r["full_name_masked"]
                    display_email = r["email_masked"]
                    institution = "***"
                    phone = "***"
                    document_number = "***"

                items.append(
                    {
                        "id": reg_id,
                        "organization_id": org_id,
                        "event_id": event_id,
                        "state": r["state"],
                        "full_name": display_name,
                        "email": display_email,
                        "phone": phone,
                        "document_number": document_number,
                        "institution": institution,
                        "email_hmac": r["email_hmac"],
                        "pii_key_id": r["pii_key_id"],
                        "pii_ciphertext_preview": r["pii_ciphertext"][:48] + "...",
                        "waitlist_position": r["waitlist_position"],
                        "waitlist_state": r["waitlist_state"],
                        "ticket_id": r["ticket_id"],
                        "ticket_state": r["ticket_state"],
                        "ticket_jti": r["ticket_jti"],
                        "self_access_token": security_manager.issue_self_access_token(
                            organization_id=org_id, registration_id=reg_id
                        ),
                        "created_at": r["created_at"],
                        "confirmed_at": r["confirmed_at"],
                        "cancelled_at": r["cancelled_at"],
                    }
                )

            next_cursor = (
                f"{items[-1]['created_at']}|{items[-1]['id']}"
                if len(items) == fetch_limit
                else None
            )
            return {"items": items, "next_cursor": next_cursor}

    def correct_registration_admin(
        self,
        *,
        registration_id: str,
        reason: str,
        updates: Dict[str, Any],
        actor: str = "manager",
    ) -> Dict[str, Any]:
        """PA-031: Manager corrects allowlisted admin fields without altering questionnaire answers or snapshot."""
        if not reason or len(reason.strip()) < 3:
            raise ProblemException(
                status=400,
                code="missing_correction_reason",
                title="Motivo requerido",
                detail="Se debe proporcionar una justificación auditada para la corrección administrativa.",
            )

        allowlisted_fields = {"full_name", "phone", "institution", "document_number"}
        invalid_keys = set(updates.keys()) - allowlisted_fields
        if invalid_keys:
            raise ProblemException(
                status=400,
                code="field_not_correctable",
                title="Campo no modificable",
                detail=f"Los campos {list(invalid_keys)} no son modificables administrativamente. Respuestas de formulario y snapshots son inmutables.",
            )

        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM registrations WHERE id = ?", (registration_id,))
            reg = cur.fetchone()
            if not reg:
                raise ProblemException(
                    status=404,
                    code="registration_not_found",
                    title="Registro no encontrado",
                    detail="No existe el registro especificado.",
                )

            org_id = reg["organization_id"]
            event_id = reg["event_id"]
            aad = f"{org_id}:{event_id}:{registration_id}"

            # Decrypt current PII, apply updates
            current_pii = security_manager.decrypt_pii(reg["pii_ciphertext"], associated_data=aad)
            updated_pii = {**current_pii, **updates}
            new_ciphertext = security_manager.encrypt_pii(updated_pii, associated_data=aad)

            new_name_masked = mask_name(updated_pii.get("full_name", "")) if "full_name" in updates else reg["full_name_masked"]
            now = utc_now_iso()

            cur.execute(
                """
                UPDATE registrations
                SET pii_ciphertext = ?, full_name_masked = ?, revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (new_ciphertext, new_name_masked, now, registration_id),
            )

            # Record audit log
            cur.execute(
                """
                INSERT INTO audit_logs (
                    id, organization_id, actor, action, target_type, target_id, reason, payload_json, created_at
                ) VALUES (?, ?, ?, 'correct_registration', 'registration', ?, ?, ?, ?)
                """,
                (
                    generate_uuidv7(),
                    org_id,
                    actor,
                    registration_id,
                    reason,
                    json.dumps({"updated_fields": list(updates.keys())}, ensure_ascii=False),
                    now,
                ),
            )

            # Emit audit event
            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.registration.corrected.v1",
                aggregate_type="registration",
                aggregate_id=registration_id,
                aggregate_version=1,
                data={
                    "registration_id": registration_id,
                    "updated_fields": list(updates.keys()),
                    "reason": reason,
                    "actor": actor,
                    "corrected_at": now,
                },
            )

            return {
                "id": registration_id,
                "status": "corrected",
                "updated_fields": list(updates.keys()),
                "reason": reason,
                "full_name_masked": new_name_masked,
                "updated_at": now,
            }

    def list_event_applications(
        self,
        *,
        event_id: str,
        state: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """PA-032: List venture applications for reviewer/manager admissibility evaluation."""
        with self.db.read_cursor() as cur:
            query = """
                SELECT a.id, a.organization_id, a.event_id, a.venture_id,
                       a.form_version_id, a.state, a.current_revision_number,
                       a.revision, a.snapshot_hash, a.created_at, a.updated_at,
                       a.submitted_at,
                       v.name AS venture_name, v.track AS venture_track,
                       (SELECT count(*) FROM application_files f WHERE f.application_id = a.id) AS files_count,
                       (SELECT count(*) FROM team_memberships m WHERE m.venture_id = a.venture_id AND m.state = 'active') AS active_members_count
                FROM applications a
                JOIN ventures v ON v.id = a.venture_id
                WHERE a.event_id = ?
            """
            params: List[Any] = [event_id]
            if state:
                query += " AND a.state = ?"
                params.append(state)
            query += " ORDER BY a.created_at DESC"

            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def admit_application(
        self,
        *,
        application_id: str,
        expected_revision: int,
        reason: str,
        checklist: Optional[Dict[str, bool]] = None,
        actor: str = "reviewer",
    ) -> Dict[str, Any]:
        """PA-032: Reviewer admits application after validating admissibility checklist."""
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM applications WHERE id = ?", (application_id,))
            app_row = cur.fetchone()
            if not app_row:
                raise ProblemException(status=404, code="application_not_found", title="Postulación no encontrada", detail="No existe la postulación.")
            if int(app_row["revision"]) != int(expected_revision):
                raise ProblemException(status=412, code="stale_revision", title="Revisión desactualizada", detail="La revisión no coincide con expected_revision.")
            if app_row["state"] not in ("submitted", "resubmitted"):
                raise ProblemException(status=409, code="invalid_state_transition", title="Estado no admisible", detail=f"Solo se pueden evaluar postulaciones en estado 'submitted' o 'resubmitted'. Estado actual: '{app_row['state']}'.")

            now = utc_now_iso()
            org_id = app_row["organization_id"]
            event_id = app_row["event_id"]
            new_rev = int(app_row["revision"]) + 1

            cur.execute(
                "UPDATE applications SET state = 'admitted', revision = ?, updated_at = ? WHERE id = ?",
                (new_rev, now, application_id),
            )

            cur.execute(
                """
                INSERT INTO audit_logs (
                    id, organization_id, actor, action, target_type, target_id, reason, payload_json, created_at
                ) VALUES (?, ?, ?, 'admit_application', 'application', ?, ?, ?, ?)
                """,
                (
                    generate_uuidv7(),
                    org_id,
                    actor,
                    application_id,
                    reason,
                    json.dumps({"checklist": checklist or {}}, ensure_ascii=False),
                    now,
                ),
            )

            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.application.admitted.v1",
                aggregate_type="application",
                aggregate_id=application_id,
                aggregate_version=new_rev,
                data={
                    "application_id": application_id,
                    "state": "admitted",
                    "reason": reason,
                    "checklist": checklist or {},
                    "admitted_at": now,
                },
            )
            return {
                "application_id": application_id,
                "state": "admitted",
                "revision": new_rev,
                "reason": reason,
                "checklist": checklist or {},
                "updated_at": now,
            }

    def reject_application(
        self,
        *,
        application_id: str,
        expected_revision: int,
        reason: str,
        checklist: Optional[Dict[str, bool]] = None,
        actor: str = "reviewer",
    ) -> Dict[str, Any]:
        """PA-032: Reviewer rejects application with justification."""
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM applications WHERE id = ?", (application_id,))
            app_row = cur.fetchone()
            if not app_row:
                raise ProblemException(status=404, code="application_not_found", title="Postulación no encontrada", detail="No existe la postulación.")
            if int(app_row["revision"]) != int(expected_revision):
                raise ProblemException(status=412, code="stale_revision", title="Revisión desactualizada", detail="La revisión no coincide con expected_revision.")
            if app_row["state"] not in ("submitted", "resubmitted"):
                raise ProblemException(status=409, code="invalid_state_transition", title="Estado no rechazable", detail=f"Solo se pueden evaluar postulaciones en estado 'submitted' o 'resubmitted'. Estado actual: '{app_row['state']}'.")

            now = utc_now_iso()
            org_id = app_row["organization_id"]
            event_id = app_row["event_id"]
            new_rev = int(app_row["revision"]) + 1

            cur.execute(
                "UPDATE applications SET state = 'rejected', revision = ?, updated_at = ? WHERE id = ?",
                (new_rev, now, application_id),
            )

            cur.execute(
                """
                INSERT INTO audit_logs (
                    id, organization_id, actor, action, target_type, target_id, reason, payload_json, created_at
                ) VALUES (?, ?, ?, 'reject_application', 'application', ?, ?, ?, ?)
                """,
                (
                    generate_uuidv7(),
                    org_id,
                    actor,
                    application_id,
                    reason,
                    json.dumps({"checklist": checklist or {}}, ensure_ascii=False),
                    now,
                ),
            )

            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.application.rejected.v1",
                aggregate_type="application",
                aggregate_id=application_id,
                aggregate_version=new_rev,
                data={
                    "application_id": application_id,
                    "state": "rejected",
                    "reason": reason,
                    "rejected_at": now,
                },
            )
            return {
                "application_id": application_id,
                "state": "rejected",
                "revision": new_rev,
                "reason": reason,
                "updated_at": now,
            }

    def import_registrations(
        self,
        *,
        event_id: str,
        rows: List[Dict[str, Any]],
        legal_basis: str,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """PA-033: Controlled batch/CSV registration import with dry-run, row-level errors and explicit legal basis."""
        if not legal_basis or len(legal_basis.strip()) < 3:
            raise ProblemException(
                status=400,
                code="missing_legal_basis",
                title="Base legal requerida",
                detail="Se debe especificar la base legal del consentimiento para la importación masiva.",
            )

        with self.db.transaction() as cur:
            cur.execute(
                "SELECT * FROM capacity_counters WHERE event_id = ?",
                (event_id,),
            )
            cap_row = cur.fetchone()
            org_id = cap_row["organization_id"] if cap_row else DEFAULT_ORG_ID
            capacity = int(cap_row["capacity"]) if cap_row else 500
            confirmed = int(cap_row["confirmed_count"]) if cap_row else 0
            remaining = max(0, capacity - confirmed)

            valid_rows = []
            row_errors = []
            seen_emails = set()

            for idx, r in enumerate(rows, start=1):
                raw_email = r.get("email", "").strip().lower()
                name = r.get("full_name", "").strip()
                doc = r.get("document_number", "").strip()

                if not raw_email or "@" not in raw_email:
                    row_errors.append({"row": idx, "email": raw_email, "code": "invalid_email", "detail": "Correo inválido o ausente."})
                    continue
                if not name:
                    row_errors.append({"row": idx, "email": raw_email, "code": "missing_name", "detail": "El nombre completo es obligatorio."})
                    continue
                if raw_email in seen_emails:
                    row_errors.append({"row": idx, "email": raw_email, "code": "duplicate_in_batch", "detail": "Correo duplicado dentro del mismo lote."})
                    continue
                seen_emails.add(raw_email)

                # Check duplicate against DB
                e_hmac = security_manager.compute_email_hmac(org_id, raw_email)
                cur.execute(
                    "SELECT id FROM registrations WHERE event_id = ? AND email_hmac = ? AND state IN ('confirmed', 'waitlisted')",
                    (event_id, e_hmac),
                )
                if cur.fetchone():
                    row_errors.append({"row": idx, "email": raw_email, "code": "already_registered", "detail": "El participante ya cuenta con un registro activo."})
                    continue

                valid_rows.append({
                    "row_index": idx,
                    "email": raw_email,
                    "email_hmac": e_hmac,
                    "full_name": name,
                    "document_number": doc,
                    "institution": r.get("institution", "UNAS"),
                    "external_ref": r.get("external_ref", f"import-{idx}"),
                })

            if dry_run:
                return {
                    "dry_run": True,
                    "total_rows": len(rows),
                    "valid_count": len(valid_rows),
                    "created_count": 0,
                    "imported_count": 0,
                    "error_count": len(row_errors),
                    "capacity_available": remaining,
                    "errors": row_errors,
                }

            # Enforce capacity
            if len(valid_rows) > remaining:
                raise ProblemException(
                    status=409,
                    code="capacity_exceeded",
                    title="Cupos insuficientes para importación",
                    detail=f"El lote contiene {len(valid_rows)} registros válidos pero solo restan {remaining} cupos disponibles.",
                )

            now = utc_now_iso()
            imported_ids = []
            for item in valid_rows:
                reg_id = generate_uuidv7()
                aad = f"{org_id}:{event_id}:{reg_id}"
                pii_payload = {
                    "full_name": item["full_name"],
                    "email": item["email"],
                    "document_number": item["document_number"],
                    "institution": item["institution"],
                }
                pii_ciphertext = security_manager.encrypt_pii(pii_payload, associated_data=aad)
                ticket_id = generate_uuidv7()
                ticket_jti = generate_uuidv7()

                cur.execute(
                    """
                    INSERT INTO registrations (
                        id, organization_id, event_id, publication_id, form_version_id,
                        state, email_hmac, email_masked, full_name_masked, pii_key_id,
                        pii_ciphertext, source, revision,
                        created_at, updated_at, confirmed_at
                    ) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, 'batch_import', 1, ?, ?, ?)
                    """,
                    (
                        reg_id,
                        org_id,
                        event_id,
                        cap_row["publication_id"] if cap_row and "publication_id" in cap_row.keys() else DEFAULT_PUBLICATION_ID,
                        DEFAULT_FORM_VERSION_ID,
                        item["email_hmac"],
                        mask_email(item["email"]),
                        mask_name(item["full_name"]),
                        security_manager.pii_key_id,
                        pii_ciphertext,
                        now,
                        now,
                        now,
                    ),
                )

                # Consent record
                consent_hash = hashlib.sha256(f"{legal_basis}:{item['email_hmac']}:{now}".encode("utf-8")).hexdigest()
                cur.execute(
                    """
                    INSERT INTO consents (
                        id, organization_id, subject_type, subject_id,
                        consent_version, consent_hash, accepted_at
                    ) VALUES (?, ?, 'registration', ?, ?, ?, ?)
                    """,
                    (
                        generate_uuidv7(),
                        org_id,
                        reg_id,
                        f"import:{legal_basis}",
                        consent_hash,
                        now,
                    ),
                )

                # QR Ticket
                self._issue_ticket_internal(
                    cur,
                    organization_id=org_id,
                    event_id=event_id,
                    registration_id=reg_id,
                )
                imported_ids.append(reg_id)

            # Increment confirmed capacity atomically (avoids lost updates and
            # overbooking when concurrent imports run on PostgreSQL).
            cur.execute(
                """
                UPDATE capacity_counters
                SET confirmed_count = confirmed_count + ?, revision = revision + 1, updated_at = ?
                WHERE event_id = ? AND confirmed_count + ? <= capacity
                """,
                (len(valid_rows), now, event_id, len(valid_rows)),
            )
            if cur.rowcount != 1:
                raise ProblemException(
                    status=409,
                    code="capacity_exceeded",
                    title="Cupos insuficientes para importación",
                    detail="La capacidad del evento cambió durante la importación y ya no hay cupos suficientes.",
                )

            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.registration_import.completed.v1",
                aggregate_type="registration_import",
                aggregate_id=generate_uuidv7(),
                aggregate_version=1,
                data={
                    "event_id": event_id,
                    "legal_basis": legal_basis,
                    "imported_count": len(valid_rows),
                    "error_count": len(row_errors),
                },
            )

            return {
                "dry_run": False,
                "total_rows": len(rows),
                "imported_count": len(valid_rows),
                "created_count": len(valid_rows),
                "error_count": len(row_errors),
                "errors": row_errors,
            }

    def execute_privacy_request(
        self,
        *,
        email: str,
        request_type: str,
        legal_hold: bool = False,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PA-034: ARCO Privacy Request handler with Legal Hold protection, erasure/tombstone and zero-PII cryptographic receipt."""
        clean_email = email.strip().lower()
        if not clean_email or "@" not in clean_email:
            raise ProblemException(status=400, code="invalid_email", title="Correo inválido", detail="Debe ingresar un correo electrónico válido.")

        if request_type not in ("access", "rectification", "erasure"):
            raise ProblemException(status=400, code="invalid_request_type", title="Tipo de solicitud inválido", detail="El tipo debe ser 'access', 'rectification' o 'erasure'.")

        org_id = DEFAULT_ORG_ID
        email_hmac = security_manager.compute_email_hmac(org_id, clean_email)
        now = utc_now_iso()
        req_id = generate_uuidv7()

        # Legal hold invariant: the rejection is evidence and must survive the
        # error, so it is persisted in its own committed transaction.
        if legal_hold and request_type == "erasure":
            with self.db.transaction() as cur:
                cur.execute(
                    """
                    INSERT INTO privacy_requests (
                        id, organization_id, email_hmac, request_type, legal_hold, state, created_at
                    ) VALUES (?, ?, ?, ?, 1, 'rejected', ?)
                    """,
                    (req_id, org_id, email_hmac, request_type, now),
                )
            raise ProblemException(
                status=409,
                code="legal_hold_active",
                title="Retención legal activa (Legal Hold)",
                detail="La solicitud de supresión (erasure) no puede ejecutarse porque el titular se encuentra sujeto a una retención legal obligatoria (Ley 29733 / auditoría).",
            )

        with self.db.transaction() as cur:
            if request_type == "erasure":
                # 1. Anonymize registrations: crypto-shred PII and replace with tombstone
                cur.execute("SELECT id FROM registrations WHERE email_hmac = ?", (email_hmac,))
                reg_ids = [r["id"] for r in cur.fetchall()]

                tombstone_pii = security_manager.encrypt_pii(
                    {"status": "TOMBSTONE_ERASED", "anonymized_at": now},
                    associated_data=f"{org_id}:tombstone:{req_id}",
                )
                for rid in reg_ids:
                    cur.execute(
                        """
                        UPDATE registrations
                        SET pii_ciphertext = ?, full_name_masked = 'ANONIMIZADO',
                            email_masked = 'anonimizado@tombstone.internal', state = 'cancelled',
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (tombstone_pii, now, rid),
                    )
                    # Questionnaire answers contain PII too: crypto-shred them.
                    cur.execute("DELETE FROM registration_answers WHERE registration_id = ?", (rid,))
                    # Revoke active tickets
                    cur.execute(
                        "UPDATE tickets SET state = 'revoked', revoked_at = ? WHERE registration_id = ? AND state = 'active'",
                        (now, rid),
                    )

                # 2. Anonymize team memberships and drop pending invitations
                cur.execute(
                    "UPDATE team_memberships SET state = 'removed', email_masked = 'anonimizado@tombstone.internal', updated_at = ? WHERE email_hmac = ?",
                    (now, email_hmac),
                )
                cur.execute("DELETE FROM team_invitations WHERE email_hmac = ?", (email_hmac,))

                # 3. Crypto-shred venture leader PII when the subject is a leader
                cur.execute(
                    "UPDATE ventures SET leader_pii_ciphertext = ?, revision = revision + 1, updated_at = ? WHERE leader_email_hmac = ?",
                    (tombstone_pii, now, email_hmac),
                )

                # 4. Generate non-PII cryptographic receipt
                tombstone_hash = hashlib.sha256(f"erased:{email_hmac}:{now}".encode("utf-8")).hexdigest()
                evidence_payload = {
                    "receipt_id": req_id,
                    "status": "erased",
                    "request_type": "erasure",
                    "tombstone_hash": tombstone_hash,
                    "anonymized_count": len(reg_ids),
                    "registrations_anonymized": len(reg_ids),
                    "executed_at": now,
                    "legal_basis": "Ley_29733_Art_20",
                }
                cur.execute(
                    """
                    INSERT INTO privacy_requests (
                        id, organization_id, email_hmac, request_type, legal_hold, state,
                        tombstone_receipt, evidence_json, created_at, completed_at
                    ) VALUES (?, ?, ?, 'erasure', 0, 'completed', ?, ?, ?, ?)
                    """,
                    (
                        req_id,
                        org_id,
                        email_hmac,
                        tombstone_hash,
                        json.dumps(evidence_payload, ensure_ascii=False),
                        now,
                        now,
                    ),
                )

                # Zero PII in outbox event
                self._emit_outbox_event(
                    cur,
                    organization_id=org_id,
                    business_event_id=DEFAULT_EVENT_ID,
                    topic="participation.privacy_request.executed.v1",
                    aggregate_type="privacy_request",
                    aggregate_id=req_id,
                    aggregate_version=1,
                    data={
                        "receipt_id": req_id,
                        "request_type": "erasure",
                        "tombstone_hash": tombstone_hash,
                        "registrations_anonymized": len(reg_ids),
                    },
                )
                return evidence_payload

            # Access / rectification requests: record the request for audit and
            # return only non-sensitive registration metadata.
            cur.execute(
                "SELECT id, state, created_at, confirmed_at FROM registrations WHERE email_hmac = ?",
                (email_hmac,),
            )
            regs = [dict(r) for r in cur.fetchall()]
            evidence_payload = {
                "receipt_id": req_id,
                "status": "completed",
                "request_type": request_type,
                "registrations_visible": len(regs),
                "executed_at": now,
                "legal_basis": "Ley_29733_Art_20",
            }
            cur.execute(
                """
                INSERT INTO privacy_requests (
                    id, organization_id, email_hmac, request_type, legal_hold, state,
                    evidence_json, created_at, completed_at
                ) VALUES (?, ?, ?, ?, 0, 'completed', ?, ?, ?)
                """,
                (
                    req_id,
                    org_id,
                    email_hmac,
                    request_type,
                    json.dumps(evidence_payload, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=DEFAULT_EVENT_ID,
                topic="participation.privacy_request.executed.v1",
                aggregate_type="privacy_request",
                aggregate_id=req_id,
                aggregate_version=1,
                data={
                    "receipt_id": req_id,
                    "request_type": request_type,
                    "registrations_visible": len(regs),
                },
            )
            return {
                "receipt_id": req_id,
                "request_type": request_type,
                "email_masked": mask_email(clean_email),
                "registrations_count": len(regs),
                "records": regs,
                "retention_policy": "5 años para certificaciones conforme a Ley 29733",
                "receipt": {
                    "receipt_id": req_id,
                    "executed_at": now,
                    "legal_basis": "Ley_29733_Art_20",
                },
            }

    def create_operations_projection(
        self,
        *,
        event_id: str,
        purpose: str,
        recipient: str = "onsite_operations",
        confirmed_only: bool = True,
    ) -> Dict[str, Any]:
        """PA-035: Purpose-bound projection with AES-GCM envelope and strict allowlist for reports/certificates."""
        allowed_purposes = {"certificate_issuance", "onsite_attendance", "institutional_reporting"}
        if purpose not in allowed_purposes:
            raise ProblemException(
                status=400,
                code="invalid_purpose",
                title="Propósito no autorizado",
                detail=f"El propósito '{purpose}' no está en la allowlist permitida ({list(allowed_purposes)}).",
            )

        with self.db.read_cursor() as cur:
            query = """
                SELECT r.id, r.organization_id, r.event_id, r.state, r.created_at, r.confirmed_at,
                       r.pii_ciphertext, t.id AS ticket_id, t.jti AS ticket_jti
                FROM registrations r
                LEFT JOIN tickets t ON t.registration_id = r.id AND t.state = 'active'
                WHERE r.event_id = ?
            """
            params: List[Any] = [event_id]
            if confirmed_only:
                query += " AND r.state = 'confirmed'"

            cur.execute(query, tuple(params))
            rows = cur.fetchall()

            allowlisted_items = []
            now = utc_now_iso()
            retention_until = (datetime.now(timezone.utc) + timedelta(days=30)).replace(microsecond=0).isoformat().replace("+00:00", "Z")

            for r in rows:
                org_id = r["organization_id"]
                reg_id = r["id"]
                aad = f"{org_id}:{event_id}:{reg_id}"
                pii = security_manager.decrypt_pii(r["pii_ciphertext"], associated_data=aad)

                if purpose == "certificate_issuance":
                    # Certificate allowlist: identity, document, institution, confirmation date
                    allowlisted_items.append({
                        "registration_id": reg_id,
                        "full_name": pii.get("full_name", ""),
                        "document_number": pii.get("document_number", ""),
                        "institution": pii.get("institution", "UNAS"),
                        "confirmed_at": r["confirmed_at"],
                    })
                elif purpose == "onsite_attendance":
                    # Onsite allowlist: ticket info, masked name, track
                    allowlisted_items.append({
                        "registration_id": reg_id,
                        "ticket_id": r["ticket_id"],
                        "ticket_jti": r["ticket_jti"],
                        "name_masked": mask_name(pii.get("full_name", "")),
                        "institution": pii.get("institution", "UNAS"),
                    })
                else:
                    # Institutional reporting allowlist: aggregated attributes
                    allowlisted_items.append({
                        "registration_id": reg_id,
                        "institution": pii.get("institution", "UNAS"),
                        "confirmed_at": r["confirmed_at"],
                    })

            payload_json = json.dumps(allowlisted_items, sort_keys=True, ensure_ascii=False)
            digest_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()

            # Encrypt envelope with AES-256-GCM
            envelope_id = generate_uuidv7()
            env_aad = f"projection:{event_id}:{purpose}:{recipient}"
            ciphertext = security_manager.encrypt_pii({"data": allowlisted_items}, associated_data=env_aad)

            # Record event in outbox with zero plaintext
            with self.db.transaction() as tx_cur:
                self._emit_outbox_event(
                    tx_cur,
                    organization_id=rows[0]["organization_id"] if rows else DEFAULT_ORG_ID,
                    business_event_id=event_id,
                    topic="participation.certificate_subject.projected.v1",
                    aggregate_type="operations_projection",
                    aggregate_id=envelope_id,
                    aggregate_version=1,
                    data={
                        "envelope_id": envelope_id,
                        "event_id": event_id,
                        "purpose": purpose,
                        "recipient": recipient,
                        "key_id": "kms-participation-operations-2026-v1",
                        "algorithm": "AES-256-GCM",
                        "records_count": len(allowlisted_items),
                        "digest_sha256": digest_sha256,
                        "retention_until": retention_until,
                        "ciphertext": ciphertext,
                    },
                )

            return {
                "envelope_id": envelope_id,
                "purpose": purpose,
                "recipient": recipient,
                "records_count": len(allowlisted_items),
                "key_id": "kms-participation-operations-2026-v1",
                "algorithm": "AES-256-GCM",
                "digest_sha256": digest_sha256,
                "retention_until": retention_until,
                "ciphertext": ciphertext,
                "ciphertext_preview": ciphertext[:64] + "...",
                "envelope": {
                    "key_id": "kms-participation-operations-2026-v1",
                    "algorithm": "AES-256-GCM",
                    "ciphertext": ciphertext,
                    "digest_sha256": digest_sha256,
                    "retention_until": retention_until,
                },
            }

    # -------------------------------------------------------------------------
    # PA-C: Ventures & Team Applications (PA-020, PA-021, PA-025)
    # -------------------------------------------------------------------------
    def create_venture_application(
        self,
        *,
        site_slug: str,
        event_slug: str,
        payload: Dict[str, Any],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        with self.db.transaction() as cur:
            org_id = DEFAULT_ORG_ID
            event_id = DEFAULT_EVENT_ID
            cached = self._check_or_prepare_idempotency(
                cur,
                organization_id=org_id,
                operation=f"create_application:{event_id}",
                idempotency_key=idempotency_key,
                request_payload=payload,
            )
            if cached is not None:
                return cached[1]

            venture_id = generate_uuidv7()
            application_id = generate_uuidv7()
            now = utc_now_iso()
            leader = payload.get("leader") or {}
            leader_email = leader.get("email", "lider@unas.edu.pe")
            leader_hmac = security_manager.compute_email_hmac(org_id, leader_email)
            aad = f"{org_id}:{event_id}:{venture_id}"

            leader_cipher = security_manager.encrypt_pii(leader, associated_data=aad)
            answers_cipher = security_manager.encrypt_pii(
                payload.get("answers") or {}, associated_data=aad
            )

            cur.execute(
                """
                INSERT INTO ventures (
                    id, organization_id, event_id, name, track,
                    leader_email_hmac, leader_pii_ciphertext, revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    venture_id,
                    org_id,
                    event_id,
                    payload.get("venture_name", "BioAmazonas Tech"),
                    payload.get("track", "Agrotech & Bioeconomía Amazónica"),
                    leader_hmac,
                    leader_cipher,
                    now,
                    now,
                ),
            )

            cur.execute(
                """
                INSERT INTO team_memberships (
                    id, organization_id, venture_id, email_hmac, email_masked,
                    role, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'leader', 'active', ?, ?)
                """,
                (
                    generate_uuidv7(),
                    org_id,
                    venture_id,
                    leader_hmac,
                    mask_email(leader_email),
                    now,
                    now,
                ),
            )

            invitations_created = []
            inv_expiry = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            for member in payload.get("members") or []:
                m_email = member.get("email", "")
                if not m_email:
                    continue
                inv_id = generate_uuidv7()
                m_hmac = security_manager.compute_email_hmac(org_id, m_email)
                raw_token = f"inv_{uuid.uuid4().hex}"
                token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
                cur.execute(
                    """
                    INSERT INTO team_invitations (
                        id, organization_id, venture_id, email_hmac, email_masked,
                        role, token_hash, state, expires_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        inv_id,
                        org_id,
                        venture_id,
                        m_hmac,
                        mask_email(m_email),
                        member.get("role", "co_founder"),
                        token_hash,
                        inv_expiry,
                        now,
                    ),
                )
                invitations_created.append(
                    {
                        "id": inv_id,
                        "email_masked": mask_email(m_email),
                        "role": member.get("role", "co_founder"),
                        "state": "pending",
                        "token": raw_token,
                        "expires_at": inv_expiry,
                    }
                )
                self._emit_outbox_event(
                    cur,
                    organization_id=org_id,
                    business_event_id=event_id,
                    topic="participation.team_invitation.changed.v1",
                    aggregate_type="team_invitation",
                    aggregate_id=inv_id,
                    aggregate_version=1,
                    data={
                        "invitation_id": inv_id,
                        "application_id": application_id,
                        "venture_id": venture_id,
                        "role": member.get("role", "co_founder"),
                        "state": "pending",
                        "expires_at": inv_expiry,
                    },
                )

            cur.execute(
                """
                INSERT INTO applications (
                    id, organization_id, event_id, venture_id, form_version_id,
                    state, current_revision_number, draft_answers_ciphertext,
                    revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', 1, ?, 1, ?, ?)
                """,
                (
                    application_id,
                    org_id,
                    event_id,
                    venture_id,
                    payload.get("form_version_id", DEFAULT_FORM_VERSION_ID),
                    answers_cipher,
                    now,
                    now,
                ),
            )

            result = {
                "application_id": application_id,
                "venture_id": venture_id,
                "venture_name": payload.get("venture_name"),
                "state": "draft",
                "revision": 1,
                "invitations": invitations_created,
                "created_at": now,
            }
            self._save_idempotency_result(
                cur,
                organization_id=org_id,
                operation=f"create_application:{event_id}",
                idempotency_key=idempotency_key,
                request_payload=payload,
                status_code=201,
                response_body=result,
            )
            return result

    # -------------------------------------------------------------------------
    # PA-C: Team Management & Invitations (PA-020, PA-021, PA-022)
    # -------------------------------------------------------------------------
    def get_venture(self, venture_id: str) -> Dict[str, Any]:
        """Get venture details, active members and invitations (PA-020)."""
        with self.db.read_cursor() as cur:
            cur.execute("SELECT * FROM ventures WHERE id = ?", (venture_id,))
            v_row = cur.fetchone()
            if not v_row:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Venture not found",
                    detail="Emprendimiento no encontrado.",
                )

            cur.execute(
                "SELECT id, role, state, email_masked, created_at FROM team_memberships WHERE venture_id = ? ORDER BY created_at ASC",
                (venture_id,),
            )
            members = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT id, role, state, email_masked, expires_at, created_at FROM team_invitations WHERE venture_id = ? ORDER BY created_at ASC",
                (venture_id,),
            )
            invitations = [dict(r) for r in cur.fetchall()]

            return {
                "venture_id": venture_id,
                "name": v_row["name"],
                "track": v_row["track"],
                "revision": v_row["revision"],
                "members": members,
                "invitations": invitations,
                "created_at": v_row["created_at"],
            }

    def invite_team_member(
        self,
        *,
        venture_id: str,
        email: str,
        role: str,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Invite a team member with role and expirable one-use token (PA-021)."""
        clean_email = email.strip().lower()
        if not clean_email or "@" not in clean_email:
            raise ProblemException(
                status=400,
                code="invalid_email",
                title="Correo inválido",
                detail="Debe ingresar un correo electrónico válido.",
            )
        if role not in ("co_founder", "member"):
            raise ProblemException(
                status=400,
                code="invalid_role",
                title="Rol inválido",
                detail="El rol debe ser 'co_founder' o 'member'.",
            )

        with self.db.transaction() as cur:
            if idempotency_key:
                cached = self._check_or_prepare_idempotency(
                    cur,
                    organization_id=DEFAULT_ORG_ID,
                    operation=f"invite_team_member:{venture_id}",
                    idempotency_key=idempotency_key,
                    request_payload={
                        "venture_id": venture_id,
                        "email": clean_email,
                        "role": role,
                    },
                )
                if cached is not None:
                    return cached[1]

            cur.execute("SELECT * FROM ventures WHERE id = ?", (venture_id,))
            v_row = cur.fetchone()
            if not v_row:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Venture not found",
                    detail="Emprendimiento no encontrado.",
                )

            org_id = v_row["organization_id"]
            m_hmac = security_manager.compute_email_hmac(org_id, clean_email)

            # Invariant: no duplicate active members
            cur.execute(
                "SELECT id FROM team_memberships WHERE venture_id = ? AND email_hmac = ? AND state = 'active'",
                (venture_id, m_hmac),
            )
            if cur.fetchone():
                raise ProblemException(
                    status=409,
                    code="already_team_member",
                    title="Ya es miembro",
                    detail="El usuario ya es un miembro activo del equipo.",
                )

            # Invariant: no duplicate pending invitations
            cur.execute(
                "SELECT id FROM team_invitations WHERE venture_id = ? AND email_hmac = ? AND state = 'pending'",
                (venture_id, m_hmac),
            )
            if cur.fetchone():
                raise ProblemException(
                    status=409,
                    code="invitation_already_pending",
                    title="Invitación pendiente",
                    detail="Ya existe una invitación pendiente para este correo.",
                )

            now = utc_now_iso()
            expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            inv_id = generate_uuidv7()
            raw_token = f"inv_{uuid.uuid4().hex}"
            token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

            cur.execute(
                """
                INSERT INTO team_invitations (
                    id, organization_id, venture_id, email_hmac, email_masked,
                    role, token_hash, state, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    inv_id,
                    org_id,
                    venture_id,
                    m_hmac,
                    mask_email(clean_email),
                    role,
                    token_hash,
                    expires_at,
                    now,
                ),
            )
            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=v_row["event_id"],
                topic="participation.team_invitation.changed.v1",
                aggregate_type="team_invitation",
                aggregate_id=inv_id,
                aggregate_version=1,
                data={
                    "invitation_id": inv_id,
                    "venture_id": venture_id,
                    "role": role,
                    "state": "pending",
                    "expires_at": expires_at,
                },
            )
            result = {
                "invitation_id": inv_id,
                "venture_id": venture_id,
                "email_masked": mask_email(clean_email),
                "role": role,
                "state": "pending",
                "token": raw_token,
                "expires_at": expires_at,
                "created_at": now,
            }
            if idempotency_key:
                self._save_idempotency_result(
                    cur,
                    organization_id=org_id,
                    operation=f"invite_team_member:{venture_id}",
                    idempotency_key=idempotency_key,
                    request_payload={
                        "venture_id": venture_id,
                        "email": clean_email,
                        "role": role,
                    },
                    status_code=201,
                    response_body=result,
                )
            return result

    def accept_invitation(self, token: str) -> Dict[str, Any]:
        """Invited member accepts invitation (PA-022)."""
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM team_invitations WHERE token_hash = ?", (token_hash,))
            inv = cur.fetchone()
            if not inv:
                raise ProblemException(
                    status=404,
                    code="invitation_not_found",
                    title="Invitación no encontrada",
                    detail="El token de invitación no es válido o ha sido revocado.",
                )
            if inv["state"] != "pending":
                raise ProblemException(
                    status=409,
                    code="invitation_already_processed",
                    title="Invitación no disponible",
                    detail=f"La invitación ya fue procesada previamente (estado actual: '{inv['state']}').",
                )

            now = utc_now_iso()
            if inv["expires_at"] and now > inv["expires_at"]:
                cur.execute("UPDATE team_invitations SET state = 'expired' WHERE id = ?", (inv["id"],))
                raise ProblemException(
                    status=410,
                    code="invitation_expired",
                    title="Invitación expirada",
                    detail="El plazo para aceptar esta invitación ha vencido.",
                )

            cur.execute("UPDATE team_invitations SET state = 'accepted' WHERE id = ?", (inv["id"],))
            mem_id = generate_uuidv7()
            cur.execute(
                """
                INSERT INTO team_memberships (
                    id, organization_id, venture_id, email_hmac, email_masked,
                    role, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    mem_id,
                    inv["organization_id"],
                    inv["venture_id"],
                    inv["email_hmac"],
                    inv["email_masked"],
                    inv["role"],
                    now,
                    now,
                ),
            )
            cur.execute("SELECT event_id FROM ventures WHERE id = ?", (inv["venture_id"],))
            v_res = cur.fetchone()
            ev_id = v_res["event_id"] if v_res else DEFAULT_EVENT_ID
            self._emit_outbox_event(
                cur,
                organization_id=inv["organization_id"],
                business_event_id=ev_id,
                topic="participation.team_membership.changed.v1",
                aggregate_type="team_membership",
                aggregate_id=mem_id,
                aggregate_version=1,
                data={
                    "membership_id": mem_id,
                    "venture_id": inv["venture_id"],
                    "role": inv["role"],
                    "state": "active",
                },
            )
            return {
                "status": "accepted",
                "membership_id": mem_id,
                "venture_id": inv["venture_id"],
                "role": inv["role"],
                "email_masked": inv["email_masked"],
            }

    def decline_invitation(self, token: str) -> Dict[str, Any]:
        """Invited member declines invitation (PA-022)."""
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM team_invitations WHERE token_hash = ?", (token_hash,))
            inv = cur.fetchone()
            if not inv:
                raise ProblemException(
                    status=404,
                    code="invitation_not_found",
                    title="Invitación no encontrada",
                    detail="El token de invitación no es válido.",
                )
            if inv["state"] != "pending":
                raise ProblemException(
                    status=409,
                    code="invitation_already_processed",
                    title="Invitación no disponible",
                    detail=f"La invitación ya fue procesada (estado actual: '{inv['state']}').",
                )

            cur.execute("UPDATE team_invitations SET state = 'declined' WHERE id = ?", (inv["id"],))
            cur.execute("SELECT event_id FROM ventures WHERE id = ?", (inv["venture_id"],))
            v_res = cur.fetchone()
            ev_id = v_res["event_id"] if v_res else DEFAULT_EVENT_ID
            self._emit_outbox_event(
                cur,
                organization_id=inv["organization_id"],
                business_event_id=ev_id,
                topic="participation.team_invitation.changed.v1",
                aggregate_type="team_invitation",
                aggregate_id=inv["id"],
                aggregate_version=1,
                data={"invitation_id": inv["id"], "venture_id": inv["venture_id"], "state": "declined"},
            )
            return {"status": "declined", "venture_id": inv["venture_id"]}

    def leave_team_member(self, venture_id: str, member_id: str) -> Dict[str, Any]:
        """Team member leaves venture with sole leader protection invariant (PA-022)."""
        with self.db.transaction() as cur:
            cur.execute(
                "SELECT * FROM team_memberships WHERE id = ? AND venture_id = ?",
                (member_id, venture_id),
            )
            mem = cur.fetchone()
            if not mem:
                raise ProblemException(
                    status=404,
                    code="member_not_found",
                    title="Miembro no encontrado",
                    detail="No existe el miembro en este equipo.",
                )
            if mem["state"] != "active":
                raise ProblemException(
                    status=409,
                    code="member_not_active",
                    title="Miembro inactivo",
                    detail=f"El miembro ya se encuentra en estado '{mem['state']}'.",
                )

            # Invariant: "siempre queda un leader antes de submit"
            if mem["role"] == "leader":
                cur.execute(
                    "SELECT count(*) as cnt FROM team_memberships WHERE venture_id = ? AND role = 'leader' AND state = 'active' AND id != ?",
                    (venture_id, member_id),
                )
                other_leaders = cur.fetchone()
                if not other_leaders or int(other_leaders["cnt"]) == 0:
                    raise ProblemException(
                        status=409,
                        code="cannot_remove_sole_leader",
                        title="Líder único",
                        detail="No se puede retirar al único líder activo del equipo.",
                    )

            now = utc_now_iso()
            cur.execute("UPDATE team_memberships SET state = 'left', updated_at = ? WHERE id = ?", (now, member_id))
            cur.execute("SELECT event_id FROM ventures WHERE id = ?", (venture_id,))
            v_res = cur.fetchone()
            ev_id = v_res["event_id"] if v_res else DEFAULT_EVENT_ID
            self._emit_outbox_event(
                cur,
                organization_id=mem["organization_id"],
                business_event_id=ev_id,
                topic="participation.team_membership.changed.v1",
                aggregate_type="team_membership",
                aggregate_id=member_id,
                aggregate_version=1,
                data={"membership_id": member_id, "venture_id": venture_id, "state": "left"},
            )
            return {"status": "left", "member_id": member_id, "venture_id": venture_id}

    # -------------------------------------------------------------------------
    # PA-C: Application Drafts, Files & Submissions (PA-023, PA-024, PA-025, PA-026, PA-027)
    # -------------------------------------------------------------------------
    def get_application(self, application_id: str) -> Dict[str, Any]:
        """Get application details with decrypted draft answers, files, members & ETag (PA-023)."""
        with self.db.read_cursor() as cur:
            cur.execute("SELECT * FROM applications WHERE id = ?", (application_id,))
            app_row = cur.fetchone()
            if not app_row:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Application not found",
                    detail="No existe la postulación solicitada.",
                )

            cur.execute("SELECT * FROM ventures WHERE id = ?", (app_row["venture_id"],))
            v_row = cur.fetchone()

            cur.execute(
                "SELECT id, role, state, email_masked, created_at FROM team_memberships WHERE venture_id = ? ORDER BY created_at ASC",
                (app_row["venture_id"],),
            )
            members = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT id, role, state, email_masked, expires_at, created_at FROM team_invitations WHERE venture_id = ? ORDER BY created_at ASC",
                (app_row["venture_id"],),
            )
            invitations = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT id, field_name, file_name, mime_type, size_bytes, sha256_hash, state, created_at, scanned_at FROM application_files WHERE application_id = ? ORDER BY created_at ASC",
                (application_id,),
            )
            files = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT revision_number, snapshot_hash, submitted_at FROM application_revisions WHERE application_id = ? ORDER BY revision_number ASC",
                (application_id,),
            )
            revisions = [dict(r) for r in cur.fetchall()]

            answers = {}
            if app_row["draft_answers_ciphertext"]:
                aad = f"{app_row['organization_id']}:{app_row['event_id']}:{app_row['venture_id']}"
                try:
                    answers = security_manager.decrypt_pii(app_row["draft_answers_ciphertext"], associated_data=aad)
                except Exception:
                    answers = {}

            return {
                "application_id": application_id,
                "organization_id": app_row["organization_id"],
                "event_id": app_row["event_id"],
                "venture_id": app_row["venture_id"],
                "venture_name": v_row["name"] if v_row else "Emprendimiento",
                "track": v_row["track"] if v_row else "General",
                "form_version_id": app_row["form_version_id"],
                "state": app_row["state"],
                "current_revision_number": app_row["current_revision_number"],
                "revision": app_row["revision"],
                "etag": f'W/"{app_row["revision"]}"',
                "answers": answers,
                "members": members,
                "invitations": invitations,
                "files": files,
                "revisions": revisions,
                "snapshot_hash": app_row["snapshot_hash"],
                "submitted_at": app_row["submitted_at"],
                "created_at": app_row["created_at"],
                "updated_at": app_row["updated_at"],
            }

    def update_application_draft(
        self,
        *,
        application_id: str,
        answers: Dict[str, Any],
        expected_revision: int,
    ) -> Dict[str, Any]:
        """Autosave/update draft answers before deadline with optimistic locking / ETag (PA-023)."""
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM applications WHERE id = ?", (application_id,))
            app_row = cur.fetchone()
            if not app_row:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Application not found",
                    detail="No existe la postulación solicitada.",
                )
            if app_row["state"] not in ("draft", "needs_changes"):
                raise ProblemException(
                    status=409,
                    code="cannot_edit_submitted_application",
                    title="Edición bloqueada",
                    detail=f"No se puede modificar una postulación en estado '{app_row['state']}'.",
                )
            if int(app_row["revision"]) != int(expected_revision):
                raise ProblemException(
                    status=412,
                    code="stale_revision",
                    title="Revisión desactualizada",
                    detail="La versión esperada no coincide con el estado en el servidor (concurrencia ETag).",
                )

            now = utc_now_iso()
            org_id = app_row["organization_id"]
            event_id = app_row["event_id"]
            venture_id = app_row["venture_id"]
            aad = f"{org_id}:{event_id}:{venture_id}"
            answers_cipher = security_manager.encrypt_pii(answers, associated_data=aad)
            new_rev = int(app_row["revision"]) + 1

            cur.execute(
                "UPDATE applications SET draft_answers_ciphertext = ?, revision = ?, updated_at = ? WHERE id = ?",
                (answers_cipher, new_rev, now, application_id),
            )
            return {
                "application_id": application_id,
                "revision": new_rev,
                "etag": f'W/"{new_rev}"',
                "state": app_row["state"],
                "answers": answers,
                "updated_at": now,
            }

    def upload_application_file(
        self,
        *,
        application_id: str,
        field_name: str,
        file_name: str,
        mime_type: str,
        size_bytes: int,
        sha256_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Attach file to application with MIME, size and automated anti-malware quarantine check (PA-024)."""
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM applications WHERE id = ?", (application_id,))
            app_row = cur.fetchone()
            if not app_row:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Application not found",
                    detail="No existe la postulación.",
                )
            if app_row["state"] not in ("draft", "needs_changes"):
                raise ProblemException(
                    status=409,
                    code="cannot_modify_application",
                    title="Modificación bloqueada",
                    detail=f"No se pueden subir archivos a una postulación en estado '{app_row['state']}'.",
                )

            # Max 10MB limit
            if size_bytes > 10 * 1024 * 1024:
                raise ProblemException(
                    status=413,
                    code="file_too_large",
                    title="Archivo demasiado grande",
                    detail="El tamaño del archivo supera el límite máximo de 10 MB.",
                )

            now = utc_now_iso()
            org_id = app_row["organization_id"]
            file_id = generate_uuidv7()
            f_hash = sha256_hash or hashlib.sha256(f"{file_name}:{size_bytes}:{now}".encode("utf-8")).hexdigest()

            # Anti-malware scan simulation
            suspicious_exts = (".exe", ".bat", ".cmd", ".sh", ".scr", ".vbs", ".dll", ".ps1")
            is_malware = any(file_name.lower().endswith(ext) for ext in suspicious_exts) or mime_type in ("application/x-msdownload", "application/x-sh")
            state = "quarantined" if is_malware else "ready"

            cur.execute(
                """
                INSERT INTO application_files (
                    id, organization_id, application_id, field_name, file_name,
                    mime_type, size_bytes, sha256_hash, state, created_at, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    org_id,
                    application_id,
                    field_name,
                    file_name,
                    mime_type,
                    size_bytes,
                    f_hash,
                    state,
                    now,
                    now,
                ),
            )
            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=app_row["event_id"],
                topic="participation.application_file.scanned.v1",
                aggregate_type="application_file",
                aggregate_id=file_id,
                aggregate_version=1,
                data={
                    "file_id": file_id,
                    "application_id": application_id,
                    "file_name": file_name,
                    "state": state,
                    "sha256_hash": f_hash,
                },
            )
            return {
                "file_id": file_id,
                "application_id": application_id,
                "field_name": field_name,
                "file_name": file_name,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "sha256_hash": f_hash,
                "state": state,
                "created_at": now,
            }

    def list_application_files(self, application_id: str) -> List[Dict[str, Any]]:
        """List attached files for an application (PA-024)."""
        with self.db.read_cursor() as cur:
            cur.execute(
                "SELECT * FROM application_files WHERE application_id = ? ORDER BY created_at ASC",
                (application_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def submit_application(
        self,
        *,
        application_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Submit venture application creating an immutable snapshot and sha256 hash (PA-025)."""
        with self.db.transaction() as cur:
            cached = self._check_or_prepare_idempotency(
                cur,
                organization_id=DEFAULT_ORG_ID,
                operation=f"submit_application:{application_id}",
                idempotency_key=idempotency_key,
                request_payload={
                    "application_id": application_id,
                    "expected_revision": expected_revision,
                },
            )
            if cached is not None:
                return cached[1]

            cur.execute("SELECT * FROM applications WHERE id = ?", (application_id,))
            app_row = cur.fetchone()
            if not app_row:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Application not found",
                    detail="Application does not exist.",
                )

            if int(app_row["revision"]) != int(expected_revision):
                raise ProblemException(
                    status=412,
                    code="stale_revision",
                    title="Stale application revision",
                    detail="The application revision does not match expected_revision.",
                )

            if app_row["state"] not in ("draft", "needs_changes"):
                raise ProblemException(
                    status=409,
                    code="invalid_state_transition",
                    title="Application cannot be submitted from current state",
                    detail=f"Current state is '{app_row['state']}'.",
                )

            # PA-024 invariant: submit bloqueado si no ready
            cur.execute(
                "SELECT id, file_name, state FROM application_files WHERE application_id = ? AND state != 'ready'",
                (application_id,),
            )
            unready_files = cur.fetchall()
            if unready_files:
                raise ProblemException(
                    status=400,
                    code="files_not_ready",
                    title="Archivos no listos",
                    detail=f"No se puede enviar la postulación: existen {len(unready_files)} archivo(s) no listos o en cuarentena.",
                )

            # Team invariant: al menos 1 líder activo
            cur.execute(
                "SELECT id FROM team_memberships WHERE venture_id = ? AND role = 'leader' AND state = 'active'",
                (app_row["venture_id"],),
            )
            if not cur.fetchone():
                raise ProblemException(
                    status=400,
                    code="no_active_leader",
                    title="Falta líder activo",
                    detail="El emprendimiento debe contar con al menos un líder activo para enviar la postulación.",
                )

            now = utc_now_iso()
            org_id = app_row["organization_id"]
            event_id = app_row["event_id"]
            venture_id = app_row["venture_id"]

            cur.execute("SELECT * FROM ventures WHERE id = ?", (venture_id,))
            v_row = cur.fetchone()
            cur.execute(
                "SELECT id, role, state, email_masked FROM team_memberships WHERE venture_id = ? AND state = 'active'",
                (venture_id,),
            )
            active_members = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT id, field_name, file_name, sha256_hash FROM application_files WHERE application_id = ? AND state = 'ready'",
                (application_id,),
            )
            ready_files = [dict(r) for r in cur.fetchall()]

            snapshot_dict = {
                "application_id": application_id,
                "venture_id": venture_id,
                "venture_name": v_row["name"] if v_row else "",
                "track": v_row["track"] if v_row else "",
                "answers_ciphertext": app_row["draft_answers_ciphertext"],
                "active_members": active_members,
                "files": ready_files,
                "revision_number": int(app_row["current_revision_number"]),
                "submitted_at": now,
            }
            snapshot_json = json.dumps(snapshot_dict, sort_keys=True, ensure_ascii=False)
            snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            new_rev = int(app_row["revision"]) + 1

            cur.execute(
                """
                UPDATE applications
                SET state = 'submitted', snapshot_hash = ?, submitted_at = ?,
                    revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (snapshot_hash, now, new_rev, now, application_id),
            )
            cur.execute(
                """
                INSERT INTO application_revisions (
                    id, organization_id, application_id, revision_number,
                    snapshot_ciphertext, snapshot_hash, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generate_uuidv7(),
                    org_id,
                    application_id,
                    int(app_row["current_revision_number"]),
                    snapshot_json,
                    snapshot_hash,
                    now,
                ),
            )

            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.application.submitted.v1",
                aggregate_type="application",
                aggregate_id=application_id,
                aggregate_version=new_rev,
                data={
                    "application_id": application_id,
                    "revision": int(app_row["current_revision_number"]),
                    "snapshot_hash": snapshot_hash,
                    "submitted_at": now,
                    "form_version_id": app_row["form_version_id"],
                },
            )
            result = {
                "application_id": application_id,
                "state": "submitted",
                "revision": new_rev,
                "current_revision_number": int(app_row["current_revision_number"]),
                "snapshot_hash": snapshot_hash,
                "submitted_at": now,
            }
            self._save_idempotency_result(
                cur,
                organization_id=org_id,
                operation=f"submit_application:{application_id}",
                idempotency_key=idempotency_key,
                request_payload={
                    "application_id": application_id,
                    "expected_revision": expected_revision,
                },
                status_code=200,
                response_body=result,
            )
            return result

    def withdraw_application(
        self,
        *,
        application_id: str,
        reason: str,
    ) -> Dict[str, Any]:
        """Withdraw application with reason and audit event (PA-026)."""
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM applications WHERE id = ?", (application_id,))
            app_row = cur.fetchone()
            if not app_row:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Application not found",
                    detail="No existe la postulación solicitada.",
                )
            if app_row["state"] in ("withdrawn", "rejected"):
                raise ProblemException(
                    status=409,
                    code="invalid_state_transition",
                    title="Estado terminal",
                    detail=f"No se puede retirar una postulación en estado terminal '{app_row['state']}'.",
                )

            now = utc_now_iso()
            org_id = app_row["organization_id"]
            event_id = app_row["event_id"]
            new_rev = int(app_row["revision"]) + 1

            cur.execute(
                "UPDATE applications SET state = 'withdrawn', revision = ?, updated_at = ? WHERE id = ?",
                (new_rev, now, application_id),
            )
            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.application.withdrawn.v1",
                aggregate_type="application",
                aggregate_id=application_id,
                aggregate_version=new_rev,
                data={
                    "application_id": application_id,
                    "state": "withdrawn",
                    "reason": reason,
                    "withdrawn_at": now,
                },
            )
            return {
                "application_id": application_id,
                "state": "withdrawn",
                "reason": reason,
                "revision": new_rev,
            }

    def request_application_changes(
        self,
        *,
        application_id: str,
        requested_changes: List[str],
        deadline: Optional[str] = None,
        reviewer_notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Organizer/Reviewer requests corrections from applicant (PA-027)."""
        with self.db.transaction() as cur:
            cur.execute("SELECT * FROM applications WHERE id = ?", (application_id,))
            app_row = cur.fetchone()
            if not app_row:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Application not found",
                    detail="No existe la postulación solicitada.",
                )
            if app_row["state"] not in ("submitted", "resubmitted"):
                raise ProblemException(
                    status=409,
                    code="invalid_state_transition",
                    title="Estado inválido para subsanación",
                    detail=f"Solo se puede solicitar subsanación a postulaciones enviadas. Estado actual: '{app_row['state']}'.",
                )

            now = utc_now_iso()
            org_id = app_row["organization_id"]
            event_id = app_row["event_id"]
            new_rev = int(app_row["revision"]) + 1

            cur.execute(
                "UPDATE applications SET state = 'needs_changes', revision = ?, updated_at = ? WHERE id = ?",
                (new_rev, now, application_id),
            )
            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.application.changes_requested.v1",
                aggregate_type="application",
                aggregate_id=application_id,
                aggregate_version=new_rev,
                data={
                    "application_id": application_id,
                    "requested_changes": requested_changes,
                    "deadline": deadline,
                    "reviewer_notes": reviewer_notes,
                    "state": "needs_changes",
                },
            )
            return {
                "application_id": application_id,
                "state": "needs_changes",
                "revision": new_rev,
                "requested_changes": requested_changes,
                "deadline": deadline,
                "reviewer_notes": reviewer_notes,
            }

    def resubmit_application(
        self,
        *,
        application_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Applicant resubmits corrected application creating new revision number 2+ (PA-027)."""
        with self.db.transaction() as cur:
            cached = self._check_or_prepare_idempotency(
                cur,
                organization_id=DEFAULT_ORG_ID,
                operation=f"resubmit_application:{application_id}",
                idempotency_key=idempotency_key,
                request_payload={
                    "application_id": application_id,
                    "expected_revision": expected_revision,
                },
            )
            if cached is not None:
                return cached[1]

            cur.execute("SELECT * FROM applications WHERE id = ?", (application_id,))
            app_row = cur.fetchone()
            if not app_row:
                raise ProblemException(
                    status=404,
                    code="tenant_forbidden",
                    title="Application not found",
                    detail="No existe la postulación solicitada.",
                )
            if int(app_row["revision"]) != int(expected_revision):
                raise ProblemException(
                    status=412,
                    code="stale_revision",
                    title="Revisión desactualizada",
                    detail="La versión esperada no coincide con el estado actual en el servidor.",
                )
            if app_row["state"] != "needs_changes":
                raise ProblemException(
                    status=409,
                    code="invalid_state_transition",
                    title="Estado no apto para reenvío",
                    detail=f"Solo se puede reenviar una postulación en estado 'needs_changes'. Estado actual: '{app_row['state']}'.",
                )

            # Check unready files
            cur.execute(
                "SELECT count(*) as cnt FROM application_files WHERE application_id = ? AND state != 'ready'",
                (application_id,),
            )
            bad_f = cur.fetchone()
            if bad_f and int(bad_f["cnt"]) > 0:
                raise ProblemException(
                    status=400,
                    code="files_not_ready",
                    title="Archivos no listos",
                    detail="Existen archivos pendientes de escaneo o en cuarentena.",
                )

            now = utc_now_iso()
            org_id = app_row["organization_id"]
            event_id = app_row["event_id"]
            venture_id = app_row["venture_id"]
            new_rev = int(app_row["revision"]) + 1
            new_rev_number = int(app_row["current_revision_number"]) + 1

            cur.execute("SELECT * FROM ventures WHERE id = ?", (venture_id,))
            v_row = cur.fetchone()
            cur.execute(
                "SELECT id, role, state, email_masked FROM team_memberships WHERE venture_id = ? AND state = 'active'",
                (venture_id,),
            )
            active_members = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT id, field_name, file_name, sha256_hash FROM application_files WHERE application_id = ? AND state = 'ready'",
                (application_id,),
            )
            ready_files = [dict(r) for r in cur.fetchall()]

            snapshot_dict = {
                "application_id": application_id,
                "venture_id": venture_id,
                "venture_name": v_row["name"] if v_row else "",
                "track": v_row["track"] if v_row else "",
                "answers_ciphertext": app_row["draft_answers_ciphertext"],
                "active_members": active_members,
                "files": ready_files,
                "revision_number": new_rev_number,
                "resubmitted_at": now,
            }
            snapshot_json = json.dumps(snapshot_dict, sort_keys=True, ensure_ascii=False)
            snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()

            cur.execute(
                """
                UPDATE applications
                SET state = 'resubmitted', current_revision_number = ?, snapshot_hash = ?,
                    submitted_at = ?, revision = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_rev_number, snapshot_hash, now, new_rev, now, application_id),
            )
            cur.execute(
                """
                INSERT INTO application_revisions (
                    id, organization_id, application_id, revision_number,
                    snapshot_ciphertext, snapshot_hash, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generate_uuidv7(),
                    org_id,
                    application_id,
                    new_rev_number,
                    snapshot_json,
                    snapshot_hash,
                    now,
                ),
            )
            self._emit_outbox_event(
                cur,
                organization_id=org_id,
                business_event_id=event_id,
                topic="participation.application.resubmitted.v1",
                aggregate_type="application",
                aggregate_id=application_id,
                aggregate_version=new_rev,
                data={
                    "application_id": application_id,
                    "revision_number": new_rev_number,
                    "snapshot_hash": snapshot_hash,
                    "resubmitted_at": now,
                },
            )
            result = {
                "application_id": application_id,
                "state": "resubmitted",
                "revision": new_rev,
                "current_revision_number": new_rev_number,
                "snapshot_hash": snapshot_hash,
                "submitted_at": now,
            }
            self._save_idempotency_result(
                cur,
                organization_id=org_id,
                operation=f"resubmit_application:{application_id}",
                idempotency_key=idempotency_key,
                request_payload={
                    "application_id": application_id,
                    "expected_revision": expected_revision,
                },
                status_code=200,
                response_body=result,
            )
            return result

    # -------------------------------------------------------------------------
    # PA-041 & PA-042: Outbox Inspector & Concurrency Burst Simulator
    # -------------------------------------------------------------------------
    def list_outbox_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.db.read_cursor() as cur:
            cur.execute(
                """
                SELECT message_id, domain_event_id, topic, aggregate_type,
                       aggregate_id, aggregate_version, partition_key, envelope_json, occurred_at
                FROM outbox
                ORDER BY occurred_at DESC, message_id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [
                {
                    "message_id": r["message_id"],
                    "domain_event_id": r["domain_event_id"],
                    "topic": r["topic"],
                    "aggregate_type": r["aggregate_type"],
                    "aggregate_id": r["aggregate_id"],
                    "aggregate_version": r["aggregate_version"],
                    "partition_key": r["partition_key"],
                    "occurred_at": r["occurred_at"],
                    "envelope": json.loads(r["envelope_json"]),
                }
                for r in cur.fetchall()
            ]

    def simulate_concurrent_burst(self, total_requests: int = 15) -> Dict[str, Any]:
        """
        Execute a multi-threaded burst of registrations (PA-011 / PA-041)
        to prove atomic capacity locking: zero overbooking, deterministic waitlist positions.
        """
        tracks = [
            "Agrotech & Bioeconomía Amazónica",
            "IA Aplicada & Transformación Digital",
            "Demo Day de Startups & Emprendimiento",
        ]
        profiles = [
            "Estudiante UNAS",
            "Investigador / Docente",
            "Líder de Emprendimiento",
        ]

        batch_tag = generate_uuidv7()[:6]

        def _worker(idx: int) -> Dict[str, Any]:
            idem_key = f"sim-burst-{batch_tag}-{idx:03d}-{generate_uuidv7()[:8]}"
            payload = {
                "form_version_id": DEFAULT_FORM_VERSION_ID,
                "consent_version": DEFAULT_CONSENT_VERSION,
                "consent_accepted": True,
                "attendee": {
                    "full_name": f"Participante Concurrente {idx:02d}",
                    "email": f"concurrente.{batch_tag}.{idx:02d}@unas.edu.pe",
                    "document_number": f"7000{idx:04d}",
                    "institution": "UNAS Tingo María",
                },
                "answers": {
                    "track": tracks[idx % len(tracks)],
                    "role_profile": profiles[idx % len(profiles)],
                    "modality": "Presencial — Auditorio Central UNAS",
                },
            }
            _, receipt = self.create_registration(
                site_slug=DEFAULT_SITE_SLUG,
                event_slug=DEFAULT_EVENT_SLUG,
                payload=payload,
                idempotency_key=idem_key,
            )
            return receipt

        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(32, max(4, total_requests))) as pool:
            futures = [pool.submit(_worker, i + 1) for i in range(total_requests)]
            for fut in as_completed(futures):
                results.append(fut.result())

        confirmed = [r for r in results if r["state"] == "confirmed"]
        waitlisted = [r for r in results if r["state"] == "waitlisted"]

        form_state = self.get_public_form(DEFAULT_SITE_SLUG, DEFAULT_EVENT_SLUG)
        cap_summary = form_state["capacity_summary"]

        return {
            "total_requests_fired": total_requests,
            "confirmed_in_burst": len(confirmed),
            "waitlisted_in_burst": len(waitlisted),
            "overbooking_detected": cap_summary["confirmed_count"] > cap_summary["capacity"],
            "capacity_summary": cap_summary,
        }

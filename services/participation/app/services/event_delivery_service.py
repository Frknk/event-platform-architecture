from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import random
from typing import Any, Callable, Mapping, Optional, Protocol
import uuid

from app.core.security import generate_uuidv7, utc_now_iso
from app.db.database import ParticipationDatabase
from app.services.audit_service import AuditContext, AuditService

ERROR_CODES = {
    "transport_unavailable",
    "transport_timeout",
    "transport_rejected",
    "invalid_envelope",
    "rate_limited",
    "transport_error",
}


@dataclass(frozen=True)
class DeliveryReceipt:
    message_id: str
    accepted: bool


class EventTransport(Protocol):
    def publish(
        self,
        envelope: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
    ) -> DeliveryReceipt: ...


class TransportUnavailable(RuntimeError):
    pass


class TransportError(RuntimeError):
    def __init__(self, error_code: str, *, retryable: bool = True) -> None:
        if error_code not in ERROR_CODES:
            error_code = "transport_error"
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable


def _format_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class OutboxDeliveryService:
    """Lease-based at-least-once delivery with explicit transport acknowledgement."""

    def __init__(
        self,
        db: ParticipationDatabase,
        transport: Optional[EventTransport],
        audit_service: Optional[AuditService] = None,
        *,
        max_attempts: int = 5,
        base_backoff_seconds: float = 1.0,
        max_backoff_seconds: float = 300.0,
        lease_seconds: int = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        jitter: Callable[[float], float] = lambda cap: random.uniform(0.0, cap),
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one.")
        if base_backoff_seconds <= 0 or max_backoff_seconds < base_backoff_seconds:
            raise ValueError("Invalid retry backoff bounds.")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least one.")
        self.db = db
        self.transport = transport
        self.audit_service = audit_service or AuditService()
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.jitter = jitter

    def process_once(self, *, organization_id: str, limit: int = 50) -> dict[str, int]:
        if self.transport is None:
            raise TransportUnavailable(
                "No event transport is configured; no delivery rows were claimed."
            )
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500.")

        claimed = self._claim_due(organization_id=organization_id, limit=limit)
        result = {"claimed": len(claimed), "published": 0, "retrying": 0, "dead_lettered": 0}
        for delivery in claimed:
            try:
                envelope = json.loads(delivery["envelope_json"])
                if not isinstance(envelope, dict):
                    raise ValueError("Envelope must be a JSON object.")
            except (TypeError, ValueError):
                # A corrupt row must never poison the whole batch: it is
                # terminal, so route it to the DLQ with its classified reason.
                self._record_failure(
                    delivery=delivery,
                    organization_id=organization_id,
                    error_code="invalid_envelope",
                    retryable=False,
                    failed_at=self.clock(),
                )
                result["dead_lettered"] += 1
                continue

            sent_at = self.clock()
            envelope["message_id"] = delivery["message_id"]
            envelope["published_at"] = _format_timestamp(sent_at)
            headers = {}
            if delivery["replay_of_delivery_id"]:
                headers["replay"] = "true"

            try:
                receipt = self.transport.publish(envelope, headers=headers)
                if (
                    not isinstance(receipt, DeliveryReceipt)
                    or not receipt.accepted
                    or receipt.message_id != delivery["message_id"]
                ):
                    raise TransportError("transport_rejected", retryable=True)
            except TransportError as err:
                state = self._record_failure(
                    delivery=delivery,
                    organization_id=organization_id,
                    error_code=err.error_code,
                    retryable=err.retryable,
                    failed_at=sent_at,
                )
            except TimeoutError:
                state = self._record_failure(
                    delivery=delivery,
                    organization_id=organization_id,
                    error_code="transport_timeout",
                    retryable=True,
                    failed_at=sent_at,
                )
            except (ConnectionError, OSError):
                state = self._record_failure(
                    delivery=delivery,
                    organization_id=organization_id,
                    error_code="transport_unavailable",
                    retryable=True,
                    failed_at=sent_at,
                )
            except Exception:
                # A buggy adapter must not strand the batch in `in_flight`.
                state = self._record_failure(
                    delivery=delivery,
                    organization_id=organization_id,
                    error_code="transport_error",
                    retryable=True,
                    failed_at=sent_at,
                )
            else:
                self._record_success(
                    delivery=delivery,
                    organization_id=organization_id,
                    envelope=envelope,
                    published_at=sent_at,
                )
                result["published"] += 1
                continue
            result["dead_lettered" if state == "dead_letter" else "retrying"] += 1
        return result

    def _claim_due(self, *, organization_id: str, limit: int) -> list[dict[str, Any]]:
        now = self.clock()
        now_text = _format_timestamp(now)
        lease_until = _format_timestamp(now + timedelta(seconds=self.lease_seconds))
        lease_token = uuid.uuid4().hex
        lock_clause = (
            " FOR UPDATE OF d SKIP LOCKED" if self.db.backend == "postgres" else ""
        )
        claimed: list[dict[str, Any]] = []
        with self.db.transaction(org_id=organization_id) as cur:
            cur.execute(
                """
                SELECT d.delivery_id, d.outbox_id, d.message_id, d.domain_event_id,
                       d.topic, d.envelope_json, d.attempts, d.replay_of_delivery_id
                FROM outbox_deliveries d
                WHERE d.organization_id = ?
                  AND (
                    (d.state = 'pending' AND d.next_attempt_at <= ?)
                    OR (d.state = 'in_flight' AND d.lease_expires_at <= ?)
                  )
                ORDER BY d.next_attempt_at, d.created_at, d.delivery_id
                LIMIT ?
                """
                + lock_clause,
                (organization_id, now_text, now_text, limit),
            )
            rows = [dict(row) for row in cur.fetchall()]
            for row in rows:
                token = uuid.uuid4().hex
                cur.execute(
                    """
                    UPDATE outbox_deliveries
                    SET state = 'in_flight', attempts = attempts + 1,
                        lease_token = ?, lease_expires_at = ?, last_error_code = NULL
                    WHERE delivery_id = ? AND organization_id = ?
                    """,
                    (token, lease_until, row["delivery_id"], organization_id),
                )
                row["attempts"] = int(row["attempts"]) + 1
                row["lease_token"] = token
                claimed.append(row)
        return claimed

    def _record_success(
        self,
        *,
        delivery: dict[str, Any],
        organization_id: str,
        envelope: dict[str, Any],
        published_at: datetime,
    ) -> None:
        published_at_text = _format_timestamp(published_at)
        with self.db.transaction(org_id=organization_id) as cur:
            cur.execute(
                """
                UPDATE outbox_deliveries
                SET state = 'published', published_at = ?, envelope_json = ?,
                    lease_token = NULL, lease_expires_at = NULL, last_error_code = NULL
                WHERE delivery_id = ? AND lease_token = ? AND state = 'in_flight'
                """,
                (
                    published_at_text,
                    json.dumps(envelope, ensure_ascii=False),
                    delivery["delivery_id"],
                    delivery["lease_token"],
                ),
            )
            if cur.rowcount != 1:
                raise RuntimeError("Delivery lease was lost before acknowledgement was recorded.")
            if not delivery["replay_of_delivery_id"]:
                cur.execute(
                    """
                    UPDATE outbox
                    SET published_at = ?, attempts = ?, envelope_json = ?
                    WHERE id = ?
                    """,
                    (
                        published_at_text,
                        delivery["attempts"],
                        json.dumps(envelope, ensure_ascii=False),
                        delivery["outbox_id"],
                    ),
                )

    def _record_failure(
        self,
        *,
        delivery: dict[str, Any],
        organization_id: str,
        error_code: str,
        retryable: bool,
        failed_at: datetime,
    ) -> str:
        dead_letter = not retryable or delivery["attempts"] >= self.max_attempts
        with self.db.transaction(org_id=organization_id) as cur:
            if dead_letter:
                cur.execute(
                    """
                    UPDATE outbox_deliveries
                    SET state = 'dead_letter', last_error_code = ?,
                        lease_token = NULL, lease_expires_at = NULL
                    WHERE delivery_id = ? AND lease_token = ? AND state = 'in_flight'
                    """,
                    (error_code, delivery["delivery_id"], delivery["lease_token"]),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("Delivery lease was lost before DLQ state was recorded.")
                cur.execute(
                    """
                    INSERT INTO event_dead_letters (
                        dead_letter_id, organization_id, delivery_id, message_id,
                        domain_event_id, topic, attempts, error_code, failed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generate_uuidv7(),
                        organization_id,
                        delivery["delivery_id"],
                        delivery["message_id"],
                        delivery["domain_event_id"],
                        delivery["topic"],
                        delivery["attempts"],
                        error_code,
                        _format_timestamp(failed_at),
                    ),
                )
                state = "dead_letter"
            else:
                cap = min(
                    self.max_backoff_seconds,
                    self.base_backoff_seconds * (2 ** (delivery["attempts"] - 1)),
                )
                delay = min(cap, max(0.0, self.jitter(cap)))
                next_attempt = _format_timestamp(failed_at + timedelta(seconds=delay))
                cur.execute(
                    """
                    UPDATE outbox_deliveries
                    SET state = 'pending', next_attempt_at = ?, last_error_code = ?,
                        lease_token = NULL, lease_expires_at = NULL
                    WHERE delivery_id = ? AND lease_token = ? AND state = 'in_flight'
                    """,
                    (
                        next_attempt,
                        error_code,
                        delivery["delivery_id"],
                        delivery["lease_token"],
                    ),
                )
                if cur.rowcount != 1:
                    raise RuntimeError("Delivery lease was lost before retry was recorded.")
                state = "pending"
            if not delivery["replay_of_delivery_id"]:
                cur.execute(
                    "UPDATE outbox SET attempts = ? WHERE id = ?",
                    (delivery["attempts"], delivery["outbox_id"]),
                )
        return "dead_letter" if state == "dead_letter" else "pending"

    def replay_dead_letter(
        self,
        *,
        organization_id: str,
        delivery_id: str,
        idempotency_key: str,
        audit_context: AuditContext,
    ) -> dict[str, str]:
        if (
            not idempotency_key
            or not audit_context.request_id
            or audit_context.actor_type is None
            or audit_context.actor_id is None
        ):
            raise ValueError("Replay requires an idempotency key and attributed audit context.")
        try:
            uuid.UUID(organization_id)
            uuid.UUID(delivery_id)
        except ValueError as err:
            raise ValueError("Replay identifiers must be valid UUIDs.") from err

        request_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        with self.db.transaction(org_id=organization_id) as cur:
            if self.db.backend == "postgres":
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(?, 0))",
                    (f"{organization_id}:{request_hash}",),
                )
            cur.execute(
                """
                SELECT delivery_id, message_id, domain_event_id, replay_of_delivery_id
                FROM outbox_deliveries
                WHERE organization_id = ? AND replay_request_hash = ?
                """,
                (organization_id, request_hash),
            )
            existing = cur.fetchone()
            if existing:
                if existing["replay_of_delivery_id"] != delivery_id:
                    raise ValueError("Replay idempotency key was already used for another event.")
                return {
                    "delivery_id": existing["delivery_id"],
                    "message_id": existing["message_id"],
                    "domain_event_id": existing["domain_event_id"],
                    "status": "already_scheduled",
                }

            cur.execute(
                """
                SELECT d.*, dl.dead_letter_id
                FROM outbox_deliveries d
                JOIN event_dead_letters dl ON dl.delivery_id = d.delivery_id
                WHERE d.organization_id = ? AND d.delivery_id = ?
                  AND d.state = 'dead_letter' AND dl.replayed_at IS NULL
                """
                + (" FOR UPDATE OF d, dl" if self.db.backend == "postgres" else ""),
                (organization_id, delivery_id),
            )
            source = cur.fetchone()
            if not source:
                raise ValueError("Only an unreplayed dead-letter event can be replayed.")

            envelope = json.loads(source["envelope_json"])
            new_message_id = generate_uuidv7()
            envelope["message_id"] = new_message_id
            envelope["published_at"] = None
            new_delivery_id = generate_uuidv7()
            cur.execute(
                """
                INSERT INTO outbox_deliveries (
                    delivery_id, organization_id, outbox_id, message_id,
                    domain_event_id, topic, aggregate_type, aggregate_id,
                    aggregate_version, partition_key, envelope_json, occurred_at,
                    state, attempts, next_attempt_at, replay_of_delivery_id,
                    replay_request_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                """,
                (
                    new_delivery_id,
                    organization_id,
                    source["outbox_id"],
                    new_message_id,
                    source["domain_event_id"],
                    source["topic"],
                    source["aggregate_type"],
                    source["aggregate_id"],
                    source["aggregate_version"],
                    source["partition_key"],
                    json.dumps(envelope, ensure_ascii=False),
                    source["occurred_at"],
                    utc_now_iso(),
                    delivery_id,
                    request_hash,
                    utc_now_iso(),
                ),
            )
            cur.execute(
                """
                UPDATE event_dead_letters
                SET replayed_at = ?, replay_delivery_id = ?
                WHERE dead_letter_id = ? AND replayed_at IS NULL
                """,
                (utc_now_iso(), new_delivery_id, source["dead_letter_id"]),
            )
            if cur.rowcount != 1:
                raise RuntimeError("Dead-letter replay was concurrently scheduled.")
            self.audit_service.record(
                cur,
                context=audit_context,
                organization_id=organization_id,
                action="replay_event",
                target_type="event_delivery",
                target_id=delivery_id,
                result="success",
                reason="operator_replay",
                payload={"replay_delivery_id": new_delivery_id},
            )
        return {
            "delivery_id": new_delivery_id,
            "message_id": new_message_id,
            "domain_event_id": source["domain_event_id"],
            "status": "scheduled",
        }

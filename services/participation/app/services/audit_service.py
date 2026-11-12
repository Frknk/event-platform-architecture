from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal, Mapping, Optional, Protocol

from app.core.security import generate_uuidv7, utc_now_iso

AuditResult = Literal["success", "rejected", "failed", "indeterminate"]
ActorType = Literal["human", "service"]
_AUDIT_RESULTS = {"success", "rejected", "failed", "indeterminate"}
_MAX_REASON_LENGTH = 500
_AUDIT_ACTIONS = {
    "correct_registration",
    "admit_application",
    "reject_application",
    "import_registrations",
    "privacy_request",
    "replay_event",
}


class RequestWithState(Protocol):
    state: Any


@dataclass(frozen=True)
class VerifiedAuditPrincipal:
    actor_type: ActorType
    actor_id: str

    def __post_init__(self) -> None:
        if self.actor_type not in ("human", "service"):
            raise ValueError("Unsupported audit actor type.")
        if not self.actor_id.strip():
            raise ValueError("A verified audit principal requires a subject identifier.")


@dataclass(frozen=True)
class AuditContext:
    request_id: str
    actor_type: Optional[ActorType]
    actor_id: Optional[str]

    def __post_init__(self) -> None:
        if (self.actor_type is None) != (self.actor_id is None):
            raise ValueError("Audit actor type and identifier must both be present or absent.")


class AuditService:
    def assign_request_id(self, request: RequestWithState) -> str:
        request_id = generate_uuidv7()
        request.state.request_id = request_id
        return request_id

    def context_for_request(self, request: RequestWithState) -> AuditContext:
        request_id = getattr(request.state, "request_id", None)
        if not request_id:
            raise RuntimeError("Audit request context requires request ID middleware.")

        principal = getattr(request.state, "verified_audit_principal", None)
        if principal is None:
            return AuditContext(request_id=request_id, actor_type=None, actor_id=None)
        if not isinstance(principal, VerifiedAuditPrincipal):
            raise TypeError("Audit principal must come from a verified authentication adapter.")

        return AuditContext(
            request_id=request_id,
            actor_type=principal.actor_type,
            actor_id=principal.actor_id,
        )

    def record(
        self,
        cursor: Any,
        *,
        context: AuditContext,
        organization_id: str,
        action: str,
        target_type: str,
        target_id: str,
        result: AuditResult,
        reason: Optional[str] = None,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if result not in _AUDIT_RESULTS:
            raise ValueError(f"Unsupported audit result: {result}")
        if action not in _AUDIT_ACTIONS:
            raise ValueError(f"Unsupported audit action: {action}")
        if not target_type or not target_id:
            raise ValueError("Audit target identifiers are required.")

        reason_text = " ".join((reason or "not_recorded").split()) or "not_recorded"
        if len(reason_text) > _MAX_REASON_LENGTH:
            raise ValueError(f"Audit reason must be {_MAX_REASON_LENGTH} characters or fewer.")
        payload_json = (
            json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)
            if payload is not None
            else None
        )

        cursor.execute(
            """
            INSERT INTO audit_logs (
                id, organization_id, actor, action, target_type, target_id,
                reason, payload_json, created_at, actor_type, actor_id, result,
                request_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generate_uuidv7(),
                organization_id,
                context.actor_id or "unattributed",
                action,
                target_type,
                target_id,
                reason_text,
                payload_json,
                utc_now_iso(),
                context.actor_type,
                context.actor_id,
                result,
                context.request_id,
            ),
        )

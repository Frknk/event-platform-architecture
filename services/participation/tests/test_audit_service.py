from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from app.services.audit_service import (
    AuditContext,
    AuditService,
    VerifiedAuditPrincipal,
)


class RecordingCursor:
    def __init__(self) -> None:
        self.query = ""
        self.params: tuple[object, ...] = ()

    def execute(self, query: str, params: tuple[object, ...]) -> None:
        self.query = query
        self.params = params


class AuditServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AuditService()

    def test_request_id_is_server_generated_not_copied_from_header(self) -> None:
        request = SimpleNamespace(
            headers={"X-Request-Id": "client-chosen-request-id"},
            state=SimpleNamespace(),
        )

        request_id = self.service.assign_request_id(request)

        self.assertNotEqual(request_id, request.headers["X-Request-Id"])
        self.assertEqual(request.state.request_id, request_id)
        self.assertEqual(UUID(request_id).version, 7)

    def test_context_has_no_actor_without_verified_principal(self) -> None:
        request = SimpleNamespace(state=SimpleNamespace(request_id="server-request-id"))

        context = self.service.context_for_request(request)

        self.assertEqual(context, AuditContext("server-request-id", None, None))

    def test_context_uses_only_typed_verified_principal(self) -> None:
        request = SimpleNamespace(
            state=SimpleNamespace(
                request_id="server-request-id",
                verified_audit_principal=VerifiedAuditPrincipal(
                    actor_type="human",
                    actor_id="oidc-subject-123",
                ),
            )
        )

        context = self.service.context_for_request(request)

        self.assertEqual(context.actor_type, "human")
        self.assertEqual(context.actor_id, "oidc-subject-123")

    def test_context_rejects_untyped_principal_data(self) -> None:
        request = SimpleNamespace(
            state=SimpleNamespace(
                request_id="server-request-id",
                verified_audit_principal={"actor_id": "client-supplied"},
            )
        )

        with self.assertRaises(TypeError):
            self.service.context_for_request(request)

    def test_record_uses_minimal_fields_and_nulls_missing_actor(self) -> None:
        cursor = RecordingCursor()
        context = AuditContext("server-request-id", None, None)

        with patch(
            "app.services.audit_service.utc_now_iso",
            return_value="2026-11-10T00:00:00Z",
        ):
            self.service.record(
                cursor,
                context=context,
                organization_id="0192f000-0000-7000-8000-000000000001",
                action="correct_registration",
                target_type="registration",
                target_id="0192f000-0000-7000-8000-000000000702",
                result="success",
            )

        normalized_query = " ".join(cursor.query.split())
        self.assertIn("actor_type, actor_id, result, request_id", normalized_query)
        self.assertEqual(cursor.params[2:6], (
            "unattributed",
            "correct_registration",
            "registration",
            "0192f000-0000-7000-8000-000000000702",
        ))
        self.assertEqual(cursor.params[6], "2026-11-10T00:00:00Z")
        self.assertIsNone(cursor.params[7])
        self.assertIsNone(cursor.params[8])
        self.assertEqual(cursor.params[9:], ("success", "server-request-id"))


if __name__ == "__main__":
    unittest.main()

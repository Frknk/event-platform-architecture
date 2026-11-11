"""PostgreSQL integration tests for the Participation service.

They are skipped unless a PostgreSQL URL is provided, e.g.:

    TEST_DATABASE_URL=postgresql://participation_app:participation_local_secret@127.0.0.1:5434/participation_db \
        pytest tests/test_postgres_integration.py

The database must exist but can be empty: ``create_app`` applies the initial
migration and the post-migration tables on startup.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.core.security import generate_uuidv7
from app.db.database import DEFAULT_ORG_ID
from app.services.audit_service import AuditContext

PG_URL = os.environ.get("TEST_DATABASE_URL") or ""

pytestmark = pytest.mark.skipif(
    not PG_URL.startswith(("postgres://", "postgresql://")),
    reason="TEST_DATABASE_URL (postgres) is not configured",
)


@pytest.fixture(scope="module")
def client():
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg is not installed.")

    with psycopg.connect(PG_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            database_name = cur.fetchone()[0]
    if not database_name.endswith("_test"):
        pytest.skip(
            "PostgreSQL integration tests require a disposable database "
            "whose name ends in '_test'."
        )

    os.environ["DATABASE_URL"] = PG_URL
    os.environ.pop("PARTICIPATION_SQLITE_PATH", None)
    app = create_app()
    assert app.state.db.backend == "postgres"
    test_client = TestClient(app, raise_server_exceptions=False)
    reset = test_client.post("/participation/v1/demo/reset", json={"capacity": 5})
    assert reset.status_code == 200, reset.text
    return test_client


def _registration_payload(email: str) -> dict:
    return {
        "form_version_id": "0192f000-0000-7000-8000-000000000301",
        "consent_version": "consent-unas-v1.0",
        "consent_accepted": True,
        "attendee": {
            "full_name": "Postgres Test",
            "email": email,
            "document_number": "70123456",
            "institution": "UNAS",
        },
        "answers": {"track": "IA Aplicada", "role_profile": "Estudiante UNAS", "modality": "Presencial"},
    }


def test_pg_new_tables_and_endpoints(client):
    reset = client.post("/participation/v1/demo/reset", json={"capacity": 5})
    assert reset.status_code == 200

    reg = client.post(
        "/participation/public/v1/sites/incuba-unas/events/congreso-innovacion-demo-day-2026/registrations",
        json=_registration_payload("pg.endpoints@unas.edu.pe"),
        headers={"Idempotency-Key": f"pg-reg-{generate_uuidv7()}"},
    )
    assert reg.status_code == 201, reg.text
    reg_id = reg.json()["id"]

    # PA-031 writes to audit_logs (created by the post-migration step)
    correction = client.patch(
        f"/participation/v1/registrations/{reg_id}",
        json={"reason": "corrección de prueba", "updates": {"phone": "+51999888777"}},
    )
    assert correction.status_code == 200, correction.text

    # PA-034 writes to privacy_requests
    access = client.post(
        "/participation/v1/privacy-requests",
        json={"email": "pg.endpoints@unas.edu.pe", "request_type": "access"},
    )
    assert access.status_code == 200, access.text
    assert access.json()["request_type"] == "access"

    erasure = client.post(
        "/participation/v1/privacy-requests",
        json={"email": "pg.endpoints@unas.edu.pe", "request_type": "erasure"},
    )
    assert erasure.status_code == 200, erasure.text
    assert erasure.json()["status"] == "erased"


def test_pg_import_capacity_guard(client):
    reset = client.post("/participation/v1/demo/reset", json={"capacity": 5})
    assert reset.status_code == 200

    rows = [
        {"full_name": f"PG Import {i}", "email": f"pg.import{i}@unas.edu.pe"}
        for i in range(3)
    ]
    first = client.post(
        "/participation/v1/events/0192f000-0000-7000-8000-000000000101/registration-imports",
        json={"rows": rows, "legal_basis": "consentimiento de prueba"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["imported_count"] == 3

    second = client.post(
        "/participation/v1/events/0192f000-0000-7000-8000-000000000101/registration-imports",
        json={
            "rows": [
                {"full_name": f"PG Extra {i}", "email": f"pg.extra{i}@unas.edu.pe"}
                for i in range(3)
            ],
            "legal_basis": "consentimiento de prueba",
        },
    )
    assert second.status_code == 409, second.text
    assert second.json()["code"] == "capacity_exceeded"

    form = client.get(
        "/participation/public/v1/sites/incuba-unas/events/congreso-innovacion-demo-day-2026/form"
    ).json()
    assert form["capacity_summary"]["confirmed_count"] == 3
    assert form["capacity_summary"]["remaining"] == 2

    registrations = client.get(
        "/participation/v1/events/0192f000-0000-7000-8000-000000000101/registrations",
        params={"limit": 100},
    ).json()["items"]
    assert len(registrations) == 3


def test_pg_outbox_listing_does_not_use_sqlite_rowid(client):
    outbox = client.get("/participation/v1/outbox", params={"limit": 10})
    assert outbox.status_code == 200, outbox.text
    assert "items" in outbox.json()


def test_pg_audit_insert_failure_rolls_back_admin_correction(client):
    reset = client.post("/participation/v1/demo/reset", json={"capacity": 5})
    assert reset.status_code == 200

    reg = client.post(
        "/participation/public/v1/sites/incuba-unas/events/congreso-innovacion-demo-day-2026/registrations",
        json=_registration_payload("pg.atomic@unas.edu.pe"),
        headers={"Idempotency-Key": f"pg-atomic-{generate_uuidv7()}"},
    )
    assert reg.status_code == 201, reg.text
    registration_id = reg.json()["id"]

    db = client.app.state.db
    with db.read_cursor() as cur:
        cur.execute(
            "SELECT pii_ciphertext, revision FROM registrations WHERE id = ?",
            (registration_id,),
        )
        original_registration = dict(cur.fetchone())

    audit_service = client.app.state.audit_service
    conflicting_audit_id = generate_uuidv7()
    with patch(
        "app.services.audit_service.generate_uuidv7",
        return_value=conflicting_audit_id,
    ):
        with db.transaction() as cur:
            audit_service.record(
                cur,
                context=AuditContext(
                    request_id=f"fixture-{generate_uuidv7()}",
                    actor_type=None,
                    actor_id=None,
                ),
                organization_id=DEFAULT_ORG_ID,
                action="privacy_request",
                target_type="test-fixture",
                target_id=generate_uuidv7(),
                result="failed",
            )

    with db.read_cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM audit_logs WHERE id = ?",
            (conflicting_audit_id,),
        )
        assert cur.fetchone()["count"] == 1

    with (
        patch.object(audit_service, "record", wraps=audit_service.record) as record_audit,
        patch(
            "app.services.audit_service.generate_uuidv7",
            return_value=conflicting_audit_id,
        ),
    ):
        correction = client.patch(
            f"/participation/v1/registrations/{registration_id}",
            json={
                "reason": "Debe revertirse si falla la auditoría",
                "updates": {"full_name": "Changed Name"},
            },
        )

    assert correction.status_code == 500
    record_audit.assert_called_once()

    with db.read_cursor() as cur:
        cur.execute(
            "SELECT pii_ciphertext, revision FROM registrations WHERE id = ?",
            (registration_id,),
        )
        current_registration = dict(cur.fetchone())
        cur.execute(
            """
            SELECT COUNT(*) AS count
            FROM audit_logs
            WHERE action = 'correct_registration' AND target_id = ?
            """,
            (registration_id,),
        )
        correction_audit_count = cur.fetchone()["count"]
        cur.execute(
            "SELECT COUNT(*) AS count FROM audit_logs WHERE id = ?",
            (conflicting_audit_id,),
        )
        collision_audit_count = cur.fetchone()["count"]

    assert current_registration == original_registration
    assert correction_audit_count == 0
    assert collision_audit_count == 1

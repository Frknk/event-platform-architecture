"""PostgreSQL integration tests for the Participation service.

They are skipped unless a PostgreSQL URL is provided, e.g.:

    TEST_DATABASE_URL=postgresql://participation_app:participation_local_secret@127.0.0.1:5434/participation_db \
        pytest tests/test_postgres_integration.py

The database must exist but can be empty: ``create_app`` applies the initial
migration and the post-migration tables on startup.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.core.security import generate_uuidv7

PG_URL = os.environ.get("TEST_DATABASE_URL") or ""

pytestmark = pytest.mark.skipif(
    not PG_URL.startswith(("postgres://", "postgresql://")),
    reason="TEST_DATABASE_URL (postgres) is not configured",
)


@pytest.fixture(scope="module")
def client():
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

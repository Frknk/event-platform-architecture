"""
End-to-End Test Suite for Public Registration and Participant Self-Service (Team 3 — Participation).

Validates canonical endpoints:
1. GET  /participation/public/v1/sites/{site}/events/{event}/form
2. POST /participation/public/v1/sites/{site}/events/{event}/registrations
3. POST /participation/public/v1/registration-access
4. GET  /participation/self/v1/registrations/{registration_id}
5. POST /participation/self/v1/registrations/{registration_id}/cancellations
6. GET  /participation/self/v1/registrations/{registration_id}/ticket

Covers:
- Success responses (200, 201, 202)
- Error responses (400, 404, 409, 422) with RFC 7807/9457 Problem Details
- Idempotency-Key headers (replay, key-reuse with different payload, missing/short key)
- Device token validation & issuance (X-Participation-Device)
- Tenant blind index HMAC duplicate prevention
- Asymmetric ECDSA (ES256) QR JWS validation and Zero-PII invariants
- Atomic capacity enforcement, waitlist ordering, and automatic FIFO promotion on cancellation
- Outbox domain event emission & zero-PII event payloads
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Any, Dict, Generator
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.security import generate_uuidv7, security_manager
from app.db.database import (
    DEFAULT_CONSENT_VERSION,
    DEFAULT_EVENT_ID,
    DEFAULT_EVENT_SLUG,
    DEFAULT_FORM_VERSION_ID,
    DEFAULT_ORG_ID,
    DEFAULT_PUBLICATION_ID,
    DEFAULT_SITE_SLUG,
)
from app.main import create_app
from app.services.abuse_protection import DEVICE_HEADER


# =============================================================================
# Test Fixtures & Helpers
# =============================================================================

@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """Provide a TestClient backed by an isolated temporary SQLite database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    app = create_app(db_path=db_path)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    try:
        os.remove(db_path)
    except OSError:
        pass


def _sample_payload(
    *,
    email: str = "estudiante.test@unas.edu.pe",
    name: str = "Valeria Rios",
    doc_number: str = "72345678",
    phone: str = "+51987654321",
    institution: str = "UNAS",
    track: str = "IA Aplicada & Transformación Digital",
    role_profile: str = "Estudiante UNAS",
    modality: str = "Presencial — Auditorio Central UNAS",
    form_version_id: str = DEFAULT_FORM_VERSION_ID,
    consent_version: str = DEFAULT_CONSENT_VERSION,
    consent_accepted: bool = True,
) -> Dict[str, Any]:
    """Helper to construct a valid registration payload matching the seeded form schema."""
    return {
        "form_version_id": form_version_id,
        "consent_version": consent_version,
        "consent_accepted": consent_accepted,
        "attendee": {
            "full_name": name,
            "email": email,
            "document_number": doc_number,
            "phone": phone,
            "institution": institution,
        },
        "answers": {
            "track": track,
            "role_profile": role_profile,
            "modality": modality,
        },
    }


def _issue_device_token(client: TestClient) -> str:
    """Issue a valid signed device token using the app abuse protection instance."""
    _, token = client.app.state.abuse_protection.issue_device_token()
    return token


# =============================================================================
# 1. GET /participation/public/v1/sites/{site}/events/{event}/form
# =============================================================================

def test_get_public_form_valid_active(client: TestClient):
    """
    PA-001: Active published event form returns 200 with schema, UI schema,
    capacity summary, and standard ETag/Cache-Control caching headers.
    """
    res = client.get(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/form"
    )
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    assert "ETag" in res.headers
    assert res.headers["Cache-Control"] == "public, max-age=5"
    assert "X-Request-Id" in res.headers

    data = res.json()
    assert data["site_slug"] == DEFAULT_SITE_SLUG
    assert data["event_slug"] == DEFAULT_EVENT_SLUG
    assert data["event_id"] == DEFAULT_EVENT_ID
    assert data["organization_id"] == DEFAULT_ORG_ID
    assert data["publication_id"] == DEFAULT_PUBLICATION_ID
    assert data["form_version_id"] == DEFAULT_FORM_VERSION_ID
    assert data["consent_version"] == DEFAULT_CONSENT_VERSION
    assert data["binding_state"] == "open"
    assert data["requires_approval"] is False
    assert len(data["schema_hash"]) == 64

    # Capacity summary
    cap = data["capacity_summary"]
    assert cap["capacity"] == 5
    assert cap["confirmed_count"] == 0
    assert cap["remaining"] == 5
    assert cap["waitlist_enabled"] is True
    assert cap["waitlist_count"] == 0
    assert cap["public_state"] == "available"

    # Schema & legal consent
    assert isinstance(data["schema"], dict)
    assert data["schema"]["type"] == "object"
    assert "track" in data["schema"]["properties"]
    assert "role_profile" in data["schema"]["properties"]
    assert "modality" in data["schema"]["properties"]
    assert set(data["schema"]["required"]) == {"track", "role_profile", "modality"}
    assert "ui:order" in data["ui_schema"]
    assert "Ley N° 29733" in data["consent_text"]


def test_get_public_form_non_existent_event_and_site(client: TestClient):
    """
    PA-001 / Error 404: Returns RFC 7807 problem details when site or event does not exist.
    """
    # Non-existent event
    res_event = client.get(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/inexistent-event-slug/form"
    )
    assert res_event.status_code == 404
    assert res_event.headers["content-type"].startswith("application/problem+json")
    problem = res_event.json()
    assert problem["status"] == 404
    assert problem["code"] == "publication_incomplete"
    assert problem["title"] == "Published event form not found"
    assert "request_id" in problem

    # Non-existent site
    res_site = client.get(
        f"/participation/public/v1/sites/unknown-site/events/{DEFAULT_EVENT_SLUG}/form"
    )
    assert res_site.status_code == 404
    assert res_site.headers["content-type"].startswith("application/problem+json")
    assert res_site.json()["code"] == "publication_incomplete"


def test_get_public_form_schema_contract_validation(client: TestClient):
    """
    PA-001: Form schema conforms to JSON Schema specifications and matches schema_hash.
    """
    res = client.get(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/form"
    )
    data = res.json()
    schema = data["schema"]

    # Verify standard JSON Schema draft declaration
    assert "$schema" in schema
    assert schema["type"] == "object"
    assert "properties" in schema
    assert "required" in schema
    assert "enum" in schema["properties"]["track"]
    assert "enum" in schema["properties"]["role_profile"]
    assert "enum" in schema["properties"]["modality"]

    # Verify schema_hash integrity
    computed_hash = hashlib.sha256(
        json.dumps(schema, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert computed_hash == data["schema_hash"]


# =============================================================================
# 2. POST /participation/public/v1/sites/{site}/events/{event}/registrations
# =============================================================================

def test_create_registration_success_with_idempotency_and_device_header(client: TestClient):
    """
    PA-002: Successfully create registration with valid Idempotency-Key & device header.
    Returns 201 Created, ticket receipt, self_access_token, and no-store cache control.
    """
    device_token = _issue_device_token(client)
    idem_key = f"idem-valid-{generate_uuidv7()}"
    payload = _sample_payload(email="lucia.torres@unas.edu.pe", name="Lucía Torres")

    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload,
        headers={
            "Idempotency-Key": idem_key,
            DEVICE_HEADER: device_token,
        },
    )

    assert res.status_code == 201
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["ETag"] == '"1"'
    assert res.headers.get(DEVICE_HEADER) == device_token
    assert "X-Request-Id" in res.headers

    receipt = res.json()
    assert receipt["organization_id"] == DEFAULT_ORG_ID
    assert receipt["event_id"] == DEFAULT_EVENT_ID
    assert receipt["state"] == "confirmed"
    assert receipt["waitlist_position"] is None
    assert receipt["ticket_id"] is not None
    assert receipt["revision"] == 1
    assert "self_access_token" in receipt

    # Verify issued self_access_token claims
    _, claims = security_manager.verify_jws(
        receipt["self_access_token"], expected_aud="participation-self"
    )
    assert claims["reg_id"] == receipt["id"]
    assert claims["org_id"] == DEFAULT_ORG_ID
    assert claims["scope"] == "registration:self"


def test_create_registration_duplicate_idempotency_key_reuse_check(client: TestClient):
    """
    PA-002 / API-CONTRACTS.md:
    1. Replay with SAME key and SAME payload returns stored 201 without consuming another slot.
    2. Replay with SAME key and DIFFERENT payload returns 409 idempotency_key_reused problem details.
    """
    device_token = _issue_device_token(client)
    idem_key = f"idem-replay-{generate_uuidv7()}"
    payload1 = _sample_payload(email="roberto.diaz@unas.edu.pe", name="Roberto Díaz")

    # 1. Initial successful registration
    r1 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload1,
        headers={"Idempotency-Key": idem_key, DEVICE_HEADER: device_token},
    )
    assert r1.status_code == 201
    receipt1 = r1.json()

    # 2. Replay with identical payload returns exact same receipt
    r2 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload1,
        headers={"Idempotency-Key": idem_key, DEVICE_HEADER: device_token},
    )
    assert r2.status_code == 201
    receipt2 = r2.json()
    assert receipt2["id"] == receipt1["id"]
    assert receipt2["ticket_id"] == receipt1["ticket_id"]

    # Verify form confirmed_count is still 1 (slot not duplicated)
    form_res = client.get(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/form"
    )
    assert form_res.json()["capacity_summary"]["confirmed_count"] == 1

    # 3. Same key with DIFFERENT payload -> 409 idempotency_key_reused
    payload_diff = _sample_payload(email="otro.alumno@unas.edu.pe", name="Otro Alumno")
    r3 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload_diff,
        headers={"Idempotency-Key": idem_key, DEVICE_HEADER: device_token},
    )
    assert r3.status_code == 409
    assert r3.headers["content-type"].startswith("application/problem+json")
    problem = r3.json()
    assert problem["code"] == "idempotency_key_reused"
    assert problem["title"] == "Idempotency-Key reused with different payload"


def test_create_registration_missing_or_invalid_idempotency_header(client: TestClient):
    """
    PA-002: Header Idempotency-Key is mandatory and must be at least 8 characters.
    - Missing header -> 422 Unprocessable Entity
    - Too short (< 8 chars) -> 400 ProblemException validation_error
    """
    payload = _sample_payload(email="marco.polo@unas.edu.pe", name="Marco Polo")

    # Missing header
    r_missing = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload,
    )
    assert r_missing.status_code == 422

    # Short key (< 8 characters)
    r_short = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload,
        headers={"Idempotency-Key": "short"},
    )
    assert r_short.status_code == 400
    assert r_short.headers["content-type"].startswith("application/problem+json")
    assert r_short.json()["code"] == "validation_error"


def test_create_registration_duplicate_participant_hmac_blind_index(client: TestClient):
    """
    PA-004: Duplicate registration detection using normalized email HMAC blind index.
    A second registration with a different Idempotency-Key but same email (case/space variant)
    is rejected with 409 already_registered.
    """
    # First registration
    client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="carmen.flores@unas.edu.pe", name="Carmen Flores"),
        headers={"Idempotency-Key": f"idem-reg1-{generate_uuidv7()}"},
    )

    # Attempt second registration with same email variant: uppercase and whitespace
    r_dup = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="  CARMEN.FLORES@UNAS.EDU.PE  ", name="Carmen Flores"),
        headers={"Idempotency-Key": f"idem-reg2-{generate_uuidv7()}"},
    )
    assert r_dup.status_code == 409
    assert r_dup.headers["content-type"].startswith("application/problem+json")
    problem = r_dup.json()
    assert problem["code"] == "already_registered"
    assert problem["title"] == "Duplicate registration detected"


def test_create_registration_invalid_consent_version_and_non_acceptance(client: TestClient):
    """
    PA-002 / AD-7: Requires explicit consent and match with the active consent_version.
    """
    # 1. Invalid consent version
    r_bad_ver = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(
            email="consent.test1@unas.edu.pe",
            consent_version="consent-unas-deprecated-v0.1",
        ),
        headers={"Idempotency-Key": f"idem-consent-1-{generate_uuidv7()}"},
    )
    assert r_bad_ver.status_code == 409
    assert r_bad_ver.json()["code"] == "consent_version_mismatch"

    # 2. Consent not accepted (consent_accepted: false)
    r_not_accepted = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(
            email="consent.test2@unas.edu.pe",
            consent_accepted=False,
        ),
        headers={"Idempotency-Key": f"idem-consent-2-{generate_uuidv7()}"},
    )
    assert r_not_accepted.status_code == 409
    assert r_not_accepted.json()["code"] == "consent_version_mismatch"


def test_create_registration_form_version_mismatch(client: TestClient):
    """
    PA-002: Form version mismatch returns 409 form_version_mismatch.
    """
    r_bad_form = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(
            email="form.ver@unas.edu.pe",
            form_version_id="0192f000-0000-7000-8000-999999999999",
        ),
        headers={"Idempotency-Key": f"idem-bad-form-{generate_uuidv7()}"},
    )
    assert r_bad_form.status_code == 409
    assert r_bad_form.json()["code"] == "form_version_mismatch"


def test_create_registration_validation_errors(client: TestClient):
    """
    PA-002: Missing required schema fields or invalid attendee identity return 422 validation_error.
    """
    # 1. Missing required answer 'modality'
    payload_missing = _sample_payload(email="missing.field@unas.edu.pe")
    del payload_missing["answers"]["modality"]
    r_missing = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload_missing,
        headers={"Idempotency-Key": f"idem-missing-{generate_uuidv7()}"},
    )
    assert r_missing.status_code == 422
    problem = r_missing.json()
    assert problem["code"] == "validation_error"
    assert any("/answers/modality" in err.get("pointer", "") for err in problem.get("errors", []))

    # 2. Malformed email (missing @)
    payload_bad_email = _sample_payload(email="notanemail")
    r_bad_email = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload_bad_email,
        headers={"Idempotency-Key": f"idem-bad-email-{generate_uuidv7()}"},
    )
    assert r_bad_email.status_code == 422
    assert r_bad_email.json()["code"] == "validation_error"

    # 3. Short full name (< 2 chars)
    payload_short_name = _sample_payload(name="X")
    r_short_name = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload_short_name,
        headers={"Idempotency-Key": f"idem-short-name-{generate_uuidv7()}"},
    )
    assert r_short_name.status_code == 422


def test_create_registration_non_existent_event(client: TestClient):
    """
    POST to non-existent event returns 404 publication_incomplete.
    """
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/ghost-event/registrations",
        json=_sample_payload(),
        headers={"Idempotency-Key": f"idem-ghost-{generate_uuidv7()}"},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "publication_incomplete"


def test_create_registration_waitlist_behavior(client: TestClient):
    """
    PA-011 & PA-012:
    - Default release capacity is 5.
    - First 5 registrations receive 'confirmed' status with a ticket_id.
    - 6th and 7th registrations receive 'waitlisted' status, waitlist_position (1, 2), and ticket_id=None.
    - Form capacity summary accurately updates public_state to 'waitlist'.
    """
    confirmed_regs = []
    for i in range(1, 6):
        res = client.post(
            f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
            json=_sample_payload(
                email=f"conf{i}@unas.edu.pe",
                name=f"Confirmed Attendee {i}",
            ),
            headers={"Idempotency-Key": f"idem-conf-{i}-{generate_uuidv7()}"},
        )
        assert res.status_code == 201
        data = res.json()
        assert data["state"] == "confirmed"
        assert data["ticket_id"] is not None
        assert data["waitlist_position"] is None
        confirmed_regs.append(data)

    # 6th registration -> waitlisted position 1
    res6 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(
            email="waitlist1@unas.edu.pe",
            name="Waitlist User One",
        ),
        headers={"Idempotency-Key": f"idem-wl-1-{generate_uuidv7()}"},
    )
    assert res6.status_code == 201
    wl1 = res6.json()
    assert wl1["state"] == "waitlisted"
    assert wl1["ticket_id"] is None
    assert wl1["waitlist_position"] == 1
    assert "self_access_token" in wl1

    # 7th registration -> waitlisted position 2
    res7 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(
            email="waitlist2@unas.edu.pe",
            name="Waitlist User Two",
        ),
        headers={"Idempotency-Key": f"idem-wl-2-{generate_uuidv7()}"},
    )
    assert res7.status_code == 201
    wl2 = res7.json()
    assert wl2["state"] == "waitlisted"
    assert wl2["ticket_id"] is None
    assert wl2["waitlist_position"] == 2

    # Check public form capacity summary
    form_res = client.get(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/form"
    )
    cap = form_res.json()["capacity_summary"]
    assert cap["capacity"] == 5
    assert cap["confirmed_count"] == 5
    assert cap["remaining"] == 0
    assert cap["waitlist_count"] == 2
    assert cap["public_state"] == "waitlist_only"


def test_create_registration_capacity_exhausted_when_waitlist_disabled(client: TestClient):
    """
    PA-011: When capacity is full and waitlist_enabled is False, registration is rejected with 409 capacity_exhausted.
    """
    # Configure event capacity=2, waitlist_enabled=False via demo reset
    client.post("/participation/v1/demo/reset", json={"capacity": 2, "waitlist_enabled": False})

    # Fill 2 spots
    for i in range(1, 3):
        res = client.post(
            f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
            json=_sample_payload(email=f"user{i}@unas.edu.pe", name=f"User {i}"),
            headers={"Idempotency-Key": f"idem-full-{i}-{generate_uuidv7()}"},
        )
        assert res.status_code == 201

    # 3rd registration -> 409 capacity_exhausted
    res3 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="user3@unas.edu.pe", name="User 3"),
        headers={"Idempotency-Key": f"idem-full-3-{generate_uuidv7()}"},
    )
    assert res3.status_code == 409
    assert res3.json()["code"] == "capacity_exhausted"


# =============================================================================
# 3. POST /participation/public/v1/registration-access
# =============================================================================

def test_registration_access_magic_link_flow(client: TestClient):
    """
    PA-006: Request single-use access magic link returns 202 Accepted.
    """
    device_token = _issue_device_token(client)
    res = client.post(
        "/participation/public/v1/registration-access",
        json={"email": "maria.santos@unas.edu.pe"},
        headers={
            "Idempotency-Key": f"idem-access-{generate_uuidv7()}",
            DEVICE_HEADER: device_token,
        },
    )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "accepted"
    assert "single-use access link" in data["detail"]
    assert "X-Request-Id" in res.headers


def test_registration_access_anti_enumeration(client: TestClient):
    """
    PA-006: Non-existent email returns the identical neutral 202 response to prevent email enumeration.
    """
    res = client.post(
        "/participation/public/v1/registration-access",
        json={"email": "nobody.nonexistent@unas.edu.pe"},
        headers={"Idempotency-Key": f"idem-access-enum-{generate_uuidv7()}"},
    )
    assert res.status_code == 202
    assert res.json()["status"] == "accepted"
    assert "single-use access link" in res.json()["detail"]


def test_registration_access_missing_idempotency_key(client: TestClient):
    """
    POST /registration-access requires Idempotency-Key header.
    """
    res = client.post(
        "/participation/public/v1/registration-access",
        json={"email": "test@unas.edu.pe"},
    )
    assert res.status_code == 422


# =============================================================================
# 4. GET /participation/self/v1/registrations/{registration_id}
# =============================================================================

def test_get_registration_self_confirmed(client: TestClient):
    """
    PA-006 & AD-8: Confirmed participant retrieves self registration details.
    PII ciphertext is securely decrypted at runtime. Returns active ticket summary and ETag.
    """
    email = "gonzalo.vega@unas.edu.pe"
    name = "Gonzalo Vega"
    doc_number = "71234567"
    phone = "+51912345678"

    create_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email=email, name=name, doc_number=doc_number, phone=phone),
        headers={"Idempotency-Key": f"idem-self-{generate_uuidv7()}"},
    )
    assert create_res.status_code == 201
    reg_id = create_res.json()["id"]

    res = client.get(f"/participation/self/v1/registrations/{reg_id}")
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["ETag"] == '"1"'

    data = res.json()
    assert data["id"] == reg_id
    assert data["organization_id"] == DEFAULT_ORG_ID
    assert data["event_id"] == DEFAULT_EVENT_ID
    assert data["state"] == "confirmed"
    assert data["revision"] == 1
    assert data["waitlist"] is None

    # Verify decrypted PII
    attendee = data["attendee"]
    assert attendee["full_name"] == name
    assert attendee["email"] == email
    assert attendee["document_number"] == doc_number
    assert attendee["phone"] == phone
    assert attendee["institution"] == "UNAS"

    # Verify decrypted answers
    answers = data["answers"]
    assert answers["track"] == "IA Aplicada & Transformación Digital"
    assert answers["role_profile"] == "Estudiante UNAS"
    assert answers["modality"] == "Presencial — Auditorio Central UNAS"

    # Verify active ticket summary
    tkt_summary = data["ticket_summary"]
    assert tkt_summary is not None
    assert tkt_summary["state"] == "active"
    assert tkt_summary["version"] == 1
    assert tkt_summary["revoked_at"] is None


def test_get_registration_self_waitlisted(client: TestClient):
    """
    PA-006: Waitlisted participant retrieves self details. Shows waitlist entry and no ticket summary.
    """
    client.post("/participation/v1/demo/reset", json={"capacity": 1, "waitlist_enabled": True})

    # User 1 confirmed
    client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="user1@unas.edu.pe", name="User 1"),
        headers={"Idempotency-Key": f"idem-wl-self-1-{generate_uuidv7()}"},
    )

    # User 2 waitlisted
    res2 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="user2@unas.edu.pe", name="User 2"),
        headers={"Idempotency-Key": f"idem-wl-self-2-{generate_uuidv7()}"},
    )
    reg2_id = res2.json()["id"]

    res = client.get(f"/participation/self/v1/registrations/{reg2_id}")
    assert res.status_code == 200
    data = res.json()
    assert data["state"] == "waitlisted"
    assert data["ticket_summary"] is None
    assert data["waitlist"]["position"] == 1
    assert data["waitlist"]["state"] == "waiting"
    assert data["waitlist"]["promoted_at"] is None


def test_get_registration_self_non_existent(client: TestClient):
    """
    Non-existent registration ID returns 404 tenant_forbidden.
    """
    ghost_id = generate_uuidv7()
    res = client.get(f"/participation/self/v1/registrations/{ghost_id}")
    assert res.status_code == 404
    assert res.json()["code"] == "tenant_forbidden"


# =============================================================================
# 5. POST /participation/self/v1/registrations/{registration_id}/cancellations
# =============================================================================

def test_cancel_registration_confirmed_no_waitlist(client: TestClient):
    """
    PA-007: Confirmed participant cancels registration without a waitlist.
    Revokes the ticket, sets state to cancelled, and decrements confirmed_count.
    """
    create_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="cancel.user@unas.edu.pe", name="Cancel User"),
        headers={"Idempotency-Key": f"idem-cancel-setup-{generate_uuidv7()}"},
    )
    reg_id = create_res.json()["id"]

    cancel_res = client.post(
        f"/participation/self/v1/registrations/{reg_id}/cancellations",
        json={"reason": "Viaje académico no programado"},
        headers={"Idempotency-Key": f"idem-cancel-exec-{generate_uuidv7()}"},
    )
    assert cancel_res.status_code == 200
    assert cancel_res.headers["Cache-Control"] == "no-store"

    body = cancel_res.json()
    assert body["id"] == reg_id
    assert body["state"] == "cancelled"
    assert body["prior_state"] == "confirmed"
    assert body["ticket_revoked"] is True
    assert body["promoted_registration_id"] is None
    assert body["cancelled_at"] is not None

    # Verify self registration state
    self_res = client.get(f"/participation/self/v1/registrations/{reg_id}")
    assert self_res.json()["state"] == "cancelled"
    assert self_res.json()["cancelled_at"] is not None

    # Capacity confirmed_count decremented to 0
    form_res = client.get(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/form"
    )
    assert form_res.json()["capacity_summary"]["confirmed_count"] == 0
    assert form_res.json()["capacity_summary"]["remaining"] == 5


def test_cancel_registration_confirmed_automatic_waitlist_promotion(client: TestClient):
    """
    PA-007 & PA-013:
    When a confirmed registration is cancelled and there are waiting attendees in waitlist,
    the #1 waitlisted attendee is atomically promoted to confirmed and issued a new ticket
    in the SAME database transaction.
    """
    client.post("/participation/v1/demo/reset", json={"capacity": 2, "waitlist_enabled": True})

    # Register 2 confirmed + 2 waitlisted
    attendees = []
    for i in range(1, 5):
        r = client.post(
            f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
            json=_sample_payload(email=f"promo.attendee{i}@unas.edu.pe", name=f"Attendee {i}"),
            headers={"Idempotency-Key": f"idem-setup-promo-{i}-{generate_uuidv7()}"},
        )
        assert r.status_code == 201
        attendees.append(r.json())

    user1_id = attendees[0]["id"]
    user2_id = attendees[1]["id"]
    user3_id = attendees[2]["id"]  # Waitlist position 1
    user4_id = attendees[3]["id"]  # Waitlist position 2

    assert attendees[0]["state"] == "confirmed"
    assert attendees[1]["state"] == "confirmed"
    assert attendees[2]["state"] == "waitlisted" and attendees[2]["waitlist_position"] == 1
    assert attendees[3]["state"] == "waitlisted" and attendees[3]["waitlist_position"] == 2

    # User 1 cancels
    cancel_res = client.post(
        f"/participation/self/v1/registrations/{user1_id}/cancellations",
        json={"reason": "Superposición de horarios"},
        headers={"Idempotency-Key": f"idem-cancel-user1-{generate_uuidv7()}"},
    )
    assert cancel_res.status_code == 200
    cancel_body = cancel_res.json()
    assert cancel_body["state"] == "cancelled"
    assert cancel_body["ticket_revoked"] is True
    assert cancel_body["promoted_registration_id"] == user3_id

    # Verify User 1 ticket is revoked
    tkt1 = client.get(f"/participation/self/v1/registrations/{user1_id}/ticket").json()
    assert tkt1["state"] == "revoked"
    assert tkt1["revoked_at"] is not None

    # Verify User 3 (formerly waitlist 1) is now confirmed and has an active ticket
    self3 = client.get(f"/participation/self/v1/registrations/{user3_id}").json()
    assert self3["state"] == "confirmed"
    assert self3["waitlist"]["state"] == "promoted"
    assert self3["waitlist"]["promoted_at"] is not None
    assert self3["ticket_summary"]["state"] == "active"

    tkt3_res = client.get(f"/participation/self/v1/registrations/{user3_id}/ticket")
    assert tkt3_res.status_code == 200
    assert tkt3_res.json()["state"] == "active"

    # Verify User 4 is still waitlisted
    self4 = client.get(f"/participation/self/v1/registrations/{user4_id}").json()
    assert self4["state"] == "waitlisted"
    assert self4["waitlist"]["state"] == "waiting"


def test_cancel_registration_waitlisted_user(client: TestClient):
    """
    PA-007: Waitlisted participant cancels their waitlist entry.
    Ticket is not revoked (never existed) and waitlist_count is decremented.
    """
    client.post("/participation/v1/demo/reset", json={"capacity": 1, "waitlist_enabled": True})

    # 1 confirmed
    client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="held@unas.edu.pe", name="Held"),
        headers={"Idempotency-Key": f"idem-wl-held-{generate_uuidv7()}"},
    )
    # 1 waitlisted
    wl_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="wl.cancel@unas.edu.pe", name="WL Cancel"),
        headers={"Idempotency-Key": f"idem-wl-to-cancel-{generate_uuidv7()}"},
    )
    wl_id = wl_res.json()["id"]

    cancel_res = client.post(
        f"/participation/self/v1/registrations/{wl_id}/cancellations",
        json={"reason": "Ya no deseo esperar cupo"},
        headers={"Idempotency-Key": f"idem-wl-cancel-{generate_uuidv7()}"},
    )
    assert cancel_res.status_code == 200
    body = cancel_res.json()
    assert body["state"] == "cancelled"
    assert body["prior_state"] == "waitlisted"
    assert body["ticket_revoked"] is False
    assert body["promoted_registration_id"] is None

    # Form shows waitlist_count is 0
    form_res = client.get(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/form"
    )
    assert form_res.json()["capacity_summary"]["waitlist_count"] == 0


def test_cancel_registration_idempotency_and_re_cancellation(client: TestClient):
    """
    PA-007: Cancellation is idempotent.
    - Replaying with SAME key returns cached response.
    - Calling cancellation with NEW key on already-cancelled registration safely returns cancelled status.
    - Reusing key with DIFFERENT reason returns 409 idempotency_key_reused.
    """
    create_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="re.cancel@unas.edu.pe", name="Re Cancel"),
        headers={"Idempotency-Key": f"idem-re-cancel-setup-{generate_uuidv7()}"},
    )
    reg_id = create_res.json()["id"]
    idem_key = f"idem-cancel-key-{generate_uuidv7()}"

    # 1. First cancellation
    r1 = client.post(
        f"/participation/self/v1/registrations/{reg_id}/cancellations",
        json={"reason": "Cancel 1"},
        headers={"Idempotency-Key": idem_key},
    )
    assert r1.status_code == 200

    # 2. Replay with SAME key & same payload
    r2 = client.post(
        f"/participation/self/v1/registrations/{reg_id}/cancellations",
        json={"reason": "Cancel 1"},
        headers={"Idempotency-Key": idem_key},
    )
    assert r2.status_code == 200
    assert r2.json()["cancelled_at"] == r1.json()["cancelled_at"]

    # 3. New cancellation call with DIFFERENT key on already-cancelled registration
    r3 = client.post(
        f"/participation/self/v1/registrations/{reg_id}/cancellations",
        json={"reason": "Repeat cancellation request"},
        headers={"Idempotency-Key": f"idem-new-key-{generate_uuidv7()}"},
    )
    assert r3.status_code == 200
    assert r3.json()["state"] == "cancelled"
    assert r3.json()["prior_state"] == "cancelled"

    # 4. Same key with DIFFERENT payload -> 409 idempotency_key_reused
    r4 = client.post(
        f"/participation/self/v1/registrations/{reg_id}/cancellations",
        json={"reason": "Different reason"},
        headers={"Idempotency-Key": idem_key},
    )
    assert r4.status_code == 409
    assert r4.json()["code"] == "idempotency_key_reused"


def test_cancel_registration_non_existent(client: TestClient):
    """
    Cancelling a non-existent registration returns 404 tenant_forbidden.
    """
    res = client.post(
        f"/participation/self/v1/registrations/{generate_uuidv7()}/cancellations",
        json={"reason": "Inexistent"},
        headers={"Idempotency-Key": f"idem-inexistent-{generate_uuidv7()}"},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "tenant_forbidden"


# =============================================================================
# 6. GET /participation/self/v1/registrations/{registration_id}/ticket
# =============================================================================

def test_get_registration_ticket_confirmed_signed_qr_jws_validation(client: TestClient):
    """
    PA-008 & PA-015:
    Ticket retrieval returns signed ES256 compact JWS, decoded claims, and no-store caching.
    Validates cryptographic signature with security_manager and verifies Zero-PII invariant.
    """
    email = "raul.castillo@unas.edu.pe"
    name = "Raúl Castillo"
    doc_number = "73456789"
    phone = "+51976543210"

    reg_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email=email, name=name, doc_number=doc_number, phone=phone),
        headers={"Idempotency-Key": f"idem-ticket-test-{generate_uuidv7()}"},
    )
    reg_id = reg_res.json()["id"]

    res = client.get(f"/participation/self/v1/registrations/{reg_id}/ticket")
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "no-store"

    data = res.json()
    assert data["registration_id"] == reg_id
    assert data["event_id"] == DEFAULT_EVENT_ID
    assert data["organization_id"] == DEFAULT_ORG_ID
    assert data["state"] == "active"
    assert data["version"] == 1
    assert data["audience"] == "onsite"
    assert data["scope"] == "general"
    assert data["revoked_at"] is None

    # Cryptographic validation of signed QR JWS compact token
    jws_compact = data["ticket_jws"]
    assert len(jws_compact.split(".")) == 3  # Header.Payload.Signature

    header, claims = security_manager.verify_jws(jws_compact, expected_aud="onsite")
    assert header["alg"] == "ES256"
    assert header["typ"] == "ticket+jws"
    assert claims["iss"] == "participation-service"
    assert claims["aud"] == "onsite"
    assert claims["reg_id"] == reg_id
    assert claims["event_id"] == DEFAULT_EVENT_ID
    assert claims["scope"] == "general"

    # Non-negotiable Architecture Invariant: Zero PII in QR Token (PA-008, PA-015, AD-8)
    jws_lower = jws_compact.lower()
    assert "raul" not in jws_lower
    assert "castillo" not in jws_lower
    assert doc_number not in jws_compact
    assert "email" not in claims
    assert "full_name" not in claims
    assert "document_number" not in claims
    assert "phone" not in claims


def test_get_registration_ticket_waitlisted_conflict(client: TestClient):
    """
    PA-008: Waitlisted participant attempting to download ticket receives 409 conflict.
    """
    client.post("/participation/v1/demo/reset", json={"capacity": 1, "waitlist_enabled": True})

    # Confirmed #1
    client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="occupy@unas.edu.pe", name="Occupy"),
        headers={"Idempotency-Key": f"idem-occupy-{generate_uuidv7()}"},
    )

    # Waitlisted #2
    wl_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="wl.ticket@unas.edu.pe", name="WL Ticket"),
        headers={"Idempotency-Key": f"idem-wl-ticket-{generate_uuidv7()}"},
    )
    wl_id = wl_res.json()["id"]

    res = client.get(f"/participation/self/v1/registrations/{wl_id}/ticket")
    assert res.status_code == 409
    assert res.headers["content-type"].startswith("application/problem+json")
    problem = res.json()
    assert problem["code"] == "waitlisted"
    assert "Registration is in state 'waitlisted'" in problem["detail"]


def test_get_registration_ticket_cancelled_shows_revoked(client: TestClient):
    """
    PA-008: Participant whose registration was cancelled has ticket state 'revoked' with revoked_at timestamp.
    """
    reg_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="revoked.test@unas.edu.pe", name="Revoked Test"),
        headers={"Idempotency-Key": f"idem-rev-setup-{generate_uuidv7()}"},
    )
    reg_id = reg_res.json()["id"]

    client.post(
        f"/participation/self/v1/registrations/{reg_id}/cancellations",
        json={"reason": "Cancelación voluntaria"},
        headers={"Idempotency-Key": f"idem-rev-cancel-{generate_uuidv7()}"},
    )

    res = client.get(f"/participation/self/v1/registrations/{reg_id}/ticket")
    assert res.status_code == 200
    data = res.json()
    assert data["state"] == "revoked"
    assert data["revoked_at"] is not None


def test_get_registration_ticket_non_existent(client: TestClient):
    """
    Ticket lookup on non-existent registration returns 404 tenant_forbidden.
    """
    res = client.get(f"/participation/self/v1/registrations/{generate_uuidv7()}/ticket")
    assert res.status_code == 404
    assert res.json()["code"] == "tenant_forbidden"


# =============================================================================
# 7. Outbox Domain Events Lifecycle Verification
# =============================================================================

def test_outbox_domain_events_lifecycle(client: TestClient):
    """
    Verifies that all registration and self-service lifecycle actions correctly emit
    domain events to the transactional outbox:
    - participation.registration.created.v1
    - participation.registration.confirmed.v1
    - participation.registration.waitlisted.v1
    - participation.registration.cancelled.v1
    - participation.ticket.revoked.v1
    - participation.registration.promoted.v1
    - participation.availability.changed.v1
    And guarantees zero PII in outbox payloads.
    """
    client.post("/participation/v1/demo/reset", json={"capacity": 1, "waitlist_enabled": True})

    secret_name = "Valentina Paredes"
    secret_email = "valentina.paredes@unas.edu.pe"

    # 1. Register confirmed user
    r1 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email=secret_email, name=secret_name),
        headers={"Idempotency-Key": f"idem-outbox-conf-{generate_uuidv7()}"},
    )
    user1_id = r1.json()["id"]

    # 2. Register waitlisted user
    r2 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_payload(email="wl.outbox@unas.edu.pe", name="WL Outbox"),
        headers={"Idempotency-Key": f"idem-outbox-wl-{generate_uuidv7()}"},
    )
    user2_id = r2.json()["id"]

    # 3. User 1 cancels -> triggers revocation & promotion of User 2
    client.post(
        f"/participation/self/v1/registrations/{user1_id}/cancellations",
        json={"reason": "Outbox lifecycle test"},
        headers={"Idempotency-Key": f"idem-outbox-cancel-{generate_uuidv7()}"},
    )

    # Fetch outbox items
    outbox_res = client.get("/participation/v1/outbox?limit=100")
    assert outbox_res.status_code == 200
    items = outbox_res.json()["items"]
    topics = [item["topic"] for item in items]

    assert "participation.registration.created.v1" in topics
    assert "participation.registration.confirmed.v1" in topics
    assert "participation.registration.waitlisted.v1" in topics
    assert "participation.registration.cancelled.v1" in topics
    assert "participation.ticket.revoked.v1" in topics
    assert "participation.registration.promoted.v1" in topics
    assert "participation.availability.changed.v1" in topics

    # Invariant: Zero PII in outbox domain events
    outbox_json = json.dumps(items)
    assert secret_email not in outbox_json
    assert "Valentina" not in outbox_json
    assert "Paredes" not in outbox_json

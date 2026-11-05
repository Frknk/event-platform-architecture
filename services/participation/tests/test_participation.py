"""
Automated Acceptance & Concurrency Test Suite for Team 3 — Participation.
Validates user stories PA-001 through PA-041 and non-negotiable architecture invariants (AD-3, AD-7, AD-8, AD-11).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
from fastapi.testclient import TestClient

from app.core.security import generate_uuidv7, security_manager
from app.db.database import (
    DEFAULT_CONSENT_VERSION,
    DEFAULT_EVENT_ID,
    DEFAULT_EVENT_SLUG,
    DEFAULT_FORM_VERSION_ID,
    DEFAULT_SITE_SLUG,
)
from app.main import create_app


def _make_client() -> TestClient:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app = create_app(db_path=tmp.name)
    return TestClient(app)


def _sample_registration_payload(email: str = "ana.torres@unas.edu.pe", name: str = "Ana Torres") -> dict:
    return {
        "form_version_id": DEFAULT_FORM_VERSION_ID,
        "consent_version": DEFAULT_CONSENT_VERSION,
        "consent_accepted": True,
        "attendee": {
            "full_name": name,
            "email": email,
            "document_number": "72345678",
            "phone": "+51987654321",
            "institution": "UNAS",
        },
        "answers": {
            "track": "IA Aplicada & Transformación Digital",
            "role_profile": "Estudiante UNAS",
            "modality": "Presencial — Auditorio Central UNAS",
        },
    }


def test_pa001_load_active_public_form():
    client = _make_client()
    res = client.get(f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/form")
    assert res.status_code == 200
    assert "ETag" in res.headers
    data = res.json()
    assert data["event_id"] == DEFAULT_EVENT_ID
    assert data["binding_state"] == "open"
    assert data["capacity_summary"]["capacity"] == 5
    assert data["capacity_summary"]["confirmed_count"] == 0


def test_pa002_idempotency_and_problem_details():
    client = _make_client()
    idem_key = f"idem-key-{generate_uuidv7()}"
    payload = _sample_registration_payload()

    # First call creates registration
    r1 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload,
        headers={"Idempotency-Key": idem_key},
    )
    assert r1.status_code == 201
    body1 = r1.json()
    assert body1["state"] == "confirmed"

    # Second call with SAME key and SAME payload returns exact same registration without consuming another spot
    r2 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload,
        headers={"Idempotency-Key": idem_key},
    )
    assert r2.status_code == 201
    assert r2.json()["id"] == body1["id"]

    # Third call with SAME key and DIFFERENT payload returns 409 idempotency_key_reused (application/problem+json)
    different_payload = _sample_registration_payload(email="otro@unas.edu.pe")
    r3 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=different_payload,
        headers={"Idempotency-Key": idem_key},
    )
    assert r3.status_code == 409
    assert r3.headers["content-type"].startswith("application/problem+json")
    assert r3.json()["code"] == "idempotency_key_reused"


def test_pa004_duplicate_detection_via_normalized_email_hmac():
    client = _make_client()
    r1 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_registration_payload(email="Carlos.Rojas@UNAS.edu.pe"),
        headers={"Idempotency-Key": f"key-1-{generate_uuidv7()}"},
    )
    assert r1.status_code == 201

    # Same email with whitespace and lowercase using a DIFFERENT Idempotency-Key -> 409 already_registered
    r2 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_registration_payload(email="  carlos.rojas@unas.edu.pe  "),
        headers={"Idempotency-Key": f"key-2-{generate_uuidv7()}"},
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "already_registered"


def test_pa008_and_pa015_asymmetric_jws_qr_contains_zero_pii():
    client = _make_client()
    r1 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_registration_payload(email="lucia.mendoza@unas.edu.pe", name="Lucía Mendoza"),
        headers={"Idempotency-Key": f"key-qr-{generate_uuidv7()}"},
    )
    assert r1.status_code == 201
    reg_id = r1.json()["id"]

    tkt_res = client.get(f"/participation/self/v1/registrations/{reg_id}/ticket")
    assert tkt_res.status_code == 200
    assert tkt_res.headers.get("Cache-Control") == "no-store"
    tkt = tkt_res.json()
    assert tkt["state"] == "active"
    assert tkt["audience"] == "onsite"

    # Cryptographically verify JWS signature and ensure zero PII in token
    jws_str = tkt["ticket_jws"]
    assert "lucia" not in jws_str.lower()
    assert "mendoza" not in jws_str.lower()
    assert "72345678" not in jws_str

    header, claims = security_manager.verify_jws(jws_str, expected_aud="onsite")
    assert header["alg"] == "ES256"
    assert claims["reg_id"] == reg_id
    assert "email" not in claims
    assert "full_name" not in claims


def test_pa011_pa012_pa041_concurrent_3x_load_zero_overbooking():
    """
    PA-011 & PA-041: 30 concurrent registrations against capacity=10.
    Must result in exactly 10 confirmed, 20 waitlisted (positions 1..20), and 0 overbooking.
    """
    client = _make_client()
    client.post("/participation/v1/demo/reset", json={"capacity": 10, "waitlist_enabled": True})

    def register_one(idx: int):
        res = client.post(
            f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
            json=_sample_registration_payload(
                email=f"participante.{idx:03d}@unas.edu.pe",
                name=f"Participante {idx:03d}",
            ),
            headers={"Idempotency-Key": f"conc-key-{idx:03d}-{generate_uuidv7()}"},
        )
        assert res.status_code == 201
        return res.json()

    with ThreadPoolExecutor(max_workers=15) as executor:
        receipts = list(executor.map(register_one, range(1, 31)))

    confirmed = [r for r in receipts if r["state"] == "confirmed"]
    waitlisted = [r for r in receipts if r["state"] == "waitlisted"]

    assert len(confirmed) == 10
    assert len(waitlisted) == 20

    positions = sorted(r["waitlist_position"] for r in waitlisted)
    assert positions == list(range(1, 21))

    form_after = client.get(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/form"
    ).json()
    assert form_after["capacity_summary"]["confirmed_count"] == 10
    assert form_after["capacity_summary"]["waitlist_count"] == 20
    assert form_after["capacity_summary"]["public_state"] == "waitlist_only"


def test_pa007_and_pa013_cancel_revokes_ticket_and_autopropagates_waitlist():
    """
    PA-007 & PA-013: When a confirmed registration is cancelled, its ticket is revoked
    and the #1 waitlisted registration is atomically promoted to confirmed with a new QR ticket.
    """
    client = _make_client()
    client.post("/participation/v1/demo/reset", json={"capacity": 2, "waitlist_enabled": True})

    # Register 2 confirmed + 2 waitlisted
    regs = []
    for i in range(1, 5):
        res = client.post(
            f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
            json=_sample_registration_payload(
                email=f"user{i}@unas.edu.pe",
                name=f"Usuario {i}",
            ),
            headers={"Idempotency-Key": f"waitlist-test-{i}-{generate_uuidv7()}"},
        )
        regs.append(res.json())

    assert regs[0]["state"] == "confirmed"
    assert regs[1]["state"] == "confirmed"
    assert regs[2]["state"] == "waitlisted" and regs[2]["waitlist_position"] == 1
    assert regs[3]["state"] == "waitlisted" and regs[3]["waitlist_position"] == 2

    # Cancel registration #1
    cancel_res = client.post(
        f"/participation/self/v1/registrations/{regs[0]['id']}/cancellations",
        json={"reason": "No podré asistir al congreso"},
        headers={"Idempotency-Key": f"cancel-1-{generate_uuidv7()}"},
    )
    assert cancel_res.status_code == 200
    cancel_body = cancel_res.json()
    assert cancel_body["state"] == "cancelled"
    assert cancel_body["ticket_revoked"] is True
    assert cancel_body["promoted_registration_id"] == regs[2]["id"]

    # Verify registration #1 ticket is now revoked
    tkt1 = client.get(f"/participation/self/v1/registrations/{regs[0]['id']}/ticket").json()
    assert tkt1["state"] == "revoked"

    # Verify registration #3 (formerly waitlist #1) is now confirmed and has an active QR ticket
    reg3_now = client.get(f"/participation/self/v1/registrations/{regs[2]['id']}").json()
    assert reg3_now["state"] == "confirmed"

    tkt3 = client.get(f"/participation/self/v1/registrations/{regs[2]['id']}/ticket").json()
    assert tkt3["state"] == "active"


def test_pa030_and_ad8_privacy_masking_and_zero_pii_outbox():
    client = _make_client()
    secret_email = "valeria.quispe@unas.edu.pe"
    secret_name = "Valeria Quispe"
    client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_registration_payload(email=secret_email, name=secret_name),
        headers={"Idempotency-Key": f"priv-{generate_uuidv7()}"},
    )

    # Backoffice listing with reveal_pii=false masks PII
    masked_list = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations?reveal_pii=false"
    ).json()["items"]
    assert len(masked_list) == 1
    assert masked_list[0]["email"] != secret_email
    assert "***" in masked_list[0]["email"]
    assert "***" in masked_list[0]["full_name"]

    # Search by exact email uses HMAC blind index
    search_list = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": "VALERIA.QUISPE@unas.edu.pe", "reveal_pii": True},
    ).json()["items"]
    assert len(search_list) == 1
    assert search_list[0]["email"] == secret_email

    # Outbox events must contain ZERO plaintext PII (AD-8)
    outbox_dump = json.dumps(client.get("/participation/v1/outbox").json())
    assert secret_email not in outbox_dump
    assert "Valeria" not in outbox_dump
    assert "72345678" not in outbox_dump

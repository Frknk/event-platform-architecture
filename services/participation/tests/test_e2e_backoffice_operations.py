"""
End-to-End Test Suite for Participation Backoffice, Admissibility, Security, and Demo Operations.
Team 3 — Participation (Event Platform Architecture).

Covers all 15 Backoffice, Admissibility, Admin, Security, and Demo endpoints:
1.  GET  /participation/v1/events/{event_id}/registrations (cursor pagination, filters, HMAC blind index search, PII masking)
2.  PATCH /participation/v1/registrations/{registration_id} (backoffice correction/updates with audit trail and outbox event)
3.  GET  /participation/v1/events/{event_id}/applications (list applications with status filtering, venture info, hiding ciphertext)
4.  POST /participation/v1/applications/{application_id}/admissions (admit application with revision check and checklist)
5.  POST /participation/v1/applications/{application_id}/rejections (reject application with reason and revision check)
6.  POST /participation/v1/events/{event_id}/registration-imports (bulk import with legal_basis, dry_run, capacity check)
7.  POST /participation/v1/privacy-requests (ARCO erasure, access, rectification, legal hold protection, zero-PII receipt)
8.  POST /participation/v1/events/{event_id}/operations-projections (AES-256-GCM envelope, purpose-bound projections)
9.  GET  /.well-known/jwks.json (JWKS public keys for onsite validation, zero private key leakage)
10. GET  /participation/v1/outbox (transactional outbox events for downstream services with limit)
11. GET  /participation/v1/contracts/openapi.yaml (OpenAPI contract schema YAML)
12. POST /participation/v1/demo/reset (reset demo state with capacity and waitlist configs)
13. POST /participation/v1/demo/concurrency-burst (simulate concurrent burst load, atomic capacity locking)
14. POST /participation/v1/demo/ingest-release (ingest release configuration event, idempotency check)
15. GET  /healthz (health check endpoint)
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional
import pytest
from fastapi.testclient import TestClient

from app.core.security import (
    generate_uuidv7,
    mask_email,
    mask_name,
    security_manager,
)
from app.db.database import (
    DEFAULT_CONSENT_VERSION,
    DEFAULT_EVENT_ID,
    DEFAULT_EVENT_SLUG,
    DEFAULT_FORM_VERSION_ID,
    DEFAULT_ORG_ID,
    DEFAULT_SITE_SLUG,
)
from app.main import create_app


# =============================================================================
# Test Fixtures & Helpers
# =============================================================================

def _make_client(*, raise_server_exceptions: bool = True) -> TestClient:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app = create_app(db_path=tmp.name)
    client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
    client._tmp_db_path = tmp.name
    return client


@pytest.fixture
def client() -> TestClient:
    c = _make_client()
    yield c
    c.app.state.db.close()
    if hasattr(c, "_tmp_db_path") and os.path.exists(c._tmp_db_path):
        try:
            os.unlink(c._tmp_db_path)
        except OSError:
            pass


def _audit_rows(client: TestClient, *, request_id: Optional[str] = None) -> List[Dict[str, Any]]:
    with client.app.state.db.read_cursor() as cur:
        if request_id:
            cur.execute(
                "SELECT * FROM audit_logs WHERE request_id = ? ORDER BY created_at, id",
                (request_id,),
            )
        else:
            cur.execute("SELECT * FROM audit_logs ORDER BY created_at, id")
        return [dict(row) for row in cur.fetchall()]


def _seed_registration(
    client: TestClient,
    *,
    email: str = "estudiante.test@unas.edu.pe",
    name: str = "Estudiante Test",
    phone: str = "+51987654321",
    document_number: str = "72345678",
    institution: str = "UNAS",
    track: str = "IA Aplicada & Transformación Digital",
    idem_key: Optional[str] = None,
) -> Dict[str, Any]:
    idem = idem_key or f"reg-seed-{generate_uuidv7()}"
    payload = {
        "form_version_id": DEFAULT_FORM_VERSION_ID,
        "consent_version": DEFAULT_CONSENT_VERSION,
        "consent_accepted": True,
        "attendee": {
            "full_name": name,
            "email": email,
            "document_number": document_number,
            "phone": phone,
            "institution": institution,
        },
        "answers": {
            "track": track,
            "role_profile": "Estudiante UNAS",
            "modality": "Presencial — Auditorio Central UNAS",
        },
    }
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=payload,
        headers={"Idempotency-Key": idem},
    )
    assert res.status_code in (201, 202)
    return res.json()


def _seed_application(
    client: TestClient,
    *,
    leader_email: str = "lider@unas.edu.pe",
    leader_name: str = "Líder Fundador",
    venture_name: str = "BioTech Amazónica",
    track: str = "Agrotech & Bioeconomía Amazónica",
    submit: bool = False,
) -> Dict[str, Any]:
    idem = f"app-seed-{generate_uuidv7()}"
    payload = {
        "form_version_id": DEFAULT_FORM_VERSION_ID,
        "consent_version": DEFAULT_CONSENT_VERSION,
        "venture_name": venture_name,
        "track": track,
        "leader": {
            "full_name": leader_name,
            "email": leader_email,
            "document_number": "71122334",
            "phone": "+51988776655",
            "institution": "UNAS",
        },
        "members": [
            {"email": f"socio-{generate_uuidv7()[:6]}@unas.edu.pe", "role": "co-founder"},
        ],
        "answers": {
            "pitch_summary": "Solución bio-sostenible para cultivo de cacao.",
            "stage": "Prototipo validado",
        },
    }
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": idem},
    )
    assert res.status_code == 201
    created_data = res.json()
    app_id = created_data["application_id"]

    if submit:
        sub_res = client.post(
            f"/participation/self/v1/applications/{app_id}/submissions",
            json={"expected_revision": 1},
            headers={"Idempotency-Key": f"sub-seed-{generate_uuidv7()}"},
        )
        assert sub_res.status_code == 200
        sub_data = sub_res.json()
        sub_data["application_id"] = app_id
        return sub_data

    return created_data


def _sample_release_envelope(
    version: int = 1,
    *,
    aggregate_id: Optional[str] = None,
    domain_event_id: Optional[str] = None,
    capacity: int = 10,
    waitlist_enabled: bool = True,
    org_id: str = DEFAULT_ORG_ID,
    topic: str = "configuration.event.release_published.v1",
) -> Dict[str, Any]:
    return {
        "message_id": generate_uuidv7(),
        "domain_event_id": domain_event_id or generate_uuidv7(),
        "topic": topic,
        "schema_version": 1,
        "producer": "event-configuration",
        "organization_id": org_id,
        "business_event_id": DEFAULT_EVENT_ID,
        "occurred_at": "2026-11-12T00:00:00Z",
        "aggregate": {
            "type": "event",
            "id": aggregate_id or generate_uuidv7(),
            "version": version,
        },
        "data": {
            "capacity": capacity,
            "waitlist_enabled": waitlist_enabled,
        },
    }


# =============================================================================
# 1. GET /participation/v1/events/{event_id}/registrations
# =============================================================================

def test_endpoint1_list_registrations_empty_and_headers(client: TestClient):
    """GET registrations on fresh DB returns 200, empty list, and Cache-Control: no-store."""
    res = client.get(f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations")
    assert res.status_code == 200
    assert res.headers.get("Cache-Control") == "no-store"
    data = res.json()
    assert data["items"] == []
    assert data["next_cursor"] is None

    # Querying a non-existent event returns empty list as well
    res_other = client.get(f"/participation/v1/events/{generate_uuidv7()}/registrations")
    assert res_other.status_code == 200
    assert res_other.json()["items"] == []


def test_endpoint1_list_registrations_cursor_pagination(client: TestClient):
    """Test cursor-based pagination (limit, next_cursor format, and non-overlapping pages)."""
    # Create 5 registrations
    created_ids = []
    for i in range(5):
        reg = _seed_registration(
            client,
            email=f"attendee.page.{i}@unas.edu.pe",
            name=f"Attendee Page {i}",
        )
        created_ids.append(reg["id"])

    # Page 1: limit=2
    res_p1 = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"limit": 2},
    )
    assert res_p1.status_code == 200
    p1 = res_p1.json()
    assert len(p1["items"]) == 2
    assert p1["next_cursor"] is not None
    assert "|" in p1["next_cursor"]

    # Page 2: limit=2 using next_cursor
    res_p2 = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"limit": 2, "cursor": p1["next_cursor"]},
    )
    assert res_p2.status_code == 200
    p2 = res_p2.json()
    assert len(p2["items"]) == 2
    assert p2["next_cursor"] is not None

    # Page 3: limit=2 using second next_cursor (should return last 1 item)
    res_p3 = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"limit": 2, "cursor": p2["next_cursor"]},
    )
    assert res_p3.status_code == 200
    p3 = res_p3.json()
    assert len(p3["items"]) == 1
    assert p3["next_cursor"] is None

    # Ensure all 5 unique IDs returned across pages without duplicates
    all_returned_ids = (
        [item["id"] for item in p1["items"]]
        + [item["id"] for item in p2["items"]]
        + [item["id"] for item in p3["items"]]
    )
    assert len(set(all_returned_ids)) == 5
    assert set(all_returned_ids) == set(created_ids)


def test_endpoint1_list_registrations_state_filtering(client: TestClient):
    """Test filtering by state: confirmed, waitlisted, and cancelled."""
    # Reset capacity to 2 to force waitlist entries
    client.post("/participation/v1/demo/reset", json={"capacity": 2, "waitlist_enabled": True})

    reg1 = _seed_registration(client, email="conf1@unas.edu.pe", name="Conf 1")
    reg2 = _seed_registration(client, email="conf2@unas.edu.pe", name="Conf 2")
    reg3 = _seed_registration(client, email="wait1@unas.edu.pe", name="Wait 1")
    assert reg1["state"] == "confirmed"
    assert reg2["state"] == "confirmed"
    assert reg3["state"] == "waitlisted"

    # Filter confirmed
    res_conf = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"state": "confirmed"},
    )
    assert res_conf.status_code == 200
    conf_items = res_conf.json()["items"]
    assert len(conf_items) == 2
    assert all(i["state"] == "confirmed" for i in conf_items)

    # Filter waitlisted
    res_wait = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"state": "waitlisted"},
    )
    assert res_wait.status_code == 200
    wait_items = res_wait.json()["items"]
    assert len(wait_items) == 1
    assert wait_items[0]["state"] == "waitlisted"
    assert wait_items[0]["id"] == reg3["id"]


def test_endpoint1_list_registrations_hmac_blind_index_search(client: TestClient):
    """Test searching by email using blind index HMAC filter (PA-004, PA-030)."""
    target_email = "target.search@unas.edu.pe"
    _seed_registration(client, email=target_email, name="Target Participant")
    _seed_registration(client, email="other.user@unas.edu.pe", name="Other Participant")

    # Match exact email
    res = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": target_email},
    )
    assert res.status_code == 200
    items = res.json()["items"]
    assert len(items) == 1
    expected_hmac = security_manager.compute_email_hmac(DEFAULT_ORG_ID, target_email)
    assert items[0]["email_hmac"] == expected_hmac

    # Non-existent email search returns empty
    res_none = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": "nobody@unas.edu.pe"},
    )
    assert res_none.status_code == 200
    assert res_none.json()["items"] == []


def test_endpoint1_list_registrations_pii_masking_vs_revealed(client: TestClient):
    """Verify that reveal_pii=False hides PII, while reveal_pii=True returns decrypted values."""
    email = "carlos.mendoza@unas.edu.pe"
    name = "Carlos Mendoza"
    phone = "+51987112233"
    doc = "76543210"
    inst = "Facultad de Agronomía UNAS"

    _seed_registration(
        client,
        email=email,
        name=name,
        phone=phone,
        document_number=doc,
        institution=inst,
    )

    # 1. Default reveal_pii=False: PII is masked
    res_masked = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": email, "reveal_pii": False},
    )
    assert res_masked.status_code == 200
    masked = res_masked.json()["items"][0]
    assert masked["full_name"] == "C*** M***"
    assert masked["email"] == "c***a@unas.edu.pe"
    assert masked["phone"] == "***"
    assert masked["document_number"] == "***"
    assert masked["institution"] == "***"
    assert masked["self_access_token"] is not None

    # 2. reveal_pii=True: Decrypted PII is returned
    res_revealed = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": email, "reveal_pii": True},
    )
    assert res_revealed.status_code == 200
    revealed = res_revealed.json()["items"][0]
    assert revealed["full_name"] == name
    assert revealed["email"] == email
    assert revealed["phone"] == phone
    assert revealed["document_number"] == doc
    assert revealed["institution"] == inst


# =============================================================================
# 2. PATCH /participation/v1/registrations/{registration_id}
# =============================================================================

def test_endpoint2_correct_registration_success_and_audit_trail(client: TestClient):
    """PA-031: Backoffice manager updates allowlisted fields with audit log and outbox event."""
    reg = _seed_registration(
        client,
        email="elena.solis@unas.edu.pe",
        name="Elena Solis",
        phone="+51999111222",
        institution="UNAS",
    )
    reg_id = reg["id"]

    updates = {
        "full_name": "Elena Solís Ramos",
        "phone": "+51999888444",
        "institution": "UNAS — Ingeniería Ambiental",
        "document_number": "70998877",
    }
    reason = "Corrección de tildes y actualización de teléfono institucional"

    res = client.patch(
        f"/participation/v1/registrations/{reg_id}",
        json={"reason": reason, "updates": updates},
        headers={"X-Actor-Id": "manager-usr-001"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["id"] == reg_id
    assert data["status"] == "corrected"
    assert data["reason"] == reason
    assert data["full_name_masked"] == "E*** S*** R***"
    assert sorted(data["updated_fields"]) == sorted(updates.keys())

    # Audit log validation
    request_id = res.headers["X-Request-Id"]
    audits = _audit_rows(client, request_id=request_id)
    assert len(audits) == 1
    audit = audits[0]
    assert audit["action"] == "correct_registration"
    assert audit["target_type"] == "registration"
    assert audit["target_id"] == reg_id
    assert audit["result"] == "success"
    assert audit["reason"] == reason
    assert json.loads(audit["payload_json"]) == {"updated_fields": sorted(updates.keys())}
    # Invariant: No plaintext PII stored in audit log
    assert "Elena Solís Ramos" not in json.dumps(audit)
    assert "+51999888444" not in json.dumps(audit)

    # Outbox event validation
    outbox_res = client.get("/participation/v1/outbox")
    events = outbox_res.json()["items"]
    corr_event = next(
        (e for e in events if e["topic"] == "participation.registration.corrected.v1" and e["aggregate_id"] == reg_id),
        None,
    )
    assert corr_event is not None
    assert corr_event["envelope"]["data"]["registration_id"] == reg_id
    assert corr_event["envelope"]["data"]["reason"] == reason

    # Verify subsequent GET with reveal_pii=True returns the newly updated fields
    revealed = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": "elena.solis@unas.edu.pe", "reveal_pii": True},
    ).json()["items"][0]
    assert revealed["full_name"] == "Elena Solís Ramos"
    assert revealed["phone"] == "+51999888444"
    assert revealed["institution"] == "UNAS — Ingeniería Ambiental"
    assert revealed["document_number"] == "70998877"


def test_endpoint2_correct_registration_rejects_non_allowlisted_fields(client: TestClient):
    """PA-031: Attempts to modify non-allowlisted fields (answers, email) fail with 400."""
    reg = _seed_registration(client, email="immutable@unas.edu.pe", name="Immutable Test")
    reg_id = reg["id"]

    # Reject 'answers' modification
    res_answers = client.patch(
        f"/participation/v1/registrations/{reg_id}",
        json={"reason": "Intento de cambiar track", "updates": {"answers": {"track": "Hacked"}}},
    )
    assert res_answers.status_code == 400
    assert res_answers.json()["code"] == "field_not_correctable"

    # Reject 'email' modification (email is immutable, handled via privacy rectification/erasure)
    res_email = client.patch(
        f"/participation/v1/registrations/{reg_id}",
        json={"reason": "Cambio de correo", "updates": {"email": "new.email@unas.edu.pe"}},
    )
    assert res_email.status_code == 400
    assert res_email.json()["code"] == "field_not_correctable"

    # Verify audit recorded the rejection
    audit_rej = _audit_rows(client, request_id=res_answers.headers["X-Request-Id"])
    assert len(audit_rej) == 1
    assert audit_rej[0]["result"] == "rejected"
    assert json.loads(audit_rej[0]["payload_json"]) == {"problem_code": "field_not_correctable"}


def test_endpoint2_correct_registration_validations_and_404(client: TestClient):
    """Test missing reason validation (400/422) and non-existent registration (404)."""
    reg = _seed_registration(client, email="val.test@unas.edu.pe", name="Val Test")
    reg_id = reg["id"]

    # Missing reason
    res_no_reason = client.patch(
        f"/participation/v1/registrations/{reg_id}",
        json={"reason": "", "updates": {"full_name": "Nombre Actualizado"}},
    )
    assert res_no_reason.status_code in (400, 422)

    # Short reason (< 3 chars)
    res_short = client.patch(
        f"/participation/v1/registrations/{reg_id}",
        json={"reason": "ok", "updates": {"full_name": "Nombre Actualizado"}},
    )
    assert res_short.status_code in (400, 422)

    # 404 on unknown ID
    unknown_id = generate_uuidv7()
    res_404 = client.patch(
        f"/participation/v1/registrations/{unknown_id}",
        json={"reason": "Corrección de ID inexistente", "updates": {"full_name": "Test"}},
    )
    assert res_404.status_code == 404
    assert res_404.json()["code"] == "registration_not_found"


# =============================================================================
# 3. GET /participation/v1/events/{event_id}/applications
# =============================================================================

def test_endpoint3_list_applications_structure_and_masking(client: TestClient):
    """PA-032: List applications with venture info, active member count, and zero ciphertext."""
    app_draft = _seed_application(
        client,
        leader_email="lider.app1@unas.edu.pe",
        venture_name="BioEmpaque Verde",
        submit=False,
    )
    app_sub = _seed_application(
        client,
        leader_email="lider.app2@unas.edu.pe",
        venture_name="AgroDron Amazonía",
        submit=True,
    )

    res = client.get(f"/participation/v1/events/{DEFAULT_EVENT_ID}/applications")
    assert res.status_code == 200
    data = res.json()
    items = data["items"]
    assert len(items) >= 2

    # Verify structure and fields
    for a in items:
        assert "id" in a
        assert "venture_name" in a
        assert "venture_track" in a
        assert "state" in a
        assert "current_revision_number" in a
        assert "files_count" in a
        assert "active_members_count" in a
        # Security invariant: Raw draft answers ciphertext must NOT be leaked in the backoffice listing
        assert "draft_answers_ciphertext" not in a

    # Verify states match
    app1_entry = next((i for i in items if i["id"] == app_draft["application_id"]), None)
    assert app1_entry is not None
    assert app1_entry["state"] == "draft"
    assert app1_entry["venture_name"] == "BioEmpaque Verde"

    app2_entry = next((i for i in items if i["id"] == app_sub["application_id"]), None)
    assert app2_entry is not None
    assert app2_entry["state"] == "submitted"
    assert app2_entry["venture_name"] == "AgroDron Amazonía"


def test_endpoint3_list_applications_state_filtering(client: TestClient):
    """Test filtering applications by state query param."""
    _seed_application(client, leader_email="draft.only@unas.edu.pe", submit=False)
    sub = _seed_application(client, leader_email="sub.only@unas.edu.pe", submit=True)

    # Filter state=submitted
    res_sub = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/applications",
        params={"state": "submitted"},
    )
    assert res_sub.status_code == 200
    items_sub = res_sub.json()["items"]
    assert len(items_sub) >= 1
    assert all(a["state"] == "submitted" for a in items_sub)
    assert any(a["id"] == sub["application_id"] for a in items_sub)

    # Filter state=draft
    res_draft = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/applications",
        params={"state": "draft"},
    )
    assert res_draft.status_code == 200
    items_draft = res_draft.json()["items"]
    assert all(a["state"] == "draft" for a in items_draft)


# =============================================================================
# 4. POST /participation/v1/applications/{application_id}/admissions
# =============================================================================

def test_endpoint4_admit_application_success(client: TestClient):
    """PA-032: Reviewer admits application with checklist, updating revision and emitting outbox."""
    app_data = _seed_application(client, leader_email="admit.success@unas.edu.pe", submit=True)
    app_id = app_data["application_id"]

    checklist = {
        "bases_cumplidas": True,
        "equipo_completo": True,
        "documentacion_adjunta": True,
    }
    reason = "Cumple con todos los requisitos de la convocatoria Demo Day 2026."

    res = client.post(
        f"/participation/v1/applications/{app_id}/admissions",
        json={
            "expected_revision": 2,
            "reason": reason,
            "checklist": checklist,
        },
        headers={"X-Actor-Id": "evaluator-01"},
    )
    assert res.status_code == 200
    result = res.json()
    assert result["application_id"] == app_id
    assert result["state"] == "admitted"
    assert result["revision"] == 3
    assert result["reason"] == reason
    assert result["checklist"] == checklist

    # Verify audit trail
    audits = _audit_rows(client, request_id=res.headers["X-Request-Id"])
    assert len(audits) == 1
    assert audits[0]["action"] == "admit_application"
    assert audits[0]["target_id"] == app_id
    assert audits[0]["result"] == "success"
    assert audits[0]["reason"] == reason
    assert json.loads(audits[0]["payload_json"]) == {"checklist": checklist}

    # Verify outbox event
    outbox = client.get("/participation/v1/outbox").json()["items"]
    admit_event = next(
        (e for e in outbox if e["topic"] == "participation.application.admitted.v1" and e["aggregate_id"] == app_id),
        None,
    )
    assert admit_event is not None
    assert admit_event["aggregate_version"] == 3
    assert admit_event["envelope"]["data"]["state"] == "admitted"


def test_endpoint4_admit_application_stale_revision_optimistic_lock(client: TestClient):
    """PA-032: Concurrency control — stale expected_revision raises 412."""
    app_data = _seed_application(client, leader_email="stale.admit@unas.edu.pe", submit=True)
    app_id = app_data["application_id"]

    # Current revision is 2, pass expected_revision=99
    res = client.post(
        f"/participation/v1/applications/{app_id}/admissions",
        json={"expected_revision": 99, "reason": "Evaluación con revisión stale"},
    )
    assert res.status_code == 412
    assert res.json()["code"] == "stale_revision"

    # Audit recorded as rejected
    audits = _audit_rows(client, request_id=res.headers["X-Request-Id"])
    assert len(audits) == 1
    assert audits[0]["result"] == "rejected"
    assert json.loads(audits[0]["payload_json"]) == {"problem_code": "stale_revision"}


def test_endpoint4_admit_application_invalid_state_and_404(client: TestClient):
    """Only 'submitted' or 'resubmitted' applications can be admitted (draft raises 409)."""
    # 1. Draft application
    draft_app = _seed_application(client, leader_email="draft.admit@unas.edu.pe", submit=False)
    res_draft = client.post(
        f"/participation/v1/applications/{draft_app['application_id']}/admissions",
        json={"expected_revision": 1, "reason": "No se puede admitir borrador"},
    )
    assert res_draft.status_code == 409
    assert res_draft.json()["code"] == "invalid_state_transition"

    # 2. Unknown application ID -> 404
    res_404 = client.post(
        f"/participation/v1/applications/{generate_uuidv7()}/admissions",
        json={"expected_revision": 1, "reason": "ID no existe"},
    )
    assert res_404.status_code == 404
    assert res_404.json()["code"] == "application_not_found"


# =============================================================================
# 5. POST /participation/v1/applications/{application_id}/rejections
# =============================================================================

def test_endpoint5_reject_application_success(client: TestClient):
    """PA-032: Reviewer rejects application with reason, updating revision and emitting outbox."""
    app_data = _seed_application(client, leader_email="reject.success@unas.edu.pe", submit=True)
    app_id = app_data["application_id"]

    reason = "La propuesta no se alinea a los ejes temáticos de bioeconomía amazónica."
    checklist = {"bases_cumplidas": False, "equipo_completo": True}

    res = client.post(
        f"/participation/v1/applications/{app_id}/rejections",
        json={
            "expected_revision": 2,
            "reason": reason,
            "checklist": checklist,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["application_id"] == app_id
    assert data["state"] == "rejected"
    assert data["revision"] == 3
    assert data["reason"] == reason

    # Verify audit trail
    audits = _audit_rows(client, request_id=res.headers["X-Request-Id"])
    assert len(audits) == 1
    assert audits[0]["action"] == "reject_application"
    assert audits[0]["result"] == "success"
    assert audits[0]["reason"] == reason

    # Verify outbox event
    outbox = client.get("/participation/v1/outbox").json()["items"]
    rej_event = next(
        (e for e in outbox if e["topic"] == "participation.application.rejected.v1" and e["aggregate_id"] == app_id),
        None,
    )
    assert rej_event is not None
    assert rej_event["envelope"]["data"]["state"] == "rejected"


def test_endpoint5_reject_application_stale_revision_and_invalid_state(client: TestClient):
    """Rejection with stale revision raises 412, and rejecting draft raises 409."""
    app_data = _seed_application(client, leader_email="reject.err@unas.edu.pe", submit=True)
    app_id = app_data["application_id"]

    # Stale revision
    res_stale = client.post(
        f"/participation/v1/applications/{app_id}/rejections",
        json={"expected_revision": 55, "reason": "Revisión desactualizada"},
    )
    assert res_stale.status_code == 412
    assert res_stale.json()["code"] == "stale_revision"

    # Draft application rejection raises 409
    draft_app = _seed_application(client, leader_email="draft.rej@unas.edu.pe", submit=False)
    res_draft = client.post(
        f"/participation/v1/applications/{draft_app['application_id']}/rejections",
        json={"expected_revision": 1, "reason": "No se puede rechazar borrador"},
    )
    assert res_draft.status_code == 409
    assert res_draft.json()["code"] == "invalid_state_transition"

    # Non-existent ID -> 404
    res_404 = client.post(
        f"/participation/v1/applications/{generate_uuidv7()}/rejections",
        json={"expected_revision": 1, "reason": "No existe"},
    )
    assert res_404.status_code == 404


# =============================================================================
# 6. POST /participation/v1/events/{event_id}/registration-imports
# =============================================================================

def test_endpoint6_import_registrations_dry_run(client: TestClient):
    """PA-033: Dry run validates rows without writing to DB or audit logs."""
    import_rows = [
        {
            "full_name": "Estudiante Válido 1",
            "email": "valido1@unas.edu.pe",
            "phone": "+51999111333",
            "document_number": "70112233",
            "institution": "UNAS",
        },
        {
            "full_name": "Invalido Correo",
            "email": "correo-sin-arroba",
            "document_number": "70999999",
        },
        {
            "full_name": "",
            "email": "sin.nombre@unas.edu.pe",
        },
        {
            "full_name": "Duplicado Mismo Lote",
            "email": "valido1@unas.edu.pe",
        },
    ]

    res = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registration-imports",
        json={
            "rows": import_rows,
            "legal_basis": "Consentimiento firmado según Ley 29733",
            "dry_run": True,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["dry_run"] is True
    assert data["total_rows"] == 4
    assert data["valid_count"] == 1
    assert data["created_count"] == 0
    assert data["imported_count"] == 0
    assert data["error_count"] == 3
    assert len(data["errors"]) == 3

    # Invariant: No registrations created and no audit log recorded in dry run
    search = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": "valido1@unas.edu.pe"},
    ).json()["items"]
    assert len(search) == 0
    assert _audit_rows(client, request_id=res.headers["X-Request-Id"]) == []


def test_endpoint6_import_registrations_real_success_and_duplicates(client: TestClient):
    """PA-033: Real import creates encrypted registrations, consents, tickets, and outbox event."""
    legal_basis = "Convenio interinstitucional UNAS - MINEDU 2026"
    import_rows = [
        {
            "full_name": "Importado Uno",
            "email": "import.uno@unas.edu.pe",
            "phone": "+51999000111",
            "document_number": "71000001",
            "institution": "UNAS Agronomía",
        },
        {
            "full_name": "Importado Dos",
            "email": "import.dos@unas.edu.pe",
            "phone": "+51999000222",
            "document_number": "71000002",
            "institution": "UNAS Forestales",
        },
    ]

    res = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registration-imports",
        json={
            "rows": import_rows,
            "legal_basis": legal_basis,
            "dry_run": False,
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["dry_run"] is False
    assert data["total_rows"] == 2
    assert data["created_count"] == 2
    assert data["imported_count"] == 2
    assert data["error_count"] == 0

    # Verify audit log
    audits = _audit_rows(client, request_id=res.headers["X-Request-Id"])
    assert len(audits) == 1
    assert audits[0]["action"] == "import_registrations"
    assert audits[0]["result"] == "success"
    assert audits[0]["reason"] == legal_basis
    assert json.loads(audits[0]["payload_json"]) == {
        "created_count": 2,
        "dry_run": False,
        "error_count": 0,
    }
    assert "import.uno@unas.edu.pe" not in json.dumps(audits)

    # Verify outbox event emitted
    outbox = client.get("/participation/v1/outbox").json()["items"]
    import_evt = next(
        (e for e in outbox if e["topic"] == "participation.registration_import.completed.v1"),
        None,
    )
    assert import_evt is not None
    assert import_evt["envelope"]["data"]["imported_count"] == 2

    # Query back via GET registrations endpoint to verify PII decrypts cleanly
    search = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": "import.uno@unas.edu.pe", "reveal_pii": True},
    ).json()["items"]
    assert len(search) == 1
    assert search[0]["full_name"] == "Importado Uno"
    assert search[0]["ticket_id"] is not None

    # Re-importing the same email in a subsequent import flags it as duplicate (already_registered)
    res_dup = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registration-imports",
        json={
            "rows": [{"full_name": "Importado Uno Bis", "email": "import.uno@unas.edu.pe"}],
            "legal_basis": legal_basis,
            "dry_run": True,
        },
    )
    assert res_dup.json()["error_count"] == 1
    assert res_dup.json()["errors"][0]["code"] == "already_registered"


def test_endpoint6_import_registrations_validations_and_capacity_guard(client: TestClient):
    """Test missing legal basis (400) and capacity exhaustion guard (409)."""
    # 1. Missing legal basis
    res_no_basis = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registration-imports",
        json={
            "rows": [{"full_name": "Test", "email": "test@unas.edu.pe"}],
            "legal_basis": "  ",
            "dry_run": False,
        },
    )
    assert res_no_basis.status_code in (400, 422)

    # 2. Capacity exceeded guard
    client.post("/participation/v1/demo/reset", json={"capacity": 2})
    rows = [
        {"full_name": f"User {i}", "email": f"cap.user.{i}@unas.edu.pe"}
        for i in range(3)
    ]
    res_cap = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registration-imports",
        json={"rows": rows, "legal_basis": "Consentimiento", "dry_run": False},
    )
    assert res_cap.status_code == 409
    assert res_cap.json()["code"] == "capacity_exceeded"


# =============================================================================
# 7. POST /participation/v1/privacy-requests
# =============================================================================

def test_endpoint7_privacy_request_legal_hold_blocks_erasure(client: TestClient):
    """PA-034: Legal hold blocks erasure with 409 and persists rejection evidence."""
    user_email = "maria.legalhold@unas.edu.pe"
    _seed_registration(client, email=user_email, name="María Legal Hold")

    res = client.post(
        "/participation/v1/privacy-requests",
        json={
            "email": user_email,
            "request_type": "erasure",
            "legal_hold": True,
            "reason": "Auditoría judicial en curso",
        },
    )
    assert res.status_code == 409
    assert res.json()["code"] == "legal_hold_active"

    # Verify rejection evidence survived the transaction rollback
    email_hmac = security_manager.compute_email_hmac(DEFAULT_ORG_ID, user_email)
    with client.app.state.db.read_cursor() as cur:
        cur.execute(
            "SELECT state, legal_hold FROM privacy_requests WHERE email_hmac = ?",
            (email_hmac,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["state"] == "rejected"
    assert int(row["legal_hold"]) == 1

    # Verify audit trail
    audits = _audit_rows(client, request_id=res.headers["X-Request-Id"])
    assert len(audits) == 1
    assert audits[0]["result"] == "rejected"
    assert user_email not in json.dumps(audits)


def test_endpoint7_privacy_request_erasure_success_and_zero_pii(client: TestClient):
    """PA-034: Erasure crypto-shreds PII, deletes answers, revokes ticket, returns zero-PII receipt."""
    user_email = "diego.erasure@unas.edu.pe"
    user_name = "Diego Erasure"
    reg = _seed_registration(client, email=user_email, name=user_name)
    reg_id = reg["id"]

    res = client.post(
        "/participation/v1/privacy-requests",
        json={
            "email": user_email,
            "request_type": "erasure",
            "legal_hold": False,
            "reason": "Derecho al olvido Ley 29733",
        },
    )
    assert res.status_code == 200
    receipt = res.json()
    assert receipt["status"] == "erased"
    assert receipt["request_type"] == "erasure"
    assert "tombstone_hash" in receipt
    assert receipt["anonymized_count"] >= 1

    # Invariant: ZERO plaintext PII in receipt payload
    assert user_email not in json.dumps(receipt)
    assert "Diego" not in json.dumps(receipt)

    # Database verification: registration is cancelled, answers purged, ticket revoked
    self_res = client.get(f"/participation/self/v1/registrations/{reg_id}")
    assert self_res.json()["state"] == "cancelled"

    with client.app.state.db.read_cursor() as cur:
        cur.execute("SELECT count(*) AS cnt FROM registration_answers WHERE registration_id = ?", (reg_id,))
        assert cur.fetchone()["cnt"] == 0
        cur.execute("SELECT state FROM tickets WHERE registration_id = ?", (reg_id,))
        assert cur.fetchone()["state"] == "revoked"

    # Outbox event verification (zero PII)
    outbox = client.get("/participation/v1/outbox").json()["items"]
    priv_evt = next(
        (e for e in outbox if e["topic"] == "participation.privacy_request.executed.v1"),
        None,
    )
    assert priv_evt is not None
    assert user_email not in json.dumps(priv_evt)


def test_endpoint7_privacy_request_access_and_rectification(client: TestClient):
    """Test ARCO access and rectification request types."""
    user_email = "rosa.arco@unas.edu.pe"
    _seed_registration(client, email=user_email, name="Rosa ARCO")

    # 1. Access request
    res_access = client.post(
        "/participation/v1/privacy-requests",
        json={"email": user_email, "request_type": "access"},
    )
    assert res_access.status_code == 200
    acc_data = res_access.json()
    assert acc_data["request_type"] == "access"
    assert acc_data["registrations_count"] == 1
    assert acc_data["email_masked"] == mask_email(user_email)

    # 2. Rectification request
    res_rect = client.post(
        "/participation/v1/privacy-requests",
        json={"email": user_email, "request_type": "rectification"},
    )
    assert res_rect.status_code == 200
    rect_data = res_rect.json()
    assert rect_data["request_type"] == "rectification"

    # 3. Validation errors
    bad_email = client.post("/participation/v1/privacy-requests", json={"email": "bad", "request_type": "access"})
    assert bad_email.status_code == 400
    assert bad_email.json()["code"] == "invalid_email"

    bad_type = client.post("/participation/v1/privacy-requests", json={"email": user_email, "request_type": "unsupported"})
    assert bad_type.status_code == 400
    assert bad_type.json()["code"] == "invalid_request_type"


# =============================================================================
# 8. POST /participation/v1/events/{event_id}/operations-projections
# =============================================================================

def test_endpoint8_operations_projection_purposes_and_encryption(client: TestClient):
    """PA-035: Purpose-bound AES-256-GCM envelope projections for certificates and onsite attendance."""
    email = "pedro.proj@unas.edu.pe"
    name = "Pedro Proyección"
    _seed_registration(client, email=email, name=name, institution="UNAS Tingo María")

    # 1. Reject invalid purpose
    bad_res = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/operations-projections",
        json={"purpose": "marketing_campaign"},
    )
    assert bad_res.status_code == 400
    assert bad_res.json()["code"] == "invalid_purpose"

    # 2. Valid purpose: certificate_issuance
    cert_res = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/operations-projections",
        json={"purpose": "certificate_issuance", "confirmed_only": True},
    )
    assert cert_res.status_code == 200
    cert_data = cert_res.json()
    assert cert_data["purpose"] == "certificate_issuance"
    assert cert_data["algorithm"] == "AES-256-GCM"
    assert "envelope" in cert_data
    env = cert_data["envelope"]
    assert "ciphertext" in env
    assert "digest_sha256" in env

    # Security check: Zero plaintext PII in outer payload
    assert email not in json.dumps(cert_data)

    # Decrypt envelope with security manager using the exact AAD
    env_aad = f"projection:{DEFAULT_EVENT_ID}:certificate_issuance:onsite_operations"
    decrypted = security_manager.decrypt_pii(env["ciphertext"], associated_data=env_aad)
    items = decrypted["data"]
    assert len(items) >= 1
    target = next((i for i in items if i["full_name"] == name), None)
    assert target is not None
    assert target["institution"] == "UNAS Tingo María"
    assert "confirmed_at" in target

    # 3. Valid purpose: onsite_attendance
    onsite_res = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/operations-projections",
        json={"purpose": "onsite_attendance", "confirmed_only": True},
    )
    assert onsite_res.status_code == 200
    onsite_aad = f"projection:{DEFAULT_EVENT_ID}:onsite_attendance:onsite_operations"
    onsite_decrypted = security_manager.decrypt_pii(
        onsite_res.json()["envelope"]["ciphertext"],
        associated_data=onsite_aad,
    )
    onsite_items = onsite_decrypted["data"]
    assert len(onsite_items) >= 1
    assert "ticket_id" in onsite_items[0]
    assert "name_masked" in onsite_items[0]

    # Verify outbox event emitted
    outbox = client.get("/participation/v1/outbox").json()["items"]
    proj_evt = next(
        (e for e in outbox if e["topic"] == "participation.certificate_subject.projected.v1"),
        None,
    )
    assert proj_evt is not None
    assert proj_evt["envelope"]["data"]["purpose"] == "onsite_attendance"


# =============================================================================
# 9. GET /.well-known/jwks.json
# =============================================================================

def test_endpoint9_get_public_jwks(client: TestClient):
    """JWKS endpoint exports EC P-256 public keys for ticket verification without leaking private key."""
    res = client.get("/.well-known/jwks.json")
    assert res.status_code == 200
    data = res.json()
    assert "keys" in data
    assert isinstance(data["keys"], list)
    assert len(data["keys"]) >= 1

    key = data["keys"][0]
    assert key["kty"] == "EC"
    assert key["crv"] == "P-256"
    assert key["use"] == "sig"
    assert key["alg"] == "ES256"
    assert "kid" in key
    assert "x" in key
    assert "y" in key
    assert "public_key_pem" in key
    # Crucial security assertion: Never leak private key 'd' in JWKS
    assert "d" not in key


# =============================================================================
# 10. GET /participation/v1/outbox
# =============================================================================

def test_endpoint10_list_outbox_events_and_pagination(client: TestClient):
    """GET /outbox returns transactional outbox events with CloudEvents envelope and limit filter."""
    # Trigger an action that creates an outbox event
    _seed_registration(client, email="outbox.test@unas.edu.pe", name="Outbox Test")

    # Default query
    res = client.get("/participation/v1/outbox")
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert len(data["items"]) >= 1

    evt = data["items"][0]
    assert "message_id" in evt
    assert "domain_event_id" in evt
    assert "topic" in evt
    assert "aggregate_type" in evt
    assert "aggregate_id" in evt
    assert "aggregate_version" in evt
    assert "partition_key" in evt
    assert "occurred_at" in evt
    assert "envelope" in evt

    env = evt["envelope"]
    assert env["schema_version"] == 1
    assert env["producer"] == "participation"
    assert "data" in env
    assert "aggregate" in env

    # Test limit parameter
    res_limit = client.get("/participation/v1/outbox", params={"limit": 1})
    assert res_limit.status_code == 200
    assert len(res_limit.json()["items"]) == 1


# =============================================================================
# 11. GET /participation/v1/contracts/openapi.yaml
# =============================================================================

def test_endpoint11_download_openapi_contract(client: TestClient):
    """GET /contracts/openapi.yaml returns the contract OpenAPI definition file."""
    res = client.get("/participation/v1/contracts/openapi.yaml")
    assert res.status_code == 200
    assert "yaml" in res.headers.get("content-type", "")
    content = res.text
    assert "openapi:" in content
    assert "/participation/v1/events/{id}/registrations" in content


# =============================================================================
# 12. POST /participation/v1/demo/reset
# =============================================================================

def test_endpoint12_demo_reset_scenario(client: TestClient):
    """POST /demo/reset resets all transactional tables and reconfigures capacity/waitlist."""
    # Seed data
    _seed_registration(client, email="reset.me@unas.edu.pe", name="Reset Attendee")
    _seed_application(client, leader_email="reset.lead@unas.edu.pe", submit=True)

    # Perform reset to capacity=12
    res = client.post(
        "/participation/v1/demo/reset",
        json={"capacity": 12, "waitlist_enabled": True},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["capacity_summary"]["capacity"] == 12
    assert data["capacity_summary"]["confirmed_count"] == 0

    # Ensure registrations and applications are wiped clean
    regs = client.get(f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations").json()["items"]
    assert regs == []
    apps = client.get(f"/participation/v1/events/{DEFAULT_EVENT_ID}/applications").json()["items"]
    assert apps == []

    # Validation: capacity bounds (1 to 500)
    res_invalid = client.post("/participation/v1/demo/reset", json={"capacity": 0})
    assert res_invalid.status_code == 422
    res_high = client.post("/participation/v1/demo/reset", json={"capacity": 999})
    assert res_high.status_code == 422


# =============================================================================
# 13. POST /participation/v1/demo/concurrency-burst
# =============================================================================

def test_endpoint13_demo_concurrency_burst(client: TestClient):
    """PA-041: Multi-threaded registration burst verifies zero overbooking and capacity lock."""
    # Reset to capacity=3 with waitlist enabled
    client.post("/participation/v1/demo/reset", json={"capacity": 3, "waitlist_enabled": True})

    res = client.post(
        "/participation/v1/demo/concurrency-burst",
        json={"total_requests": 8},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_requests_fired"] == 8
    assert data["overbooking_detected"] is False
    assert data["confirmed_in_burst"] <= 3
    assert data["waitlisted_in_burst"] >= 5

    cap = data["capacity_summary"]
    assert cap["capacity"] == 3
    assert cap["confirmed_count"] == 3
    assert cap["waitlist_count"] == 5

    # Check validation
    res_zero = client.post("/participation/v1/demo/concurrency-burst", json={"total_requests": 0})
    assert res_zero.status_code == 422


# =============================================================================
# 14. POST /participation/v1/demo/ingest-release
# =============================================================================

def test_endpoint14_demo_ingest_release_success_and_idempotency(client: TestClient):
    """PA-010: Idempotently ingests release published configuration event."""
    envelope = _sample_release_envelope(version=1, capacity=25, waitlist_enabled=True)

    # 1. Initial ingestion
    res1 = client.post("/participation/v1/demo/ingest-release", json=envelope)
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["status"] == "materialized"

    # Form now reflects updated capacity 25
    form_res = client.get(f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/form")
    assert form_res.json()["capacity_summary"]["capacity"] == 25

    # 2. Re-ingesting duplicate event envelope is ignored idempotently
    res2 = client.post("/participation/v1/demo/ingest-release", json=envelope)
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["status"] == "duplicate_ignored"


def test_endpoint14_demo_ingest_release_validation_errors(client: TestClient):
    """Test validation errors: incomplete envelope, invalid tenant, unsupported topic."""
    envelope = _sample_release_envelope(version=1)

    # Missing domain_event_id
    bad_env = dict(envelope)
    del bad_env["domain_event_id"]
    res_bad = client.post("/participation/v1/demo/ingest-release", json=bad_env)
    assert res_bad.status_code == 400
    assert res_bad.json()["code"] == "validation_error"

    # Unsupported topic
    wrong_topic = dict(envelope, topic="unknown.topic.v1")
    res_topic = client.post("/participation/v1/demo/ingest-release", json=wrong_topic)
    assert res_topic.status_code == 400
    assert res_topic.json()["code"] == "validation_error"

    # Tenant forbidden (different org_id)
    wrong_org = dict(envelope, organization_id=generate_uuidv7())
    res_org = client.post("/participation/v1/demo/ingest-release", json=wrong_org)
    assert res_org.status_code == 403
    assert res_org.json()["code"] == "tenant_forbidden"


# =============================================================================
# 15. GET /healthz
# =============================================================================

def test_endpoint15_healthcheck(client: TestClient):
    """GET /healthz returns status ok, team name, and default event id."""
    res = client.get("/healthz")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["service"] == "participation"
    assert data["team"] == "Team 3 — Participation"
    assert data["default_event_id"] == DEFAULT_EVENT_ID

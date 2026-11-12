"""
Automated Acceptance & Concurrency Test Suite for Team 3 — Participation.
Validates user stories PA-001 through PA-041 and non-negotiable architecture invariants (AD-3, AD-7, AD-8, AD-11).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.core.security import generate_uuidv7, security_manager
from app.db.database import (
    DEFAULT_CONSENT_VERSION,
    DEFAULT_EVENT_ID,
    DEFAULT_EVENT_SLUG,
    DEFAULT_FORM_VERSION_ID,
    DEFAULT_ORG_ID,
    DEFAULT_SITE_SLUG,
)
from app.main import create_app


def _make_client(*, raise_server_exceptions: bool = True) -> TestClient:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    app = create_app(db_path=tmp.name)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _audit_rows(client: TestClient, *, request_id: str | None = None) -> list[dict]:
    with client.app.state.db.read_cursor() as cur:
        if request_id:
            cur.execute(
                "SELECT * FROM audit_logs WHERE request_id = ? ORDER BY created_at, id",
                (request_id,),
            )
        else:
            cur.execute("SELECT * FROM audit_logs ORDER BY created_at, id")
        return [dict(row) for row in cur.fetchall()]


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


def _sample_venture_payload(leader_email: str = "lider.agro@unas.edu.pe") -> dict:
    return {
        "form_version_id": DEFAULT_FORM_VERSION_ID,
        "consent_version": DEFAULT_CONSENT_VERSION,
        "venture_name": "BioAmazonas Tech",
        "track": "Agrotech & Bioeconomía Amazónica",
        "leader": {
            "full_name": "Lider Agro",
            "email": leader_email,
            "document_number": "71234567",
            "phone": "+51987654322",
            "institution": "UNAS",
        },
        "members": [
            {"email": "cofounder1@unas.edu.pe", "role": "co_founder"},
            {"email": "member1@unas.edu.pe", "role": "member"},
        ],
        "answers": {
            "problem": "Pérdida post-cosecha de cacao amazónico",
            "solution": "Sensores IoT de fermentación controlada",
            "stage": "Prototipo funcional",
        },
    }


def test_pa020_and_pa021_create_venture_draft_and_invitations():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"venture-create-{generate_uuidv7()}"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["state"] == "draft"
    assert body["venture_name"] == "BioAmazonas Tech"
    venture_id = body["venture_id"]
    invitations = body["invitations"]
    assert len(invitations) == 2
    assert "token" in invitations[0]
    assert invitations[0]["state"] == "pending"

    # PA-021: Leader invites an extra member
    inv_res = client.post(
        f"/participation/self/v1/ventures/{venture_id}/invitations",
        json={"email": "nuevo.miembro@unas.edu.pe", "role": "member"},
    )
    assert inv_res.status_code == 201
    inv_data = inv_res.json()
    assert inv_data["state"] == "pending"
    assert "token" in inv_data

    # Reject duplicate pending invitation
    dup_res = client.post(
        f"/participation/self/v1/ventures/{venture_id}/invitations",
        json={"email": "nuevo.miembro@unas.edu.pe", "role": "member"},
    )
    assert dup_res.status_code == 409
    assert dup_res.json()["code"] == "invitation_already_pending"


def test_pa022_accept_and_decline_team_invitations_and_leave_protection():
    client = _make_client()
    payload = _sample_venture_payload(leader_email="lider.inv@unas.edu.pe")
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-inv-{generate_uuidv7()}"},
    )
    v_data = res.json()
    venture_id = v_data["venture_id"]
    invitation1 = v_data["invitations"][0]
    invitation2 = v_data["invitations"][1]

    # Member 1 accepts
    acc_res = client.post(f"/participation/public/v1/team-invitations/{invitation1['token']}/acceptance")
    assert acc_res.status_code == 200
    acc_data = acc_res.json()
    assert acc_data["status"] == "accepted"
    mem1_id = acc_data["membership_id"]

    # Trying to accept again gives 409
    acc_dup = client.post(f"/participation/public/v1/team-invitations/{invitation1['token']}/acceptance")
    assert acc_dup.status_code == 409

    # Member 2 declines
    dec_res = client.post(f"/participation/public/v1/team-invitations/{invitation2['token']}/declination")
    assert dec_res.status_code == 200
    assert dec_res.json()["status"] == "declined"

    # Member 1 leaves voluntarily
    leave_res = client.post(f"/participation/self/v1/ventures/{venture_id}/members/{mem1_id}/leave")
    assert leave_res.status_code == 200
    assert leave_res.json()["status"] == "left"

    # Sole leader cannot leave (invariant: siempre queda un leader antes de submit)
    v_info = client.get(f"/participation/self/v1/ventures/{venture_id}").json()
    leader_mem = [m for m in v_info["members"] if m["role"] == "leader"][0]
    sole_leader_leave = client.post(f"/participation/self/v1/ventures/{venture_id}/members/{leader_mem['id']}/leave")
    assert sole_leader_leave.status_code == 409
    assert sole_leader_leave.json()["code"] == "cannot_remove_sole_leader"


def test_pa023_edit_draft_with_etag_optimistic_locking():
    client = _make_client()
    payload = _sample_venture_payload(leader_email="lider.draft@unas.edu.pe")
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-draft-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # GET application draft includes ETag
    get_res = client.get(f"/participation/self/v1/applications/{app_id}")
    assert get_res.status_code == 200
    assert "ETag" in get_res.headers
    etag = get_res.headers["ETag"]
    app_data = get_res.json()
    assert app_data["revision"] == 1

    # Autosave / edit draft
    patch_res = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={"answers": {"market_size": "$10M anuales", "traction": "5 clientes piloto"}, "expected_revision": 1},
        headers={"If-Match": etag},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["revision"] == 2

    # Stale update with old revision returns 412
    stale_res = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={"answers": {"tam": "Updated"}, "expected_revision": 1},
    )
    assert stale_res.status_code == 412
    assert stale_res.json()["code"] == "stale_revision"


def test_pa024_and_pa025_file_quarantine_blocks_submission_and_clean_submits():
    client = _make_client()
    payload = _sample_venture_payload(leader_email="lider.files@unas.edu.pe")
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-files-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Upload clean pitch deck
    f_clean = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "pitch_deck",
            "file_name": "Pitch_Deck_BioAmazonas.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1024 * 500,
        },
    )
    assert f_clean.status_code == 201
    assert f_clean.json()["state"] == "ready"

    # Upload suspicious executable file -> gets quarantined
    f_malware = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "script",
            "file_name": "payload_test.exe",
            "mime_type": "application/x-msdownload",
            "size_bytes": 1024,
        },
    )
    assert f_malware.status_code == 201
    assert f_malware.json()["state"] == "quarantined"

    # PA-024 / PA-025 invariant: submit blocked if not ready (quarantined file exists)
    blocked_submit = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-block-{generate_uuidv7()}"},
    )
    assert blocked_submit.status_code == 400
    assert blocked_submit.json()["code"] == "files_not_ready"

    # Create a fresh clean application with only ready files
    p2 = _sample_venture_payload(leader_email="lider.clean@unas.edu.pe")
    r2 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=p2,
        headers={"Idempotency-Key": f"v-clean-{generate_uuidv7()}"},
    )
    app2_id = r2.json()["application_id"]
    client.post(
        f"/participation/self/v1/applications/{app2_id}/file-uploads",
        json={
            "field_name": "pitch_deck",
            "file_name": "Pitch_Deck_V2.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1024 * 200,
        },
    )

    # Clean submit succeeds
    sub_res = client.post(
        f"/participation/self/v1/applications/{app2_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-ok-{generate_uuidv7()}"},
    )
    assert sub_res.status_code == 200
    sub_data = sub_res.json()
    assert sub_data["state"] == "submitted"
    assert "snapshot_hash" in sub_data
    assert len(sub_data["snapshot_hash"]) == 64


def test_pa026_withdraw_application():
    client = _make_client()
    payload = _sample_venture_payload(leader_email="lider.with@unas.edu.pe")
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-with-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Submit application
    client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-w-{generate_uuidv7()}"},
    )

    # Withdraw with reason
    with_res = client.post(
        f"/participation/self/v1/applications/{app_id}/withdrawals",
        json={"reason": "Enfoque en otra ronda de capital"},
    )
    assert with_res.status_code == 200
    assert with_res.json()["state"] == "withdrawn"

    # Cannot withdraw again from terminal state
    with_dup = client.post(
        f"/participation/self/v1/applications/{app_id}/withdrawals",
        json={"reason": "Segunda solicitud"},
    )
    assert with_dup.status_code == 409


def test_pa027_change_request_and_resubmission_creates_revision_2():
    client = _make_client()
    payload = _sample_venture_payload(leader_email="lider.subsanar@unas.edu.pe")
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-subs-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # 1. Submit Revision 1
    sub1 = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-rev1-{generate_uuidv7()}"},
    )
    assert sub1.status_code == 200
    rev1_hash = sub1.json()["snapshot_hash"]

    # 2. Reviewer requests changes (needs_changes)
    cr_res = client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={
            "requested_changes": ["Ampliar información del modelo financiero", "Subir carta de aval"],
            "deadline": "2026-11-20T23:59:59Z",
            "reviewer_notes": "Propuesta interesante, subsanar en 5 días.",
        },
    )
    assert cr_res.status_code == 200
    assert cr_res.json()["state"] == "needs_changes"
    new_rev = cr_res.json()["revision"]

    # 3. Leader edits draft to address requested changes
    edit_res = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={"answers": {"financials": "Modelo con ROI proyectado al 25%"}, "expected_revision": new_rev},
    )
    assert edit_res.status_code == 200
    latest_rev = edit_res.json()["revision"]

    # 4. Leader resubmits application -> generates revision 2
    resub_res = client.post(
        f"/participation/self/v1/applications/{app_id}/resubmissions",
        json={"expected_revision": latest_rev},
        headers={"Idempotency-Key": f"resub-{generate_uuidv7()}"},
    )
    assert resub_res.status_code == 200
    resub_data = resub_res.json()
    assert resub_data["state"] == "resubmitted"
    assert resub_data["current_revision_number"] == 2
    assert resub_data["snapshot_hash"] != rev1_hash


def test_pa031_admin_correction_allowlisted_fields_and_audit():
    client = _make_client()
    reg_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_registration_payload(email="juan.perez@unas.edu.pe", name="Juan Peres"),
        headers={"Idempotency-Key": f"reg-corr-{generate_uuidv7()}"},
    )
    assert reg_res.status_code == 201
    reg_id = reg_res.json()["id"]

    # 1. Reject modification of non-allowlisted field (answers, email)
    bad_field_res = client.patch(
        f"/participation/v1/registrations/{reg_id}",
        json={"reason": "Corrección de prueba", "updates": {"answers": {"track": "Hacked"}}},
    )
    assert bad_field_res.status_code == 400
    assert bad_field_res.json()["code"] == "field_not_correctable"
    rejected_audit = _audit_rows(client, request_id=bad_field_res.headers["X-Request-Id"])
    assert len(rejected_audit) == 1
    assert rejected_audit[0]["action"] == "correct_registration"
    assert rejected_audit[0]["result"] == "rejected"
    assert json.loads(rejected_audit[0]["payload_json"]) == {
        "problem_code": "field_not_correctable"
    }

    # 2. Reject missing reason
    no_reason_res = client.patch(
        f"/participation/v1/registrations/{reg_id}",
        json={"reason": "", "updates": {"full_name": "Juan Pérez"}},
    )
    assert no_reason_res.status_code in (400, 422)

    # 3. Successful administrative correction of allowlisted fields
    good_corr = client.patch(
        f"/participation/v1/registrations/{reg_id}",
        json={
            "reason": "Corrección de tilde en apellido y teléfono de contacto",
            "updates": {
                "full_name": "Juan Pérez",
                "phone": "+51999888777",
                "institution": "UNAS Agronomía",
            },
            "actor": "manager",
        },
        headers={"X-Request-Id": "client-forged-request-id"},
    )
    assert good_corr.status_code == 200
    assert good_corr.headers["X-Request-Id"] != "client-forged-request-id"
    corr_data = good_corr.json()
    assert corr_data["status"] == "corrected"
    assert corr_data["full_name_masked"] == "J*** P***"
    correction_audit = _audit_rows(client, request_id=good_corr.headers["X-Request-Id"])
    assert len(correction_audit) == 1
    assert correction_audit[0]["result"] == "success"
    assert correction_audit[0]["actor_type"] is None
    assert correction_audit[0]["actor_id"] is None
    assert correction_audit[0]["actor"] == "unattributed"
    assert "juan.perez@unas.edu.pe" not in json.dumps(correction_audit)
    assert "Juan Pérez" not in json.dumps(correction_audit)
    assert "+51999888777" not in json.dumps(correction_audit)
    assert (
        correction_audit[0]["reason"]
        == "Corrección de tilde en apellido y teléfono de contacto"
    )
    assert json.loads(correction_audit[0]["payload_json"]) == {
        "updated_fields": ["full_name", "institution", "phone"]
    }

    # Check reveal_pii=True returns decrypted corrected full_name
    search = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": "juan.perez@unas.edu.pe", "reveal_pii": True},
    ).json()["items"]
    assert len(search) == 1
    assert search[0]["full_name"] == "Juan Pérez"

    # 4. Verify outbox event emitted
    outbox = client.get("/participation/v1/outbox").json()["items"]
    corr_event = next((e for e in outbox if e["topic"] == "participation.registration.corrected.v1"), None)
    assert corr_event is not None
    assert corr_event["aggregate_id"] == reg_id


def test_pa032_admissibility_admission_and_rejection_with_optimistic_locking():
    client = _make_client()

    # Create and submit App 1
    app1_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=_sample_venture_payload(leader_email="lider1.adm@unas.edu.pe"),
        headers={"Idempotency-Key": f"app1-{generate_uuidv7()}"},
    )
    app1_id = app1_res.json()["application_id"]
    client.post(
        f"/participation/self/v1/applications/{app1_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-app1-{generate_uuidv7()}"},
    )

    # 1. Admit App 1 with checklist and reason
    admit_res = client.post(
        f"/participation/v1/applications/{app1_id}/admissions",
        json={
            "expected_revision": 2,
            "reason": "Cumple con todos los requisitos de admisibilidad y documentación técnica.",
            "checklist": {
                "bases_cumplidas": True,
                "equipo_completo": True,
                "archivos_validos": True,
            },
        },
    )
    assert admit_res.status_code == 200
    assert admit_res.json()["state"] == "admitted"
    admit_audit = _audit_rows(client, request_id=admit_res.headers["X-Request-Id"])
    assert len(admit_audit) == 1
    assert admit_audit[0]["action"] == "admit_application"
    assert admit_audit[0]["result"] == "success"

    # Create and submit App 2
    app2_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=_sample_venture_payload(leader_email="lider2.rej@unas.edu.pe"),
        headers={"Idempotency-Key": f"app2-{generate_uuidv7()}"},
    )
    app2_id = app2_res.json()["application_id"]
    client.post(
        f"/participation/self/v1/applications/{app2_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-app2-{generate_uuidv7()}"},
    )

    # 2. Stale revision fails with 412
    stale_res = client.post(
        f"/participation/v1/applications/{app2_id}/rejections",
        json={
            "expected_revision": 999,
            "reason": "No cumple criterios mínimos.",
        },
    )
    assert stale_res.status_code == 412
    stale_audit = _audit_rows(client, request_id=stale_res.headers["X-Request-Id"])
    assert len(stale_audit) == 1
    assert stale_audit[0]["action"] == "reject_application"
    assert stale_audit[0]["result"] == "rejected"

    # 3. Reject App 2 with correct expected revision
    rej_res = client.post(
        f"/participation/v1/applications/{app2_id}/rejections",
        json={
            "expected_revision": 2,
            "reason": "La propuesta no se alinea a los ejes temáticos del evento.",
            "checklist": {
                "bases_cumplidas": False,
                "equipo_completo": True,
            },
        },
    )
    assert rej_res.status_code == 200
    assert rej_res.json()["state"] == "rejected"
    rejection_audit = _audit_rows(client, request_id=rej_res.headers["X-Request-Id"])
    assert len(rejection_audit) == 1
    assert rejection_audit[0]["action"] == "reject_application"
    assert rejection_audit[0]["result"] == "success"

    # Verify listing shows both
    app_list = client.get(f"/participation/v1/events/{DEFAULT_EVENT_ID}/applications").json()["items"]
    assert len(app_list) >= 2


def test_pa033_batch_registration_import_dry_run_and_errors():
    client = _make_client()

    import_rows = [
        {
            "full_name": "Estudiante Importado 1",
            "email": "importado1@unas.edu.pe",
            "phone": "+51999111222",
            "institution": "UNAS",
            "document_number": "70112233",
            "answers": {"track": "IA Aplicada"},
        },
        {
            "full_name": "Invalido Sin Email",
            "email": "correo-no-valido",
            "document_number": "70999999",
        },
    ]

    # 1. Dry Run mode (no database mutations)
    dry_res = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registration-imports",
        json={
            "rows": import_rows,
            "legal_basis": "Consentimiento firmado según Ley 29733",
            "dry_run": True,
        },
    )
    assert dry_res.status_code == 200
    dry_data = dry_res.json()
    assert dry_data["dry_run"] is True
    assert dry_data["total_rows"] == 2
    assert dry_data["valid_count"] == 1
    assert dry_data["created_count"] == 0
    assert dry_data["error_count"] == 1
    assert _audit_rows(client, request_id=dry_res.headers["X-Request-Id"]) == []

    # Database still has 0 registrations from import
    search = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": "importado1@unas.edu.pe"},
    ).json()["items"]
    assert len(search) == 0

    # 2. Real import (dry_run=False)
    real_res = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registration-imports",
        json={
            "rows": import_rows,
            "legal_basis": "Consentimiento firmado según Ley 29733",
            "dry_run": False,
        },
    )
    assert real_res.status_code == 200
    real_data = real_res.json()
    assert real_data["dry_run"] is False
    assert real_data["created_count"] == 1
    assert real_data["error_count"] == 1
    import_audit = _audit_rows(client, request_id=real_res.headers["X-Request-Id"])
    assert len(import_audit) == 1
    assert import_audit[0]["action"] == "import_registrations"
    assert import_audit[0]["result"] == "success"
    assert import_audit[0]["reason"] == "Consentimiento firmado según Ley 29733"
    assert json.loads(import_audit[0]["payload_json"]) == {
        "created_count": 1,
        "dry_run": False,
        "error_count": 1,
    }
    assert "importado1@unas.edu.pe" not in json.dumps(import_audit)
    assert "70112233" not in json.dumps(import_audit)

    # Registration exists now
    search_after = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": "importado1@unas.edu.pe", "reveal_pii": True},
    ).json()["items"]
    assert len(search_after) == 1
    assert search_after[0]["full_name"] == "Estudiante Importado 1"


def test_pa034_privacy_arco_erasure_legal_hold_and_zero_pii_receipt():
    client = _make_client()
    user_email = "maria.arco@unas.edu.pe"
    reg_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_registration_payload(email=user_email, name="María Arco"),
        headers={"Idempotency-Key": f"reg-arco-{generate_uuidv7()}"},
    )
    assert reg_res.status_code == 201
    reg_id = reg_res.json()["id"]

    # 1. Erasure blocked by Legal Hold -> HTTP 409
    lh_res = client.post(
        "/participation/v1/privacy-requests",
        json={
            "email": user_email,
            "request_type": "erasure",
            "legal_hold": True,
            "reason": "Auditoría en curso",
        },
    )
    assert lh_res.status_code == 409
    assert lh_res.json()["code"] == "legal_hold_active"
    legal_hold_audit = _audit_rows(client, request_id=lh_res.headers["X-Request-Id"])
    assert len(legal_hold_audit) == 1
    assert legal_hold_audit[0]["action"] == "privacy_request"
    assert legal_hold_audit[0]["result"] == "rejected"
    assert user_email not in json.dumps(legal_hold_audit)
    with client.app.state.db.read_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM audit_legal_hold_events")
        assert cur.fetchone()["count"] == 0

    # 2. Erasure executed without Legal Hold -> Tombstone, ticket revoked, zero PII receipt
    erase_res = client.post(
        "/participation/v1/privacy-requests",
        json={
            "email": user_email,
            "request_type": "erasure",
            "legal_hold": False,
        },
    )
    assert erase_res.status_code == 200
    receipt = erase_res.json()
    assert receipt["status"] == "erased"
    assert "tombstone_hash" in receipt
    assert receipt["anonymized_count"] >= 1
    erasure_audit = _audit_rows(client, request_id=erase_res.headers["X-Request-Id"])
    assert len(erasure_audit) == 1
    assert erasure_audit[0]["action"] == "privacy_request"
    assert erasure_audit[0]["result"] == "success"
    assert erasure_audit[0]["target_id"] == receipt["receipt_id"]
    assert json.loads(erasure_audit[0]["payload_json"])["request_type"] == "erasure"
    assert user_email not in json.dumps(erasure_audit)
    # Zero plaintext PII in receipt
    assert user_email not in json.dumps(receipt)
    assert "María" not in json.dumps(receipt)

    # 3. Registration state in database is tombstoned/cancelled
    self_res = client.get(f"/participation/self/v1/registrations/{reg_id}")
    assert self_res.json()["state"] == "cancelled"


def test_pa043_audit_insert_failure_rolls_back_sensitive_change():
    client = _make_client(raise_server_exceptions=False)
    reg_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_registration_payload(email="atomic.audit@unas.edu.pe", name="Original Name"),
        headers={"Idempotency-Key": f"atomic-audit-{generate_uuidv7()}"},
    )
    assert reg_res.status_code == 201
    reg_id = reg_res.json()["id"]

    with patch.object(
        client.app.state.audit_service,
        "record",
        side_effect=RuntimeError("audit storage unavailable"),
    ):
        correction_res = client.patch(
            f"/participation/v1/registrations/{reg_id}",
            json={
                "reason": "Must rollback if audit fails",
                "updates": {"full_name": "Changed Name"},
            },
        )

    assert correction_res.status_code == 500
    registered = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"email": "atomic.audit@unas.edu.pe", "reveal_pii": True},
    ).json()["items"]
    assert registered[0]["full_name"] == "Original Name"
    assert _audit_rows(client) == []


def test_pa035_purpose_bound_aes_gcm_operations_projection():
    client = _make_client()
    # Register a confirmed participant
    client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_registration_payload(email="pedro.ops@unas.edu.pe", name="Pedro Operaciones"),
        headers={"Idempotency-Key": f"reg-ops-{generate_uuidv7()}"},
    )

    # 1. Unauthorized purpose fails with 400
    bad_purpose = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/operations-projections",
        json={"purpose": "marketing_campaign"},
    )
    assert bad_purpose.status_code == 400
    assert bad_purpose.json()["code"] == "invalid_purpose"

    # 2. Valid purpose (certificate_issuance) produces encrypted envelope
    good_proj = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/operations-projections",
        json={"purpose": "certificate_issuance", "confirmed_only": True},
    )
    assert good_proj.status_code == 200
    proj_data = good_proj.json()
    assert proj_data["purpose"] == "certificate_issuance"
    assert "envelope" in proj_data
    env = proj_data["envelope"]
    assert "key_id" in env
    assert "ciphertext" in env
    assert "digest_sha256" in env
    assert "retention_until" in env
    # Verify no plaintext PII exposed in the unencrypted outer projection
    assert "pedro.ops@unas.edu.pe" not in json.dumps(proj_data)


# ---------------------------------------------------------------------------
# Corrections from review: PostgreSQL parity, capacity race, demo reset,
# erasure completeness and idempotent commands.
# ---------------------------------------------------------------------------
def test_pa024_demo_reset_after_file_upload():
    client = _make_client()
    payload = _sample_venture_payload(leader_email="lider.reset@unas.edu.pe")
    created = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-reset-{generate_uuidv7()}"},
    )
    assert created.status_code == 201
    app_id = created.json()["application_id"]

    upload = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={"field_name": "pitch", "file_name": "pitch.pdf", "size_bytes": 1024},
    )
    assert upload.status_code == 201

    reset = client.post("/participation/v1/demo/reset", json={"capacity": 5})
    assert reset.status_code == 200

    apps = client.get(f"/participation/v1/events/{DEFAULT_EVENT_ID}/applications").json()["items"]
    assert apps == []


def test_pa034_erasure_scrubs_answers_and_persists_legal_hold():
    client = _make_client()
    db = client.app.state.db
    email = "borrar.pii@unas.edu.pe"
    email_hmac = security_manager.compute_email_hmac(DEFAULT_ORG_ID, email)

    reg = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/registrations",
        json=_sample_registration_payload(email=email, name="Titular Borrar"),
        headers={"Idempotency-Key": f"erase-{generate_uuidv7()}"},
    ).json()

    legal_hold = client.post(
        "/participation/v1/privacy-requests",
        json={"email": email, "request_type": "erasure", "legal_hold": True},
    )
    assert legal_hold.status_code == 409

    # The rejection is evidence and must survive the transaction rollback
    with db.read_cursor() as cur:
        cur.execute(
            "SELECT state, legal_hold FROM privacy_requests WHERE email_hmac = ?",
            (email_hmac,),
        )
        rejected = cur.fetchone()
    assert rejected is not None
    assert rejected["state"] == "rejected"
    assert int(rejected["legal_hold"]) == 1

    erased = client.post(
        "/participation/v1/privacy-requests",
        json={"email": email, "request_type": "erasure"},
    )
    assert erased.status_code == 200

    with db.read_cursor() as cur:
        cur.execute(
            "SELECT count(*) AS cnt FROM registration_answers WHERE registration_id = ?",
            (reg["id"],),
        )
        assert cur.fetchone()["cnt"] == 0
        cur.execute(
            "SELECT count(*) AS cnt FROM privacy_requests WHERE request_type = 'erasure' AND state = 'completed'"
        )
        assert cur.fetchone()["cnt"] == 1


def test_pa034_rectification_is_recorded_as_rectification():
    client = _make_client()
    db = client.app.state.db
    email = "rectificar@unas.edu.pe"
    email_hmac = security_manager.compute_email_hmac(DEFAULT_ORG_ID, email)

    res = client.post(
        "/participation/v1/privacy-requests",
        json={"email": email, "request_type": "rectification"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["request_type"] == "rectification"
    assert body["registrations_count"] == 0

    with db.read_cursor() as cur:
        cur.execute(
            "SELECT request_type, state FROM privacy_requests WHERE email_hmac = ?",
            (email_hmac,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["request_type"] == "rectification"
    assert row["state"] == "completed"


def test_pa033_import_respects_atomic_capacity_guard():
    client = _make_client()
    client.post("/participation/v1/demo/reset", json={"capacity": 5})

    def _rows(prefix: str, count: int):
        return [
            {"full_name": f"{prefix} {i}", "email": f"{prefix}{i}@unas.edu.pe"}
            for i in range(count)
        ]

    first = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registration-imports",
        json={"rows": _rows("cap-a-", 4), "legal_basis": "consentimiento demo"},
    )
    assert first.status_code == 200
    assert first.json()["imported_count"] == 4

    second = client.post(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registration-imports",
        json={"rows": _rows("cap-b-", 2), "legal_basis": "consentimiento demo"},
    )
    assert second.status_code == 409
    assert second.json()["code"] == "capacity_exceeded"

    registrations = client.get(
        f"/participation/v1/events/{DEFAULT_EVENT_ID}/registrations",
        params={"limit": 100},
    ).json()["items"]
    assert len(registrations) == 4
    form = client.get(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/form"
    ).json()
    assert form["capacity_summary"]["confirmed_count"] == 4


def test_pa025_submit_is_idempotent_and_listing_hides_ciphertext():
    client = _make_client()
    payload = _sample_venture_payload(leader_email="lider.idem@unas.edu.pe")
    created = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-idem-{generate_uuidv7()}"},
    )
    app_id = created.json()["application_id"]

    apps = client.get(f"/participation/v1/events/{DEFAULT_EVENT_ID}/applications").json()["items"]
    assert any(a["id"] == app_id for a in apps)
    assert all("draft_answers_ciphertext" not in a for a in apps)

    key = f"submit-idem-{generate_uuidv7()}"
    first = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 200
    second = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 200
    assert second.json()["snapshot_hash"] == first.json()["snapshot_hash"]
    assert second.json()["revision"] == first.json()["revision"]

    db = client.app.state.db
    with db.read_cursor() as cur:
        cur.execute(
            "SELECT count(*) AS cnt FROM application_revisions WHERE application_id = ?",
            (app_id,),
        )
        assert cur.fetchone()["cnt"] == 1


def test_pa021_invitation_is_idempotent_with_same_key():
    client = _make_client()
    payload = _sample_venture_payload(leader_email="lider.inv.idem@unas.edu.pe")
    venture = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-inv-idem-{generate_uuidv7()}"},
    ).json()

    key = f"inv-idem-{generate_uuidv7()}"
    body = {"email": "idem.member@unas.edu.pe", "role": "member"}
    first = client.post(
        f"/participation/self/v1/ventures/{venture['venture_id']}/invitations",
        json=body,
        headers={"Idempotency-Key": key},
    )
    second = client.post(
        f"/participation/self/v1/ventures/{venture['venture_id']}/invitations",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["invitation_id"] == first.json()["invitation_id"]

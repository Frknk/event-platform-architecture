"""
Automated End-to-End Test Suite for Ventures & Team Applications (PA-C).

Validates all 14 Ventures & Team Applications endpoints:
1. POST /participation/public/v1/sites/{site}/events/{event}/applications (create draft venture application with leader and answers)
2. GET /participation/self/v1/applications/{application_id} (get draft application)
3. PATCH /participation/self/v1/applications/{application_id} (update answers with expected_revision check)
4. GET /participation/self/v1/ventures/{venture_id} (venture details)
5. POST /participation/self/v1/ventures/{venture_id}/invitations (invite team member)
6. POST /participation/public/v1/team-invitations/{token}/acceptance (accept invitation)
7. POST /participation/public/v1/team-invitations/{token}/declination (decline invitation)
8. POST /participation/self/v1/ventures/{venture_id}/members/{member_id}/leave (member leaves venture)
9. POST /participation/self/v1/applications/{application_id}/file-uploads (upload pitch deck/attachments)
10. GET /participation/self/v1/applications/{application_id}/files (list uploaded files)
11. POST /participation/self/v1/applications/{application_id}/submissions (submit application with revision check)
12. POST /participation/self/v1/applications/{application_id}/withdrawals (withdraw submitted application)
13. POST /participation/v1/applications/{application_id}/change-requests (reviewer requests changes)
14. POST /participation/self/v1/applications/{application_id}/resubmissions (applicant resubmits after change request)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import tempfile
import uuid
import pytest
from fastapi.testclient import TestClient

from app.core.security import generate_uuidv7
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


def _sample_venture_payload(
    *,
    leader_email: str = "lider.unas@unas.edu.pe",
    leader_name: str = "Ing. Manuel Rivas",
    venture_name: str = "BioAmazonas Tech",
    track: str = "Agrotech & Bioeconomía Amazónica",
    members: list[dict] | None = None,
    answers: dict | None = None,
) -> dict:
    if members is None:
        members = [
            {"email": "cofounder1@unas.edu.pe", "role": "co_founder"},
            {"email": "member1@unas.edu.pe", "role": "member"},
        ]
    if answers is None:
        answers = {
            "problem": "Pérdida post-cosecha de cacao amazónico",
            "solution": "Sensores IoT de fermentación controlada",
            "stage": "Prototipo funcional",
        }
    return {
        "form_version_id": DEFAULT_FORM_VERSION_ID,
        "consent_version": DEFAULT_CONSENT_VERSION,
        "venture_name": venture_name,
        "track": track,
        "leader": {
            "full_name": leader_name,
            "email": leader_email,
            "document_number": "71234567",
            "phone": "+51999888777",
            "institution": "UNAS",
        },
        "members": members,
        "answers": answers,
    }


def _get_outbox_events(client: TestClient) -> list[dict]:
    res = client.get("/participation/v1/outbox")
    assert res.status_code == 200
    items = res.json().get("items", [])
    for item in items:
        if "envelope" in item and isinstance(item["envelope"], dict) and "data" in item["envelope"]:
            item["data"] = item["envelope"]["data"]
    return items


# =============================================================================
# 1. POST /participation/public/v1/sites/{site}/events/{event}/applications
# =============================================================================

def test_create_venture_application_success():
    client = _make_client()
    payload = _sample_venture_payload(
        leader_email="carla.sanchez@unas.edu.pe",
        leader_name="Carla Sanchez",
        venture_name="Amazonia Drone AI",
        track="Monitoreo Forestal",
    )
    idem_key = f"create-v-{generate_uuidv7()}"

    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": idem_key},
    )
    assert res.status_code == 201
    body = res.json()

    assert "application_id" in body
    assert "venture_id" in body
    assert body["venture_name"] == "Amazonia Drone AI"
    assert body["state"] == "draft"
    assert body["revision"] == 1
    assert "created_at" in body

    # Invitations check
    invitations = body["invitations"]
    assert len(invitations) == 2
    for inv in invitations:
        assert inv["state"] == "pending"
        assert "token" in inv
        assert inv["token"].startswith("inv_")
        assert "email_masked" in inv
        assert "expires_at" in inv

    # Outbox check
    outbox = _get_outbox_events(client)
    inv_events = [e for e in outbox if e["topic"] == "participation.team_invitation.changed.v1"]
    assert len(inv_events) == 2


def test_create_venture_application_idempotency_same_payload():
    client = _make_client()
    payload = _sample_venture_payload()
    idem_key = f"idem-create-{generate_uuidv7()}"

    # First attempt
    res1 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": idem_key},
    )
    assert res1.status_code == 201
    data1 = res1.json()

    # Second attempt with exact same payload and key returns identical response
    res2 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": idem_key},
    )
    assert res2.status_code == 201
    assert res2.json()["application_id"] == data1["application_id"]
    assert res2.json()["venture_id"] == data1["venture_id"]


def test_create_venture_application_idempotency_payload_mismatch():
    client = _make_client()
    payload = _sample_venture_payload()
    idem_key = f"idem-mismatch-{generate_uuidv7()}"

    # First attempt
    r1 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": idem_key},
    )
    assert r1.status_code == 201

    # Second attempt with modified payload using same idempotency key
    modified_payload = dict(payload)
    modified_payload["venture_name"] = "Different Venture Name"
    r2 = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=modified_payload,
        headers={"Idempotency-Key": idem_key},
    )
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_key_reused"


def test_create_venture_application_missing_idempotency_header():
    client = _make_client()
    payload = _sample_venture_payload()

    # Missing Idempotency-Key header returns 422
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
    )
    assert res.status_code == 422


def test_create_venture_application_with_empty_member_email_and_defaults():
    client = _make_client()
    payload = _sample_venture_payload(
        members=[
            {"email": "", "role": "member"},  # Empty email should be skipped
            {"email": "valid.collab@unas.edu.pe", "role": "member"},
        ],
        answers={},
    )
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-skip-{generate_uuidv7()}"},
    )
    assert res.status_code == 201
    body = res.json()
    assert len(body["invitations"]) == 1
    assert body["invitations"][0]["email_masked"] == "v***b@unas.edu.pe"


def test_create_venture_application_validation_missing_fields():
    client = _make_client()
    idem_key = f"val-err-{generate_uuidv7()}"

    # Missing venture_name and leader
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json={"form_version_id": DEFAULT_FORM_VERSION_ID, "consent_version": DEFAULT_CONSENT_VERSION},
        headers={"Idempotency-Key": idem_key},
    )
    assert res.status_code == 422


# =============================================================================
# 2. GET /participation/self/v1/applications/{application_id}
# 3. PATCH /participation/self/v1/applications/{application_id}
# =============================================================================

def test_get_application_draft_details_and_etag():
    client = _make_client()
    payload = _sample_venture_payload(leader_email="lucia.ramos@unas.edu.pe")
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-get-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # GET application
    get_res = client.get(f"/participation/self/v1/applications/{app_id}")
    assert get_res.status_code == 200
    assert "ETag" in get_res.headers
    assert get_res.headers["ETag"] == 'W/"1"'

    body = get_res.json()
    assert body["application_id"] == app_id
    assert body["state"] == "draft"
    assert body["revision"] == 1
    assert body["etag"] == 'W/"1"'
    assert body["current_revision_number"] == 1
    assert body["venture_name"] == "BioAmazonas Tech"
    assert body["answers"]["problem"] == "Pérdida post-cosecha de cacao amazónico"
    assert len(body["members"]) >= 1
    assert body["members"][0]["role"] == "leader"
    assert len(body["invitations"]) == 2
    assert body["files"] == []
    assert body["revisions"] == []


def test_get_application_not_found():
    client = _make_client()
    fake_id = generate_uuidv7()
    res = client.get(f"/participation/self/v1/applications/{fake_id}")
    assert res.status_code == 404
    assert res.json()["code"] == "tenant_forbidden"


def test_patch_application_draft_expected_revision_success():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-patch-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    new_answers = {
        "problem": "Pérdida post-cosecha de cacao amazónico",
        "solution": "Sensores IoT de fermentación controlada y secado solar inteligente",
        "market_size": "TAM $45M, SAM $8M",
    }

    # Patch draft using expected_revision=1
    patch_res = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={"answers": new_answers, "expected_revision": 1},
    )
    assert patch_res.status_code == 200
    pdata = patch_res.json()
    assert pdata["revision"] == 2
    assert pdata["etag"] == 'W/"2"'
    assert patch_res.headers["ETag"] == 'W/"2"'
    assert pdata["answers"]["market_size"] == "TAM $45M, SAM $8M"

    # Verify answers persisted by GET
    get_res = client.get(f"/participation/self/v1/applications/{app_id}")
    assert get_res.status_code == 200
    assert get_res.json()["answers"]["market_size"] == "TAM $45M, SAM $8M"
    assert get_res.json()["revision"] == 2


def test_patch_application_draft_using_if_match_header():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-ifmatch-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # First update via expected_revision=1 -> revision becomes 2
    client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={"answers": {"step": "1"}, "expected_revision": 1},
    )

    # Second update using If-Match: W/"2"
    patch_res = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={"answers": {"step": "2", "traction": "3 cooperativas aliadas"}},
        headers={"If-Match": 'W/"2"'},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["revision"] == 3
    assert patch_res.headers["ETag"] == 'W/"3"'


def test_patch_application_draft_stale_revision_conflict():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-stale-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Revision is 1; send expected_revision=99 -> 412 Precondition Failed
    stale_res = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={"answers": {"tam": "Updated"}, "expected_revision": 99},
    )
    assert stale_res.status_code == 412
    assert stale_res.json()["code"] == "stale_revision"


def test_patch_application_draft_not_found():
    client = _make_client()
    fake_id = generate_uuidv7()
    res = client.patch(
        f"/participation/self/v1/applications/{fake_id}",
        json={"answers": {}, "expected_revision": 1},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "tenant_forbidden"


def test_patch_application_cannot_edit_when_submitted():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-subedit-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Submit application
    sub = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-{generate_uuidv7()}"},
    )
    assert sub.status_code == 200

    # Attempt to patch draft answers when state is submitted -> 409 Conflict
    patch_res = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={"answers": {"late_change": "true"}, "expected_revision": 2},
    )
    assert patch_res.status_code == 409
    assert patch_res.json()["code"] == "cannot_edit_submitted_application"


# =============================================================================
# 4. GET /participation/self/v1/ventures/{venture_id}
# 5. POST /participation/self/v1/ventures/{venture_id}/invitations
# 6. POST /participation/public/v1/team-invitations/{token}/acceptance
# 7. POST /participation/public/v1/team-invitations/{token}/declination
# 8. POST /participation/self/v1/ventures/{venture_id}/members/{member_id}/leave
# =============================================================================

def test_get_venture_details_success():
    client = _make_client()
    payload = _sample_venture_payload(venture_name="AquaAmazonas", track="Acuicultura")
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-getv-{generate_uuidv7()}"},
    )
    venture_id = res.json()["venture_id"]

    v_res = client.get(f"/participation/self/v1/ventures/{venture_id}")
    assert v_res.status_code == 200
    v_data = v_res.json()
    assert v_data["venture_id"] == venture_id
    assert v_data["name"] == "AquaAmazonas"
    assert v_data["track"] == "Acuicultura"
    assert v_data["revision"] == 1
    assert len(v_data["members"]) == 1
    assert v_data["members"][0]["role"] == "leader"
    assert len(v_data["invitations"]) == 2


def test_get_venture_details_not_found():
    client = _make_client()
    fake_id = generate_uuidv7()
    res = client.get(f"/participation/self/v1/ventures/{fake_id}")
    assert res.status_code == 404
    assert res.json()["code"] == "tenant_forbidden"


def test_invite_team_member_success_and_idempotency():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-inv-{generate_uuidv7()}"},
    )
    venture_id = res.json()["venture_id"]
    idem_key = f"inv-key-{generate_uuidv7()}"

    # Invite new member
    inv_res = client.post(
        f"/participation/self/v1/ventures/{venture_id}/invitations",
        json={"email": "nuevo.agrotech@unas.edu.pe", "role": "co_founder"},
        headers={"Idempotency-Key": idem_key},
    )
    assert inv_res.status_code == 201
    inv_data = inv_res.json()
    assert inv_data["role"] == "co_founder"
    assert inv_data["state"] == "pending"
    assert "token" in inv_data
    assert "expires_at" in inv_data

    # Idempotent replay with same key returns identical invitation
    replay_res = client.post(
        f"/participation/self/v1/ventures/{venture_id}/invitations",
        json={"email": "nuevo.agrotech@unas.edu.pe", "role": "co_founder"},
        headers={"Idempotency-Key": idem_key},
    )
    assert replay_res.status_code == 201
    assert replay_res.json()["invitation_id"] == inv_data["invitation_id"]


def test_invite_team_member_duplicate_pending():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-dup-{generate_uuidv7()}"},
    )
    venture_id = res.json()["venture_id"]

    # Initial invitation was for 'cofounder1@unas.edu.pe'; invite again without idempotency key
    dup_res = client.post(
        f"/participation/self/v1/ventures/{venture_id}/invitations",
        json={"email": "cofounder1@unas.edu.pe", "role": "member"},
    )
    assert dup_res.status_code == 409
    assert dup_res.json()["code"] == "invitation_already_pending"


def test_invite_team_member_already_active_member():
    client = _make_client()
    leader_email = "lider.unico@unas.edu.pe"
    payload = _sample_venture_payload(leader_email=leader_email)
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-act-{generate_uuidv7()}"},
    )
    venture_id = res.json()["venture_id"]

    # Invite the leader (who is already an active member)
    dup_act = client.post(
        f"/participation/self/v1/ventures/{venture_id}/invitations",
        json={"email": leader_email, "role": "co_founder"},
    )
    assert dup_act.status_code == 409
    assert dup_act.json()["code"] == "already_team_member"


def test_invite_team_member_validation_errors():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-val-{generate_uuidv7()}"},
    )
    venture_id = res.json()["venture_id"]

    # Invalid email
    bad_email = client.post(
        f"/participation/self/v1/ventures/{venture_id}/invitations",
        json={"email": "correo-no-valido", "role": "member"},
    )
    assert bad_email.status_code == 400
    assert bad_email.json()["code"] == "invalid_email"

    # Invalid role
    bad_role = client.post(
        f"/participation/self/v1/ventures/{venture_id}/invitations",
        json={"email": "valido@unas.edu.pe", "role": "super_admin"},
    )
    assert bad_role.status_code == 400
    assert bad_role.json()["code"] == "invalid_role"

    # Unknown venture
    fake_id = generate_uuidv7()
    v_unknown = client.post(
        f"/participation/self/v1/ventures/{fake_id}/invitations",
        json={"email": "valido@unas.edu.pe", "role": "member"},
    )
    assert v_unknown.status_code == 404


def test_accept_invitation_success_and_duplicates():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-acc-{generate_uuidv7()}"},
    )
    v_data = res.json()
    venture_id = v_data["venture_id"]
    token = v_data["invitations"][0]["token"]

    # Accept invitation
    acc_res = client.post(f"/participation/public/v1/team-invitations/{token}/acceptance")
    assert acc_res.status_code == 200
    acc_data = acc_res.json()
    assert acc_data["status"] == "accepted"
    assert "membership_id" in acc_data
    assert acc_data["venture_id"] == venture_id
    assert acc_data["role"] == "co_founder"

    # Second acceptance of same token -> 409 Conflict
    dup_acc = client.post(f"/participation/public/v1/team-invitations/{token}/acceptance")
    assert dup_acc.status_code == 409
    assert dup_acc.json()["code"] == "invitation_already_processed"

    # Verify team member is now active in venture
    v_details = client.get(f"/participation/self/v1/ventures/{venture_id}").json()
    active_mems = [m for m in v_details["members"] if m["state"] == "active"]
    assert len(active_mems) == 2  # leader + accepted co-founder


def test_accept_invitation_invalid_token():
    client = _make_client()
    res = client.post("/participation/public/v1/team-invitations/token_ficticio_12345/acceptance")
    assert res.status_code == 404
    assert res.json()["code"] == "invitation_not_found"


def test_accept_invitation_expired():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-exp-{generate_uuidv7()}"},
    )
    token = res.json()["invitations"][0]["token"]

    # Manually expire invitation in database
    past_iso = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    with client.app.state.db.transaction() as cur:
        cur.execute("UPDATE team_invitations SET expires_at = ?", (past_iso,))

    # Accepting expired invitation -> 410 Gone
    exp_res = client.post(f"/participation/public/v1/team-invitations/{token}/acceptance")
    assert exp_res.status_code == 410
    assert exp_res.json()["code"] == "invitation_expired"


def test_decline_invitation_success_and_duplicates():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-dec-{generate_uuidv7()}"},
    )
    token = res.json()["invitations"][1]["token"]

    # Decline invitation
    dec_res = client.post(f"/participation/public/v1/team-invitations/{token}/declination")
    assert dec_res.status_code == 200
    assert dec_res.json()["status"] == "declined"

    # Outbox check
    outbox = _get_outbox_events(client)
    declined_events = [e for e in outbox if e["topic"] == "participation.team_invitation.changed.v1" and e["data"].get("state") == "declined"]
    assert len(declined_events) >= 1

    # Second decline -> 409 Conflict
    dup_dec = client.post(f"/participation/public/v1/team-invitations/{token}/declination")
    assert dup_dec.status_code == 409
    assert dup_dec.json()["code"] == "invitation_already_processed"


def test_decline_invitation_invalid_token():
    client = _make_client()
    res = client.post("/participation/public/v1/team-invitations/token_no_existe/declination")
    assert res.status_code == 404
    assert res.json()["code"] == "invitation_not_found"


def test_member_leaves_venture_success_and_sole_leader_protection():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-leave-{generate_uuidv7()}"},
    )
    v_data = res.json()
    venture_id = v_data["venture_id"]
    token = v_data["invitations"][0]["token"]

    # 1. Member accepts invitation
    acc = client.post(f"/participation/public/v1/team-invitations/{token}/acceptance").json()
    member_id = acc["membership_id"]

    # 2. Member leaves venture
    leave_res = client.post(f"/participation/self/v1/ventures/{venture_id}/members/{member_id}/leave")
    assert leave_res.status_code == 200
    assert leave_res.json()["status"] == "left"
    assert leave_res.json()["member_id"] == member_id

    # Member leaving again -> 409 Conflict
    leave_again = client.post(f"/participation/self/v1/ventures/{venture_id}/members/{member_id}/leave")
    assert leave_again.status_code == 409
    assert leave_again.json()["code"] == "member_not_active"

    # 3. Sole leader tries to leave -> 409 Conflict (cannot remove sole leader)
    v_info = client.get(f"/participation/self/v1/ventures/{venture_id}").json()
    leader_mem = [m for m in v_info["members"] if m["role"] == "leader"][0]
    sole_leave = client.post(f"/participation/self/v1/ventures/{venture_id}/members/{leader_mem['id']}/leave")
    assert sole_leave.status_code == 409
    assert sole_leave.json()["code"] == "cannot_remove_sole_leader"


def test_leader_can_leave_if_co_leader_exists():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-coleader-{generate_uuidv7()}"},
    )
    venture_id = res.json()["venture_id"]
    now = datetime.now(timezone.utc).isoformat()

    # Insert a second active leader directly
    co_leader_id = generate_uuidv7()
    with client.app.state.db.transaction() as cur:
        cur.execute(
            """
            INSERT INTO team_memberships (
                id, organization_id, venture_id, email_hmac, email_masked,
                role, state, created_at, updated_at
            ) VALUES (?, ?, ?, 'hmac_coleader', 'c***@unas.edu.pe', 'leader', 'active', ?, ?)
            """,
            (co_leader_id, DEFAULT_ORG_ID, venture_id, now, now),
        )

    # First leader can now leave
    v_info = client.get(f"/participation/self/v1/ventures/{venture_id}").json()
    leader_1 = [m for m in v_info["members"] if m["id"] != co_leader_id and m["role"] == "leader"][0]
    leave_ok = client.post(f"/participation/self/v1/ventures/{venture_id}/members/{leader_1['id']}/leave")
    assert leave_ok.status_code == 200
    assert leave_ok.json()["status"] == "left"


def test_leave_venture_member_not_found():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-lerr-{generate_uuidv7()}"},
    )
    venture_id = res.json()["venture_id"]
    fake_member_id = generate_uuidv7()

    leave_res = client.post(f"/participation/self/v1/ventures/{venture_id}/members/{fake_member_id}/leave")
    assert leave_res.status_code == 404
    assert leave_res.json()["code"] == "member_not_found"


# =============================================================================
# 9. POST /participation/self/v1/applications/{application_id}/file-uploads
# 10. GET /participation/self/v1/applications/{application_id}/files
# =============================================================================

def test_upload_file_clean_pdf_ready_and_list_files():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-fup-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # 1. Upload clean pitch deck
    f_res = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "pitch_deck",
            "file_name": "BioAmazonas_PitchDeck_Final.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1024 * 350,
        },
    )
    assert f_res.status_code == 201
    f_data = f_res.json()
    assert f_data["field_name"] == "pitch_deck"
    assert f_data["file_name"] == "BioAmazonas_PitchDeck_Final.pdf"
    assert f_data["state"] == "ready"
    assert "file_id" in f_data
    assert "sha256_hash" in f_data

    # 2. Upload supplementary video attachment
    v_res = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "demo_video",
            "file_name": "demo_prototipo.mp4",
            "mime_type": "video/mp4",
            "size_bytes": 1024 * 1024 * 5,
        },
    )
    assert v_res.status_code == 201
    assert v_res.json()["state"] == "ready"

    # 3. GET /participation/self/v1/applications/{app_id}/files
    list_res = client.get(f"/participation/self/v1/applications/{app_id}/files")
    assert list_res.status_code == 200
    items = list_res.json()["items"]
    assert len(items) == 2
    names = [f["file_name"] for f in items]
    assert "BioAmazonas_PitchDeck_Final.pdf" in names
    assert "demo_prototipo.mp4" in names


def test_upload_suspicious_file_quarantined():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-fmal-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Suspicious .sh script
    f_sh = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "script",
            "file_name": "install.sh",
            "mime_type": "application/x-sh",
            "size_bytes": 2048,
        },
    )
    assert f_sh.status_code == 201
    assert f_sh.json()["state"] == "quarantined"

    # Suspicious .exe file
    f_exe = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "executable",
            "file_name": "setup.exe",
            "mime_type": "application/octet-stream",
            "size_bytes": 1024 * 20,
        },
    )
    assert f_exe.status_code == 201
    assert f_exe.json()["state"] == "quarantined"


def test_upload_file_too_large():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-flarge-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # 15MB file (exceeds 10MB limit) -> 413
    res_large = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "pitch_deck",
            "file_name": "Huge_Dataset.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 15 * 1024 * 1024,
        },
    )
    assert res_large.status_code == 413
    assert res_large.json()["code"] == "file_too_large"


def test_upload_file_non_existent_application():
    client = _make_client()
    fake_id = generate_uuidv7()
    res = client.post(
        f"/participation/self/v1/applications/{fake_id}/file-uploads",
        json={
            "field_name": "deck",
            "file_name": "test.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1024,
        },
    )
    assert res.status_code == 404
    assert res.json()["code"] == "tenant_forbidden"


def test_upload_file_blocked_when_application_submitted():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-flock-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Submit application
    client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-flock-{generate_uuidv7()}"},
    )

    # Attempt to upload file to submitted application -> 409
    res_f = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "pitch_deck",
            "file_name": "Late_Deck.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1024,
        },
    )
    assert res_f.status_code == 409
    assert res_f.json()["code"] == "cannot_modify_application"


# =============================================================================
# 11. POST /participation/self/v1/applications/{application_id}/submissions
# =============================================================================

def test_submit_application_success():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-subok-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Upload clean file
    client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "pitch_deck",
            "file_name": "Deck.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1024 * 100,
        },
    )

    # Submit with expected_revision=1
    sub_res = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"subm-{generate_uuidv7()}"},
    )
    assert sub_res.status_code == 200
    s_data = sub_res.json()
    assert s_data["application_id"] == app_id
    assert s_data["state"] == "submitted"
    assert s_data["revision"] == 2
    assert s_data["current_revision_number"] == 1
    assert "snapshot_hash" in s_data
    assert len(s_data["snapshot_hash"]) == 64
    assert "submitted_at" in s_data

    # Outbox event
    outbox = _get_outbox_events(client)
    sub_events = [e for e in outbox if e["topic"] == "participation.application.submitted.v1"]
    assert len(sub_events) >= 1
    assert sub_events[0]["data"]["snapshot_hash"] == s_data["snapshot_hash"]


def test_submit_application_idempotency():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-subidem-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]
    idem_key = f"sub-idem-key-{generate_uuidv7()}"

    # First submit
    sub1 = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": idem_key},
    )
    assert sub1.status_code == 200
    data1 = sub1.json()

    # Replay submit with same key
    sub2 = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": idem_key},
    )
    assert sub2.status_code == 200
    assert sub2.json()["snapshot_hash"] == data1["snapshot_hash"]


def test_submit_application_stale_revision():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-substale-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Application is revision 1; expect 2 -> 412
    sub = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 2},
        headers={"Idempotency-Key": f"sub-st-{generate_uuidv7()}"},
    )
    assert sub.status_code == 412
    assert sub.json()["code"] == "stale_revision"


def test_submit_application_blocked_by_quarantined_files():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-subquar-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Upload quarantined file
    client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "malicious",
            "file_name": "virus.bat",
            "mime_type": "application/x-bat",
            "size_bytes": 1024,
        },
    )

    # Submission blocked -> 400
    sub = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-quar-{generate_uuidv7()}"},
    )
    assert sub.status_code == 400
    assert sub.json()["code"] == "files_not_ready"


def test_submit_application_blocked_without_active_leader():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-subnolead-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]
    venture_id = res.json()["venture_id"]

    # Manually deactivate leader in database
    with client.app.state.db.transaction() as cur:
        cur.execute("UPDATE team_memberships SET state = 'left' WHERE venture_id = ?", (venture_id,))

    # Submission blocked -> 400
    sub = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-nolead-{generate_uuidv7()}"},
    )
    assert sub.status_code == 400
    assert sub.json()["code"] == "no_active_leader"


def test_submit_application_invalid_state_transition():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-subtrans-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # First submit succeeds
    sub1 = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub1-{generate_uuidv7()}"},
    )
    assert sub1.status_code == 200

    # Submitting already submitted application with new key -> 409
    sub2 = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 2},
        headers={"Idempotency-Key": f"sub2-{generate_uuidv7()}"},
    )
    assert sub2.status_code == 409
    assert sub2.json()["code"] == "invalid_state_transition"


def test_submit_application_not_found():
    client = _make_client()
    fake_id = generate_uuidv7()
    sub = client.post(
        f"/participation/self/v1/applications/{fake_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-fake-{generate_uuidv7()}"},
    )
    assert sub.status_code == 404
    assert sub.json()["code"] == "tenant_forbidden"


# =============================================================================
# 12. POST /participation/self/v1/applications/{application_id}/withdrawals
# =============================================================================

def test_withdraw_application_success():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-wdrw-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Submit application
    client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-wdrw-{generate_uuidv7()}"},
    )

    # Withdraw application
    w_res = client.post(
        f"/participation/self/v1/applications/{app_id}/withdrawals",
        json={"reason": "Cambio de giro de negocio y pivot hacia mercado B2B"},
    )
    assert w_res.status_code == 200
    w_data = w_res.json()
    assert w_data["application_id"] == app_id
    assert w_data["state"] == "withdrawn"
    assert w_data["reason"] == "Cambio de giro de negocio y pivot hacia mercado B2B"
    assert w_data["revision"] == 3

    # Outbox event check
    outbox = _get_outbox_events(client)
    w_events = [e for e in outbox if e["topic"] == "participation.application.withdrawn.v1"]
    assert len(w_events) >= 1


def test_withdraw_application_terminal_state_conflict():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-wdrt-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # First withdrawal succeeds
    w1 = client.post(
        f"/participation/self/v1/applications/{app_id}/withdrawals",
        json={"reason": "Retiro 1"},
    )
    assert w1.status_code == 200

    # Withdrawing already withdrawn application -> 409 Conflict
    w2 = client.post(
        f"/participation/self/v1/applications/{app_id}/withdrawals",
        json={"reason": "Retiro 2"},
    )
    assert w2.status_code == 409
    assert w2.json()["code"] == "invalid_state_transition"


def test_withdraw_application_not_found():
    client = _make_client()
    fake_id = generate_uuidv7()
    res = client.post(
        f"/participation/self/v1/applications/{fake_id}/withdrawals",
        json={"reason": "Retiro de prueba"},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "tenant_forbidden"


# =============================================================================
# 13. POST /participation/v1/applications/{application_id}/change-requests
# 14. POST /participation/self/v1/applications/{application_id}/resubmissions
# =============================================================================

def test_request_application_changes_success():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-reqch-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Submit application
    client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-reqch-{generate_uuidv7()}"},
    )

    # Reviewer requests changes
    cr_res = client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={
            "requested_changes": [
                "Adjuntar carta de respaldo de la facultad de agronomía",
                "Detallar el plan de monetización para pequeños agricultores",
            ],
            "deadline": "2026-11-25T23:59:59Z",
            "reviewer_notes": "Propuesta sólida en biotecnología. Subsanar requisitos documentarios.",
        },
    )
    assert cr_res.status_code == 200
    cr_data = cr_res.json()
    assert cr_data["application_id"] == app_id
    assert cr_data["state"] == "needs_changes"
    assert cr_data["revision"] == 3
    assert len(cr_data["requested_changes"]) == 2
    assert cr_data["deadline"] == "2026-11-25T23:59:59Z"

    # Outbox event check
    outbox = _get_outbox_events(client)
    cr_events = [e for e in outbox if e["topic"] == "participation.application.changes_requested.v1"]
    assert len(cr_events) >= 1


def test_patch_application_allowed_during_needs_changes_state():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-nc-patch-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Submit application
    client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-nc-{generate_uuidv7()}"},
    )

    # Reviewer requests changes
    cr = client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={"requested_changes": ["Clarificar tecnología"]},
    ).json()
    new_rev = cr["revision"]

    # Patch draft during needs_changes state is allowed
    patch_res = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={"answers": {"clarification": "Tecnología basada en espirulina"}, "expected_revision": new_rev},
    )
    assert patch_res.status_code == 200
    assert patch_res.json()["state"] == "needs_changes"
    assert patch_res.json()["answers"]["clarification"] == "Tecnología basada en espirulina"


def test_upload_file_allowed_during_needs_changes_state():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-nc-fup-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Submit and request changes
    client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-fup-{generate_uuidv7()}"},
    )
    client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={"requested_changes": ["Adjuntar aval"]},
    )

    # File upload during needs_changes state is allowed
    f_res = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "endorsement_letter",
            "file_name": "carta_aval_unas.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1024 * 120,
        },
    )
    assert f_res.status_code == 201
    assert f_res.json()["state"] == "ready"


def test_request_application_changes_allowed_on_resubmitted_application():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-resub-cr-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # 1. Submit
    client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-1-{generate_uuidv7()}"},
    )

    # 2. First change request
    cr1 = client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={"requested_changes": ["Paso 1"]},
    ).json()

    # 3. Resubmit
    client.post(
        f"/participation/self/v1/applications/{app_id}/resubmissions",
        json={"expected_revision": cr1["revision"]},
        headers={"Idempotency-Key": f"resub-1-{generate_uuidv7()}"},
    )

    # 4. Reviewer requests changes on 'resubmitted' state -> succeeds!
    cr2 = client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={"requested_changes": ["Paso 2"]},
    )
    assert cr2.status_code == 200
    assert cr2.json()["state"] == "needs_changes"


def test_request_application_changes_invalid_state():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-crinv-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # In 'draft' state, reviewer cannot request changes -> 409
    cr_res = client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={"requested_changes": ["Cambiar modelo"]},
    )
    assert cr_res.status_code == 409
    assert cr_res.json()["code"] == "invalid_state_transition"


def test_request_application_changes_not_found():
    client = _make_client()
    fake_id = generate_uuidv7()
    res = client.post(
        f"/participation/v1/applications/{fake_id}/change-requests",
        json={"requested_changes": ["Detalle"]},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "tenant_forbidden"


def test_resubmit_application_success_creates_revision_2():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-resub-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # 1. Submit Revision 1
    sub1 = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-r1-{generate_uuidv7()}"},
    )
    assert sub1.status_code == 200
    rev1_hash = sub1.json()["snapshot_hash"]

    # 2. Reviewer requests changes -> revision becomes 3
    client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={"requested_changes": ["Actualizar tracción"]},
    )

    # 3. Applicant updates draft answers -> revision becomes 4
    patch_res = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={"answers": {"traction": "10 hectáreas de cacao monitoreadas con éxito"}, "expected_revision": 3},
    )
    assert patch_res.status_code == 200
    latest_rev = patch_res.json()["revision"]
    assert latest_rev == 4

    # 4. Applicant resubmits application
    idem_key = f"resub-key-{generate_uuidv7()}"
    resub_res = client.post(
        f"/participation/self/v1/applications/{app_id}/resubmissions",
        json={"expected_revision": latest_rev},
        headers={"Idempotency-Key": idem_key},
    )
    assert resub_res.status_code == 200
    resub_data = resub_res.json()
    assert resub_data["application_id"] == app_id
    assert resub_data["state"] == "resubmitted"
    assert resub_data["current_revision_number"] == 2
    assert resub_data["revision"] == 5
    assert resub_data["snapshot_hash"] != rev1_hash

    # Idempotent replay of resubmission returns identical response
    replay_resub = client.post(
        f"/participation/self/v1/applications/{app_id}/resubmissions",
        json={"expected_revision": latest_rev},
        headers={"Idempotency-Key": idem_key},
    )
    assert replay_resub.status_code == 200
    assert replay_resub.json()["snapshot_hash"] == resub_data["snapshot_hash"]

    # Outbox event check
    outbox = _get_outbox_events(client)
    resub_events = [e for e in outbox if e["topic"] == "participation.application.resubmitted.v1"]
    assert len(resub_events) >= 1
    assert resub_events[0]["data"]["revision_number"] == 2


def test_resubmit_application_stale_revision():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-resstale-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Submit and request changes
    client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-{generate_uuidv7()}"},
    )
    client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={"requested_changes": ["Detalle"]},
    )

    # Server revision is 3, pass expected_revision=1 -> 412
    resub = client.post(
        f"/participation/self/v1/applications/{app_id}/resubmissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"resub-{generate_uuidv7()}"},
    )
    assert resub.status_code == 412
    assert resub.json()["code"] == "stale_revision"


def test_resubmit_application_invalid_state():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-resstate-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # In 'draft' state, cannot resubmit -> 409
    resub = client.post(
        f"/participation/self/v1/applications/{app_id}/resubmissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"resub-{generate_uuidv7()}"},
    )
    assert resub.status_code == 409
    assert resub.json()["code"] == "invalid_state_transition"


def test_resubmit_application_blocked_by_quarantined_file():
    client = _make_client()
    payload = _sample_venture_payload()
    res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"v-resbadf-{generate_uuidv7()}"},
    )
    app_id = res.json()["application_id"]

    # Submit & request changes
    client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"sub-{generate_uuidv7()}"},
    )
    cr = client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={"requested_changes": ["Subir nuevo archivo"]},
    ).json()
    new_rev = cr["revision"]

    # Applicant uploads suspicious file during changes window
    client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "patch",
            "file_name": "malicious.sh",
            "mime_type": "application/x-sh",
            "size_bytes": 1024,
        },
    )

    # Resubmission blocked -> 400
    resub = client.post(
        f"/participation/self/v1/applications/{app_id}/resubmissions",
        json={"expected_revision": new_rev},
        headers={"Idempotency-Key": f"resub-blocked-{generate_uuidv7()}"},
    )
    assert resub.status_code == 400
    assert resub.json()["code"] == "files_not_ready"


def test_resubmit_application_not_found():
    client = _make_client()
    fake_id = generate_uuidv7()
    res = client.post(
        f"/participation/self/v1/applications/{fake_id}/resubmissions",
        json={"expected_revision": 1},
        headers={"Idempotency-Key": f"resub-{generate_uuidv7()}"},
    )
    assert res.status_code == 404
    assert res.json()["code"] == "tenant_forbidden"


# =============================================================================
# 15. Comprehensive End-to-End Lifecycle & Invariants Audit
# =============================================================================

def test_full_venture_and_application_lifecycle_e2e():
    """
    Comprehensive E2E test exercising the full lifecycle:
    1. Create draft venture application with leader and 2 invitations.
    2. GET draft application and verify ETag and decrypted answers.
    3. Member 1 accepts invitation -> becomes active team member.
    4. Member 2 declines invitation.
    5. Leader invites Member 3 -> Member 3 accepts invitation.
    6. Upload clean pitch deck & clean canvas -> verify in files list.
    7. Update draft answers with optimistic locking revision increment.
    8. Submit application (creates Revision 1 immutable snapshot).
    9. Reviewer requests changes (state -> needs_changes).
    10. Leader updates draft answers to satisfy reviewer.
    11. Applicant resubmits (creates Revision 2 immutable snapshot).
    12. Reviewer requests second round of changes.
    13. Applicant resubmits (creates Revision 3 immutable snapshot).
    14. Applicant withdraws application with audit reason (state -> withdrawn).
    15. Verify all outbox domain events were produced and audit trail is intact.
    """
    client = _make_client()

    # Step 1: Create Venture Application
    payload = _sample_venture_payload(
        leader_email="ana.coordinadora@unas.edu.pe",
        leader_name="Dra. Ana Lopez",
        venture_name="BioFertil Amazónico",
        track="Biotecnología Aplicada",
        members=[
            {"email": "quimico1@unas.edu.pe", "role": "co_founder"},
            {"email": "agronomo1@unas.edu.pe", "role": "member"},
        ],
        answers={
            "summary": "Biofertilizante a partir de biomasa residual de palma y cacao",
            "readiness": "TRL 4",
        },
    )
    create_res = client.post(
        f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}/events/{DEFAULT_EVENT_SLUG}/applications",
        json=payload,
        headers={"Idempotency-Key": f"e2e-create-{generate_uuidv7()}"},
    )
    assert create_res.status_code == 201
    create_data = create_res.json()
    app_id = create_data["application_id"]
    venture_id = create_data["venture_id"]
    assert create_data["state"] == "draft"
    assert create_data["revision"] == 1
    token1 = create_data["invitations"][0]["token"]
    token2 = create_data["invitations"][1]["token"]

    # Step 2: GET Draft Application
    get_draft = client.get(f"/participation/self/v1/applications/{app_id}")
    assert get_draft.status_code == 200
    assert get_draft.headers["ETag"] == 'W/"1"'
    assert get_draft.json()["answers"]["summary"] == "Biofertilizante a partir de biomasa residual de palma y cacao"

    # Step 3: Member 1 accepts invitation
    acc1 = client.post(f"/participation/public/v1/team-invitations/{token1}/acceptance")
    assert acc1.status_code == 200
    assert acc1.json()["status"] == "accepted"

    # Step 4: Member 2 declines invitation
    dec2 = client.post(f"/participation/public/v1/team-invitations/{token2}/declination")
    assert dec2.status_code == 200
    assert dec2.json()["status"] == "declined"

    # Step 5: Leader invites Member 3 and Member 3 accepts
    inv3 = client.post(
        f"/participation/self/v1/ventures/{venture_id}/invitations",
        json={"email": "biologo3@unas.edu.pe", "role": "member"},
        headers={"Idempotency-Key": f"inv3-{generate_uuidv7()}"},
    )
    assert inv3.status_code == 201
    token3 = inv3.json()["token"]
    acc3 = client.post(f"/participation/public/v1/team-invitations/{token3}/acceptance")
    assert acc3.status_code == 200
    assert acc3.json()["status"] == "accepted"

    # Verify venture roster: 1 leader + 2 accepted members = 3 active members
    v_info = client.get(f"/participation/self/v1/ventures/{venture_id}").json()
    active_mems = [m for m in v_info["members"] if m["state"] == "active"]
    assert len(active_mems) == 3

    # Step 6: Upload files
    f1 = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "pitch_deck",
            "file_name": "BioFertil_Pitch.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1024 * 500,
        },
    )
    assert f1.status_code == 201
    assert f1.json()["state"] == "ready"

    f2 = client.post(
        f"/participation/self/v1/applications/{app_id}/file-uploads",
        json={
            "field_name": "lean_canvas",
            "file_name": "Canvas_BioFertil.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1024 * 250,
        },
    )
    assert f2.status_code == 201
    assert f2.json()["state"] == "ready"

    files_list = client.get(f"/participation/self/v1/applications/{app_id}/files").json()
    assert len(files_list["items"]) == 2

    # Step 7: Autosave / update draft answers (Revision 1 -> 2)
    patch_draft = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={
            "answers": {
                "summary": "Biofertilizante a partir de biomasa residual de palma y cacao",
                "readiness": "TRL 4 validado en laboratorio UNAS",
                "target_customers": "150 productores de Tingo María",
            },
            "expected_revision": 1,
        },
    )
    assert patch_draft.status_code == 200
    assert patch_draft.json()["revision"] == 2

    # Step 8: Submit Application (Revision 1 Snapshot created, Revision becomes 3)
    sub1 = client.post(
        f"/participation/self/v1/applications/{app_id}/submissions",
        json={"expected_revision": 2},
        headers={"Idempotency-Key": f"e2e-sub1-{generate_uuidv7()}"},
    )
    assert sub1.status_code == 200
    sub1_data = sub1.json()
    assert sub1_data["state"] == "submitted"
    assert sub1_data["current_revision_number"] == 1
    assert sub1_data["revision"] == 3
    rev1_hash = sub1_data["snapshot_hash"]

    # Step 9: Reviewer requests changes (Revision 3 -> 4)
    cr1 = client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={
            "requested_changes": ["Ampliar estructura de costos y pruebas de fitotoxicidad"],
            "deadline": "2026-11-30T18:00:00Z",
            "reviewer_notes": "Excelente iniciativa biotecnológica; subsanar costos.",
        },
    )
    assert cr1.status_code == 200
    assert cr1.json()["state"] == "needs_changes"
    assert cr1.json()["revision"] == 4

    # Step 10: Applicant updates draft answers (Revision 4 -> 5)
    patch2 = client.patch(
        f"/participation/self/v1/applications/{app_id}",
        json={
            "answers": {
                "summary": "Biofertilizante a partir de biomasa residual de palma y cacao",
                "readiness": "TRL 4 validado en laboratorio UNAS",
                "costs": "Costo por kilo: S/. 1.20, precio sugerido S/. 2.50",
            },
            "expected_revision": 4,
        },
    )
    assert patch2.status_code == 200
    assert patch2.json()["revision"] == 5

    # Step 11: Applicant resubmits (Revision 2 Snapshot created, Revision becomes 6)
    resub1 = client.post(
        f"/participation/self/v1/applications/{app_id}/resubmissions",
        json={"expected_revision": 5},
        headers={"Idempotency-Key": f"e2e-resub1-{generate_uuidv7()}"},
    )
    assert resub1.status_code == 200
    resub1_data = resub1.json()
    assert resub1_data["state"] == "resubmitted"
    assert resub1_data["current_revision_number"] == 2
    assert resub1_data["revision"] == 6
    rev2_hash = resub1_data["snapshot_hash"]
    assert rev2_hash != rev1_hash

    # Step 12: Reviewer requests second round of changes (Revision 6 -> 7)
    cr2 = client.post(
        f"/participation/v1/applications/{app_id}/change-requests",
        json={"requested_changes": ["Adjuntar permiso ambiental preliminar"]},
    )
    assert cr2.status_code == 200
    assert cr2.json()["state"] == "needs_changes"
    assert cr2.json()["revision"] == 7

    # Step 13: Resubmit second time (Revision 3 Snapshot created, Revision becomes 8)
    resub2 = client.post(
        f"/participation/self/v1/applications/{app_id}/resubmissions",
        json={"expected_revision": 7},
        headers={"Idempotency-Key": f"e2e-resub2-{generate_uuidv7()}"},
    )
    assert resub2.status_code == 200
    resub2_data = resub2.json()
    assert resub2_data["current_revision_number"] == 3
    assert resub2_data["revision"] == 8
    assert resub2_data["snapshot_hash"] != rev2_hash

    # Step 14: Applicant withdraws application (Revision 8 -> 9)
    w_res = client.post(
        f"/participation/self/v1/applications/{app_id}/withdrawals",
        json={"reason": "Financiamiento obtenido mediante subsidio internacional ProCiencia"},
    )
    assert w_res.status_code == 200
    assert w_res.json()["state"] == "withdrawn"
    assert w_res.json()["revision"] == 9

    # Step 15: Check Application final state & Revision snapshots in DB
    final_app = client.get(f"/participation/self/v1/applications/{app_id}").json()
    assert final_app["state"] == "withdrawn"
    assert final_app["revision"] == 9
    assert len(final_app["revisions"]) == 3  # Rev 1, Rev 2, Rev 3 snapshots stored!

    # Step 16: Verify Outbox Events
    outbox = _get_outbox_events(client)
    topics = [e["topic"] for e in outbox]
    assert "participation.team_invitation.changed.v1" in topics
    assert "participation.team_membership.changed.v1" in topics
    assert "participation.application_file.scanned.v1" in topics
    assert "participation.application.submitted.v1" in topics
    assert "participation.application.changes_requested.v1" in topics
    assert "participation.application.resubmitted.v1" in topics
    assert "participation.application.withdrawn.v1" in topics

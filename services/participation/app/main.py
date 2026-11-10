"""
FastAPI Entrypoint for Team 3 — Participation API.
Implements the canonical endpoints defined in contracts/API-CONTRACTS.md and TEAM-3-PARTICIPATION.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.core.problem import ProblemException, create_problem_response
from app.core.security import generate_uuidv7, security_manager
from app.db.database import (
    DEFAULT_EVENT_ID,
    DEFAULT_EVENT_SLUG,
    DEFAULT_SITE_SLUG,
    ParticipationDatabase,
)
from app.services.participation_service import ParticipationService


class AttendeeModel(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=160)
    email: str
    document_number: Optional[str] = ""
    phone: Optional[str] = ""
    institution: Optional[str] = "UNAS"


class RegistrationCreateRequest(BaseModel):
    form_version_id: str
    consent_version: str
    consent_accepted: bool
    attendee: AttendeeModel
    answers: Dict[str, Any]


class CancellationRequest(BaseModel):
    reason: str = Field(default="user_cancelled", min_length=2)


class VentureApplicationRequest(BaseModel):
    form_version_id: str
    consent_version: str
    venture_name: str
    track: str
    leader: AttendeeModel
    members: list[Dict[str, str]] = Field(default_factory=list)
    answers: Dict[str, Any] = Field(default_factory=dict)


class ApplicationSubmitRequest(BaseModel):
    expected_revision: int = 1


class VentureInviteRequest(BaseModel):
    email: str
    role: str = "member"


class ApplicationDraftUpdateRequest(BaseModel):
    answers: Dict[str, Any]
    expected_revision: int = 1


class FileUploadRequest(BaseModel):
    field_name: str
    file_name: str
    mime_type: str = "application/pdf"
    size_bytes: int
    sha256_hash: Optional[str] = None


class ApplicationWithdrawalRequest(BaseModel):
    reason: str = "applicant_withdrew"


class ApplicationChangeRequest(BaseModel):
    requested_changes: list[str]
    deadline: Optional[str] = None
    reviewer_notes: Optional[str] = None


class ApplicationResubmitRequest(BaseModel):
    expected_revision: int = 2


class RegistrationCorrectionRequest(BaseModel):
    reason: str = Field(..., min_length=3)
    updates: Dict[str, Any]
    actor: Optional[str] = "manager"


class ApplicationAdmissibilityDecisionRequest(BaseModel):
    expected_revision: int
    reason: str = Field(..., min_length=3)
    checklist: Optional[Dict[str, bool]] = None


class RegistrationImportRequest(BaseModel):
    rows: list[Dict[str, Any]]
    legal_basis: str = Field(..., min_length=3)
    dry_run: bool = False


class PrivacyRequestInput(BaseModel):
    email: str
    request_type: str = "erasure"
    legal_hold: bool = False
    reason: Optional[str] = None


class OperationsProjectionRequest(BaseModel):
    purpose: str = "certificate_issuance"
    recipient: str = "onsite_operations"
    confirmed_only: bool = True


class DemoResetRequest(BaseModel):
    capacity: int = Field(default=5, ge=1, le=500)
    waitlist_enabled: bool = True


class ConcurrencyBurstRequest(BaseModel):
    total_requests: int = Field(default=15, ge=1, le=100)


def create_app(db_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(
        title="Participation API — Event Platform Architecture (Team 3)",
        version="1.0.0",
        description="Microservicio de Inscripción Concurrente, Cupos Atómicos, Waitlist, Tickets QR JWS y Postulaciones.",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "ETag", "Cache-Control", "Retry-After"],
    )

    db = ParticipationDatabase(db_path=db_path)
    service = ParticipationService(db=db)
    app.state.db = db
    app.state.service = service

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        incoming_req_id = request.headers.get("X-Request-Id")
        request_id = incoming_req_id if incoming_req_id and len(incoming_req_id) >= 8 else generate_uuidv7()
        request.state.request_id = request_id
        response: Response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response

    @app.exception_handler(ProblemException)
    async def problem_exception_handler(request: Request, exc: ProblemException):
        return create_problem_response(request, exc)

    # -------------------------------------------------------------------------
    # Public Endpoints (PA-A)
    # -------------------------------------------------------------------------
    @app.get("/participation/public/v1/sites/{site}/events/{event}/form")
    def get_public_form(site: str, event: str, response: Response):
        data = service.get_public_form(site, event)
        response.headers["ETag"] = f'"{data["schema_hash"][:16]}"'
        response.headers["Cache-Control"] = "public, max-age=5"
        return data

    @app.post("/participation/public/v1/sites/{site}/events/{event}/registrations")
    def create_registration(
        site: str,
        event: str,
        body: RegistrationCreateRequest,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        status_code, receipt = service.create_registration(
            site_slug=site,
            event_slug=event,
            payload=body.model_dump(),
            idempotency_key=idempotency_key,
        )
        return JSONResponse(
            status_code=status_code,
            content=receipt,
            headers={"Cache-Control": "no-store", "ETag": f'"{receipt["revision"]}"'},
        )

    @app.post("/participation/public/v1/registration-access", status_code=202)
    def request_registration_access(
        payload: Dict[str, Any],
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        """PA-006: Always return neutral 202 response to prevent email enumeration."""
        return {
            "status": "accepted",
            "detail": "If an active registration matches the provided address, a single-use access link has been sent.",
        }

    # -------------------------------------------------------------------------
    # Participant Self-Service Endpoints (PA-A / PA-B)
    # -------------------------------------------------------------------------
    @app.get("/participation/self/v1/registrations/{registration_id}")
    def get_registration_self(registration_id: str, response: Response):
        data = service.get_registration_self(registration_id)
        response.headers["Cache-Control"] = "no-store"
        response.headers["ETag"] = f'"{data["revision"]}"'
        return data

    @app.post("/participation/self/v1/registrations/{registration_id}/cancellations")
    def cancel_registration(
        registration_id: str,
        body: CancellationRequest,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        result = service.cancel_registration(
            registration_id=registration_id,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
        return JSONResponse(status_code=200, content=result, headers={"Cache-Control": "no-store"})

    @app.get("/participation/self/v1/registrations/{registration_id}/ticket")
    def get_registration_ticket(registration_id: str, response: Response):
        data = service.get_registration_ticket(registration_id)
        response.headers["Cache-Control"] = "no-store"
        return data

    # -------------------------------------------------------------------------
    # Ventures & Applications Endpoints (PA-C)
    # -------------------------------------------------------------------------
    @app.post("/participation/public/v1/sites/{site}/events/{event}/applications", status_code=201)
    def create_venture_application(
        site: str,
        event: str,
        body: VentureApplicationRequest,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.create_venture_application(
            site_slug=site,
            event_slug=event,
            payload=body.model_dump(),
            idempotency_key=idempotency_key,
        )

    @app.post("/participation/self/v1/applications/{application_id}/submissions")
    def submit_application(
        application_id: str,
        body: ApplicationSubmitRequest,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.submit_application(
            application_id=application_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
        )

    @app.get("/participation/self/v1/ventures/{venture_id}")
    def get_venture(venture_id: str):
        return service.get_venture(venture_id)

    @app.post("/participation/self/v1/ventures/{venture_id}/invitations", status_code=201)
    def invite_team_member(
        venture_id: str,
        body: VentureInviteRequest,
        idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    ):
        return service.invite_team_member(
            venture_id=venture_id,
            email=body.email,
            role=body.role,
            idempotency_key=idempotency_key,
        )

    @app.post("/participation/public/v1/team-invitations/{token}/acceptance")
    def accept_invitation(token: str):
        return service.accept_invitation(token=token)

    @app.post("/participation/public/v1/team-invitations/{token}/declination")
    def decline_invitation(token: str):
        return service.decline_invitation(token=token)

    @app.post("/participation/self/v1/ventures/{venture_id}/members/{member_id}/leave")
    def leave_team_member(venture_id: str, member_id: str):
        return service.leave_team_member(venture_id=venture_id, member_id=member_id)

    @app.get("/participation/self/v1/applications/{application_id}")
    def get_application(application_id: str, response: Response):
        app_data = service.get_application(application_id)
        response.headers["ETag"] = app_data["etag"]
        return app_data

    @app.patch("/participation/self/v1/applications/{application_id}")
    def update_application_draft(
        application_id: str,
        body: ApplicationDraftUpdateRequest,
        response: Response,
        if_match: Optional[str] = Header(None, alias="If-Match"),
    ):
        expected_rev = body.expected_revision
        if if_match:
            clean_match = if_match.replace('W/"', '').replace('"', '').strip()
            if clean_match.isdigit():
                expected_rev = int(clean_match)
        res = service.update_application_draft(
            application_id=application_id,
            answers=body.answers,
            expected_revision=expected_rev,
        )
        response.headers["ETag"] = res["etag"]
        return res

    @app.post("/participation/self/v1/applications/{application_id}/file-uploads", status_code=201)
    def upload_application_file(application_id: str, body: FileUploadRequest):
        return service.upload_application_file(
            application_id=application_id,
            field_name=body.field_name,
            file_name=body.file_name,
            mime_type=body.mime_type,
            size_bytes=body.size_bytes,
            sha256_hash=body.sha256_hash,
        )

    @app.get("/participation/self/v1/applications/{application_id}/files")
    def list_application_files(application_id: str):
        return {"items": service.list_application_files(application_id)}

    @app.post("/participation/self/v1/applications/{application_id}/withdrawals")
    def withdraw_application(application_id: str, body: ApplicationWithdrawalRequest):
        return service.withdraw_application(application_id=application_id, reason=body.reason)

    @app.post("/participation/v1/applications/{application_id}/change-requests")
    def request_application_changes(application_id: str, body: ApplicationChangeRequest):
        return service.request_application_changes(
            application_id=application_id,
            requested_changes=body.requested_changes,
            deadline=body.deadline,
            reviewer_notes=body.reviewer_notes,
        )

    @app.post("/participation/self/v1/applications/{application_id}/resubmissions")
    def resubmit_application(
        application_id: str,
        body: ApplicationResubmitRequest,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
    ):
        return service.resubmit_application(
            application_id=application_id,
            expected_revision=body.expected_revision,
            idempotency_key=idempotency_key,
        )

    # -------------------------------------------------------------------------
    # Backoffice & Security Endpoints (PA-D / PA-E)
    # -------------------------------------------------------------------------
    @app.get("/participation/v1/events/{event_id}/registrations")
    def list_event_registrations(
        event_id: str,
        response: Response,
        state: Optional[str] = Query(default=None),
        email: Optional[str] = Query(default=None),
        reveal_pii: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=100),
        cursor: Optional[str] = Query(default=None),
        from_date: Optional[str] = Query(default=None),
        to_date: Optional[str] = Query(default=None),
    ):
        response.headers["Cache-Control"] = "no-store"
        return service.list_event_registrations(
            event_id=event_id,
            state=state,
            email_query=email,
            reveal_pii=reveal_pii,
            limit=limit,
            cursor=cursor,
            from_date=from_date,
            to_date=to_date,
        )

    @app.patch("/participation/v1/registrations/{registration_id}")
    def correct_registration_admin(
        registration_id: str,
        body: RegistrationCorrectionRequest,
    ):
        """PA-031: Manager corrects allowlisted administrative fields with audit logging."""
        return service.correct_registration_admin(
            registration_id=registration_id,
            reason=body.reason,
            updates=body.updates,
            actor=body.actor or "manager",
        )

    @app.get("/participation/v1/events/{event_id}/applications")
    def list_event_applications(
        event_id: str,
        state: Optional[str] = Query(default=None),
    ):
        """PA-032: Reviewer/Manager lists applications with venture info and status."""
        return {"items": service.list_event_applications(event_id=event_id, state=state)}

    @app.post("/participation/v1/applications/{application_id}/admissions")
    def admit_application(
        application_id: str,
        body: ApplicationAdmissibilityDecisionRequest,
    ):
        """PA-032: Reviewer evaluates and admits an application with checklist and reason."""
        return service.admit_application(
            application_id=application_id,
            expected_revision=body.expected_revision,
            reason=body.reason,
            checklist=body.checklist,
        )

    @app.post("/participation/v1/applications/{application_id}/rejections")
    def reject_application(
        application_id: str,
        body: ApplicationAdmissibilityDecisionRequest,
    ):
        """PA-032: Reviewer evaluates and rejects an application with checklist and reason."""
        return service.reject_application(
            application_id=application_id,
            expected_revision=body.expected_revision,
            reason=body.reason,
            checklist=body.checklist,
        )

    @app.post("/participation/v1/events/{event_id}/registration-imports")
    def import_registrations(
        event_id: str,
        body: RegistrationImportRequest,
    ):
        """PA-033: Controlled batch registration import with dry-run and capacity enforcement."""
        return service.import_registrations(
            event_id=event_id,
            rows=body.rows,
            legal_basis=body.legal_basis,
            dry_run=body.dry_run,
        )

    @app.post("/participation/v1/privacy-requests")
    def execute_privacy_request(
        body: PrivacyRequestInput,
    ):
        """PA-034: ARCO Privacy request with Legal Hold blocking and zero-PII receipt."""
        return service.execute_privacy_request(
            email=body.email,
            request_type=body.request_type,
            legal_hold=body.legal_hold,
            reason=body.reason,
        )

    @app.post("/participation/v1/events/{event_id}/operations-projections")
    def create_operations_projection(
        event_id: str,
        body: OperationsProjectionRequest,
    ):
        """PA-035: Purpose-bound operations projection with AES-GCM envelope."""
        return service.create_operations_projection(
            event_id=event_id,
            purpose=body.purpose,
            recipient=body.recipient,
            confirmed_only=body.confirmed_only,
        )

    @app.get("/.well-known/jwks.json")
    def get_jwks():
        return security_manager.get_public_jwks()

    @app.get("/participation/v1/outbox")
    def list_outbox_events(limit: int = Query(default=50, ge=1, le=200)):
        return {"items": service.list_outbox_events(limit=limit)}

    @app.get("/participation/v1/contracts/openapi.yaml")
    def download_openapi_contract():
        contract_path = Path(__file__).resolve().parents[1] / "contracts" / "openapi.yaml"
        return FileResponse(contract_path, media_type="application/yaml")

    # -------------------------------------------------------------------------
    # Classroom Live Demo Helpers (Reset Capacity & Concurrent Burst Simulation)
    # -------------------------------------------------------------------------
    @app.post("/participation/v1/demo/reset")
    def reset_demo_scenario(body: DemoResetRequest):
        db.reset_demo_state(capacity=body.capacity, waitlist_enabled=body.waitlist_enabled)
        return service.get_public_form(DEFAULT_SITE_SLUG, DEFAULT_EVENT_SLUG)

    @app.post("/participation/v1/demo/concurrency-burst")
    def trigger_concurrency_burst(body: ConcurrencyBurstRequest):
        return service.simulate_concurrent_burst(total_requests=body.total_requests)

    @app.post("/participation/v1/demo/ingest-release")
    def ingest_release_event(envelope: Dict[str, Any]):
        return service.ingest_release_published(envelope)

    @app.get("/healthz")
    def healthcheck():
        return {
            "status": "ok",
            "service": "participation",
            "team": "Team 3 — Participation",
            "default_event_id": DEFAULT_EVENT_ID,
        }

    web_dist = Path(__file__).resolve().parents[3] / "apps" / "participation-web" / "dist"
    if web_dist.exists():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="participation-web")

    return app


app = create_app()

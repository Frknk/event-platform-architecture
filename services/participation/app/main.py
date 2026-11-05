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
    ):
        response.headers["Cache-Control"] = "no-store"
        return service.list_event_registrations(
            event_id=event_id,
            state=state,
            email_query=email,
            reveal_pii=reveal_pii,
            limit=limit,
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

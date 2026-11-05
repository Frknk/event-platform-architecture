"""
RFC 9457 Problem Details error handling for Participation API (API-CONTRACTS.md).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import Request
from fastapi.responses import JSONResponse
from app.core.security import generate_uuidv7


class ProblemException(Exception):
    """Canonical domain error mapped to application/problem+json."""

    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        errors: Optional[List[Dict[str, Any]]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.errors = errors or []
        self.headers = headers or {}


def create_problem_response(request: Request, exc: ProblemException) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or generate_uuidv7()
    slug = exc.code.replace("_", "-")
    body = {
        "type": f"https://errors.example.pe/{slug}",
        "title": exc.title,
        "status": exc.status,
        "code": exc.code,
        "detail": exc.detail,
        "request_id": request_id,
        "errors": exc.errors,
    }
    headers = {"X-Request-Id": request_id, "Cache-Control": "no-store", **exc.headers}
    return JSONResponse(
        status_code=exc.status,
        content=body,
        media_type="application/problem+json",
        headers=headers,
    )

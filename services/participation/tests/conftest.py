"""Shared pytest fixtures for participation service tests."""

from __future__ import annotations

from pathlib import Path
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """Provide a TestClient backed by an isolated, auto-cleaned SQLite database."""
    db_path = str(tmp_path / "participation_test.db")
    app = create_app(db_path=db_path)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.state.db.close()

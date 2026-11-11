from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import sys
import uuid

from app.db.database import ParticipationDatabase
from app.services.event_delivery_service import OutboxDeliveryService


def _load_transport(specification: str):
    module_name, separator, factory_name = specification.partition(":")
    if not separator or not module_name or not factory_name:
        raise ValueError("Transport must use the 'module:factory' format.")
    factory = getattr(importlib.import_module(module_name), factory_name)
    transport = factory()
    if not callable(getattr(transport, "publish", None)):
        raise TypeError("Configured transport must implement publish(envelope, headers=...).")
    return transport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Process Participation transactional outbox.")
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        uuid.UUID(args.organization_id)
    except ValueError:
        parser.error("--organization-id must be a UUID.")

    transport_spec = os.environ.get("PARTICIPATION_EVENT_TRANSPORT")
    if not transport_spec:
        print(
            "Blocked: set PARTICIPATION_EVENT_TRANSPORT=module:factory to configure "
            "a real transport. No rows were claimed or changed.",
            file=sys.stderr,
        )
        return 2
    database_url = os.environ.get("DATABASE_URL") or os.environ.get("PARTICIPATION_DB_URL")
    sqlite_path = os.environ.get("PARTICIPATION_SQLITE_PATH")
    if not database_url and not sqlite_path:
        print(
            "Blocked: configure DATABASE_URL, PARTICIPATION_DB_URL, or "
            "PARTICIPATION_SQLITE_PATH. No database was opened.",
            file=sys.stderr,
        )
        return 2
    if not database_url and sqlite_path and not Path(sqlite_path).is_file():
        print(
            "Blocked: the configured SQLite database file does not exist. "
            "The worker never creates or migrates databases.",
            file=sys.stderr,
        )
        return 2

    transport = _load_transport(transport_spec)
    db = ParticipationDatabase(
        db_url=database_url,
        db_path=sqlite_path,
        initialize=False,
    )
    try:
        result = OutboxDeliveryService(db, transport).process_once(
            organization_id=args.organization_id,
            limit=args.limit,
        )
        print(json.dumps(result, sort_keys=True))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

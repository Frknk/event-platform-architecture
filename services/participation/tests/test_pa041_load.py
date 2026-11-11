from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import platform
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import generate_uuidv7
from app.db.database import (
    DEFAULT_CONSENT_VERSION,
    DEFAULT_EVENT_ID,
    DEFAULT_EVENT_SLUG,
    DEFAULT_FORM_VERSION_ID,
    DEFAULT_SITE_SLUG,
)
from app.main import create_app
from app.services.abuse_protection import DEVICE_HEADER


BASE_PEAK_ASSUMPTION = 10
LOAD_MULTIPLIER = 3
REGISTRATION_COUNT = BASE_PEAK_ASSUMPTION * LOAD_MULTIPLIER
REGISTRATION_WORKERS = REGISTRATION_COUNT
CANCEL_COUNT = 5


def _registration_payload(index: int) -> dict:
    return {
        "form_version_id": DEFAULT_FORM_VERSION_ID,
        "consent_version": DEFAULT_CONSENT_VERSION,
        "consent_accepted": True,
        "attendee": {
            "full_name": f"Load Participant {index:03d}",
            "email": f"pa041-{index:03d}@load-test.invalid",
            "document_number": f"TEST{index:08d}",
            "institution": "Synthetic load test",
        },
        "answers": {
            "track": "Load test",
            "role_profile": "Synthetic",
            "modality": "Online",
        },
    }


def _parallel_requests(client, count: int, workers: int, make_request):
    barrier = threading.Barrier(workers)

    def run(index: int) -> dict:
        try:
            barrier.wait(timeout=30)
            started = time.perf_counter()
            response = make_request(index)
            return {
                "status": response.status_code,
                "latency_ms": (time.perf_counter() - started) * 1000,
                "body": response.json(),
            }
        except Exception as exc:
            return {
                "status": 0,
                "latency_ms": 0.0,
                "error_type": type(exc).__name__,
            }

    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(run, range(count)))


def _latency_summary(results: list[dict]) -> dict[str, float]:
    samples = sorted(result["latency_ms"] for result in results)
    p95_index = max(0, math.ceil(0.95 * len(samples)) - 1)
    return {
        "min_ms": round(samples[0], 3),
        "median_ms": round(samples[len(samples) // 2], 3),
        "p95_ms_nearest_rank": round(samples[p95_index], 3),
        "max_ms": round(samples[-1], 3),
    }


def _phase_summary(results: list[dict], elapsed_seconds: float) -> dict:
    return {
        "requests": len(results),
        "workers": len(results),
        "elapsed_seconds": round(elapsed_seconds, 6),
        "throughput_requests_per_second": round(len(results) / elapsed_seconds, 3),
        "latency": _latency_summary(results),
        "status_counts": {
            str(status): sum(result["status"] == status for result in results)
            for status in sorted({result["status"] for result in results})
        },
    }


def test_pa041_3x_peak_registration_waitlist_and_concurrent_promotion(monkeypatch):
    monkeypatch.setenv("PARTICIPATION_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("PARTICIPATION_RATE_LIMIT_IP_MAX", "90")
    monkeypatch.setenv("PARTICIPATION_RATE_LIMIT_EVENT_MAX", "90")
    monkeypatch.setenv("PARTICIPATION_RATE_LIMIT_DEVICE_MAX", "90")
    monkeypatch.setenv("PARTICIPATION_RATE_LIMIT_IP_CHALLENGE_AFTER", "60")
    monkeypatch.setenv("PARTICIPATION_RATE_LIMIT_EVENT_CHALLENGE_AFTER", "60")
    monkeypatch.setenv("PARTICIPATION_RATE_LIMIT_DEVICE_CHALLENGE_AFTER", "60")

    with tempfile.TemporaryDirectory(prefix="pa041-") as temp_dir:
        db_path = str(Path(temp_dir) / "participation-load-test.sqlite")
        app = create_app(db_path=db_path)
        client = TestClient(app)
        try:
            reset = client.post(
                "/participation/v1/demo/reset",
                json={"capacity": BASE_PEAK_ASSUMPTION, "waitlist_enabled": True},
            )
            assert reset.status_code == 200

            registration_url = (
                f"/participation/public/v1/sites/{DEFAULT_SITE_SLUG}"
                f"/events/{DEFAULT_EVENT_SLUG}/registrations"
            )
            device_tokens = [
                app.state.abuse_protection.issue_device_token()[1]
                for _ in range(REGISTRATION_COUNT)
            ]
            registration_started = time.perf_counter()
            registration_results = _parallel_requests(
                client,
                REGISTRATION_COUNT,
                REGISTRATION_WORKERS,
                lambda index: client.post(
                    registration_url,
                    json=_registration_payload(index),
                    headers={
                        "Idempotency-Key": f"pa041-register-{index:03d}-{generate_uuidv7()}",
                        DEVICE_HEADER: device_tokens[index],
                    },
                ),
            )
            registration_elapsed = time.perf_counter() - registration_started
            registration_errors = [
                result for result in registration_results if result["status"] != 201
            ]
            assert not registration_errors, [
                {key: value for key, value in result.items() if key != "body"}
                for result in registration_errors
            ]
            receipts = [result["body"] for result in registration_results]
            confirmed_receipts = [
                receipt for receipt in receipts if receipt["state"] == "confirmed"
            ]
            waitlisted_receipts = [
                receipt for receipt in receipts if receipt["state"] == "waitlisted"
            ]
            assert len(confirmed_receipts) == BASE_PEAK_ASSUMPTION
            assert len(waitlisted_receipts) == REGISTRATION_COUNT - BASE_PEAK_ASSUMPTION
            assert sorted(
                receipt["waitlist_position"] for receipt in waitlisted_receipts
            ) == list(range(1, len(waitlisted_receipts) + 1))

            confirmed_ids = [receipt["id"] for receipt in confirmed_receipts[:CANCEL_COUNT]]
            cancel_started = time.perf_counter()
            cancel_results = _parallel_requests(
                client,
                CANCEL_COUNT,
                CANCEL_COUNT,
                lambda index: client.post(
                    f"/participation/self/v1/registrations/{confirmed_ids[index]}/cancellations",
                    json={"reason": "PA-041 synthetic load test"},
                    headers={
                        "Idempotency-Key": f"pa041-cancel-{index:03d}-{generate_uuidv7()}"
                    },
                ),
            )
            cancel_elapsed = time.perf_counter() - cancel_started
            cancel_errors = [
                result for result in cancel_results if result["status"] != 200
            ]
            assert not cancel_errors, [
                {key: value for key, value in result.items() if key != "body"}
                for result in cancel_errors
            ]
            cancel_bodies = [result["body"] for result in cancel_results]
            assert all(body["ticket_revoked"] for body in cancel_bodies)

            promoted_ids = {
                body["promoted_registration_id"] for body in cancel_bodies
            }
            expected_promoted_ids = {
                receipt["id"]
                for receipt in sorted(
                    waitlisted_receipts,
                    key=lambda item: item["waitlist_position"],
                )[:CANCEL_COUNT]
            }
            assert promoted_ids == expected_promoted_ids

            with app.state.db.read_cursor() as cur:
                cur.execute(
                    """
                    SELECT capacity, confirmed_count, waitlist_count
                    FROM capacity_counters WHERE event_id = ?
                    """,
                    (DEFAULT_EVENT_ID,),
                )
                counters = dict(cur.fetchone())
                cur.execute(
                    """
                    SELECT state, COUNT(*) AS count
                    FROM registrations WHERE event_id = ?
                    GROUP BY state
                    """,
                    (DEFAULT_EVENT_ID,),
                )
                registration_states = {
                    row["state"]: int(row["count"]) for row in cur.fetchall()
                }
                cur.execute(
                    """
                    SELECT registration_id,
                           COUNT(*) AS ticket_count,
                           SUM(CASE WHEN state = 'active' THEN 1 ELSE 0 END) AS active_count
                    FROM tickets
                    WHERE event_id = ?
                    GROUP BY registration_id
                    """,
                    (DEFAULT_EVENT_ID,),
                )
                ticket_counts = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                    SELECT COUNT(*) AS count, COUNT(DISTINCT jti) AS unique_jtis
                    FROM tickets WHERE event_id = ?
                    """,
                    (DEFAULT_EVENT_ID,),
                )
                ticket_uniqueness = dict(cur.fetchone())
                cur.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM tickets t
                    JOIN registrations r ON r.id = t.registration_id
                    WHERE t.event_id = ? AND t.state = 'active' AND r.state <> 'confirmed'
                    """,
                    (DEFAULT_EVENT_ID,),
                )
                active_ticket_state_mismatches = int(cur.fetchone()["count"])

            assert counters["confirmed_count"] == counters["capacity"] == BASE_PEAK_ASSUMPTION
            assert counters["waitlist_count"] == len(waitlisted_receipts) - CANCEL_COUNT
            assert registration_states.get("confirmed") == BASE_PEAK_ASSUMPTION
            assert registration_states.get("cancelled") == CANCEL_COUNT
            assert registration_states.get("waitlisted") == len(waitlisted_receipts) - CANCEL_COUNT
            assert all(int(row["active_count"]) <= 1 for row in ticket_counts)
            assert int(ticket_uniqueness["count"]) == int(ticket_uniqueness["unique_jtis"])
            assert active_ticket_state_mismatches == 0
            active_ticket_count = sum(int(row["active_count"]) for row in ticket_counts)
            assert active_ticket_count == BASE_PEAK_ASSUMPTION

            all_results = registration_results + cancel_results
            total_elapsed = registration_elapsed + cancel_elapsed
            rate_limit_metrics = app.state.abuse_protection.metrics_snapshot()
            assert rate_limit_metrics.get("allowed:request") == REGISTRATION_COUNT
            assert not any(key.startswith("challenged:") for key in rate_limit_metrics)

            report = {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "environment": {
                    "python": platform.python_version(),
                    "database": "SQLite temporary file",
                    "sqlite": sqlite3.sqlite_version,
                    "app_transport": "FastAPI TestClient / in-process ASGI",
                    "database_url_configured": False,
                },
                "profile": {
                    "base_peak_assumption_concurrent_registrations": BASE_PEAK_ASSUMPTION,
                    "base_peak_is_measured_demand": False,
                    "load_multiplier": LOAD_MULTIPLIER,
                    "registration_requests": REGISTRATION_COUNT,
                    "registration_workers": REGISTRATION_WORKERS,
                    "capacity": BASE_PEAK_ASSUMPTION,
                    "concurrent_cancellations": CANCEL_COUNT,
                    "total_business_requests": len(all_results),
                },
                "registration_phase": _phase_summary(
                    registration_results, registration_elapsed
                ),
                "cancellation_phase": _phase_summary(cancel_results, cancel_elapsed),
                "combined": {
                    "elapsed_seconds_sum_of_phases": round(total_elapsed, 6),
                    "throughput_requests_per_second": round(
                        len(all_results) / total_elapsed, 3
                    ),
                    "latency": _latency_summary(all_results),
                    "failed_requests": sum(
                        result["status"] not in (201, 200) for result in all_results
                    ),
                },
                "invariants": {
                    "capacity": counters["capacity"],
                    "confirmed": counters["confirmed_count"],
                    "waitlist_remaining": counters["waitlist_count"],
                    "cancelled": registration_states.get("cancelled", 0),
                    "promoted_ids_unique": len(promoted_ids),
                    "active_tickets": active_ticket_count,
                    "ticket_records": int(ticket_uniqueness["count"]),
                    "ticket_jtis_unique": (
                        int(ticket_uniqueness["count"])
                        == int(ticket_uniqueness["unique_jtis"])
                    ),
                    "max_active_tickets_per_registration": max(
                        int(row["active_count"]) for row in ticket_counts
                    ),
                    "active_tickets_match_confirmed_state": (
                        active_ticket_state_mismatches == 0
                    ),
                },
                "pa040": {
                    "finite_test_only_limits": {
                        "ip_per_window": 90,
                        "event_per_window": 90,
                        "device_per_window": 90,
                        "challenge_after": 60,
                    },
                    "registration_requests_allowed": rate_limit_metrics.get(
                        "allowed:request", 0
                    ),
                    "challenge_requests": rate_limit_metrics.get(
                        "challenged:request", 0
                    ),
                },
                "latency_method": "Nearest-rank p95: sorted latencies, element ceil(0.95*N), one-based.",
            }
            print("PA041_RESULT_JSON=" + json.dumps(report, sort_keys=True))
        finally:
            client.close()
            app.state.db.close()

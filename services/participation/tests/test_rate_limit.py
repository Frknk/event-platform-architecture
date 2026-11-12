from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import ipaddress
import json
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.services.abuse_protection import AbuseProtection, RateLimitConfig


REGISTRATION_PATH = (
    "/participation/public/v1/sites/test-site/events/test-event/registrations"
)


def _client(monkeypatch, **settings) -> TestClient:
    defaults = {
        "PARTICIPATION_RATE_LIMIT_IP_MAX": "20",
        "PARTICIPATION_RATE_LIMIT_EVENT_MAX": "20",
        "PARTICIPATION_RATE_LIMIT_DEVICE_MAX": "20",
        "PARTICIPATION_RATE_LIMIT_IP_CHALLENGE_AFTER": "100",
        "PARTICIPATION_RATE_LIMIT_EVENT_CHALLENGE_AFTER": "100",
        "PARTICIPATION_RATE_LIMIT_DEVICE_CHALLENGE_AFTER": "100",
        "PARTICIPATION_ANTIBOT_CHALLENGE_DIFFICULTY": "8",
    }
    defaults.update({key: str(value) for key, value in settings.items()})
    for key, value in defaults.items():
        monkeypatch.setenv(key, value)
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    return TestClient(create_app(db_path=tmp.name))


def _request(client: TestClient, headers: dict | None = None):
    return client.post(REGISTRATION_PATH, json={}, headers=headers or {})


def _solve(token: str, difficulty: int) -> str:
    encoded_payload = token.rsplit(".", 1)[0]
    padded = encoded_payload + "=" * (-len(encoded_payload) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    challenge_id = payload["id"]
    for nonce in range(10_000_000):
        digest = hashlib.sha256(f"{challenge_id}:{nonce}".encode("ascii")).digest()
        full_bytes, remaining_bits = divmod(difficulty, 8)
        if any(digest[index] for index in range(full_bytes)):
            continue
        if remaining_bits and digest[full_bytes] >> (8 - remaining_bits):
            continue
        return str(nonce)
    raise AssertionError("Could not solve the configured test challenge.")


def _invalid_proof(token: str, difficulty: int) -> str:
    encoded_payload = token.rsplit(".", 1)[0]
    padded = encoded_payload + "=" * (-len(encoded_payload) % 4)
    challenge_id = json.loads(base64.urlsafe_b64decode(padded))["id"]
    for nonce in range(10_000):
        digest = hashlib.sha256(f"{challenge_id}:{nonce}".encode("ascii")).digest()
        full_bytes, remaining_bits = divmod(difficulty, 8)
        if any(digest[index] for index in range(full_bytes)):
            return str(nonce)
        if remaining_bits and digest[full_bytes] >> (8 - remaining_bits):
            return str(nonce)
    raise AssertionError("Could not find a deterministic invalid proof.")


def test_ip_rate_limit_returns_429_and_retry_after_and_ignores_forwarded_ip(monkeypatch):
    client = _client(monkeypatch, PARTICIPATION_RATE_LIMIT_IP_MAX=1)

    first = _request(client, {"X-Forwarded-For": "198.51.100.10"})
    assert first.status_code == 422

    second = _request(client, {"X-Forwarded-For": "198.51.100.11"})
    assert second.status_code == 429
    assert 1 <= int(second.headers["Retry-After"]) <= 60
    assert second.json()["code"] == "rate_limit_exceeded"


def test_first_tokenless_request_is_counted_against_the_issued_device(monkeypatch):
    client = _client(
        monkeypatch,
        PARTICIPATION_RATE_LIMIT_DEVICE_MAX=1,
        PARTICIPATION_RATE_LIMIT_IP_MAX=100,
        PARTICIPATION_RATE_LIMIT_EVENT_MAX=100,
    )

    first = _request(client)
    assert first.status_code == 422
    device = first.headers["X-Participation-Device"]
    assert device

    second = _request(client, {"X-Participation-Device": device})
    assert second.status_code == 429
    assert second.json()["code"] == "rate_limit_exceeded"


def test_retry_after_matches_sliding_window_expiry():
    guard = AbuseProtection(
        RateLimitConfig(
            window_seconds=10,
            ip_limit=1,
            event_limit=10,
            device_limit=10,
            ip_challenge_after=10,
            event_challenge_after=10,
            device_challenge_after=10,
        )
    )
    with patch("app.services.abuse_protection.time.monotonic", side_effect=[100.0, 103.0]):
        assert guard.check_request(
            peer_ip="192.0.2.1", event_key=None, device_id=None
        ).allowed
        limited = guard.check_request(
            peer_ip="192.0.2.1", event_key=None, device_id=None
        )
    assert limited.retry_after == 7


def test_rate_limits_are_separately_enforced_for_event_and_signed_device():
    event_guard = AbuseProtection(
        RateLimitConfig(
            ip_limit=10,
            event_limit=1,
            device_limit=10,
            ip_challenge_after=10,
            event_challenge_after=10,
            device_challenge_after=10,
        )
    )
    assert event_guard.check_request(
        peer_ip="192.0.2.1", event_key="event-a", device_id=None
    ).allowed
    event_limited = event_guard.check_request(
        peer_ip="192.0.2.2", event_key="event-a", device_id=None
    )
    assert not event_limited.allowed
    assert event_limited.limited_dimensions == ("event",)

    device_guard = AbuseProtection(
        RateLimitConfig(
            ip_limit=10,
            event_limit=10,
            device_limit=1,
            ip_challenge_after=10,
            event_challenge_after=10,
            device_challenge_after=10,
        )
    )
    device_id, token = device_guard.issue_device_token()
    assert device_guard.verify_device_token(token) == device_id
    assert device_guard.check_request(
        peer_ip="192.0.2.1", event_key=None, device_id=device_id
    ).allowed
    device_limited = device_guard.check_request(
        peer_ip="192.0.2.2", event_key=None, device_id=device_id
    )
    assert not device_limited.allowed
    assert device_limited.limited_dimensions == ("device",)
    assert device_guard.verify_device_token("client-chosen-device-id") is None


def test_saturated_ip_does_not_create_windows_for_later_dimensions():
    guard = AbuseProtection(
        RateLimitConfig(
            ip_limit=1,
            event_limit=100,
            device_limit=100,
            ip_challenge_after=100,
            event_challenge_after=100,
            device_challenge_after=100,
        )
    )
    with patch("app.services.abuse_protection.time.monotonic", return_value=100.0):
        assert guard.check_request(
            peer_ip="192.0.2.7", event_key="event-a", device_id="a" * 32
        ).allowed
        limited = guard.check_request(
            peer_ip="192.0.2.7", event_key="event-b", device_id="b" * 32
        )

    assert limited.limited_dimensions == ("ip",)
    # The rejected request must not allocate windows for the attacker-chosen
    # event key or the discarded device token.
    tracked = sorted(name for name, _ in guard._windows)
    assert tracked == ["device", "event", "ip"]
    assert ("event", guard._fingerprint("event", "event-b")) not in guard._windows
    assert ("device", guard._fingerprint("device", "b" * 32)) not in guard._windows


def test_concurrent_requests_cannot_exceed_limit():
    guard = AbuseProtection(
        RateLimitConfig(
            ip_limit=3,
            event_limit=20,
            device_limit=20,
            ip_challenge_after=20,
            event_challenge_after=20,
            device_challenge_after=20,
        )
    )

    def attempt(_index: int):
        return guard.check_request(
            peer_ip="192.0.2.10",
            event_key="event",
            device_id=None,
        ).allowed

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(attempt, range(12)))
    assert sum(results) == 3


def test_challenge_is_required_valid_and_single_use(monkeypatch):
    client = _client(
        monkeypatch,
        PARTICIPATION_RATE_LIMIT_IP_CHALLENGE_AFTER=1,
        PARTICIPATION_RATE_LIMIT_IP_MAX=10,
    )
    assert _request(client).status_code == 422
    first_challenge = _request(client)
    assert first_challenge.status_code == 428
    challenge = first_challenge.json()["challenge"]
    device = first_challenge.headers["X-Participation-Device"]
    proof = _solve(challenge["token"], challenge["difficulty"])
    headers = {
        "X-Participation-Device": device,
        "X-Participation-Challenge": challenge["token"],
        "X-Participation-Proof": proof,
    }

    accepted = _request(client, headers)
    assert accepted.status_code == 422

    reused = _request(client, headers)
    assert reused.status_code == 428
    assert reused.json()["code"] == "challenge_reused"


def test_invalid_challenge_is_rejected(monkeypatch):
    client = _client(
        monkeypatch,
        PARTICIPATION_RATE_LIMIT_IP_CHALLENGE_AFTER=1,
        PARTICIPATION_RATE_LIMIT_IP_MAX=10,
    )
    assert _request(client).status_code == 422
    challenge_response = _request(client)
    challenge = challenge_response.json()["challenge"]
    result = _request(
        client,
        {
            "X-Participation-Device": challenge_response.headers[
                "X-Participation-Device"
            ],
            "X-Participation-Challenge": challenge["token"],
            "X-Participation-Proof": _invalid_proof(
                challenge["token"], challenge["difficulty"]
            ),
        },
    )
    assert result.status_code == 428
    assert result.json()["code"] == "challenge_invalid"


def test_allowlist_bypasses_only_rate_checks_and_metrics_have_no_identifiers(monkeypatch):
    guard = AbuseProtection(
        RateLimitConfig(
            ip_limit=1,
            event_limit=1,
            device_limit=1,
            ip_challenge_after=0,
            event_challenge_after=0,
            device_challenge_after=0,
            allowlist=(ipaddress.ip_network("192.0.2.0/24"),),
        )
    )
    for _ in range(3):
        assert guard.check_request(
            peer_ip="192.0.2.8",
            event_key="sensitive-event",
            device_id="opaque-device-identifier",
        ).allowed

    metrics = guard.metrics_snapshot()
    assert metrics == {"allowed:allowlist": 3}
    assert all(
        value not in repr(metrics)
        for value in ("192.0.2.8", "sensitive-event", "opaque-device-identifier")
    )
    assert not guard.protects("POST", "/participation/v1/privacy-requests")


def test_allowlist_is_loaded_from_operational_configuration(monkeypatch):
    monkeypatch.setenv("PARTICIPATION_RATE_LIMIT_ALLOWLIST", "192.0.2.0/24,198.51.100.7")
    guard = AbuseProtection.from_env()
    assert guard.peer_is_allowlisted("192.0.2.44")
    assert guard.peer_is_allowlisted("198.51.100.7")
    assert not guard.peer_is_allowlisted("198.51.100.8")


def test_metrics_count_allow_limited_and_challenged_without_identifiers():
    guard = AbuseProtection(
        RateLimitConfig(
            ip_limit=2,
            event_limit=10,
            device_limit=10,
            ip_challenge_after=1,
            event_challenge_after=10,
            device_challenge_after=10,
        )
    )
    allowed = guard.check_request(
        peer_ip="203.0.113.19",
        event_key="participant-event",
        device_id="opaque-device-id",
    )
    assert allowed.allowed and not allowed.challenge_required
    challenged = guard.check_request(
        peer_ip="203.0.113.19",
        event_key="participant-event",
        device_id="opaque-device-id",
    )
    assert challenged.challenge_required
    guard.record_challenged()
    limited = guard.check_request(
        peer_ip="203.0.113.19",
        event_key="participant-event",
        device_id="opaque-device-id",
    )
    assert not limited.allowed

    metrics = guard.metrics_snapshot()
    assert metrics == {
        "allowed:request": 1,
        "challenged:request": 1,
        "limited:ip": 1,
    }
    assert all(
        value not in repr(metrics)
        for value in ("203.0.113.19", "participant-event", "opaque-device-id")
    )

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import uuid
from typing import Optional


DEVICE_HEADER = "X-Participation-Device"
CHALLENGE_HEADER = "X-Participation-Challenge"
PROOF_HEADER = "X-Participation-Proof"
_PROTECTED_PATHS = (
    re.compile(r"^/participation/public/v1/sites/[^/]+/events/[^/]+/registrations$"),
    re.compile(r"^/participation/public/v1/sites/[^/]+/events/[^/]+/applications$"),
    re.compile(r"^/participation/public/v1/registration-access$"),
    re.compile(r"^/participation/public/v1/team-invitations/[^/]+/(?:acceptance|declination)$"),
)
_EVENT_PATH = re.compile(r"/events/([^/]+)/(?:registrations|applications)$")
_MAX_TRACKED_KEYS = 50_000


@dataclass(frozen=True)
class RateLimitConfig:
    window_seconds: int = 60
    ip_limit: int = 60
    event_limit: int = 300
    device_limit: int = 60
    ip_challenge_after: int = 30
    event_challenge_after: int = 100
    device_challenge_after: int = 30
    challenge_difficulty: int = 16
    challenge_ttl_seconds: int = 180
    allowlist: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()

    @classmethod
    def from_env(cls) -> "RateLimitConfig":
        def positive_int(
            name: str,
            default: int,
            *,
            minimum: int = 1,
            maximum: Optional[int] = None,
        ) -> int:
            raw = os.environ.get(name)
            if raw is None:
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer.") from exc
            if value < minimum or (maximum is not None and value > maximum):
                bounds = f"between {minimum} and {maximum}" if maximum else f"at least {minimum}"
                raise ValueError(f"{name} must be {bounds}.")
            return value

        allowlist_raw = os.environ.get("PARTICIPATION_RATE_LIMIT_ALLOWLIST", "")
        try:
            allowlist = tuple(
                ipaddress.ip_network(value.strip(), strict=False)
                for value in allowlist_raw.split(",")
                if value.strip()
            )
        except ValueError as exc:
            raise ValueError("PARTICIPATION_RATE_LIMIT_ALLOWLIST contains an invalid CIDR.") from exc

        return cls(
            window_seconds=positive_int("PARTICIPATION_RATE_LIMIT_WINDOW_SECONDS", 60),
            ip_limit=positive_int("PARTICIPATION_RATE_LIMIT_IP_MAX", 60),
            event_limit=positive_int("PARTICIPATION_RATE_LIMIT_EVENT_MAX", 300),
            device_limit=positive_int("PARTICIPATION_RATE_LIMIT_DEVICE_MAX", 60),
            ip_challenge_after=positive_int(
                "PARTICIPATION_RATE_LIMIT_IP_CHALLENGE_AFTER", 30, minimum=0
            ),
            event_challenge_after=positive_int(
                "PARTICIPATION_RATE_LIMIT_EVENT_CHALLENGE_AFTER", 100, minimum=0
            ),
            device_challenge_after=positive_int(
                "PARTICIPATION_RATE_LIMIT_DEVICE_CHALLENGE_AFTER", 30, minimum=0
            ),
            challenge_difficulty=positive_int(
                "PARTICIPATION_ANTIBOT_CHALLENGE_DIFFICULTY",
                16,
                minimum=1,
                maximum=32,
            ),
            challenge_ttl_seconds=positive_int(
                "PARTICIPATION_ANTIBOT_CHALLENGE_TTL_SECONDS", 180
            ),
            allowlist=allowlist,
        )


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: Optional[int] = None
    limited_dimensions: tuple[str, ...] = ()
    challenge_required: bool = False


class AbuseProtection:
    """Process-local rate limiting, signed browser tokens, and one-use PoW challenges."""

    def __init__(self, config: RateLimitConfig, *, secret: Optional[bytes] = None) -> None:
        self.config = config
        self._secret = secret or secrets.token_bytes(32)
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, str], deque[float]] = {}
        self._consumed_challenges: dict[str, float] = {}
        self._metrics: Counter[tuple[str, str]] = Counter()
        self._last_cleanup = 0.0

    @classmethod
    def from_env(cls) -> "AbuseProtection":
        configured_secret = os.environ.get("PARTICIPATION_ANTIBOT_SECRET")
        if configured_secret is not None and len(configured_secret.encode("utf-8")) < 32:
            raise ValueError("PARTICIPATION_ANTIBOT_SECRET must contain at least 32 bytes.")
        return cls(
            RateLimitConfig.from_env(),
            secret=configured_secret.encode("utf-8") if configured_secret else None,
        )

    @staticmethod
    def protects(method: str, path: str) -> bool:
        return method.upper() == "POST" and any(pattern.fullmatch(path) for pattern in _PROTECTED_PATHS)

    @staticmethod
    def event_from_path(path: str) -> Optional[str]:
        match = _EVENT_PATH.search(path)
        return match.group(1) if match else None

    def peer_is_allowlisted(self, peer_ip: str) -> bool:
        try:
            address = ipaddress.ip_address(peer_ip)
        except ValueError:
            return False
        return any(address in network for network in self.config.allowlist)

    def issue_device_token(self) -> tuple[str, str]:
        device_id = uuid.uuid4().hex
        expires_at = int(time.time()) + 30 * 24 * 60 * 60
        payload = f"{device_id}.{expires_at}"
        token = self._sign("device", payload)
        return device_id, token

    def verify_device_token(self, token: Optional[str]) -> Optional[str]:
        if not token or len(token) > 256:
            return None
        payload = self._verify("device", token)
        if payload is None:
            return None
        try:
            device_id, expires_raw = payload.rsplit(".", 1)
            expires_at = int(expires_raw)
        except (ValueError, TypeError):
            return None
        if expires_at <= int(time.time()) or not re.fullmatch(r"[0-9a-f]{32}", device_id):
            return None
        return device_id

    def check_request(
        self,
        *,
        peer_ip: str,
        event_key: Optional[str],
        device_id: Optional[str],
    ) -> RateLimitDecision:
        if self.peer_is_allowlisted(peer_ip):
            with self._lock:
                self._metrics[("allowed", "allowlist")] += 1
            return RateLimitDecision(allowed=True)

        dimensions: list[tuple[str, str, int, int]] = [
            ("ip", peer_ip, self.config.ip_limit, self.config.ip_challenge_after),
        ]
        if event_key:
            dimensions.append(
                ("event", event_key, self.config.event_limit, self.config.event_challenge_after)
            )
        if device_id:
            dimensions.append(
                ("device", device_id, self.config.device_limit, self.config.device_challenge_after)
            )

        now = time.monotonic()
        with self._lock:
            self._cleanup(now)
            keys: list[tuple[str, str, int, int, deque[float]]] = []
            for name, value, limit, challenge_after in dimensions:
                key = (name, self._fingerprint(name, value))
                timestamps = self._windows.setdefault(key, deque())
                while timestamps and timestamps[0] <= now - self.config.window_seconds:
                    timestamps.popleft()
                keys.append((name, key[1], limit, challenge_after, timestamps))

            if len(self._windows) > _MAX_TRACKED_KEYS:
                self._cleanup(now, force=True)
                if len(self._windows) > _MAX_TRACKED_KEYS:
                    self._metrics[("limited", "capacity")] += 1
                    return RateLimitDecision(
                        allowed=False,
                        retry_after=self.config.window_seconds,
                        limited_dimensions=("capacity",),
                    )

            saturated = [entry for entry in keys if len(entry[4]) >= entry[2]]
            if saturated:
                retry_after = max(
                    1,
                    max(
                        int(entry[4][0] + self.config.window_seconds - now + 0.999)
                        for entry in saturated
                    ),
                )
                limited_dimensions = tuple(sorted({entry[0] for entry in saturated}))
                for dimension in limited_dimensions:
                    self._metrics[("limited", dimension)] += 1
                return RateLimitDecision(
                    allowed=False,
                    retry_after=retry_after,
                    limited_dimensions=limited_dimensions,
                )

            for _, _, _, _, timestamps in keys:
                timestamps.append(now)
            suspicious = [
                entry[0]
                for entry in keys
                if len(entry[4]) > entry[3]
            ]
            challenge_required = bool(suspicious)
            if not challenge_required:
                self._metrics[("allowed", "request")] += 1
            return RateLimitDecision(allowed=True, challenge_required=challenge_required)

    def issue_challenge(
        self,
        *,
        peer_ip: str,
        event_key: Optional[str],
        device_id: str,
    ) -> dict[str, object]:
        now = int(time.time())
        payload = {
            "id": uuid.uuid4().hex,
            "exp": now + self.config.challenge_ttl_seconds,
            "difficulty": self.config.challenge_difficulty,
            "scope": self._fingerprint(
                "scope", f"{peer_ip}:{event_key or '-'}:{device_id}"
            ),
        }
        encoded = self._b64(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        token = self._sign("challenge", encoded)
        return {
            "token": token,
            "difficulty": self.config.challenge_difficulty,
            "expires_at": payload["exp"],
        }

    def verify_and_consume_challenge(
        self,
        *,
        token: Optional[str],
        proof: Optional[str],
        peer_ip: str,
        event_key: Optional[str],
        device_id: str,
    ) -> str:
        if not token or not proof:
            return "challenge_required"
        if len(token) > 2048:
            return "challenge_invalid"
        encoded = self._verify("challenge", token)
        if encoded is None:
            return "challenge_invalid"
        try:
            payload = json.loads(self._unb64(encoded))
            challenge_id = payload["id"]
            expires_at = int(payload["exp"])
            difficulty = int(payload["difficulty"])
            expected_scope = self._fingerprint(
                "scope", f"{peer_ip}:{event_key or '-'}:{device_id}"
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return "challenge_invalid"
        if (
            not isinstance(challenge_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", challenge_id)
            or expires_at <= int(time.time())
            or difficulty != self.config.challenge_difficulty
            or not hmac.compare_digest(str(payload.get("scope", "")), expected_scope)
            or not re.fullmatch(r"[0-9]{1,20}", proof)
        ):
            return "challenge_invalid"
        digest = hashlib.sha256(f"{challenge_id}:{proof}".encode("ascii")).digest()
        if not self._has_leading_zero_bits(digest, difficulty):
            return "challenge_invalid"

        with self._lock:
            self._cleanup(time.monotonic())
            if challenge_id in self._consumed_challenges:
                return "challenge_reused"
            self._consumed_challenges[challenge_id] = float(expires_at)
            self._metrics[("allowed", "challenge")] += 1
            self._metrics[("allowed", "request")] += 1
        return "valid"

    def record_challenged(self) -> None:
        with self._lock:
            self._metrics[("challenged", "request")] += 1

    def metrics_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                f"{outcome}:{dimension}": count
                for (outcome, dimension), count in sorted(self._metrics.items())
            }

    @staticmethod
    def _has_leading_zero_bits(digest: bytes, difficulty: int) -> bool:
        full_bytes, remaining_bits = divmod(difficulty, 8)
        if any(digest[index] != 0 for index in range(full_bytes)):
            return False
        return remaining_bits == 0 or digest[full_bytes] >> (8 - remaining_bits) == 0

    def _fingerprint(self, purpose: str, value: str) -> str:
        return hmac.new(
            self._secret,
            f"{purpose}:{value}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _sign(self, purpose: str, payload: str) -> str:
        signature = hmac.new(
            self._secret,
            f"{purpose}:{payload}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return f"{payload}.{self._b64(signature)}"

    def _verify(self, purpose: str, token: str) -> Optional[str]:
        try:
            payload, signature_encoded = token.rsplit(".", 1)
            signature = self._unb64(signature_encoded)
        except (ValueError, TypeError, binascii.Error):
            return None
        expected = hmac.new(
            self._secret,
            f"{purpose}:{payload}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return payload if hmac.compare_digest(signature, expected) else None

    @staticmethod
    def _b64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _unb64(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def _cleanup(self, now: float, *, force: bool = False) -> None:
        if not force and now - self._last_cleanup < self.config.window_seconds:
            return
        oldest = now - self.config.window_seconds
        for key, timestamps in list(self._windows.items()):
            while timestamps and timestamps[0] <= oldest:
                timestamps.popleft()
            if not timestamps:
                del self._windows[key]
        wall_now = time.time()
        for challenge_id, expires_at in list(self._consumed_challenges.items()):
            if expires_at <= wall_now:
                del self._consumed_challenges[challenge_id]
        self._last_cleanup = now

"""
Security, Privacy & Cryptography Module for Team 3 — Participation.
Implements:
- UUIDv7 generation (RFC 9562 time-ordered UUIDs)
- AES-256-GCM authenticated encryption for PII at rest (AD-8)
- HMAC-SHA256 blind indexing for email deduplication & search without plaintext indexes (PA-004, PA-030)
- Asymmetric JWS (ES256 / ECDSA P-256) signing and verification for QR Tickets & Self-Access Tokens (AD-11, PA-008, PA-015)
- JWKS export (/.well-known/jwks.json) for Onsite Operations cryptographic verification
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_uuidv7() -> str:
    """Generate a canonical RFC 9562 UUIDv7 string (starts with 019...)."""
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)

    uuid_int = (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
    return str(uuid.UUID(int=uuid_int))


def utc_now_iso() -> str:
    """Return current UTC timestamp in RFC 3339 format."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def normalize_email(email: str) -> str:
    """Canonical email normalization before HMAC computation (PA-004)."""
    return email.strip().lower()


def mask_email(email: str) -> str:
    """Mask email for operator views without full PII clearance (PA-030)."""
    norm = normalize_email(email)
    if "@" not in norm:
        return "***"
    local, domain = norm.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "***" if local else "***"
    else:
        masked_local = f"{local[0]}***{local[-1]}"
    return f"{masked_local}@{domain}"


def mask_name(full_name: str) -> str:
    """Mask participant full name for privacy-preserving backoffice listing."""
    parts = [p for p in full_name.strip().split() if p]
    if not parts:
        return "***"
    return " ".join(f"{p[0]}***" for p in parts)


class SecurityManager:
    """Manages AES-256-GCM PII encryption, HMAC blind indexes, and ES256 JWS ticket keys."""

    def __init__(self) -> None:
        # Deterministic or environment-provided keys for reproducible local/testing runs
        seed_material = os.environ.get(
            "PARTICIPATION_MASTER_SECRET",
            "incuba-unas-participation-master-key-2026-v1",
        ).encode("utf-8")
        self.pii_key_id = "pii-aes256gcm-2026-01"
        self._aes_key = hashlib.sha256(seed_material + b":aes-gcm").digest()
        self._hmac_key = hashlib.sha256(seed_material + b":hmac-blind-index").digest()

        # Asymmetric ECDSA P-256 keypair for QR Ticket JWS (ES256)
        self.ticket_key_id = "part-ticket-es256-2026-01"
        private_value = int.from_bytes(
            hashlib.sha256(seed_material + b":es256-ticket-key").digest(), "big"
        ) % (
            0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551 - 1
        ) + 1
        self._ec_private_key = ec.derive_private_key(private_value, ec.SECP256R1())
        self._ec_public_key = self._ec_private_key.public_key()

    def compute_email_hmac(self, organization_id: str, email: str) -> str:
        """Compute tenant-scoped HMAC-SHA256 blind index for normalized email."""
        normalized = normalize_email(email)
        msg = f"{organization_id}:{normalized}".encode("utf-8")
        return hmac.new(self._hmac_key, msg, hashlib.sha256).hexdigest()

    def encrypt_pii(self, payload: Dict[str, Any], associated_data: str) -> str:
        """Encrypt PII dictionary using AES-256-GCM with authenticated associated data."""
        aesgcm = AESGCM(self._aes_key)
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        aad = associated_data.encode("utf-8")
        ciphertext = aesgcm.encrypt(nonce, plaintext, aad)
        envelope = {
            "v": 1,
            "alg": "A256GCM",
            "kid": self.pii_key_id,
            "nonce": _b64url_encode(nonce),
            "ct": _b64url_encode(ciphertext),
        }
        return base64.b64encode(json.dumps(envelope).encode("utf-8")).decode("ascii")

    def decrypt_pii(self, token: str, associated_data: str) -> Dict[str, Any]:
        """Decrypt AES-256-GCM PII ciphertext envelope."""
        raw = json.loads(base64.b64decode(token.encode("ascii")).decode("utf-8"))
        nonce = _b64url_decode(raw["nonce"])
        ciphertext = _b64url_decode(raw["ct"])
        aesgcm = AESGCM(self._aes_key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, associated_data.encode("utf-8"))
        return json.loads(plaintext.decode("utf-8"))

    def sign_jws(self, claims: Dict[str, Any], typ: str = "at+jwt") -> str:
        """Sign a compact JWS using ES256 (ECDSA P-256 + SHA-256) per RFC 7515."""
        header = {
            "alg": "ES256",
            "typ": typ,
            "kid": self.ticket_key_id,
        }
        header_b64 = _b64url_encode(
            json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        payload_b64 = _b64url_encode(
            json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        der_sig = self._ec_private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        sig_b64 = _b64url_encode(raw_sig)
        return f"{header_b64}.{payload_b64}.{sig_b64}"

    def verify_jws(self, jws_compact: str, expected_aud: str | None = None) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Verify ES256 signature and return (header, claims)."""
        parts = jws_compact.split(".")
        if len(parts) != 3:
            raise ValueError("Invalid JWS compact serialization")
        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        raw_sig = _b64url_decode(sig_b64)
        if len(raw_sig) != 64:
            raise ValueError("Invalid ES256 signature length")
        r = int.from_bytes(raw_sig[:32], "big")
        s = int.from_bytes(raw_sig[32:], "big")
        der_sig = encode_dss_signature(r, s)
        self._ec_public_key.verify(der_sig, signing_input, ec.ECDSA(hashes.SHA256()))

        header = json.loads(_b64url_decode(header_b64).decode("utf-8"))
        claims = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if expected_aud and claims.get("aud") != expected_aud:
            raise ValueError(f"Invalid audience: expected {expected_aud}")
        now_ts = int(time.time())
        if "exp" in claims and now_ts >= int(claims["exp"]):
            raise ValueError("Token expired")
        return header, claims

    def issue_ticket_jws(
        self,
        *,
        jti: str,
        organization_id: str,
        event_id: str,
        registration_id: str,
        scope: str = "general",
        version: int = 1,
        ttl_hours: int = 72,
    ) -> Tuple[str, Dict[str, Any], str, str]:
        """
        Issue a zero-PII asymmetric JWS QR credential for Onsite check-in (PA-008, PA-015, AD-11).
        Returns (jws_compact, claims, issued_at_iso, expires_at_iso).
        """
        now = datetime.now(timezone.utc).replace(microsecond=0)
        exp = now + timedelta(hours=ttl_hours)
        claims = {
            "jti": jti,
            "iss": "participation-service",
            "aud": "onsite",
            "org_id": organization_id,
            "event_id": event_id,
            "reg_id": registration_id,
            "scope": scope,
            "ver": version,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
        jws_token = self.sign_jws(claims, typ="ticket+jws")
        return (
            jws_token,
            claims,
            now.isoformat().replace("+00:00", "Z"),
            exp.isoformat().replace("+00:00", "Z"),
        )

    def issue_self_access_token(
        self,
        *,
        organization_id: str,
        registration_id: str,
        ttl_minutes: int = 30,
    ) -> str:
        """Issue a single-purpose participant self-service token (AUTH-AND-AUTHORIZATION-CONTRACT.md)."""
        now = datetime.now(timezone.utc)
        exp = now + timedelta(minutes=ttl_minutes)
        claims = {
            "jti": generate_uuidv7(),
            "iss": "participation-service",
            "aud": "participation-self",
            "org_id": organization_id,
            "reg_id": registration_id,
            "scope": "registration:self",
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
        return self.sign_jws(claims, typ="self-access+jwt")

    def get_public_jwks(self) -> Dict[str, Any]:
        """Export active public key in standard RFC 7517 JSON Web Key Set (JWKS) format."""
        numbers = self._ec_public_key.public_numbers()
        x_b64 = _b64url_encode(numbers.x.to_bytes(32, "big"))
        y_b64 = _b64url_encode(numbers.y.to_bytes(32, "big"))
        public_pem = self._ec_public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        return {
            "keys": [
                {
                    "kty": "EC",
                    "crv": "P-256",
                    "use": "sig",
                    "alg": "ES256",
                    "kid": self.ticket_key_id,
                    "x": x_b64,
                    "y": y_b64,
                    "public_key_pem": public_pem,
                }
            ]
        }


security_manager = SecurityManager()

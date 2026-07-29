"""
security.py — Python mirror of the Laravel backend's App\\Support\\ServiceSignature.

Service-to-service auth uses two layers, verified in this order by the inbound
guard (mirrors app/Http/Middleware/VerifyInternalRequest.php):

  1. JWT  — HS256, short-lived (default 60s TTL), replay-guarded via `jti`.
  2. HMAC — SHA-256 hex of a canonical string over the RAW request body.

Exact HTTP header names (identical to OcrClient.php / VerifyInternalRequest.php):
  - Authorization: Bearer <jwt>
  - X-Timestamp: <unix-seconds>
  - X-Signature: <hmac-sha256-hex>

Directionality:
  - Inbound  /v1/enrich       : iss = doctorshero-backend, aud = kh-worker
  - Outbound callbacks        : iss = kh-worker,           aud = doctorshero-backend

The HMAC canonical string (must match ServiceSignature::hmac exactly):

    <timestamp> "\\n" <UPPERCASE-METHOD> "\\n" <path> "\\n" <sha256-hex(body)>

`path` is the request path WITH a leading slash, e.g. "/v1/enrich" — the same
value VerifyInternalRequest builds with '/'.ltrim($request->path(), '/').
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
import uuid

import jwt  # PyJWT

# --- Header names (mirror ServiceSignature.php / OcrClient.php exactly) -------
HEADER_AUTHORIZATION = "Authorization"
HEADER_TIMESTAMP = "X-Timestamp"
HEADER_SIGNATURE = "X-Signature"

# --- Identities ---------------------------------------------------------------
BACKEND_NAME = "doctorshero-backend"
WORKER_NAME = "kh-worker"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def jwt_secret() -> str:
    return _env("KH_WORKER_JWT_SECRET")


def hmac_secret() -> str:
    return _env("KH_WORKER_HMAC_SECRET")


def jwt_ttl() -> int:
    return int(_env("KH_WORKER_JWT_TTL", "60") or "60")


def clock_skew() -> int:
    return int(_env("KH_WORKER_CLOCK_SKEW", "30") or "30")


class ReplayGuard:
    """In-memory jti replay guard. Stores each seen jti until its JWT expires
    (plus clock skew), mirroring the Cache::add TTL logic in
    VerifyInternalRequest.php. Process-local (single worker instance)."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_store(self, jti: str, expires_at: float) -> bool:
        """Return True if jti is new (accepted); False if it is a replay."""
        now = time.time()
        with self._lock:
            # opportunistic sweep of expired entries
            for k in [k for k, exp in self._seen.items() if exp < now]:
                self._seen.pop(k, None)
            if jti in self._seen:
                return False
            self._seen[jti] = expires_at
            return True


_replay_guard = ReplayGuard()


# --- JWT ----------------------------------------------------------------------
def mint_jwt(iss: str, aud: str, ttl: int | None = None) -> str:
    """Mint an HS256 JWT identical in claim shape to ServiceSignature::mintJwt."""
    now = int(time.time())
    ttl = jwt_ttl() if ttl is None else ttl
    payload = {
        "iss": iss,
        "aud": aud,
        "iat": now,
        "nbf": now - 1,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, jwt_secret(), algorithm="HS256")


def verify_jwt(token: str, expected_iss: str, expected_aud: str) -> dict:
    """Verify an inbound JWT. Mirrors ServiceSignature::verifyJwt + the replay
    guard from VerifyInternalRequest. Raises ValueError on any failure."""
    try:
        # PyJWT 2.x auto-validates the `aud` claim internally whenever the
        # token carries one — and raises InvalidAudienceError before this
        # function's own manual `iss`/`aud` checks below ever run — unless
        # an `audience=` is passed to decode() or aud-verification is
        # disabled here. We do the manual checks ourselves right after, so
        # disable PyJWT's built-in one to avoid it firing first.
        claims = jwt.decode(
            token,
            jwt_secret(),
            algorithms=["HS256"],
            leeway=clock_skew(),
            options={"require": ["exp", "iat", "nbf"], "verify_aud": False},
        )
    except jwt.PyJWTError as exc:  # signature / exp / nbf / malformed
        raise ValueError(f"jwt decode failed: {exc}") from exc

    if claims.get("iss") != expected_iss:
        raise ValueError("Invalid token issuer")
    if claims.get("aud") != expected_aud:
        raise ValueError("Invalid token audience")
    jti = claims.get("jti")
    if not jti:
        raise ValueError("Missing token id")

    exp = float(claims.get("exp", time.time()))
    if not _replay_guard.check_and_store(jti, exp + clock_skew()):
        raise ValueError("Replay detected")

    return claims


# --- HMAC ---------------------------------------------------------------------
def compute_hmac(method: str, path: str, body: str, timestamp: str) -> str:
    """Hex HMAC-SHA256 over the canonical string. Mirrors ServiceSignature::hmac."""
    canonical = "\n".join(
        [
            timestamp,
            method.upper(),
            path,
            hashlib.sha256(body.encode("utf-8")).hexdigest(),
        ]
    )
    return hmac.new(
        hmac_secret().encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_hmac(method: str, path: str, body: str, timestamp: str, signature: str) -> bool:
    """Mirror ServiceSignature::verifyHmac: digit-only timestamp, freshness
    window = clock skew, constant-time compare."""
    if not timestamp.isdigit():
        return False
    if abs(int(time.time()) - int(timestamp)) > clock_skew():
        return False
    expected = compute_hmac(method, path, body, timestamp)
    return hmac.compare_digest(expected, signature)


def build_outbound_headers(method: str, path: str, body: str) -> dict[str, str]:
    """Produce the signed headers for an outbound callback to the backend.
    iss = kh-worker, aud = doctorshero-backend."""
    ts = str(int(time.time()))
    token = mint_jwt(WORKER_NAME, BACKEND_NAME)
    sig = compute_hmac(method, path, body, ts)
    return {
        HEADER_AUTHORIZATION: f"Bearer {token}",
        HEADER_TIMESTAMP: ts,
        HEADER_SIGNATURE: sig,
        "Content-Type": "application/json",
    }


def verify_inbound(method: str, path: str, body: str, headers: dict[str, str]) -> None:
    """Verify an inbound request from the backend. Order mirrors
    VerifyInternalRequest.php: JWT (sig/exp/iss/aud/jti + replay) → HMAC.
    Raises ValueError on any failure (caller returns 401)."""
    # header lookup is case-insensitive (Starlette normalises, but be safe)
    lower = {k.lower(): v for k, v in headers.items()}
    auth = lower.get(HEADER_AUTHORIZATION.lower(), "")
    if not auth.lower().startswith("bearer "):
        raise ValueError("missing token")
    token = auth[7:].strip()

    verify_jwt(token, expected_iss=BACKEND_NAME, expected_aud=WORKER_NAME)

    timestamp = lower.get(HEADER_TIMESTAMP.lower(), "")
    signature = lower.get(HEADER_SIGNATURE.lower(), "")
    if not timestamp or not signature:
        raise ValueError("signature missing")

    if not verify_hmac(method, path, body, timestamp, signature):
        raise ValueError("signature")

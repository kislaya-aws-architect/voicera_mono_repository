"""
Security helpers added in hardening/phase-0-critical-fixes.

This module covers three of the Phase-0 findings from the VoicERA deep
analysis without touching the real-time pipeline itself:

  SEC-01  Unauthenticated WebSocket entrypoints (/agent, /plivo/agent,
          /browser/agent) - closed with a short-lived, signed session
          token minted only by this server and embedded in the WebSocket
          URL it hands back to the telephony provider.
  SEC-02  Unauthenticated /outbound/call/ - closed by reusing the existing
          INTERNAL_API_KEY convention already used elsewhere in this
          codebase (voicera_backend's X-API-Key dependency).
  SEC-03  Telephony webhooks trusted with no signature check - closed for
          Plivo (well-documented X-Plivo-Signature-V3 HMAC scheme) and
          best-effort for Vobiz (see VOBIZ_WEBHOOK_VERIFICATION note below).

None of this changes the STT -> LLM -> TTS pipeline; it only gates the
entrypoints that lead into it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Optional

from fastapi import Header, HTTPException, Request, WebSocket, status
from loguru import logger


# ---------------------------------------------------------------------------
# Shared internal API key (SEC-02)
#
# Reuses the same INTERNAL_API_KEY already required for calls to
# voicera_backend (see utils/backend_utils.py) rather than introducing a
# second secret to provision and rotate.
# ---------------------------------------------------------------------------

def _internal_api_key() -> str:
    return os.environ.get("INTERNAL_API_KEY", "")


async def require_internal_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
) -> bool:
    """FastAPI dependency: gate an HTTP endpoint behind the shared internal API key."""
    expected = _internal_api_key()
    if not expected:
        logger.error("INTERNAL_API_KEY is not configured - rejecting request.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server misconfiguration: INTERNAL_API_KEY not set",
        )
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key")
    return True


# ---------------------------------------------------------------------------
# Signed, short-lived session tokens for the real-time WebSocket entrypoints
# (SEC-01)
#
# The provider (Vobiz/Plivo) never sees our INTERNAL_API_KEY - it only ever
# sees the resulting opaque token embedded in the WebSocket URL our own
# /answer, /plivo/answer webhooks return. The token is minted at the moment
# we build that URL and verified once, at WebSocket connect time, before
# `websocket.accept()`. It is intentionally short-lived (default 60s):
# it only needs to survive the round trip from "we returned XML" to "the
# provider opens the WebSocket", not the duration of the call itself.
# ---------------------------------------------------------------------------

DEFAULT_SESSION_TOKEN_TTL_SECONDS = 60


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _session_secret() -> bytes:
    key = _internal_api_key()
    if not key:
        raise RuntimeError(
            "INTERNAL_API_KEY is not configured - cannot mint or verify "
            "WebSocket session tokens. Set INTERNAL_API_KEY in .env."
        )
    return key.encode("utf-8")


def mint_agent_session_token(
    agent_id: str,
    provider: str,
    ttl_seconds: int = DEFAULT_SESSION_TOKEN_TTL_SECONDS,
) -> str:
    """
    Mint a short-lived, HMAC-signed token authorizing exactly one WebSocket
    connection for the given agent_id/provider pair.
    """
    payload = {
        "agent_id": agent_id,
        "provider": provider,
        "exp": int(time.time()) + ttl_seconds,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_session_secret(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"


def verify_agent_session_token(token: str, agent_id: str, provider: str) -> bool:
    """
    Verify a token minted by mint_agent_session_token(). Checks signature,
    expiry, and that it was issued for this exact agent_id/provider - a
    token for one agent cannot be replayed against another.
    """
    try:
        payload_part, sig_part = token.split(".", 1)
        payload_bytes = _b64url_decode(payload_part)
        signature = _b64url_decode(sig_part)
    except (ValueError, Exception):
        return False

    expected_sig = hmac.new(_session_secret(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected_sig):
        return False

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        return False

    if payload.get("agent_id") != agent_id:
        return False
    if payload.get("provider") != provider:
        return False
    if int(payload.get("exp", 0)) < int(time.time()):
        return False

    return True


def browser_test_endpoint_enabled() -> bool:
    """
    The /browser/agent/{agent_id} WebSocket is a dev/test entrypoint with no
    corresponding telephony webhook to mint a session token from. Default
    ON for local development; set ENABLE_BROWSER_TEST_ENDPOINT=false to
    disable it entirely in a real deployment.
    """
    return os.environ.get("ENABLE_BROWSER_TEST_ENDPOINT", "true").strip().lower() != "false"


async def authorize_browser_websocket(websocket: WebSocket) -> bool:
    """
    Gate the browser test WebSocket behind the same INTERNAL_API_KEY used
    for service-to-service calls, passed as a `token` query parameter
    (browsers can't set custom headers on a WebSocket handshake). Closes
    the connection and returns False on failure; safe to call pre-accept.
    """
    if not browser_test_endpoint_enabled():
        await websocket.close(code=1008, reason="Browser test endpoint is disabled")
        return False

    expected = _internal_api_key()
    token = websocket.query_params.get("token")
    if not expected or not token or not hmac.compare_digest(token, expected):
        logger.warning("🚫 Rejected /browser/agent WebSocket connect: invalid or missing token")
        await websocket.close(code=1008, reason="Invalid or missing token")
        return False
    return True


async def authorize_agent_websocket(websocket: WebSocket, agent_id: str, provider: str) -> bool:
    """
    Check the `token` query parameter on an incoming WebSocket connection
    against a session token minted by this server. Call this BEFORE
    websocket.accept(). On failure, closes the connection with 1008
    (policy violation) and returns False.
    """
    token = websocket.query_params.get("token")
    if not token or not verify_agent_session_token(token, agent_id, provider):
        logger.warning(f"🚫 Rejected WebSocket connect for agent={agent_id} provider={provider}: invalid/missing session token")
        await websocket.close(code=1008, reason="Invalid or missing session token")
        return False
    return True


# ---------------------------------------------------------------------------
# Telephony webhook signature verification (SEC-03)
# ---------------------------------------------------------------------------
#
# Plivo: well-documented HMAC-SHA256 V3 scheme.
#   - Header `X-Plivo-Signature-V3`      : base64-encoded HMAC-SHA256 signature
#   - Header `X-Plivo-Signature-V3-Nonce`: nonce sent alongside the signature
#   - Message signed = full webhook URL (as configured with Plivo) + nonce
#   - Key = the Plivo Auth Token for the account
#
# Vobiz: Vobiz's own documentation (docs.vobiz.ai) states its callback
# signing scheme is HMAC-SHA256 and "nearly identical" to Plivo's V3 scheme,
# using `X-Vobiz-Signature-V2/V3`-style headers. This module implements the
# same URL+nonce HMAC-SHA256 construction for Vobiz. IMPORTANT: confirm the
# exact header names and payload construction against the current Vobiz
# docs (docs.vobiz.ai -> Core Concepts -> Validating Callbacks) for your
# account before relying on this in production - webhook signing schemes do
# change, and this was implemented from secondary documentation, not by
# testing against a live Vobiz account. Until confirmed, treat
# VOBIZ_WEBHOOK_VERIFICATION as a meaningful but not yet fully certified
# mitigation, and keep WebSocket session tokens (above) as the primary
# control - they do not depend on provider-specific signing details at all.

def _hmac_sha256_b64(key: str, message: str) -> str:
    digest = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_plivo_signature(request: Request, full_url: str, auth_token: str) -> bool:
    signature = request.headers.get("X-Plivo-Signature-V3")
    nonce = request.headers.get("X-Plivo-Signature-V3-Nonce")
    if not signature or not nonce or not auth_token:
        return False
    expected = _hmac_sha256_b64(auth_token, full_url + nonce)
    return hmac.compare_digest(signature, expected)


def verify_vobiz_signature(request: Request, full_url: str, auth_token: str) -> bool:
    # See the module-level note above - confirm against current Vobiz docs
    # before treating this as fully certified.
    signature = request.headers.get("X-Vobiz-Signature-V3") or request.headers.get("X-Vobiz-Signature-V2")
    nonce = request.headers.get("X-Vobiz-Signature-V3-Nonce") or request.headers.get("X-Vobiz-Signature-V2-Nonce")
    if not signature or not nonce or not auth_token:
        return False
    expected = _hmac_sha256_b64(auth_token, full_url + nonce)
    return hmac.compare_digest(signature, expected)


def webhook_verification_enabled(env_var: str) -> bool:
    """
    Verification defaults to ON. Set the given env var to "false" to
    explicitly opt out (e.g. while confirming the Vobiz scheme against a
    live account) - opting out is logged loudly so it isn't silently left
    off in production.
    """
    enabled = os.environ.get(env_var, "true").strip().lower() != "false"
    if not enabled:
        logger.warning(f"⚠️  {env_var}=false - webhook signature verification is DISABLED for this provider.")
    return enabled

"""FastAPI server for telephony integration with optimized TCP settings."""

import os
import socket
import json
import traceback
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

from loguru import logger
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .bot import bot
from utils.telemetry import router as telemetry_router
from utils.backend_utils import (
    create_meeting_in_backend,
    update_meeting_end_time,
    fetch_agent_config_from_backend,
    fetch_integration_key,
)
from utils.batching import create_batch_router
from utils.security import (
    require_internal_api_key,
    mint_agent_session_token,
    authorize_agent_websocket,
    authorize_browser_websocket,
    verify_plivo_signature,
    verify_vobiz_signature,
    webhook_verification_enabled,
)


load_dotenv()

# Constants
AGENT_CONFIGS_DIR = Path("agent_configs")


# === TCP_NODELAY WebSocket Protocol ===

def create_nodelay_websocket_protocol():
    """Create a WebSocket protocol class with TCP_NODELAY enabled.
    
    This disables Nagle's algorithm for lower latency on small packets,
    which is critical for real-time voice applications.
    """
    try:
        from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol

        class NoDelayWebSocketProtocol(WebSocketProtocol):
            def connection_made(self, transport):
                # Set TCP_NODELAY before calling parent
                try:
                    sock = transport.get_extra_info("socket")
                    if sock is not None:
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        logger.debug("TCP_NODELAY enabled on WebSocket connection")
                except Exception as e:
                    logger.warning(f"Failed to set TCP_NODELAY: {e}")
                
                super().connection_made(transport)

        return NoDelayWebSocketProtocol
    
    except ImportError:
        logger.warning("Could not import WebSocketProtocol from uvicorn, TCP_NODELAY not available")
        return None


# === Pydantic Models ===

class OutboundCallRequest(BaseModel):
    """Request model for initiating outbound calls."""
    customer_number: str
    agent_id: str
    custom_field: Optional[str] = None
    caller_id: Optional[str] = None


# === Helper Functions ===

def _get_env_or_raise(key: str) -> str:
    """Get environment variable or raise ValueError."""
    value = os.environ.get(key)
    if not value:
        raise ValueError(f"Missing required environment variable: {key}")
    return value


async def make_outbound_call_vobiz(
    customer_number: str,
    agent_id: str,
    caller_id: Optional[str] = None,
) -> dict:
    """Make an outbound call using Vobiz API.

    Vobiz Auth ID and Auth Token are loaded from the backend Integrations collection
    for the agent's organization (models VobizAuthId and VobizAuthToken), not from .env.

    Args:
        customer_number: Phone number to call
        agent_id: Agent ID to use
        caller_id: Optional caller ID (defaults to VOBIZ_CALLER_ID env var)

    Returns:
        Vobiz API response dictionary

    Raises:
        ValueError: If required credentials are missing
        requests.HTTPError: If API call fails
    """
    agent_config = await fetch_agent_config_from_backend(agent_id)
    if not agent_config:
        raise ValueError(f"Could not load agent config for agent_id={agent_id}")
    org_id = agent_config.get("org_id")
    if not org_id:
        raise ValueError("Agent has no org_id; cannot resolve Vobiz credentials from Integrations")

    auth_id = fetch_integration_key(org_id, "VobizAuthId")
    auth_token = fetch_integration_key(org_id, "VobizAuthToken")
    if not auth_id or not auth_token:
        raise ValueError(
            "Vobiz Auth ID and Auth Token must be configured in Integrations (Telephony) for this organization."
        )

    server_url = _get_env_or_raise("JOHNAIC_SERVER_URL")
    vobiz_api_base_url = _get_env_or_raise("VOBIZ_API_BASE")

    from_number = caller_id or os.environ.get("VOBIZ_CALLER_ID")
    if not from_number:
        raise ValueError("No caller_id provided and VOBIZ_CALLER_ID not set")

    headers = {
        "X-Auth-ID": auth_id,
        "X-Auth-Token": auth_token,
        "Content-Type": "application/json",
    }
    payload = {
        "from": from_number,
        "to": customer_number,
        "answer_url": f"{server_url}/answer?agent_id={agent_id}",
        "answer_method": "POST",
    }

    logger.info(f"📞 Outbound call: {from_number} → {customer_number} (agent: {agent_id})")
    
    vobiz_api_url = f"{vobiz_api_base_url}/Account/{auth_id}/Call/"
    response = requests.post(vobiz_api_url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()

    result = response.json()
    logger.info(f"✅ Call initiated: {result.get('call_uuid', 'unknown')}")
    return result


async def make_outbound_call_plivo(
    customer_number: str,
    agent_id: str,
    caller_id: Optional[str] = None,
) -> dict:
    """Make an outbound call using Plivo API with org-scoped Integration credentials."""
    agent_config = await fetch_agent_config_from_backend(agent_id)
    if not agent_config:
        raise ValueError(f"Could not load agent config for agent_id={agent_id}")
    org_id = agent_config.get("org_id")
    if not org_id:
        raise ValueError("Agent has no org_id; cannot resolve Plivo credentials from Integrations")

    auth_id = fetch_integration_key(org_id, "PlivoAuthId")
    auth_token = fetch_integration_key(org_id, "PlivoAuthToken")
    if not auth_id or not auth_token:
        raise ValueError(
            "Plivo Auth ID and Auth Token must be configured in Integrations (Telephony) for this organization."
        )

    server_url = _get_env_or_raise("JOHNAIC_SERVER_URL")
    plivo_api_base_url = os.environ.get("PLIVO_API_BASE", "https://api.plivo.com/v1")

    from_number = caller_id or os.environ.get("PLIVO_CALLER_ID")
    if not from_number:
        raise ValueError("No caller_id provided and PLIVO_CALLER_ID not set")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    payload = {
        "from": from_number,
        "to": customer_number,
        "answer_url": f"{server_url}/plivo/answer?agent_id={agent_id}",
        "answer_method": "POST",
        "hangup_url": f"{server_url}/plivo/hangup?agent_id={agent_id}",
        "hangup_method": "POST",
    }

    logger.info(f"📞 Outbound Plivo call: {from_number} → {customer_number} (agent: {agent_id})")
    plivo_api_url = f"{plivo_api_base_url}/Account/{auth_id}/Call/"
    response = requests.post(
        plivo_api_url,
        json=payload,
        headers=headers,
        auth=(auth_id, auth_token),
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    logger.info(f"✅ Plivo call initiated: {result.get('request_uuid', 'unknown')}")
    return result


async def make_outbound_call_provider(
    customer_number: str,
    agent_id: str,
    caller_id: Optional[str] = None,
) -> dict:
    """Dispatch outbound call based on agent telephony provider."""
    agent_config = await fetch_agent_config_from_backend(agent_id)
    if not agent_config:
        raise ValueError(f"Could not load agent config for agent_id={agent_id}")
    provider = (agent_config.get("telephony_provider") or "Vobiz").strip().lower()
    logger.info(f"Outbound provider={provider} agent_id={agent_id}")
    if provider == "plivo":
        return await make_outbound_call_plivo(customer_number, agent_id, caller_id)
    return await make_outbound_call_vobiz(customer_number, agent_id, caller_id)


def _build_stream_xml(websocket_url: str) -> str:
    """Build Vobiz XML response for WebSocket streaming."""
    sample_rate = int(os.environ.get("SAMPLE_RATE", "8000"))
    
    # Use L16 for 16kHz per Vobiz spec (μ-law is 8kHz only)
    if sample_rate == 16000:
        content_type = "audio/x-l16;rate=16000"
    else:
        content_type = f"audio/x-mulaw;rate={sample_rate}"
        
    logger.info(f"Sending XML with contentType: {content_type}")
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Stream bidirectional="true" keepCallAlive="true" contentType="{content_type}">
        {websocket_url}
    </Stream>
</Response>'''


def _resolve_call_identifier(payload: Dict[str, Any]) -> str:
    """Resolve canonical provider call identifier for meeting_id."""
    if not isinstance(payload, dict):
        return "unknown"

    candidate_paths = [
        ("CallUUID",),            # Vobiz webhook
        ("call_uuid",),
        ("call_id",),
        ("callId",),              # Plivo/ws variants
        ("callSid",),
        ("CallSid",),
        ("request_uuid",),        # outbound response (fallback only)
        ("start", "callId"),      # nested websocket start object
        ("start", "callSid"),
    ]

    for path in candidate_paths:
        value: Any = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if value:
            resolved = str(value).strip()
            if resolved:
                return resolved
    return "unknown"


# === FastAPI App ===

app = FastAPI(
    title="Telephony Agent API",
    description="Voice bot API for telephony integration",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# SEC-08 (hardening/phase-0-critical-fixes): no wildcard-origin fallback -
# see voicera_backend/app/main.py for the identical fix and rationale.
_allowed_origins_raw = os.environ.get("ALLOWED_ORIGINS", "")
ALLOWED_ORIGINS = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()]
if not ALLOWED_ORIGINS:
    logger.warning(
        "ALLOWED_ORIGINS is not set - CORS will reject all cross-origin "
        "browser requests. Set ALLOWED_ORIGINS in voice_2_voice_server/.env."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(telemetry_router)
app.include_router(create_batch_router(make_outbound_call_provider))


# COST-01 (hardening/phase-0-critical-fixes) stopgap rate limiting.
#
# This is a per-IP limit at the application layer - a real deployment
# behind nginx/a CDN should also rate-limit at the edge (Phase 2), and
# per-IP limiting is imperfect for calls arriving via a shared telephony
# provider IP range. It is still a meaningful backstop against a runaway
# retry loop or casual abuse hitting the two endpoints that directly cost
# money: outbound calling and the answer webhooks that stand up a full
# STT/LLM/TTS session.
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

OUTBOUND_CALL_RATE_LIMIT = os.environ.get("OUTBOUND_CALL_RATE_LIMIT", "20/minute")
ANSWER_WEBHOOK_RATE_LIMIT = os.environ.get("ANSWER_WEBHOOK_RATE_LIMIT", "60/minute")


# === Webhook signature verification helper (SEC-03) ===
#
# NOTE (Phase 1 follow-up, REL-01): this performs its own
# fetch_agent_config_from_backend() call, which - like every other backend
# call in this file - is currently synchronous I/O inside an async
# coroutine. It is not made worse by this change (the same call already
# happens in log_meeting()), but the two should be consolidated into one
# fetch once the async-HTTP-client migration lands.
async def _verify_provider_webhook(request: Request, agent_id: Optional[str], provider: str) -> bool:
    """
    Verify that an inbound answer/hangup webhook genuinely came from the
    configured telephony provider for this agent's organization. Returns
    True if the request should proceed.

    Behavior is deliberately asymmetric:
      - Signature present but WRONG  -> always reject (this is a forged
        or tampered request; there is no safe fallback).
      - No signing credential configured for this org yet -> log a loud
        warning and allow the request through, so orgs that haven't
        finished configuring telephony credentials aren't silently locked
        out of receiving calls. Configure VobizAuthToken/PlivoAuthToken in
        Integrations to close this gap for a given org.
    """
    env_var = "VOBIZ_WEBHOOK_VERIFICATION" if provider == "vobiz" else "PLIVO_WEBHOOK_VERIFICATION"
    if not webhook_verification_enabled(env_var):
        return True

    if not agent_id:
        return True  # existing handlers already 404/no-op on a missing agent_id

    agent_config = await fetch_agent_config_from_backend(agent_id)
    if not agent_config:
        return True  # existing handlers already handle "agent not found"

    org_id = agent_config.get("org_id")
    if not org_id:
        return True

    key_name = "VobizAuthToken" if provider == "vobiz" else "PlivoAuthToken"
    auth_token = fetch_integration_key(org_id, key_name)
    if not auth_token:
        logger.warning(
            f"⚠️  No {key_name} configured for org={org_id} - cannot verify {provider} "
            f"webhook signature for agent={agent_id}. Allowing request through; "
            f"configure {key_name} in Integrations to close this gap."
        )
        return True

    full_url = str(request.url)
    verify_fn = verify_vobiz_signature if provider == "vobiz" else verify_plivo_signature
    return verify_fn(request, full_url, auth_token)


# === Routes ===

@app.get("/")
async def root():
    """Health check endpoint."""
    return {"service": "Telephony Server", "status": "running"}


@app.get("/health")
async def health():
    """Detailed health check."""
    return {"status": "healthy"}


@app.post("/outbound/call/", dependencies=[Depends(require_internal_api_key)])
@limiter.limit(OUTBOUND_CALL_RATE_LIMIT)
async def make_outbound_call(request: Request, payload: OutboundCallRequest):
    """Initiate an outbound call.

    SEC-02 (hardening/phase-0-critical-fixes): this endpoint dials out using
    the organization's live telephony credentials and previously had no
    authentication at all - anyone who could reach this port could place
    calls (and run up the bill) at will. It now requires the X-API-Key
    header (INTERNAL_API_KEY), the same convention already used for
    service-to-service calls to voicera_backend, plus a per-IP rate limit
    (COST-01 stopgap - see OUTBOUND_CALL_RATE_LIMIT).

    Note: the parameter was renamed from `request` to `payload` (and an
    explicit `request: Request` added) because slowapi's rate limiter needs
    a real Starlette Request object to key on, and the name `request` was
    already taken by the Pydantic body model.

    Args:
        payload: Outbound call parameters

    Returns:
        Call initiation result
    """
    try:
        result = await make_outbound_call_provider(
            payload.customer_number,
            payload.agent_id,
            payload.caller_id,
        )
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": "Outbound call initiated",
                "customer_number": payload.customer_number,
                "agent_id": payload.agent_id,
                "caller_id": payload.caller_id,
                "result": result,
            },
        )
    except ValueError as e:
        logger.error(f"❌ Invalid request: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"❌ Outbound call failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def log_meeting(agent_id: str, form_data_dict: dict):
    """Log meeting/call data to backend."""
    try:
        agent_config = await fetch_agent_config_from_backend(agent_id)
        agent_type = agent_config.get("agent_type")
        org_id = agent_config.get("org_id")

        direction = form_data_dict.get("Direction", "outbound")
        hangup_cause = str(form_data_dict.get("HangupCause", "")).upper()
        call_status = str(form_data_dict.get("CallStatus", "")).lower()
        is_busy = hangup_cause == "USER_BUSY" or call_status in {"busy", "no-answer", "failed"}
        start_time_utc = datetime.now(timezone.utc).isoformat()
        end_time_utc = start_time_utc if is_busy else ""
        is_inbound = direction == "inbound"

        meeting_data = {
            "meeting_id": _resolve_call_identifier(form_data_dict),
            "agent_type": agent_type,
            "org_id": org_id,
            "start_time_utc": start_time_utc,
            "end_time_utc": end_time_utc,
            "inbound": is_inbound,
            "call_type": "inbound" if is_inbound else "outbound",
            "from_number": form_data_dict.get("From", "unknown"),
            "to_number": form_data_dict.get("To", "unknown"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "call_busy": is_busy,
        }
        logger.info(f"Meeting data: {meeting_data}")
        await create_meeting_in_backend(meeting_data)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Meeting log failed: {e}")
        return {"status": "error", "message": str(e)}


@app.api_route("/answer", methods=["GET", "POST"])
@limiter.limit(ANSWER_WEBHOOK_RATE_LIMIT)
async def vobiz_answer_webhook(request: Request):
    """Vobiz answer webhook - returns XML with WebSocket URL.

    This endpoint is called by Vobiz when a call is answered.
    It returns XML instructing Vobiz to connect to our WebSocket.
    """
    agent_id = request.query_params.get("agent_id")

    # SEC-03: reject webhooks that don't verify as genuinely from Vobiz
    # (see _verify_provider_webhook for the fail-open exception when no
    # signing credential is configured yet for this org).
    if not await _verify_provider_webhook(request, agent_id, "vobiz"):
        logger.warning(f"🚫 Rejected /answer webhook for agent={agent_id}: signature verification failed")
        raise HTTPException(status_code=401, detail="Webhook signature verification failed")

    form_data = await request.form()
    form_data_dict = dict(form_data)
    event = form_data_dict.get("Event", "unknown")
    hangup_cause = form_data_dict.get("HangupCause", "USER_BUSY")

    if event == "StartApp":
        await log_meeting(agent_id, form_data_dict)
        websocket_prefix = os.environ.get("JOHNAIC_WEBSOCKET_URL", "")
        # SEC-01: embed a short-lived signed session token so the WebSocket
        # endpoint below can verify this connection was actually issued by
        # us, rather than accepting any connection that knows/guesses an
        # agent_id.
        session_token = mint_agent_session_token(agent_id, "vobiz")
        websocket_url = f"{websocket_prefix}/agent/{agent_id}?token={session_token}"
        return Response(
            content=_build_stream_xml(websocket_url),
            media_type="application/xml",
        )
    elif event == "Hangup" and hangup_cause == "USER_BUSY":
        logger.info("User hung up the call")
        await log_meeting(agent_id, form_data_dict)
    else:
        logger.info("Hang URL Event Sent")


@app.websocket("/agent/{agent_id}")
async def websocket_endpoint(websocket: WebSocket, agent_id: str):
    """WebSocket endpoint for Vobiz audio streaming.

    Args:
        websocket: WebSocket connection
        agent_id: Agent ID to use
    """
    # SEC-01: verify the signed session token BEFORE accepting the
    # connection. authorize_agent_websocket() closes the connection itself
    # on failure and returns False - it is safe to call before accept().
    if not await authorize_agent_websocket(websocket, agent_id, "vobiz"):
        return

    await websocket.accept()
    logger.info(f"🔌 WebSocket connected: agent={agent_id}")

    call_sid = None
    stream_sid = None

    try:
        # Load agent configuration
        agent_config = await fetch_agent_config_from_backend(agent_id)
        agent_type = agent_config.get("agent_type")

        logger.info(f"📥 Agent config: {agent_config}")
        if not agent_config:
            logger.error(f"❌ Failed to fetch agent config from backend: {agent_id}")
            return

        # Wait for start event with call metadata
        first_message = await websocket.receive_text()
        data = json.loads(first_message)

        if data.get("event") != "start":
            logger.warning(f"⚠️ Expected 'start' event, got: {data.get('event')}")
            return

        start_info = data.get("start", {})
        call_sid = start_info.get("callSid") or start_info.get("callId", "unknown")
        stream_sid = start_info.get("streamSid") or start_info.get("streamId", "unknown")

        logger.info(f"📞 Call started: call_sid={call_sid}, stream_sid={stream_sid}")
        logger.debug(f"📋 Start info: {start_info}")

        await bot(
            websocket,
            stream_sid,
            call_sid,
            agent_type,
            agent_config,
            provider="vobiz",
        )

    except FileNotFoundError as e:
        logger.error(f"❌ {e}")
        await websocket.close(code=1008, reason="Agent config not found")
    except Exception as e:
        logger.error(f"❌ WebSocket error: {e}")
        logger.debug(traceback.format_exc())
    finally:
        logger.info(f"🔌 WebSocket closed: call_sid={call_sid}")


@app.api_route("/plivo/answer", methods=["GET", "POST"])
@limiter.limit(ANSWER_WEBHOOK_RATE_LIMIT)
async def plivo_answer_webhook(request: Request):
    """Plivo answer webhook - returns XML with WebSocket URL."""
    agent_id = request.query_params.get("agent_id")

    if not await _verify_provider_webhook(request, agent_id, "plivo"):
        logger.warning(f"🚫 Rejected /plivo/answer webhook for agent={agent_id}: signature verification failed")
        raise HTTPException(status_code=401, detail="Webhook signature verification failed")

    form_data = await request.form()
    form_data_dict = dict(form_data)
    await log_meeting(agent_id, form_data_dict)

    websocket_prefix = os.environ.get("JOHNAIC_WEBSOCKET_URL", "")
    session_token = mint_agent_session_token(agent_id, "plivo")
    websocket_url = f"{websocket_prefix}/plivo/agent/{agent_id}?token={session_token}"
    return Response(
        content=_build_stream_xml(websocket_url),
        media_type="application/xml",
    )


@app.api_route("/plivo/hangup", methods=["GET", "POST"])
async def plivo_hangup_webhook(request: Request):
    """Plivo hangup callback for provider-native call completion events."""
    agent_id = request.query_params.get("agent_id")

    if not await _verify_provider_webhook(request, agent_id, "plivo"):
        logger.warning(f"🚫 Rejected /plivo/hangup webhook for agent={agent_id}: signature verification failed")
        raise HTTPException(status_code=401, detail="Webhook signature verification failed")

    form_data = await request.form()
    form_data_dict = dict(form_data)
    await log_meeting(agent_id, form_data_dict)
    return Response(status_code=200)


@app.websocket("/plivo/agent/{agent_id}")
async def plivo_websocket_endpoint(websocket: WebSocket, agent_id: str):
    """WebSocket endpoint for Plivo audio streaming."""
    # SEC-01: verify the signed session token before doing any work. This
    # connection has not been accept()-ed yet (bot() accepts internally for
    # the Plivo transport), and closing an unaccepted WebSocket is safe.
    if not await authorize_agent_websocket(websocket, agent_id, "plivo"):
        return

    logger.info(f"🔌 Plivo WebSocket connected: agent={agent_id}")

    call_sid = None
    try:
        agent_config = await fetch_agent_config_from_backend(agent_id)
        if not agent_config:
            logger.error(f"❌ Failed to fetch agent config from backend: {agent_id}")
            return
        agent_type = agent_config.get("agent_type")

        call_sid = await bot(
            websocket,
            stream_sid=None,
            call_sid=None,
            agent_type=agent_type,
            agent_config=agent_config,
            provider="plivo",
        )
    except FileNotFoundError as e:
        logger.error(f"❌ {e}")
        await websocket.close(code=1008, reason="Agent config not found")
    except Exception as e:
        logger.error(f"❌ Plivo WebSocket error: {e}")
        logger.debug(traceback.format_exc())
    finally:
        logger.info(f"🔌 Plivo WebSocket closed: call_sid={call_sid}")


@app.websocket("/browser/agent/{agent_id}")
async def browser_websocket_endpoint(websocket: WebSocket, agent_id: str):
    """WebSocket endpoint for browser testing with live transcript events."""
    # SEC-01: this is a dev/test entrypoint with no telephony webhook to
    # mint a session token from, so it is gated directly behind
    # INTERNAL_API_KEY (passed as ?token=... since browsers can't set
    # custom headers on a WebSocket handshake) and can be disabled outright
    # via ENABLE_BROWSER_TEST_ENDPOINT=false. Previously this endpoint had
    # no authentication at all.
    if not await authorize_browser_websocket(websocket):
        return

    await websocket.accept()
    logger.info(f"🔌 Browser WebSocket connected: agent={agent_id}")

    call_sid = None
    stream_sid = None

    try:
        agent_config = await fetch_agent_config_from_backend(agent_id)
        agent_type = agent_config.get("agent_type")

        if not agent_config:
            logger.error(f"❌ Failed to fetch agent config from backend: {agent_id}")
            return

        first_message = await websocket.receive_text()
        data = json.loads(first_message)
        if data.get("event") != "start":
            logger.warning(f"⚠️ Expected 'start' event, got: {data.get('event')}")
            return

        start_info = data.get("start", {})
        call_sid = start_info.get("callSid") or start_info.get("callId", "unknown")
        stream_sid = start_info.get("streamSid") or start_info.get("streamId", "unknown")

        org_id = agent_config.get("org_id")
        start_time_utc = datetime.now(timezone.utc).isoformat()
        await create_meeting_in_backend(
            {
                "meeting_id": call_sid,
                "agent_type": agent_type,
                "org_id": org_id,
                "start_time_utc": start_time_utc,
                "created_at": start_time_utc,
                "call_type": "web",
                "inbound": False,
            }
        )

        async def send_transcript(role: str, content: str, timestamp: Optional[str]):
            await websocket.send_text(
                json.dumps(
                    {
                        "event": "transcript",
                        "role": role,
                        "content": content,
                        "timestamp": timestamp,
                    }
                )
            )

        # Browser client streams 16 kHz L16 PCM (see test-browser-dialog.tsx).
        await bot(
            websocket,
            stream_sid,
            call_sid,
            agent_type,
            agent_config,
            provider="browser",
            transcript_callback=send_transcript,
            sample_rate=16000,
        )

    except FileNotFoundError as e:
        logger.error(f"❌ {e}")
        await websocket.close(code=1008, reason="Agent config not found")
    except Exception as e:
        logger.error(f"❌ Browser WebSocket error: {e}")
        logger.debug(traceback.format_exc())
    finally:
        logger.info(f"🔌 Browser WebSocket closed: call_sid={call_sid}")

def run_server(host: str = "0.0.0.0", port: int = 7860, log_level: str = "info"):
    """Run the server with optimized settings for low-latency voice applications.
    
    Args:
        host: Host to bind to
        port: Port to bind to
        log_level: Logging level
    """
    import uvicorn

    # Create config with optimized settings
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level=log_level,
        # Use uvloop for better async performance (if available)
        loop="auto",
        # HTTP/1.1 settings
        http="auto",
        # WebSocket settings
        ws="websockets",
    )

    # Set custom WebSocket protocol with TCP_NODELAY
    nodelay_protocol = create_nodelay_websocket_protocol()
    if nodelay_protocol:
        config.ws_protocol_class = nodelay_protocol
        logger.info("✅ TCP_NODELAY enabled for WebSocket connections (Nagle's algorithm disabled)")
    else:
        logger.warning("⚠️ Could not enable TCP_NODELAY, latency may be affected")

    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    run_server(host="0.0.0.0", port=7860, log_level="info")

"""
Sajag / Glific webhook routes (SaveLIFE Foundation WhatsApp integration).

This is the new API surface referenced in the 9 Jul architecture note — VoicERA
was previously telephony-only (Vobiz, Plivo, browser WebSocket, all in
voice_2_voice_server). This router is a plain REST surface in voicera_backend
instead, because Glific POSTs discrete WhatsApp events (text/voice-note/photo),
not a live audio stream.

STATUS AS OF THIS COMMIT — read before deploying:
  - Confirmed / built: webhook route, request validation, phone-number HMAC
    hashing, report storage, STT call-out, LLM classification call-out.
  - Explicitly NOT built (stubs only, see app/services/sajag_pipeline.py):
    message-text PII redaction, photo face/number-plate redaction.
  - Explicitly PROVISIONAL, pending confirmation from the Glific team
    (call was still being scheduled as of 15 Jul):
      * exact payload shape Glific will actually POST (see GlificWebhookPayload
        in app/models/schemas.py)
      * signature/auth scheme Glific supports — this file implements a simple
        shared-secret header as a placeholder, not a confirmed contract
      * how a status update gets pushed back to the citizen (SLF's mechanism
        for this is unknown — see mark_report_status below, which updates our
        own record but does not yet notify Glific/the citizen of anything)
      * whether triangulation-tier scoring happens here or on SLF's side

Do not point this at a real SLF/Glific instance without re-reading the above.
"""
import base64
import logging
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, status

from app.auth import get_current_user
from app.config import settings
from app.models.schemas import (
    GlificWebhookPayload,
    SajagReportResponse,
    SajagReportStatusUpdate,
)
from app.services import sajag_hashing, sajag_pipeline, sajag_report_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sajag", tags=["sajag-glific"])


async def verify_glific_webhook_secret(
    x_glific_webhook_secret: Optional[str] = Header(None, alias="X-Glific-Webhook-Secret")
) -> bool:
    """
    PROVISIONAL auth dependency for the inbound Glific webhook.

    A plain shared-secret header is the simplest thing that could work and is
    easy to swap out — but we do not yet know what Glific actually supports on
    their side (HMAC-signed body? a different header name? IP allowlisting
    instead?). Do not treat this as a settled security design.
    """
    if not settings.SAJAG_GLIFIC_WEBHOOK_SECRET:
        logger.error("SAJAG_GLIFIC_WEBHOOK_SECRET not configured — rejecting webhook call.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Sajag webhook secret not configured on this server",
        )
    if x_glific_webhook_secret != settings.SAJAG_GLIFIC_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Glific-Webhook-Secret header",
        )
    return True


async def _process_voice_note_async(report_id: str, voice_note_url: str, language_id: str) -> None:
    """
    Background task: fetch the voice note, transcribe, classify. Runs after the
    webhook has already returned 202 to Glific, so STT/LLM latency never makes
    Glific's request time out.

    PROVISIONAL, best-effort: this assumes voice_note_url is a directly fetchable
    URL. Per open item "i" in the 9 Jul note, we don't actually know that yet —
    Glific may instead send a media ID requiring a separate authenticated lookup
    call against their API. If the plain GET below fails, that's the most likely
    reason. Replace this fetch step once the Glific team confirms the real
    mechanism; the transcribe/classify/store logic after it should not need to
    change.
    """
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(voice_note_url)
            resp.raise_for_status()
            voice_note_b64 = base64.b64encode(resp.content).decode("utf-8")
    except httpx.HTTPError as e:
        logger.error(
            "Could not fetch voice note for report %s from %s (%s) — this is "
            "expected until the Glific media-passing mechanism is confirmed.",
            report_id, voice_note_url, e,
        )
        return

    transcription = await sajag_pipeline.transcribe_voice_note(voice_note_b64, language_id)
    if transcription:
        # PII redaction runs here, before anything derived from the transcript is
        # persisted further — but see the loud warning in
        # sajag_pipeline.redact_message_text(): it is NOT yet implemented.
        transcription = sajag_pipeline.redact_message_text(transcription)

    hazard_tags = await sajag_pipeline.classify_report(transcription) if transcription else None

    sajag_report_service.update_report_processing(
        report_id=report_id,
        transcription=transcription,
        hazard_tags=hazard_tags,
    )


@router.post("/glific/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_glific_report(
    payload: GlificWebhookPayload,
    background_tasks: BackgroundTasks,
    _: bool = Depends(verify_glific_webhook_secret),
) -> Dict[str, Any]:
    """
    Inbound webhook Glific calls when a citizen submits a Sajag hazard report.

    Per the concept note's user journey: the process only continues if consent
    was given — Glific is expected to have already gated on this before calling
    us, but we check again here rather than trusting that silently.
    """
    if not payload.consent_given:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="consent_given must be true — report not stored without consent",
        )

    try:
        phone_hash = sajag_hashing.hash_phone_number(payload.contact_phone)
    except RuntimeError as e:
        # Fails closed: refuse to store the report rather than store a
        # reporter's phone number unhashed.
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    message_text = sajag_pipeline.redact_message_text(payload.message_text) if payload.message_text else None
    photo_url = sajag_pipeline.redact_media(payload.photo_url)

    location_dict = payload.location.dict() if payload.location else None

    report = sajag_report_service.create_report(
        glific_contact_id=payload.glific_contact_id,
        contact_phone_hash=phone_hash,
        channel=payload.channel,
        language_id=payload.language_id,
        location=location_dict,
        photo_url=photo_url,
        received_at=payload.received_at,
    )

    # If there's a text message but no voice note, store the (stub-redacted) text
    # directly rather than waiting on the background task.
    if message_text and not payload.voice_note_url:
        sajag_report_service.update_report_processing(report["report_id"], transcription=message_text)

    # Voice note transcription + classification happen after we've already
    # acknowledged Glific's call, since STT/LLM latency shouldn't block the
    # webhook response. See _process_voice_note_async's docstring for why the
    # fetch step inside it is a best-effort guess, not a confirmed contract.
    if payload.voice_note_url:
        background_tasks.add_task(
            _process_voice_note_async,
            report["report_id"],
            payload.voice_note_url,
            payload.language_id or "hi",
        )

    return {"status": "received", "report_id": report["report_id"]}


@router.get("/reports", response_model=List[SajagReportResponse])
async def list_sajag_reports(
    status_filter: Optional[str] = None,
    limit: int = 50,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> List[Dict[str, Any]]:
    """List Sajag reports for internal/dashboard use. Protected — same auth as the
    rest of the admin-facing API, not the webhook secret."""
    return sajag_report_service.list_reports(status=status_filter, limit=limit)


@router.get("/reports/{report_id}", response_model=SajagReportResponse)
async def get_sajag_report(
    report_id: str,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    report = sajag_report_service.get_report(report_id)
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return report


@router.patch("/reports/{report_id}/status", response_model=SajagReportResponse)
async def update_sajag_report_status(
    report_id: str,
    body: SajagReportStatusUpdate,
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Update a report's status (e.g. Triaged -> Validated -> Escalated).

    NOTE: this only updates our own record. It does NOT notify Glific or the
    citizen — per open item "f" in the 9 Jul note, how a status change gets
    communicated back to the reporter is still undesigned and depends on a
    mechanism SLF hasn't specified yet. Closing that loop (Section 5, step 5 of
    the concept note) is not implemented by this endpoint.
    """
    try:
        report = sajag_report_service.update_report_status(report_id, body.status, body.note)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    if not report:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return report

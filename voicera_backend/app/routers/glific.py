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
import json
import logging
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from pydantic import ValidationError

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


async def parse_glific_payload(request: Request) -> GlificWebhookPayload:
    """
    Parses the raw request body into GlificWebhookPayload, defensively handling
    a DOUBLE-ENCODED body — confirmed happening in practice from Glific's real
    platform on 2026-07-21, not a hypothetical: the POST body arrived as a JSON
    STRING containing JSON (e.g. the literal text '"{\\"a\\":1}"') rather than an
    actual JSON object ('{"a":1}'). FastAPI's normal automatic body-parsing
    correctly rejects this with a "model_attributes_type" Pydantic error, since
    a string isn't a mapping — so we intercept here and try a second decode
    before giving up.

    Most likely trigger, unconfirmed: a nested object in the Post Body template
    (our `location` field) — Glific's own "Call a Webhook" docs only show flat
    examples (no nesting), and their templating engine may not serialize nested
    structures correctly. Whatever the platform-side cause, unwrapping
    defensively here is safer than assuming every future Glific payload will
    arrive well-formed.
    """
    raw_body = await request.body()
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Body is not valid JSON: {e}"
        )

    if isinstance(parsed, str):
        logger.warning("Glific webhook body arrived double-encoded (JSON string containing JSON) — unwrapping.")
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Body was double-encoded and the inner content is not valid JSON either: {e}",
            )

    if not isinstance(parsed, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Body must be a JSON object")

    try:
        return GlificWebhookPayload(**parsed)
    except ValidationError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=e.errors())


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


async def _process_text_report_async(report_id: str, message_text: str, language_id: str, contact_phone: str) -> None:
    """
    Background task: classify a text-only report (no voice note, so no STT step
    needed), then push to Margdarshak.

    CHANGED 2026-07-21 per SLF's confirmed design: only VOICE NOTES are
    translated — VoicERA is a voice platform, translation is specifically a
    voice-pipeline step (Hindi speech -> English text for SLF ops, corrected
    direction 2026-07-27). Typed text passes through exactly as the citizen
    wrote it; translate_to_english() is no longer called here. (It was called
    here before the 07-21 fix — that was a real gap, not the intended design,
    now corrected.)
    """
    hazard_tags = await sajag_pipeline.classify_report(message_text)
    updated_report = sajag_report_service.update_report_processing(report_id, hazard_tags=hazard_tags)
    if updated_report:
        await sajag_pipeline.push_to_margdarshak(updated_report, contact_phone)


async def _process_voice_note_async(
    report_id: str, voice_note_url: str, language_id: str, contact_phone: str,
    existing_message_text: Optional[str] = None
) -> None:
    """
    Background task: fetch the voice note, transcribe, classify. Runs after the
    webhook has already returned 202 to Glific, so STT/LLM latency never makes
    Glific's request time out.

    existing_message_text: if the citizen also sent typed text alongside the voice
    note, it was already stored synchronously as `transcription` before this task
    started (see receive_glific_report). Passed in here so this task COMBINES the
    voice transcription with it instead of overwriting it — previously, whichever
    of the text-sync-write or this task ran last would silently clobber the other's
    contribution to `transcription`. Fixed per SLF: neither citizen input should be
    dropped when both text and voice arrive on the same report.

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
            raw_audio_bytes = resp.content
    except httpx.HTTPError as e:
        logger.error(
            "Could not fetch voice note for report %s from %s (%s) — this is "
            "expected until the Glific media-passing mechanism is confirmed.",
            report_id, voice_note_url, e,
        )
        return

    try:
        pcm_bytes = await sajag_pipeline.convert_to_pcm16_mono_16k(raw_audio_bytes)
    except RuntimeError as e:
        # Discovered during local testing: the STT server expects raw 16kHz mono
        # PCM with zero format detection on its side. Without this conversion step,
        # any voice note not already in that exact shape silently transcribes to
        # empty string rather than erroring — so a conversion failure here is the
        # more honest failure mode, logged loudly rather than producing a
        # confusing empty transcription downstream.
        logger.error("Audio conversion failed for report %s: %s", report_id, e)
        return

    voice_note_b64 = base64.b64encode(pcm_bytes).decode("utf-8")

    transcription = await sajag_pipeline.transcribe_voice_note(voice_note_b64, language_id)
    if transcription:
        # PII redaction runs here, before anything derived from the transcript is
        # persisted further — but see the loud warning in
        # sajag_pipeline.redact_message_text(): it is NOT yet implemented.
        transcription = sajag_pipeline.redact_message_text(transcription)

    # Translate BEFORE combining with typed text, not after — corrected 2026-07-27
    # alongside the direction fix. The old code translated the COMBINED string
    # (typed English + voice Devanagari mixed together), which is out-of-distribution
    # for a single-direction NMT model. Translating only the voice-derived
    # `transcription` (always pure Devanagari, since STT is Hindi-only) keeps the
    # model's input exactly matching what it was trained on. existing_message_text
    # is assumed already English (per the voice-only-translation rule) and is
    # prepended to the translated output the same way it's prepended to the
    # source-of-record transcription below — untested assumption for mixed-input
    # cases, since we've not yet had a real report with both typed AND voice input
    # to confirm this reads naturally end to end.
    translated_voice = (
        await sajag_pipeline.translate_to_english(transcription, language_id)
        if transcription else None
    )

    # Combine rather than overwrite: if the citizen also sent typed text, keep both,
    # clearly labeled, instead of the voice transcription silently replacing it.
    if transcription and existing_message_text:
        combined_transcription = f"{existing_message_text}\n\n[Voice note transcription]\n{transcription}"
    else:
        combined_transcription = transcription or existing_message_text

    if translated_voice and existing_message_text:
        translated_text_en = f"{existing_message_text}\n\n[Voice note - translated]\n{translated_voice}"
    else:
        translated_text_en = translated_voice or existing_message_text

    # transcription stays in the original language (source of record, per the
    # concept note's data model). translated_text_en is the SLF-facing English
    # version — corrected 2026-07-27: citizens speak Hindi, SLF ops need English,
    # this used to run the opposite direction.
    hazard_tags = await sajag_pipeline.classify_report(combined_transcription) if combined_transcription else None

    updated_report = sajag_report_service.update_report_processing(
        report_id=report_id,
        transcription=combined_transcription,
        translated_text_en=translated_text_en,
        hazard_tags=hazard_tags,
    )
    if updated_report:
        await sajag_pipeline.push_to_margdarshak(updated_report, contact_phone)


@router.post("/glific/webhook", status_code=status.HTTP_202_ACCEPTED)
async def receive_glific_report(
    background_tasks: BackgroundTasks,
    payload: GlificWebhookPayload = Depends(parse_glific_payload),
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
    photos = sajag_pipeline.redact_media(payload.photos)

    location_dict = payload.location.dict() if payload.location else None

    report = sajag_report_service.create_report(
        glific_contact_id=payload.glific_contact_id,
        contact_phone_hash=phone_hash,
        channel=payload.channel,
        language_id=payload.language_id,
        location=location_dict,
        photos=photos,
        received_at=payload.received_at,
    )

    # Always store typed text synchronously the moment it arrives — previously this
    # only happened when there was no voice note, so a citizen sending both text and
    # a voice note had their typed text silently dropped. Fixed per SLF: neither
    # input should be lost. (See _process_voice_note_async for how the two are
    # combined once/if the voice note finishes transcribing.)
    if message_text:
        sajag_report_service.update_report_processing(report["report_id"], transcription=message_text)

    # Text-only background task (translate + classify) only runs when there's no
    # voice note — when both are present, _process_voice_note_async below handles
    # translation/classification on the COMBINED text instead, so the two tasks
    # don't race to overwrite each other's output.
    if message_text and not payload.voice_note_url:
        background_tasks.add_task(
            _process_text_report_async, report["report_id"], message_text, payload.language_id or "hi",
            payload.contact_phone,
        )

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
            payload.contact_phone,
            message_text,
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
"""
Sajag processing pipeline: transcription + hazard classification for a single
inbound report. Deliberately calls ai4bharat_stt_server and llm_server directly
over HTTP rather than going through the Pipecat pipeline in voice_2_voice_server —
that pipeline is built for live streaming telephony audio (WebSocket frames), not
a single discrete voice note arriving via webhook. See README_sajag_glific.md for
why this is a separate call path.
"""
import asyncio
import logging
import re
from typing import Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def convert_to_pcm16_mono_16k(audio_bytes: bytes) -> bytes:
    """
    Convert arbitrary input audio (whatever format a voice note actually arrives
    as — WAV with a header, WhatsApp-native OGG/Opus, mp3, anything) into raw
    headerless 16kHz mono 16-bit PCM.

    This exists because ai4bharat_stt_server's /transcribe does zero format
    handling on its side — see _decode_audio_b64() in ai4bharat_stt_server/server.py,
    which is a bare `np.frombuffer(audio_bytes, dtype=np.int16)`. Sending it a real
    WAV file (header included) or a non-16kHz/non-mono/non-PCM file doesn't error;
    it silently decodes to noise and the model correctly returns an empty
    transcription. Discovered directly during local testing (2026-07-20) — the
    Sajag pipeline had no conversion step before this, meaning any voice note that
    wasn't already exactly 16kHz mono s16le PCM would have failed silently, with no
    error logged anywhere, in production too.

    Requires ffmpeg on PATH — added to voicera_backend/Dockerfile alongside this.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ar", "16000", "-ac", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(input=audio_bytes)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg audio conversion failed (exit {proc.returncode}): "
            f"{stderr.decode(errors='replace')[-500:]}"
        )
    return stdout


async def transcribe_voice_note(audio_b64: str, language_id: str = "hi") -> Optional[str]:
    """
    Call ai4bharat_stt_server's POST /transcribe.

    Verified against the actual server code (ai4bharat_stt_server/server.py):
    request body is {"audio_b64": ..., "language_id": ...}, response is {"text": ...}.
    """
    url = f"{settings.SAJAG_STT_SERVER_URL.rstrip('/')}/transcribe"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json={"audio_b64": audio_b64, "language_id": language_id})
            resp.raise_for_status()
            return resp.json().get("text")
    except httpx.HTTPError as e:
        logger.error(f"STT call to {url} failed: {e}")
        return None


async def classify_report(text: str) -> Optional[List[str]]:
    """
    Call llm_server's OpenAI-compatible /v1/chat/completions endpoint to tag a
    report against a hazard taxonomy.

    PROVISIONAL: the hazard taxonomy itself isn't defined anywhere in the concept
    note or the 9 Jul architecture note beyond the three triangulation tiers
    (Confirmed/Emerging/Contextual, which are a separate, unscored field — see
    sajag_report_service.py). The tag list below is a starting placeholder, not a
    confirmed taxonomy — needs sign-off before this is treated as real classification
    output rather than a rough first pass.
    """
    if not text:
        return None

    placeholder_taxonomy = [
        "missing_signage", "no_footpath", "unmarked_crossing", "poor_lighting",
        "blind_merge", "speeding_reported", "median_gap", "other",
    ]

    url = f"{settings.SAJAG_LLM_SERVER_URL.rstrip('/')}/v1/chat/completions"
    system_prompt = (
        "You are classifying a road-safety hazard report from a citizen WhatsApp "
        "message for SaveLIFE Foundation. Given the report text, return the single "
        f"best-fitting tag from this list, and nothing else: {placeholder_taxonomy}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                json={
                    "model": settings.SAJAG_LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": text},
                    ],
                    "max_tokens": 20,
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            tag = data["choices"][0]["message"]["content"].strip()
            return [tag] if tag in placeholder_taxonomy else ["other"]
    except (httpx.HTTPError, KeyError, IndexError) as e:
        logger.error(f"LLM classification call to {url} failed: {e}")
        return None


def redact_message_text(text: str) -> str:
    """
    NOT IMPLEMENTED. Per open item "d" in the 9 Jul architecture note, redacting
    PII that might appear inside free-text message content (names, other people's
    phone numbers, etc. — separate from the reporter's own phone number, which is
    hashed in sajag_hashing.py) needs a scoping decision before it can be built or
    estimated. This function currently returns the text unchanged and logs a
    warning so it can never be silently mistaken for working redaction.

    DO NOT wire this report pipeline into anything citizen-facing or
    government-facing until this is actually implemented — see Section 8 of the
    concept note (Data Protection, Ethics & Safeguarding): "no reporter can be
    exposed to retaliation or identified in any government-facing output."
    """
    logger.warning(
        "redact_message_text() is a stub — message text is being stored/forwarded "
        "WITHOUT PII redaction. Do not use for real reports until this is built."
    )
    return text


def redact_media(photos: Optional[List[str]]) -> Optional[List[str]]:
    """
    NOT IMPLEMENTED. Per open item "e" in the 9 Jul architecture note, automated
    face/number-plate redaction in photos has no existing foundation in VoicERA and
    needs its own technical evaluation. This function is a pass-through stub only.

    Takes/returns a list — SLF's confirmed contract is an array of photo URLs, not
    a single photo per report.
    """
    if photos:
        logger.warning(
            "redact_media() is a stub — %d photo(s) are being stored/forwarded "
            "WITHOUT face/number-plate redaction.",
            len(photos),
        )
    return photos


_DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")


async def push_to_margdarshak(report: Dict, contact_phone: str) -> None:
    """
    Pushes a processed Sajag report to SLF's Margdarshak dashboard.

    Endpoint, auth, and payload shape confirmed by Pawan (SLF CTO) on
    2026-07-21 — this is the first CONFIRMED upstream contract, replacing the
    internal/pull-only API (GET /sajag/reports) that was the only option
    before today. Currently points at SLF's own TEST key; Pawan confirmed the
    key changes for production — SAJAG_MARGDARSHAK_API_KEY must be rotated
    before this runs against real citizen data, not just left on the test key.

    message_text follows the confirmed rule: only VOICE NOTES are translated
    (VoicERA is a voice platform; translation is a voice-pipeline step).
    Typed text passes through as-is, never translated. So: translated_text_en
    if it exists (voice path — the Hindi voice note translated to English,
    corrected direction as of 2026-07-27), otherwise the raw transcription
    (text path, which is identical to what the citizen typed, since it was
    never translated in the first place).

    contact_phone is passed in RAW, not our internally-hashed version — SLF's
    own sample payload explicitly includes a raw phone number, presumably for
    their own case-management/follow-up needs. Deliberate, flagged choice: our
    own stored copy stays hashed as always (see sajag_hashing); the raw number
    is used only in-memory, for this one outbound call, never additionally
    persisted here.
    """
    message_text = report.get("translated_text_en") or report.get("transcription")
    body = {
        "glific_contact_id": report.get("glific_contact_id"),
        "contact_phone": contact_phone,
        "consent_given": True,  # only ever reach here if consent_given was true at intake
        "message_text": message_text,
        "photos": report.get("photos") or [],
        "location": {
            "latitude": report.get("latitude"),
            "longitude": report.get("longitude"),
        },
    }
    report_id = report.get("report_id")
    logger.info(f"Margdarshak push body for report {report_id}: {body}")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                settings.SAJAG_MARGDARSHAK_URL,
                json=body,
                headers={"X-API-Key": settings.SAJAG_MARGDARSHAK_API_KEY},
            )
            resp.raise_for_status()
            logger.info(f"Pushed report {report_id} to Margdarshak: HTTP {resp.status_code} — body: {resp.text}")
    except httpx.HTTPStatusError as e:
        # The status alone (e.g. "400 BAD REQUEST") doesn't say WHY — the
        # response body almost always does. Discovered 2026-07-21: the
        # original except block only logged the exception's string form,
        # which drops the body entirely, on a real 400 from this exact
        # endpoint with no way to tell what was wrong with the payload.
        logger.error(
            f"Margdarshak push failed for report {report_id}: "
            f"HTTP {e.response.status_code} — body: {e.response.text}"
        )
    except httpx.HTTPError as e:
        logger.error(f"Margdarshak push failed for report {report_id}: {e}")


async def translate_to_english(text: str, source_language_id: str = "hi") -> Optional[str]:
    """
    Translate a Hindi voice-note transcription into English — per the contract
    corrected with SLF on 2026-07-27: citizens speak Hindi (the STT model is
    Hindi-only, so its output is always Devanagari), and SLF's ops team, reading
    the Margdarshak dashboard, needs English. This function used to run the
    opposite direction (English -> Hindi) under the name translate_to_hindi(),
    which was backwards for the real use case — Kislaya and Pawan confirmed
    this by email 2026-07-27. Renamed, not just rewritten, since the old name
    would now describe the wrong behavior.

    Trigger condition is the direct inverse of the old function's: skip only if
    the text contains NO Devanagari at all (already English/Latin, nothing to
    translate); translate whenever Devanagari is present. Given the STT model
    is Hindi-only, this means translation now runs on essentially every real
    voice-note report — the opposite of the old logic, which (also given the
    Hindi-only STT) was skipping translation on essentially every real report.

    Calls the same dedicated IndicTrans2 translation service
    (SAJAG_TRANSLATION_SERVER_URL) as before, but the model loaded there needs
    to be the indic-en variant now, not en-indic — see indictrans_server.py.
    That's a model swap on the Linux box, not something this function controls.
    """
    if not text:
        return None

    if not _DEVANAGARI_RE.search(text):
        return text  # already English/Latin — nothing to translate

    url = f"{settings.SAJAG_TRANSLATION_SERVER_URL.rstrip('/')}/translate"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url, json={"text": text, "src_lang": "hin_Deva", "tgt_lang": "eng_Latn"}
            )
            resp.raise_for_status()
            translated = resp.json().get("text")
            # repr(), not the raw string — translated text routinely contains
            # embedded newlines (see the "[Voice note - translated]" combine
            # pattern in glific.py), which would otherwise split this log entry
            # across multiple lines and break a plain `grep`. Matches how
            # push_to_margdarshak's existing body log is already grep-safe —
            # that one happens to get this for free from dict repr.
            logger.info(f"English translation succeeded: {translated!r}")
            return translated
    except (httpx.HTTPError, KeyError) as e:
        logger.error(f"English translation call to {url} failed: {e}")
        return None

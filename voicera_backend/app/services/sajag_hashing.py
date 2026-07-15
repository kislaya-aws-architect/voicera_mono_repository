"""
Phone number hashing for Sajag reports.

Deliberately NOT reusing the pattern in app/services/knowledge_service.py
(`hashlib.sha256(org_id.encode()).hexdigest()[:48]`) — that's an unkeyed hash,
which is fine for an org_id (high-entropy, not worth reversing) but wrong for a
phone number. A 10-digit Indian mobile number has only ~10 billion possible
values, so an unkeyed SHA-256 of it can be reversed in seconds with a precomputed
table. A keyed HMAC cannot be reversed without the secret key, even though the
input space is exactly as small.

This was flagged explicitly in the 9 Jul architecture note (item c) as something
to fix, not copy.
"""
import hashlib
import hmac
import logging
import re

from app.config import settings

logger = logging.getLogger(__name__)


def _normalize_phone(raw_phone: str) -> str:
    """
    Strip all non-digit characters and normalize to a bare-digits, country-code-
    included form, so the same reporter hashes identically regardless of whether
    Glific sends "+91 98765 43210", "919876543210", or "9876543210".

    Assumes India (91) when no country code is present — reasonable for a
    WhatsApp-based India road-safety platform, but flag if Sajag ever needs to
    handle non-Indian numbers.
    """
    digits = re.sub(r"\D", "", raw_phone.strip())
    if len(digits) == 10:
        digits = f"91{digits}"
    return digits


def hash_phone_number(raw_phone: str) -> str:
    """
    Return a keyed HMAC-SHA256 hex digest of a phone number.

    Raises RuntimeError if SAJAG_GLIFIC_WEBHOOK_SECRET isn't configured — this is
    intentional. We should refuse to store a report rather than silently fall back
    to an unkeyed (effectively reversible) hash for a vulnerable reporter's phone
    number. See Section 8 (Data Protection, Ethics & Safeguarding) of the Sajag
    concept note.
    """
    secret = settings.SAJAG_GLIFIC_WEBHOOK_SECRET
    if not secret:
        logger.error(
            "SAJAG_GLIFIC_WEBHOOK_SECRET is not configured — refusing to hash "
            "phone number. Set it in .env before accepting live Sajag reports."
        )
        raise RuntimeError("SAJAG_GLIFIC_WEBHOOK_SECRET is not configured")

    normalized = _normalize_phone(raw_phone)
    return hmac.new(
        key=secret.encode("utf-8"),
        msg=normalized.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

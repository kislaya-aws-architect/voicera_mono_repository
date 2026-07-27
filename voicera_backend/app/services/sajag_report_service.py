"""
Sajag report storage.

Stores hazard reports coming in from the Glific/WhatsApp webhook in their own
collection ("SajagReports") rather than reusing the agents/calls collections —
this is a distinct entity with its own lifecycle (see status workflow in the
concept note's "Capture & data model" section: Received -> Triaged -> Validated
-> Escalated -> In-Progress -> Resolved -> Feedback-Sent).
"""
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.database import get_database

logger = logging.getLogger(__name__)

COLLECTION_NAME = "SajagReports"

VALID_STATUSES = [
    "Received",
    "Triaged",
    "Validated",
    "Escalated",
    "In-Progress",
    "Resolved",
    "Feedback-Sent",
]


def create_report(
    glific_contact_id: str,
    contact_phone_hash: str,
    channel: str,
    language_id: Optional[str],
    location: Optional[Dict[str, Any]],
    photos: Optional[List[str]],
    received_at: Optional[str],
) -> Dict[str, Any]:
    """Create a new Sajag report in status 'Received'. Transcription/classification
    are filled in afterwards by the pipeline (see sajag_pipeline.py) — a report is
    stored immediately even if STT/LLM processing hasn't finished, so we never lose
    an inbound report to a downstream failure."""
    db = get_database()
    collection = db[COLLECTION_NAME]

    now = datetime.utcnow().isoformat()
    report_id = str(uuid.uuid4())

    doc = {
        "report_id": report_id,
        "glific_contact_id": glific_contact_id,
        "contact_phone_hash": contact_phone_hash,
        "channel": channel,
        "language_id": language_id,
        "transcription": None,
        "translated_text_en": None,
        "hazard_tags": None,
        "latitude": location.get("latitude") if location else None,
        "longitude": location.get("longitude") if location else None,
        "location_accuracy_meters": location.get("accuracy_meters") if location else None,
        "photos": photos or [],
        # Triangulation tier (Confirmed / Emerging / Contextual) is NOT computed here.
        # Per open item "h" in the 9 Jul note, whether that scoring happens on
        # VoicERA's side or SLF's is still unconfirmed. Left null until that's decided.
        "triangulation_tier": None,
        "status": "Received",
        "received_at": received_at or now,
        "created_at": now,
        "updated_at": now,
    }

    collection.insert_one(doc)
    logger.info(f"Created Sajag report {report_id} for contact {glific_contact_id}")
    return doc


def update_report_processing(
    report_id: str,
    transcription: Optional[str] = None,
    translated_text_en: Optional[str] = None,
    hazard_tags: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Attach STT transcription / English translation / LLM classification results to
    an existing report."""
    db = get_database()
    collection = db[COLLECTION_NAME]

    update_fields: Dict[str, Any] = {"updated_at": datetime.utcnow().isoformat()}
    if transcription is not None:
        update_fields["transcription"] = transcription
    if translated_text_en is not None:
        update_fields["translated_text_en"] = translated_text_en
    if hazard_tags is not None:
        update_fields["hazard_tags"] = hazard_tags

    collection.update_one({"report_id": report_id}, {"$set": update_fields})
    return get_report(report_id)


def update_report_status(report_id: str, status: str, note: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Must be one of {VALID_STATUSES}")

    db = get_database()
    collection = db[COLLECTION_NAME]

    update_fields = {"status": status, "updated_at": datetime.utcnow().isoformat()}
    if note:
        update_fields["status_note"] = note

    collection.update_one({"report_id": report_id}, {"$set": update_fields})
    logger.info(f"Sajag report {report_id} status -> {status}")
    return get_report(report_id)


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    db = get_database()
    return db[COLLECTION_NAME].find_one({"report_id": report_id}, {"_id": 0})


def list_reports(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    db = get_database()
    query = {"status": status} if status else {}
    cursor = db[COLLECTION_NAME].find(query, {"_id": 0}).sort("created_at", -1).limit(limit)
    return list(cursor)

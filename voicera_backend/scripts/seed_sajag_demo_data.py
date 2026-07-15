"""
Seed realistic-looking demo data into the SajagReports collection.

Purpose: let the Sajag dashboard (voicera_frontend/app/(dashboard)/sajag) be
demoed as a standalone Proof of Concept — e.g. to Karthik/Madhu, or to SLF
themselves — without waiting for the real Glific integration to go live.

This is clearly demo data, not a simulation of a working pipeline:
  - Report text is written directly (not actually run through STT)
  - Hazard tags are assigned directly (not actually run through the LLM
    classifier in app/services/sajag_pipeline.py)
  - Phone numbers are fake, but still hashed through the real
    app/services/sajag_hashing.py so the storage layer being demoed is real
  - Locations are real coordinates along Indian highways, for visual realism
    on the map links, but are not tied to any actual SLF corridor data

Run from voicera_backend/:
    python scripts/seed_sajag_demo_data.py
    python scripts/seed_sajag_demo_data.py --wipe   # clear existing demo data first

Requires SAJAG_GLIFIC_WEBHOOK_SECRET to be set in .env (same requirement as
the real webhook — the hashing utility refuses to run without it).
"""
import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import get_database  # noqa: E402
from app.services import sajag_hashing, sajag_report_service  # noqa: E402

# Scenarios drawn directly from the concept note's own illustrative examples
# (Section 4, "The user journey") plus a few more spanning all three
# triangulation tiers and several statuses, so the dashboard doesn't look
# artificially uniform.
DEMO_REPORTS = [
    {
        "phone": "9812300001",
        "text": "There is no footpath near the school crossing on NH48. Children have to walk on the highway shoulder every morning.",
        "tags": ["no_footpath"],
        "lat": 19.0822, "lng": 72.8811,
        "tier": "Confirmed",
        "status": "Escalated",
        "days_ago": 6,
    },
    {
        "phone": "9812300002",
        "text": "Missing median gap barrier near the village turn — two-wheelers cut across into oncoming traffic constantly.",
        "tags": ["median_gap"],
        "lat": 18.9982, "lng": 73.1197,
        "tier": "Confirmed",
        "status": "In-Progress",
        "days_ago": 9,
    },
    {
        "phone": "9812300003",
        "text": "The shoulder near the bus stop has no lighting at all. Women from our village avoid walking there after dark.",
        "tags": ["poor_lighting"],
        "lat": 19.2183, "lng": 72.9781,
        "tier": "Emerging",
        "status": "Validated",
        "days_ago": 4,
    },
    {
        "phone": "9812300004",
        "text": "Tractors from the farms merge directly onto the expressway with no visibility of oncoming traffic. Almost had an accident last week.",
        "tags": ["blind_merge"],
        "lat": 18.6298, "lng": 73.7997,
        "tier": "Emerging",
        "status": "Triaged",
        "days_ago": 2,
    },
    {
        "phone": "9812300005",
        "text": "Trucks routinely speed through the village stretch, no signage warning them to slow down near the crossing.",
        "tags": ["speeding_reported", "missing_signage"],
        "lat": 19.1590, "lng": 72.9989,
        "tier": "Confirmed",
        "status": "Resolved",
        "days_ago": 21,
    },
    {
        "phone": "9812300006",
        "text": "Unmarked crossing point used daily by workers going to the fields on the other side of the highway.",
        "tags": ["unmarked_crossing"],
        "lat": 19.0330, "lng": 73.0297,
        "tier": "Contextual",
        "status": "Received",
        "days_ago": 0,
    },
    {
        "phone": "9812300007",
        "text": "Same missing footpath issue as before near the school — still not fixed, children still walking on the road.",
        "tags": ["no_footpath"],
        "lat": 19.0825, "lng": 72.8815,
        "tier": "Confirmed",
        "status": "Feedback-Sent",
        "days_ago": 30,
    },
]


def seed(wipe: bool = False) -> None:
    db = get_database()
    collection = db[sajag_report_service.COLLECTION_NAME]

    if wipe:
        result = collection.delete_many({"glific_contact_id": {"$regex": "^demo_"}})
        print(f"Wiped {result.deleted_count} existing demo report(s).")

    created = 0
    for i, scenario in enumerate(DEMO_REPORTS, start=1):
        contact_id = f"demo_contact_{i:03d}"
        received_at = (datetime.utcnow() - timedelta(days=scenario["days_ago"])).isoformat()

        phone_hash = sajag_hashing.hash_phone_number(scenario["phone"])

        report = sajag_report_service.create_report(
            glific_contact_id=contact_id,
            contact_phone_hash=phone_hash,
            channel="whatsapp",
            language_id="hi",
            location={"latitude": scenario["lat"], "longitude": scenario["lng"], "accuracy_meters": 15.0},
            photo_url=None,
            received_at=received_at,
        )

        sajag_report_service.update_report_processing(
            report["report_id"],
            transcription=scenario["text"],
            hazard_tags=scenario["tags"],
        )

        # create_report/update_report_processing don't set triangulation_tier or
        # status beyond "Received" — set both directly here since seeding demo
        # data is a special case, not something the real pipeline does today.
        collection.update_one(
            {"report_id": report["report_id"]},
            {"$set": {"triangulation_tier": scenario["tier"], "status": scenario["status"]}},
        )

        created += 1
        print(f"  [{created}/{len(DEMO_REPORTS)}] {contact_id} -> {scenario['status']} ({scenario['tier']})")

    print(f"\nSeeded {created} demo Sajag report(s). View them at /sajag in the dashboard.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wipe", action="store_true", help="Delete existing demo_* reports first")
    args = parser.parse_args()
    seed(wipe=args.wipe)

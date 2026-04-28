"""POST /update-deal — move a GHL contact to the right pipeline stage and add a note."""
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import httpx, os

router = APIRouter()

GHL_API_KEY = os.getenv("GHL_API_KEY", "")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "")
GHL_BASE = "https://services.leadconnectorhq.com"

# Pipeline stage name → stage ID mapping (use GHL API to look up if needed)
PIPELINE_STAGES = {
    "New Lead": {"pipeline": "OsChWDlo8VZVOb6ENFl9"},
    "Contacted": {"pipeline": "OsChWDlo8VZVOb6ENFl9"},
    "Appointment Set": {"pipeline": "OsChWDlo8VZVOb6ENFl9"},
    "Offer Sent": {"pipeline": "OsChWDlo8VZVOb6ENFl9"},
    "Under Contract": {"pipeline": "OsChWDlo8VZVOb6ENFl9"},
    "Closed": {"pipeline": "OsChWDlo8VZVOb6ENFl9"},
}

class DealUpdate(BaseModel):
    contact_id: str
    stage_name: str
    deal_value: Optional[float] = None
    property_address: Optional[str] = None
    notes: Optional[str] = None

@router.post("/update-deal")
async def update_deal(body: DealUpdate):
    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json",
        "Version": "2021-07-28",
    }
    results = {}

    async with httpx.AsyncClient() as client:
        # First look up existing opportunities for this contact
        opps_r = await client.get(
            f"{GHL_BASE}/opportunities/search",
            headers=headers,
            params={"location_id": GHL_LOCATION_ID, "contact_id": body.contact_id}
        )
        opps = opps_r.json().get("opportunities", [])

        # Get pipeline stages to find the right stage_id
        pipeline_id = "OsChWDlo8VZVOb6ENFl9"  # default probate pipeline
        stages_r = await client.get(
            f"{GHL_BASE}/opportunities/pipelines/{pipeline_id}",
            headers=headers,
            params={"locationId": GHL_LOCATION_ID}
        )
        stages_data = stages_r.json()

        # Find stage_id by name
        stage_id = None
        for stage in stages_data.get("stages", []):
            if stage.get("name", "").lower() == body.stage_name.lower():
                stage_id = stage["id"]
                break

        if opps and stage_id:
            opp_id = opps[0]["id"]
            update_body = {"stageId": stage_id}
            if body.deal_value:
                update_body["monetaryValue"] = body.deal_value
            if body.property_address:
                update_body["name"] = f"Deal - {body.property_address}"

            r = await client.put(
                f"{GHL_BASE}/opportunities/{opp_id}",
                headers=headers,
                json=update_body
            )
            results["opportunity"] = r.status_code

        # Add note
        if body.notes:
            note_r = await client.post(
                f"{GHL_BASE}/contacts/{body.contact_id}/notes",
                headers=headers,
                json={"body": body.notes, "userId": ""}
            )
            results["note"] = note_r.status_code

    return {"status": "ok", "contact_id": body.contact_id, "stage": body.stage_name, "results": results}

"""Bitrix24 API wrapper."""
import time
import requests
from config import B24_WEBHOOK_URL

_last_call = 0.0


def _throttle():
    """Keep ~2 req/sec max."""
    global _last_call
    elapsed = time.time() - _last_call
    if elapsed < 0.5:
        time.sleep(0.5 - elapsed)
    _last_call = time.time()


def call(method: str, params: dict | None = None) -> dict:
    _throttle()
    url = f"{B24_WEBHOOK_URL}/{method}"
    resp = requests.get(url, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_new_leads(limit: int = 50) -> list[dict]:
    """Fetch leads with status NEW, newest first."""
    params = {
        "order[ID]": "DESC",
        "filter[STATUS_ID]": "NEW",
        "select[0]": "ID",
        "select[1]": "TITLE",
        "select[2]": "NAME",
        "select[3]": "LAST_NAME",
        "select[4]": "COMPANY_TITLE",
        "select[5]": "SOURCE_ID",
        "select[6]": "SOURCE_DESCRIPTION",
        "select[7]": "PHONE",
        "select[8]": "EMAIL",
        "select[9]": "DATE_CREATE",
        "select[10]": "COMMENTS",
        "start": 0,
    }
    data = call("crm.lead.list.json", params)
    return data.get("result", [])


def get_lead_activities(lead_id: int) -> list[dict]:
    """Get activities (calls, emails) linked to a lead."""
    params = {
        "filter[OWNER_TYPE_ID]": 1,  # Lead
        "filter[OWNER_ID]": lead_id,
        "select[0]": "ID",
        "select[1]": "SUBJECT",
        "select[2]": "TYPE_ID",
        "select[3]": "DESCRIPTION",
        "select[4]": "SETTINGS",
        "select[5]": "FILES",
        "select[6]": "PROVIDER_ID",
    }
    data = call("crm.activity.list.json", params)
    return data.get("result", [])


def get_call_record_url(lead_id: int) -> str | None:
    """Find Novofon call record URL for a lead via voximplant stats."""
    params = {
        "FILTER[CRM_ENTITY_TYPE]": "LEAD",
        "FILTER[CRM_ENTITY_ID]": lead_id,
        "SORT": "CALL_START_DATE",
        "ORDER": "DESC",
    }
    try:
        data = call("voximplant.statistic.get.json", params)
        for rec in data.get("result", []):
            url = rec.get("CALL_RECORD_URL")
            if url:
                return url
    except Exception:
        pass
    return None


def get_lead_email_body(lead_id: int) -> str | None:
    """Get email body from lead's CRM activities."""
    import re
    import html as html_module
    activities = get_lead_activities(lead_id)
    for act in activities:
        if act.get("PROVIDER_ID") == "CRM_EMAIL" or act.get("TYPE_ID") == "4":
            desc = act.get("DESCRIPTION", "")
            if desc:
                # Strip HTML tags
                clean = re.sub(r"<[^>]+>", " ", desc)
                clean = html_module.unescape(clean)
                clean = re.sub(r"\s+", " ", clean).strip()
                if len(clean) > 50:  # Skip empty/tiny bodies
                    return clean[:5000]
    return None


def update_lead_status(lead_id: int, status_id: str, comment: str = ""):
    """Update lead status and optionally add comment."""
    params = {
        "id": lead_id,
        "fields[STATUS_ID]": status_id,
    }
    if comment:
        params["fields[COMMENTS]"] = comment
    call("crm.lead.update.json", params)

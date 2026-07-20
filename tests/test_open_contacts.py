"""Unit tests for the photo → contacts feature in fw_import.

Covered:
  - merge_contacts_into_candidate: distribution + dedup (no service involved)
  - open_contacts_by_photo: calls contacts-service over HTTP (requests mocked)
  - import_hh_to_fw(open_contacts=False): the lookup is never invoked

The paid bot lives behind contacts-service; hr-radar only makes an HTTP call,
which is mocked here — the real service/bot is never touched. Dedup/budget/audit
are the service's responsibility (tested in contacts-service).
"""
import sys
import json
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import fw_import  # noqa: E402


# ─── merge_contacts_into_candidate ───

def test_merge_distributes_by_category():
    candidate = {}
    contacts = {
        "phones": ["79161234567"],
        "emails": ["a@b.ru"],
        "telegram": ["@petrov"],
        "whatsapp": ["79990001122"],
        "vk": ["https://vk.com/petrov"],
        "instagram": ["https://instagram.com/petrov"],
        "other_socials": ["https://ok.ru/petrov"],
    }
    fw_import.merge_contacts_into_candidate(candidate, contacts)

    assert {"Phone": "79161234567"} in candidate["Contacts"]
    assert {"Email": "a@b.ru"} in candidate["Contacts"]
    assert {"Telegram": "@petrov"} in candidate["Contacts"]
    assert {"WhatsApp": "79990001122"} in candidate["Contacts"]
    assert candidate["SocialLinks"]["VK"] == "https://vk.com/petrov"
    assert candidate["SocialLinks"]["Instagram"] == "https://instagram.com/petrov"
    assert candidate["SocialLinks"]["OK"] == "https://ok.ru/petrov"


def test_merge_deduplicates_against_existing():
    candidate = {
        "Contacts": [{"Telegram": "@ivan"}],
        "SocialLinks": {"Telegram": "https://t.me/ivan"},
    }
    contacts = {
        "telegram": ["@ivan", "@petrov"],   # @ivan already present
        "vk": ["https://vk.com/new"],
    }
    fw_import.merge_contacts_into_candidate(candidate, contacts)

    tg_entries = [c for c in candidate["Contacts"] if c.get("Telegram") == "@ivan"]
    assert len(tg_entries) == 1, "existing telegram must not be duplicated"
    assert {"Telegram": "@petrov"} in candidate["Contacts"]
    # SocialLinks Telegram must not be overwritten; VK added.
    assert candidate["SocialLinks"]["Telegram"] == "https://t.me/ivan"
    assert candidate["SocialLinks"]["VK"] == "https://vk.com/new"


# ─── open_contacts_by_photo (HTTP → contacts-service) ───

def _fake_service_response(payload, status=200):
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    fake = MagicMock()
    fake.post.return_value = resp
    return fake


def test_open_contacts_calls_service(monkeypatch):
    payload = {
        "found": True, "cached": False,
        "phones": ["79161234567"], "emails": ["x@y.ru"], "telegram": ["@target"],
        "whatsapp": [], "vk": ["https://vk.com/target"], "instagram": [], "other_socials": [],
    }
    fake_req = _fake_service_response(payload)
    monkeypatch.setattr(fw_import, "requests", fake_req)
    monkeypatch.setattr(fw_import, "CONTACTS_SERVICE_URL", "http://svc:8099")
    monkeypatch.setattr(fw_import, "CONTACTS_SERVICE_TOKEN", "tok")

    import base64
    photo_b64 = base64.b64encode(b"jpeg-bytes").decode()
    hh = {"photo": {"500": "http://example/pic.jpg"}}
    url = "https://hh.ru/resume/abcdef123456"

    res = fw_import.open_contacts_by_photo(hh, url, photo_b64=photo_b64)
    assert res["phones"] == ["79161234567"]
    assert res["telegram"] == ["@target"]
    assert res["vk"] == ["https://vk.com/target"]
    # POST ушёл на сервис с identity=hh_url и токеном
    fake_req.post.assert_called_once()
    _, kwargs = fake_req.post.call_args
    assert kwargs["json"]["identity"] == url
    assert kwargs["headers"]["X-Auth-Token"] == "tok"


def test_open_contacts_no_service_url_skips_call(monkeypatch):
    fake_req = MagicMock()
    monkeypatch.setattr(fw_import, "requests", fake_req)
    monkeypatch.setattr(fw_import, "CONTACTS_SERVICE_URL", "")   # сервис не настроен
    res = fw_import.open_contacts_by_photo({"photo": {"500": "u"}}, "https://hh.ru/resume/x", photo_b64="Zg==")
    assert res == fw_import._empty_contacts()
    fake_req.post.assert_not_called()


def test_open_contacts_no_url_skips_call(monkeypatch):
    fake_req = MagicMock()
    monkeypatch.setattr(fw_import, "requests", fake_req)
    monkeypatch.setattr(fw_import, "CONTACTS_SERVICE_URL", "http://svc:8099")
    res = fw_import.open_contacts_by_photo({"photo": {"500": "u"}}, "", photo_b64="Zg==")
    assert res == fw_import._empty_contacts()
    fake_req.post.assert_not_called()


# ─── import_hh_to_fw(open_contacts=False) ───

def test_import_open_contacts_false_never_calls_lookup(monkeypatch):
    fake_hh = types.ModuleType("hh_api")
    fake_hh.get_resume = lambda rid: {
        "alternate_url": "https://hh.ru/resume/deadbeef",
        "first_name": "Ivan", "last_name": "Petrov",
    }
    fake_hh.hh_request = lambda *a, **k: MagicMock(status_code=404)
    monkeypatch.setitem(sys.modules, "hh_api", fake_hh)

    monkeypatch.setattr(fw_import, "_load_import_log", lambda: {})
    monkeypatch.setattr(fw_import, "_save_import_log", lambda log: None)

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"Result": {"CandidateId": 555}}
    resp.text = ""
    fake_requests = MagicMock()
    fake_requests.post.return_value = resp
    fake_requests.get.return_value = resp
    monkeypatch.setattr(fw_import, "requests", fake_requests)

    # Spy: the photo lookup must stay untouched when the feature is off.
    spy = MagicMock()
    monkeypatch.setattr(fw_import, "open_contacts_by_photo", spy)

    res = fw_import.import_hh_to_fw("deadbeef", 123, open_contacts=False)

    spy.assert_not_called()
    assert res["candidate_id"] == 555

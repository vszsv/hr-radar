#!/usr/bin/env python3
"""Lead Intake v2 — three independent channels:
1. IMAP — new emails on client@btl-agency.ru
2. B24 — CRM form submissions (status NEW)
3. B24 — calls with recordings (transcribe + qualify)

Only NEW items are processed (tracked in SQLite).
"""
import sys
import time
import traceback
import imaplib
import email
import email.message
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
import re
import html as html_module

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from config import (
    POLL_INTERVAL_SEC, AUTO_UPDATE_STATUS, DEBUG_SKIPPED,
    IMAP_HOST, IMAP_USER, IMAP_PASS,
)
from db import init_db, is_processed, save_result
from b24 import get_new_leads, get_lead_activities, get_call_record_url, update_lead_status
from qualifier import qualify_lead
from transcriber import process_call
from notifier import notify_qualified_lead, notify_call, notify_form


# ── Helpers ──────────────────────────────────────────────

def _handle_result(item_id: str, title: str, result: dict, source: str, contact: dict):
    """Notify + save based on qualification result."""
    qualified = result.get("qualified")
    cat = result.get("category", "unknown")

    if qualified or qualified is None or cat == "needs_review":
        notify_qualified_lead(int(item_id), title, result, source, contact)
        if AUTO_UPDATE_STATUS and qualified and str(item_id).isdigit():
            update_lead_status(int(item_id), "IN_PROCESS", f"[AI] {result.get('summary', '')}")
    else:
        if AUTO_UPDATE_STATUS and cat in ("spam", "vendor"):
            update_lead_status(int(item_id), "UC_C9CDH2", f"[AI] {result.get('reason', '')}")
        if DEBUG_SKIPPED:
            icon = {"spam": "🗑", "vendor": "🏭", "current_project": "🔄"}.get(cat, "⏭️")
            from notifier import send_message
            send_message(f"{icon} <i>Пропущено ({cat}):</i> {title[:80]}\n<i>{result.get('reason','')[:100]}</i>")

    is_q = 1 if qualified else (0 if qualified is False else -1)
    save_result(int(item_id) if str(item_id).isdigit() else hash(item_id),
                source, title, is_q, cat, result.get("summary", ""))


def _decode_header(raw):
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for data, charset in parts:
        if isinstance(data, bytes):
            out.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(data)
    return " ".join(out)


def _extract_text(msg):
    texts = []
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                texts.append(payload.decode(charset, errors="replace"))
            elif ct == "text/html" and not texts:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                raw = payload.decode(charset, errors="replace")
                clean = re.sub(r"<[^>]+>", " ", raw)
                clean = html_module.unescape(clean)
                clean = re.sub(r"\s+", " ", clean).strip()
                texts.append(clean)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            raw = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                raw = re.sub(r"<[^>]+>", " ", raw)
                raw = html_module.unescape(raw)
                raw = re.sub(r"\s+", " ", raw).strip()
            texts.append(raw)
    return "\n".join(texts)[:5000]


# ── Channel 1: IMAP emails ──────────────────────────────

def poll_imap():
    """Check for new emails via IMAP. Returns count of new items processed."""
    count = 0
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
        conn.login(IMAP_USER, IMAP_PASS)
        print("[imap] Connected OK")

        since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%d-%b-%Y")

        for folder in ["INBOX"]:
            try:
                status, _ = conn.select(folder, readonly=True)
                if status != "OK":
                    continue
            except Exception as e:
                print(f"[imap] Cannot open {folder}: {e}")
                continue

            print(f"[imap] Searching {folder} since {since}...")
            _, msg_nums = conn.search(None, f"(SINCE {since} UNSEEN)")
            ids = msg_nums[0].split() if msg_nums[0] else []
            print(f"[imap] Found {len(ids)} unseen messages")
            if not ids:
                _, msg_nums_all = conn.search(None, f"(SINCE {since})")
                ids = msg_nums_all[0].split() if msg_nums_all[0] else []
                ids = ids[-10:]
                print(f"[imap] Fallback: {len(ids)} recent messages")

            for i, msg_id in enumerate(reversed(ids[-50:])):
                # Use IMAP UID as unique key
                print(f"[imap/{folder}] Checking msg {i+1}/{min(len(ids),50)}: seq={msg_id}", flush=True)
                _, uid_data = conn.fetch(msg_id, "(UID)")
                uid_str = uid_data[0].decode() if uid_data[0] else ""
                uid_match = re.search(r"UID\s+(\d+)", uid_str)
                uid = int(uid_match.group(1)) if uid_match else int(msg_id)
                db_key = 1000000 + uid  # Offset to avoid collision with B24 lead IDs

                if is_processed(db_key):
                    continue

                # Fetch full message
                _, full_data = conn.fetch(msg_id, "(RFC822)")
                if not full_data or not full_data[0]:
                    continue
                raw = full_data[0][1] if isinstance(full_data[0], tuple) else b""
                msg = email.message_from_bytes(raw)

                subj = _decode_header(msg.get("Subject", ""))
                from_addr = _decode_header(msg.get("From", ""))
                body = _extract_text(msg)

                # Extract sender email
                email_match = re.search(r"[\w.-]+@[\w.-]+", from_addr)
                sender_email = email_match.group(0) if email_match else from_addr
                sender_name = re.sub(r"<[^>]+>", "", from_addr).strip().strip('"')

                print(f"[imap/{folder}] New email: {subj[:60]} from {sender_email}")

                lead_text = f"Тема: {subj}\nОт: {sender_name} ({sender_email})\n\nТело письма:\n{body}"
                result = qualify_lead(lead_text, source_type="email")

                contact = {"name": sender_name, "email": sender_email, "company": "", "phone": ""}
                _handle_result(db_key, subj, result, "email", contact)
                count += 1

        conn.close()
        conn.logout()
    except Exception as e:
        print(f"[imap] Error: {e}")
        traceback.print_exc()

    return count


# ── Channel 2: B24 CRM forms ────────────────────────────

def poll_b24_forms():
    """Check for new CRM form submissions in B24."""
    count = 0
    leads = get_new_leads()

    for lead in leads:
        lead_id = int(lead["ID"])
        title = lead.get("TITLE", "")

        # Only process CRM forms
        src = (lead.get("SOURCE_ID") or "").upper()
        is_form = (
            src in ("CRM_FORM", "WEB")
            or "crm-форм" in title.lower()
            or "заполнение" in title.lower()
        )
        if not is_form:
            continue

        if is_processed(lead_id):
            continue

        phones = [p["VALUE"] for p in (lead.get("PHONE") or []) if p.get("VALUE")]
        emails_list = [e["VALUE"] for e in (lead.get("EMAIL") or []) if e.get("VALUE")]
        name = " ".join(p for p in [lead.get("NAME", ""), lead.get("LAST_NAME", "")] if p).strip()
        company = lead.get("COMPANY_TITLE", "") or ""
        if company == "Без названия":
            company = ""

        print(f"[b24-form] New form: #{lead_id} {title}")

        # Notify about form immediately
        notify_form(lead_id, title, name, company, phones[0] if phones else "")

        # Qualify
        lead_text = f"Форма с сайта: {title}\nИмя: {name}\nКомпания: {company}\nТелефон: {', '.join(phones)}\nEmail: {', '.join(emails_list)}"
        result = qualify_lead(lead_text, source_type="форма с сайта")

        contact = {"name": name, "company": company, "phone": phones[0] if phones else "", "email": emails_list[0] if emails_list else ""}
        _handle_result(lead_id, title, result, "form", contact)
        count += 1

    return count


# ── Channel 3: Novofon call statistics ──────────────────

def poll_novofon_calls():
    """Check for new calls via voximplant.statistic.get — independent of lead status."""
    count = 0
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00")
        from b24 import call as b24_call
        data = b24_call("voximplant.statistic.get.json", {
            "FILTER[>CALL_START_DATE]": since,
            "FILTER[CALL_TYPE]": "2",  # 2 = incoming
            "SORT": "CALL_START_DATE",
            "ORDER": "DESC",
        })

        for rec in data.get("result", []):
            call_id = rec.get("ID")
            if not call_id:
                continue

            db_key = 2000000 + int(call_id)  # Offset to avoid collision
            if is_processed(db_key):
                continue

            phone = rec.get("PHONE_NUMBER", "?")
            duration = int(rec.get("CALL_DURATION", 0))
            record_url = rec.get("CALL_RECORD_URL")
            lead_id = rec.get("CRM_ENTITY_ID", "")
            failed_code = rec.get("CALL_FAILED_CODE", "")

            # Determine if answered
            answered = duration > 0 and failed_code == "200"

            print(f"[novofon] Call #{call_id}: {phone} | {duration}s | record={'yes' if record_url else 'no'}")

            if not answered or not record_url:
                # Пропущенный или без записи
                status = "missed_call" if not answered else "call_no_recording"
                save_result(db_key, "call", f"Звонок от {phone}", 0, status, f"{'Пропущенный' if not answered else 'Без записи'} ({duration}s): {phone}")
                if DEBUG_SKIPPED:
                    from notifier import send_message
                    send_message(f"⏭️ <i>Пропущено:</i> 📞 {'Пропущенный' if not answered else 'Без записи'} звонок {phone} ({duration}s)")
            else:
                # Есть запись — транскрибируем и квалифицируем
                transcript = process_call(record_url, db_key)
                if transcript:
                    lead_text = f"Входящий звонок на номер агентства.\nТелефон звонящего: {phone}\nДлительность: {duration}с\n\nТранскрипт разговора:\n{transcript}"
                    result = qualify_lead(lead_text, source_type="звонок")
                    b24_link = f"\n🔗 https://btl-piter.bitrix24.ru/crm/lead/details/{lead_id}/" if lead_id else ""
                    notify_call(db_key if not lead_id else int(lead_id), phone, transcript, duration=duration, missed=False)
                    contact = {"name": "", "company": "", "phone": phone, "email": ""}
                    _handle_result(db_key, f"Звонок от {phone}", result, "call", contact)
                else:
                    save_result(db_key, "call", f"Звонок от {phone}", 0, "transcribe_failed", f"Не удалось транскрибировать: {phone}")

            count += 1

    except Exception as e:
        print(f"[novofon] Error: {e}")
        traceback.print_exc()

    return count


# ── Main loop ────────────────────────────────────────────

def run_cycle():
    print("[cycle] Starting: imap...", flush=True)
    n1 = poll_imap()
    print(f"[cycle] imap done ({n1}). forms...", flush=True)
    n2 = poll_b24_forms()
    print(f"[cycle] forms done ({n2}). calls...", flush=True)
    n3 = poll_novofon_calls()
    print(f"[cycle] calls done ({n3}).", flush=True)
    total = n1 + n2 + n3
    if total:
        print(f"[intake] Cycle done: {n1} emails, {n2} forms, {n3} calls")
    return total


def main():
    print("[intake] Lead Intake v2 starting...")
    print(f"[intake] IMAP: {IMAP_USER}")
    print(f"[intake] Polling every {POLL_INTERVAL_SEC}s")
    init_db()

    # Initial scan
    print("[intake] Initial scan...")
    try:
        run_cycle()
    except Exception as e:
        print(f"[intake] Initial scan error: {e}")
        traceback.print_exc()

    # Polling loop
    while True:
        time.sleep(POLL_INTERVAL_SEC)
        try:
            run_cycle()
        except Exception as e:
            print(f"[intake] Cycle error: {e}")
            traceback.print_exc()
            time.sleep(30)


if __name__ == "__main__":
    main()

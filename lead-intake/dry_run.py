#!/usr/bin/env python3
"""Dry run — process current NEW leads, print results, no status changes, no TG notifications."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from db import init_db
from b24 import get_new_leads, get_call_record_url, get_lead_activities
from qualifier import qualify_lead
from transcriber import process_call
from imap_reader import fetch_email_body

init_db()

leads = get_new_leads()
print(f"Found {len(leads)} NEW leads\n")

for lead in leads:
    lid = lead["ID"]
    title = lead.get("TITLE", "")
    src = lead.get("SOURCE_ID", "")
    
    # Detect source
    source = "email"
    if src == "CALL" or "звонок" in title.lower():
        source = "call"
    elif src in ("CRM_FORM", "WEB") or "crm-форм" in title.lower() or "заполнение" in title.lower():
        source = "form"
    
    phones = [p["VALUE"] for p in (lead.get("PHONE") or []) if p.get("VALUE")]
    emails = [e["VALUE"] for e in (lead.get("EMAIL") or []) if e.get("VALUE")]
    name = " ".join(p for p in [lead.get("NAME",""), lead.get("LAST_NAME","")] if p).strip()
    company = lead.get("COMPANY_TITLE","") or ""
    
    print(f"--- #{lid} [{source}] {title}")
    print(f"    Contact: {name} | {company} | {', '.join(phones)} | {', '.join(emails)}")
    
    if source == "call":
        # Check if answered
        activities = get_lead_activities(int(lid))
        missed = True
        for act in activities:
            settings = act.get("SETTINGS") or {}
            if not settings.get("MISSED_CALL"):
                missed = False
        
        record_url = get_call_record_url(int(lid)) if not missed else None
        
        if missed:
            print(f"    ❌ Пропущенный звонок")
        elif record_url:
            print(f"    🎙️ Есть запись, транскрибирую...")
            transcript = process_call(record_url, int(lid))
            if transcript:
                print(f"    📝 Транскрипт: {transcript[:200]}...")
                lead_text = f"Звонок от {phones[0] if phones else '?'}.\nТранскрипт:\n{transcript}"
                result = qualify_lead(lead_text, "звонок")
                print(f"    🤖 Квалификация: {result}")
        else:
            print(f"    ☎️ Отвеченный, но без записи")
    
    elif source == "form":
        lead_text = f"Форма: {title}\nИмя: {name}\nКомпания: {company}\nТелефон: {', '.join(phones)}"
        result = qualify_lead(lead_text, "форма с сайта")
        print(f"    🤖 Квалификация: {result}")
    
    else:
        email_body = fetch_email_body(title, sender_email=emails[0] if emails else "")
        if email_body:
            print(f"    📧 IMAP body: {len(email_body)} chars")
            lead_text = f"Тема: {title}\nОт: {name} ({', '.join(emails)})\nКомпания: {company}\n\nТело письма:\n{email_body}"
        else:
            print(f"    ⚠️ No IMAP body found")
            lead_text = f"Тема: {title}\nОт: {name} ({', '.join(emails)})\nКомпания: {company}\nОписание: {lead.get('SOURCE_DESCRIPTION','')}"
        result = qualify_lead(lead_text, "email")
        print(f"    🤖 Квалификация: {result}")
    
    print()

#!/usr/bin/env python3
"""Quick test: fetch email bodies for key leads."""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from imap_reader import fetch_email_body
from qualifier import qualify_lead

tests = [
    ("RE: EXT \\\\ ARS MAGNA CG \\\\ Уралхим  \\\\ 45 лет и День Химика 2026", "olga.nikonova@uralchem.com"),
    ("RE: ОПХ // ПРОМО бренда ШИХАН // Онлайн-бриф", "svetlana.makarskaya@unitedbrew.com"),
    ("Приглашение к участию в Процедуре по выбору единого поставщика корпоративных мероприятий для Группы Компаний Бургер Кинг", "polina.lee@burgerkingrus.ru"),
    ("RE: Defa group дегустации в торговых сетях", "s.peregontseva@defagroup.com"),
]

for subj, sender in tests:
    print(f"\n{'='*60}")
    print(f"Subject: {subj[:70]}")
    print(f"Sender: {sender}")
    body = fetch_email_body(subj, sender_email=sender, days_back=60)
    if body:
        print(f"✅ Body found: {len(body)} chars")
        print(f"Preview: {body[:300]}...")
        
        lead_text = f"Тема: {subj}\nОт: {sender}\n\nТело письма:\n{body}"
        result = qualify_lead(lead_text, "email")
        print(f"\n🤖 Квалификация: qualified={result['qualified']}, category={result['category']}, confidence={result['confidence']}")
        print(f"   Reason: {result['reason']}")
        print(f"   Summary: {result['summary']}")
    else:
        print(f"❌ No body found")

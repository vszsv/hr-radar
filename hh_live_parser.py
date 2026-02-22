#!/usr/bin/env python3
"""
HH Resume Parser - live login + parse in single browser session.
No cookie export/import - everything happens in one Playwright context.
"""

import os
import re
import json
import time
import imaplib
import email
from datetime import datetime, timedelta, UTC
from pathlib import Path
from typing import Dict, List

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from playwright_stealth import Stealth


def _load_env():
    for line in Path('/opt/hr-radar/.env').read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip())

_load_env()

HH_LOGIN = os.environ.get('HH_LOGIN', '')
HH_PASSWORD = os.environ.get('HH_PASSWORD', '')
IMAP_HOST = os.environ.get('HH_IMAP_HOST', os.environ.get('IMAP_HOST', 'imap.yandex.ru'))
IMAP_PORT = int(os.environ.get('HH_IMAP_PORT', os.environ.get('IMAP_PORT', '993')))
IMAP_USER = os.environ.get('HH_IMAP_USER', os.environ.get('IMAP_USER', ''))
IMAP_PASS = os.environ.get('HH_IMAP_PASS', os.environ.get('IMAP_PASS', ''))


def fetch_hh_code(max_age_min=10):
    """Fetch latest HH verification code from email."""
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        M.login(IMAP_USER, IMAP_PASS)
        M.select('INBOX')
        since = (datetime.now(UTC) - timedelta(days=1)).strftime('%d-%b-%Y')
        st, data = M.search(None, f'(SINCE {since})')
        ids = data[0].split() if data and data[0] else []
        for mid in reversed(ids[-60:]):
            st, msg_data = M.fetch(mid, '(RFC822)')
            if st != 'OK':
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subj = str(email.header.make_header(email.header.decode_header(msg.get('Subject', ''))))
            frm = (msg.get('From', '') or '').lower()
            blob = (subj + ' ' + frm).lower()
            if not any(k in blob for k in ['hh', 'headhunter', 'код', 'code', 'подтверж']):
                continue
            body = ''
            if msg.is_multipart():
                for p in msg.walk():
                    if p.get_content_type() in ('text/plain', 'text/html'):
                        payload = p.get_payload(decode=True)
                        if payload:
                            body = payload.decode(p.get_content_charset() or 'utf-8', errors='ignore')
                            break
            else:
                payload = msg.get_payload(decode=True)
                body = payload.decode(msg.get_content_charset() or 'utf-8', errors='ignore') if payload else ''
            m = re.search(r'\b(\d{4,8})\b', subj + '\n' + body)
            if m:
                M.logout()
                return m.group(1)
        M.logout()
    except Exception as e:
        print(f"IMAP error: {e}")
    return None


def _click(page, selectors):
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=2000)
            return True
        except Exception:
            pass
    return False


def _fill(page, selectors, value):
    for sel in selectors:
        try:
            page.locator(sel).first.fill(value, timeout=2000)
            return True
        except Exception:
            pass
    return False


def login_hh(page):
    """Login to HH in the current page."""
    print("  Logging into HH...")
    page.goto('https://hh.ru/account/login?backurl=%2F', wait_until='domcontentloaded', timeout=30000)
    time.sleep(3)
    
    _click(page, ['text=Войти', 'a[data-qa="login"]', 'button:has-text("Войти")'])
    time.sleep(1)
    
    _fill(page, ['input[name="email"]', 'input[type="email"]', 'input[name="login"]', 'input[data-qa="login-input-username"]'], HH_LOGIN)
    _click(page, ['button[type="submit"]', 'button:has-text("Продолжить")', 'button:has-text("Войти")'])
    time.sleep(2)
    
    _fill(page, ['input[name="password"]', 'input[type="password"]', 'input[data-qa="login-input-password"]'], HH_PASSWORD)
    _click(page, ['button[type="submit"]', 'button:has-text("Войти")', 'button:has-text("Продолжить")'])
    time.sleep(3)
    
    # Handle OTP if needed
    for _ in range(6):
        if '/account/login' not in page.url:
            break
        code = fetch_hh_code()
        if code:
            if _fill(page, ['input[name="otp"]', 'input[name="code"]', 'input[inputmode="numeric"]', 'input[type="tel"]'], code):
                _click(page, ['button[type="submit"]', 'button:has-text("Подтвердить")', 'button:has-text("Продолжить")'])
        time.sleep(3)
    
    logged_in = '/account/login' not in page.url
    print(f"  Login {'✅ OK' if logged_in else '❌ FAILED'} (URL: {page.url[:60]})")
    return logged_in


def parse_resume(page) -> Dict:
    """Parse resume from currently loaded page."""
    result = {
        'ok': False,
        'first_name': '',
        'last_name': '',
        'middle_name': '',
        'age': '',
        'gender': '',
        'city': '',
        'salary': '',
        'position': '',
        'experience': [],
        'education': [],
        'skills': [],
        'languages': [],
        'about': '',
    }
    
    body_text = page.locator('body').text_content()[:5000]
    body_clean = re.sub(r'\s+', ' ', body_text)
    
    if 'Произошла ошибка' in body_clean or 'Войдите или зарегистрируйтесь' in body_clean:
        result['reason'] = 'auth_required_or_error'
        return result
    
    if not any(k in body_clean for k in ['Опыт работы', 'Образование', 'Навыки', 'Ключевые навыки']):
        result['reason'] = 'no_resume_content'
        return result
    
    result['ok'] = True
    
    # Name
    try:
        el = page.locator('[data-qa="resume-personal-name"]').first
        name = el.text_content(timeout=2000).strip()
        parts = name.split()
        result['last_name'] = parts[0] if parts else ''
        result['first_name'] = parts[1] if len(parts) > 1 else ''
        result['middle_name'] = parts[2] if len(parts) > 2 else ''
    except Exception:
        pass
    
    # Position
    try:
        el = page.locator('[data-qa="resume-block-title-position"]').first
        result['position'] = el.text_content(timeout=2000).strip()
    except Exception:
        pass
    
    # Age
    try:
        el = page.locator('[data-qa="resume-personal-age"]').first
        text = el.text_content(timeout=2000)
        m = re.search(r'(\d+)', text)
        if m:
            result['age'] = m.group(1)
        if 'женщина' in text.lower():
            result['gender'] = 'Ж'
        elif 'мужчина' in text.lower():
            result['gender'] = 'М'
    except Exception:
        pass
    
    # City
    try:
        el = page.locator('[data-qa="resume-personal-address"]').first
        result['city'] = el.text_content(timeout=2000).strip()
    except Exception:
        pass
    
    # Salary
    try:
        el = page.locator('[data-qa="resume-block-salary"]').first
        result['salary'] = el.text_content(timeout=2000).strip()
    except Exception:
        pass
    
    # Experience
    try:
        items = page.locator('[data-qa="resume-block-experience-position"]').all()
        for item in items[:10]:
            try:
                text = item.text_content(timeout=1500).strip()
                if text:
                    result['experience'].append(text)
            except Exception:
                pass
    except Exception:
        pass
    
    # Skills
    try:
        tags = page.locator('[data-qa="bloko-tag__text"]').all()
        for tag in tags[:30]:
            try:
                text = tag.text_content(timeout=1000).strip()
                if text and len(text) > 1:
                    result['skills'].append(text)
            except Exception:
                pass
    except Exception:
        pass
    
    # Languages
    try:
        items = page.locator('[data-qa="resume-block-language-item"]').all()
        if not items:
            items = page.locator('[data-qa="resume-block-languages"] p').all()
        for item in items[:5]:
            try:
                text = item.text_content(timeout=1000).strip()
                if text:
                    result['languages'].append(text)
            except Exception:
                pass
    except Exception:
        pass
    
    # About
    try:
        el = page.locator('[data-qa="resume-block-skills-content"]').first
        result['about'] = el.text_content(timeout=2000).strip()[:2000]
    except Exception:
        pass
    
    return result


def parse_resumes_batch(resume_urls: List[str], max_count: int = 20) -> List[Dict]:
    """Login once, then parse multiple resumes."""
    
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/opt/hr-radar/ms-playwright')
    
    stealth = Stealth()
    results = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(viewport={'width': 1920, 'height': 1080})
        stealth.apply_stealth_sync(context)
        
        page = context.new_page()
        
        # Login
        if not login_hh(page):
            browser.close()
            return [{'ok': False, 'reason': 'login_failed', 'url': url} for url in resume_urls]
        
        # Parse each resume
        for i, url in enumerate(resume_urls[:max_count]):
            print(f"  [{i+1}/{min(len(resume_urls), max_count)}] {url[-40:]}...")
            
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=20000)
                time.sleep(4)
                
                result = parse_resume(page)
                result['external_link'] = url
                results.append(result)
                
                if result.get('ok'):
                    name = f"{result.get('first_name', '')} {result.get('last_name', '')}".strip()
                    print(f"    ✅ {name} | skills:{len(result.get('skills',[]))} exp:{len(result.get('experience',[]))}")
                else:
                    print(f"    ❌ {result.get('reason', 'unknown')}")
                
                time.sleep(2)
                
            except Exception as e:
                print(f"    ❌ Error: {e}")
                results.append({'ok': False, 'reason': str(e), 'external_link': url})
        
        browser.close()
    
    return results


if __name__ == '__main__':
    test_urls = [
        'https://hh.ru/resume/9421d8df0005234c030045769b387339426469',
    ]
    print("🧪 Testing live parser...")
    results = parse_resumes_batch(test_urls)
    for r in results:
        print(f"\nResult: ok={r.get('ok')}")
        if r.get('ok'):
            print(f"  Name: {r.get('first_name')} {r.get('last_name')}")
            print(f"  City: {r.get('city')}")
            print(f"  Skills: {r.get('skills')}")
        else:
            print(f"  Reason: {r.get('reason')}")
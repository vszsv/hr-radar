#!/usr/bin/env python3
import os, re, imaplib, email, time, json
from datetime import datetime, timedelta, UTC
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

def _load_env_fallback(path='/opt/hr-radar/.env'):
    vals = {}
    try:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            vals[k.strip()] = v.strip()
    except Exception:
        pass
    return vals

_FALLBACK = _load_env_fallback()

HH_LOGIN = os.environ.get('HH_LOGIN', _FALLBACK.get('HH_LOGIN',''))
HH_PASSWORD = os.environ.get('HH_PASSWORD', _FALLBACK.get('HH_PASSWORD',''))
COOKIES_PATH = Path(os.environ.get('HH_COOKIES_PATH', _FALLBACK.get('HH_COOKIES_PATH','/opt/hr-radar/data/hh_cookies.json')))
IMAP_HOST = os.environ.get('HH_IMAP_HOST', os.environ.get('IMAP_HOST', _FALLBACK.get('HH_IMAP_HOST', _FALLBACK.get('IMAP_HOST','imap.yandex.ru'))))
IMAP_PORT = int(os.environ.get('HH_IMAP_PORT', os.environ.get('IMAP_PORT', _FALLBACK.get('HH_IMAP_PORT', _FALLBACK.get('IMAP_PORT','993')))))
IMAP_USER = os.environ.get('HH_IMAP_USER', os.environ.get('IMAP_USER', _FALLBACK.get('HH_IMAP_USER', _FALLBACK.get('IMAP_USER',''))))
IMAP_PASS = os.environ.get('HH_IMAP_PASS', os.environ.get('IMAP_PASS', _FALLBACK.get('HH_IMAP_PASS', _FALLBACK.get('IMAP_PASS',''))))


def fetch_latest_hh_code(max_age_min=10):
    M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    M.login(IMAP_USER, IMAP_PASS)
    M.select('INBOX')
    since = (datetime.now(UTC) - timedelta(days=1)).strftime('%d-%b-%Y')
    st, data = M.search(None, f'(SINCE {since})')
    ids = data[0].split() if data and data[0] else []
    for mid in reversed(ids[-60:]):
        st, msg_data = M.fetch(mid, '(RFC822)')
        if st != 'OK' or not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        subj = str(email.header.make_header(email.header.decode_header(msg.get('Subject',''))))
        frm = (msg.get('From','') or '').lower()
        blob = (subj + ' ' + frm).lower()
        if not any(k in blob for k in ['hh', 'headhunter', 'код', 'code', 'подтверж']):
            continue
        body = ''
        if msg.is_multipart():
            for p in msg.walk():
                if p.get_content_type() in ('text/plain','text/html'):
                    payload = p.get_payload(decode=True)
                    if payload:
                        body = payload.decode(p.get_content_charset() or 'utf-8', errors='ignore')
                        break
        else:
            payload = msg.get_payload(decode=True)
            body = payload.decode(msg.get_content_charset() or 'utf-8', errors='ignore') if payload else ''
        txt = subj + '\n' + body
        m = re.search(r'\b(\d{4,8})\b', txt)
        if m:
            M.logout()
            return m.group(1)
    M.logout()
    return None


def click_any(page, selectors):
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=1500)
            return True
        except Exception:
            pass
    return False


def type_first(page, selectors, value):
    for sel in selectors:
        try:
            page.locator(sel).first.fill(value, timeout=1500)
            return True
        except Exception:
            pass
    return False


def main():
    if not HH_LOGIN or not HH_PASSWORD:
        raise SystemExit('HH_LOGIN/HH_PASSWORD missing')

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        page = context.new_page()
        page.goto('https://hh.ru/account/login?backurl=%2F', wait_until='domcontentloaded', timeout=60000)

        # some pages require clicking "Войти" first
        click_any(page, ['text=Войти', 'a[data-qa="login"]', 'button:has-text("Войти")'])

        ok_user = type_first(page,
            ['input[name="email"]', 'input[type="email"]', 'input[name="login"]', 'input[data-qa="login-input-username"]'],
            HH_LOGIN)
        if ok_user:
            click_any(page, ['button[type="submit"]', 'button:has-text("Продолжить")', 'button:has-text("Войти")'])

        # password step
        type_first(page,
            ['input[name="password"]', 'input[type="password"]', 'input[data-qa="login-input-password"]'],
            HH_PASSWORD)
        click_any(page, ['button[type="submit"]', 'button:has-text("Войти")', 'button:has-text("Продолжить")'])

        # possible code step
        for _ in range(6):
            if page.url.startswith('https://hh.ru/') and '/account/login' not in page.url:
                break
            code_filled = False
            code = fetch_latest_hh_code(max_age_min=10)
            if code:
                code_filled = type_first(page,
                    ['input[name="otp"]', 'input[name="code"]', 'input[inputmode="numeric"]', 'input[type="tel"]'],
                    code)
                if code_filled:
                    click_any(page, ['button[type="submit"]', 'button:has-text("Подтвердить")', 'button:has-text("Продолжить")'])
            time.sleep(2)

        page.goto('https://hh.ru/employer', wait_until='domcontentloaded', timeout=60000)
        if '/account/login' in page.url:
            raise SystemExit('HH login failed (still on login page)')

        COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        cookies = context.cookies()
        COOKIES_PATH.write_text(json.dumps({'cookies': cookies}, ensure_ascii=False), encoding='utf-8')
        print(f'cookies_saved={len(cookies)} path={COOKIES_PATH}')
        browser.close()


if __name__ == '__main__':
    main()

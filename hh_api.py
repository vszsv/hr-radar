"""HeadHunter API client with auto-refresh."""

import os
import requests
import time
import logging
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ENV_PATH = Path(__file__).parent / '.env'
load_dotenv(ENV_PATH)

HH_BASE = 'https://api.hh.ru'
USER_AGENT = 'AM-HR-Radar/1.0 (david@btl-agency.ru)'


def _load_tokens():
    """Load current tokens from .env."""
    load_dotenv(ENV_PATH, override=True)
    return {
        'access_token': os.environ.get('HH_ACCESS_TOKEN', ''),
        'refresh_token': os.environ.get('HH_REFRESH_TOKEN', ''),
        'client_id': os.environ.get('HH_CLIENT_ID', ''),
        'client_secret': os.environ.get('HH_CLIENT_SECRET', ''),
    }


def _save_tokens(access_token: str, refresh_token: str):
    """Update tokens in .env file."""
    env_text = ENV_PATH.read_text()
    
    import re
    env_text = re.sub(
        r'^HH_ACCESS_TOKEN=.*$',
        f'HH_ACCESS_TOKEN={access_token}',
        env_text, flags=re.MULTILINE
    )
    env_text = re.sub(
        r'^HH_REFRESH_TOKEN=.*$',
        f'HH_REFRESH_TOKEN={refresh_token}',
        env_text, flags=re.MULTILINE
    )
    
    ENV_PATH.write_text(env_text)
    os.environ['HH_ACCESS_TOKEN'] = access_token
    os.environ['HH_REFRESH_TOKEN'] = refresh_token
    logger.info('HH tokens refreshed and saved to .env')


def refresh_tokens() -> str:
    """Refresh access token using refresh_token. Returns new access_token."""
    t = _load_tokens()
    
    r = requests.post(f'{HH_BASE}/token', data={
        'grant_type': 'refresh_token',
        'refresh_token': t['refresh_token'],
        'client_id': t['client_id'],
        'client_secret': t['client_secret'],
    }, timeout=30)
    
    if r.status_code != 200:
        err = r.json() if r.headers.get('content-type', '').startswith('application/json') else {}
        if err.get('error') == 'invalid_grant' and 'not expired' in err.get('error_description', ''):
            logger.info('HH token still valid, no refresh needed')
            return t['access_token']
        raise Exception(f'HH token refresh failed: {r.status_code} {r.text}')
    
    data = r.json()
    new_access = data['access_token']
    new_refresh = data['refresh_token']
    
    _save_tokens(new_access, new_refresh)
    return new_access


def hh_request(method: str, path: str, **kwargs) -> requests.Response:
    """Make HH API request with auto-refresh on 403."""
    t = _load_tokens()
    headers = {
        'Authorization': f'Bearer {t["access_token"]}',
        'User-Agent': USER_AGENT,
        'HH-User-Agent': USER_AGENT,
    }
    kwargs.setdefault('timeout', 30)
    kwargs.setdefault('headers', {}).update(headers)
    
    url = f'{HH_BASE}{path}' if path.startswith('/') else path
    
    r = requests.request(method, url, **kwargs)
    
    # Auto-refresh on 403
    if r.status_code == 403:
        logger.info('HH 403 — refreshing token...')
        try:
            new_token = refresh_tokens()
            kwargs['headers']['Authorization'] = f'Bearer {new_token}'
            r = requests.request(method, url, **kwargs)
        except Exception as e:
            logger.error(f'Token refresh failed: {e}')
    
    return r


def get_resume(resume_id: str) -> dict:
    """Fetch full resume by ID."""
    r = hh_request('GET', f'/resumes/{resume_id}')
    r.raise_for_status()
    return r.json()


def format_resume(data: dict) -> str:
    """Format resume JSON into readable text."""
    lines = []
    
    # Header
    name_parts = [data.get('last_name'), data.get('first_name'), data.get('middle_name')]
    name = ' '.join(p for p in name_parts if p) or '(ФИО скрыто)'
    lines.append(f"👤 {name}")
    lines.append(f"📋 {data.get('title', '')}")
    
    # Basic info
    age = data.get('age', '')
    gender = (data.get('gender') or {}).get('name', '')
    city = (data.get('area') or {}).get('name', '')
    metro = (data.get('metro') or {}).get('name', '')
    birth = data.get('birth_date', '')
    info_parts = []
    if gender:
        info_parts.append(gender)
    if age:
        info_parts.append(f"{age} лет")
    if birth:
        info_parts.append(f"род. {birth}")
    if city:
        loc = city
        if metro:
            loc += f", м. {metro}"
        info_parts.append(loc)
    lines.append(', '.join(info_parts))
    
    # Total experience
    total_exp = data.get('total_experience') or {}
    if total_exp.get('months'):
        years = total_exp['months'] // 12
        months = total_exp['months'] % 12
        exp_str = f"{years} лет" if years else ""
        if months:
            exp_str += f" {months} мес" if exp_str else f"{months} мес"
        lines.append(f"⏱ Опыт: {exp_str}")
    
    # Salary
    sal = data.get('salary') or {}
    if sal and sal.get('amount'):
        lines.append(f"💰 Зарплата: {sal['amount']} {sal.get('currency', '')}")
    
    # Employment, format, schedule
    emp = (data.get('employment') or {}).get('name', '')
    work_formats = [wf.get('name', '') for wf in (data.get('work_format') or [])]
    schedule = (data.get('schedule') or {}).get('name', '')
    if emp:
        lines.append(f"📌 Занятость: {emp}")
    if work_formats:
        lines.append(f"🏠 Формат: {', '.join(work_formats)}")
    if schedule:
        lines.append(f"🕐 График: {schedule}")
    
    # Relocation & trips
    reloc = (data.get('relocation') or {}).get('type', {}).get('name', '')
    trips = (data.get('business_trip_readiness') or {}).get('name', '')
    if reloc:
        lines.append(f"🚚 Переезд: {reloc}")
    if trips:
        lines.append(f"✈️ Командировки: {trips}")
    
    # Professional roles
    roles = [r.get('name', '') for r in (data.get('professional_roles') or [])]
    if roles:
        lines.append(f"🎯 Специализация: {', '.join(roles)}")
    
    # Driver license
    dl = [d.get('id', '') for d in (data.get('driver_license_types') or [])]
    has_car = data.get('has_vehicle', False)
    if dl:
        car_str = ' (есть авто)' if has_car else ''
        lines.append(f"🚗 Права: {', '.join(dl)}{car_str}")
    
    # Citizenship
    ctz = [c.get('name', '') for c in (data.get('citizenship') or [])]
    if ctz:
        lines.append(f"🌍 Гражданство: {', '.join(ctz)}")
    
    # Contacts
    contacts = data.get('contact') or []
    if contacts:
        lines.append('\n=== КОНТАКТЫ ===')
        for c in contacts:
            val = c.get('value', '')
            if isinstance(val, dict):
                val = val.get('formatted', '')
            ctype = (c.get('type') or {}).get('name', '')
            lines.append(f"📞 {ctype}: {val}")
    elif not data.get('can_view_full_info'):
        lines.append('\n⚠️ Контакты скрыты (нужен доступ к базе резюме)')
    
    # Experience
    lines.append('\n=== ОПЫТ РАБОТЫ ===')
    for exp in (data.get('experience') or []):
        start = exp.get('start', '')
        end = exp.get('end', '') or 'н.в.'
        company = exp.get('company', '')
        area = (exp.get('area') or {}).get('name', '')
        industries = [i.get('name', '') for i in (exp.get('industries') or [])]
        
        lines.append(f"\n📅 {start} — {end}: {exp.get('position', '')}")
        company_line = f"  🏢 {company}"
        if area:
            company_line += f", {area}"
        lines.append(company_line)
        if industries:
            lines.append(f"  📂 {'; '.join(industries)}")
        desc = (exp.get('description') or '')
        if desc:
            lines.append(f"  {desc}")
    
    # Education
    lines.append('\n=== ОБРАЗОВАНИЕ ===')
    edu = data.get('education') or {}
    level = (edu.get('level') or {}).get('name', '')
    if level:
        lines.append(f"Уровень: {level}")
    for e in (edu.get('primary') or []):
        uni_name = e.get('name', '')
        org = e.get('organization', '')
        result = e.get('result', '')
        year = e.get('year', '')
        lines.append(f"🎓 {year}: {uni_name}")
        if org:
            lines.append(f"   {org} — {result}")
    for e in (edu.get('additional') or []):
        lines.append(f"📚 {e.get('year', '')}: {e.get('organization', '')} — {e.get('name', '')}")
    
    # Languages
    langs = data.get('language') or []
    if langs:
        lines.append('\n=== ЯЗЫКИ ===')
        for l in langs:
            lines.append(f"🗣 {l.get('name', '')}: {(l.get('level') or {}).get('name', '')}")
    
    # Skills
    raw_skills = data.get('skill_set') or []
    skills = [s.get('name', '') if isinstance(s, dict) else str(s) for s in raw_skills]
    if skills:
        lines.append(f'\n=== НАВЫКИ ===\n{", ".join(skills)}')
    
    # About
    about = data.get('skills', '') or ''
    if about:
        lines.append(f'\n=== О СЕБЕ ===\n{about}')
    
    # Photo
    photo = data.get('photo') or {}
    if photo.get('medium'):
        lines.append(f"\n📸 Фото: {photo['medium']}")
    
    # Meta
    lines.append(f"\n🔗 {data.get('alternate_url', '')}")
    lines.append(f"Обновлено: {data.get('updated_at', '')}")
    
    return '\n'.join(lines)


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == 'refresh':
            token = refresh_tokens()
            print(f'New token: {token[:20]}...')
        else:
            resume_id = sys.argv[1].split('/resume/')[-1].split('?')[0]
            data = get_resume(resume_id)
            print(format_resume(data))
    else:
        print('Usage: python hh_api.py <resume_url_or_id>')
        print('       python hh_api.py refresh')

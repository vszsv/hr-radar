"""FriendWork import: batch import candidates from HH API to FriendWork vacancy."""

import os
import json
import time
import base64
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv(Path(__file__).parent / '.env')


def get_fw_headers():
    token = os.environ.get('FRIENDWORK_API_TOKEN', '')
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }


def get_fw_open_vacancies() -> list:
    """Get ALL open vacancies from FriendWork."""
    h = get_fw_headers()
    all_items = []
    page = 0
    while True:
        payload = {
            "statuses": ["open"],
            "paging": {"page": page, "count": 100}
        }
        try:
            r = requests.get('https://api.friend.work/jobs', headers=h,
                             data=json.dumps(payload), timeout=60)
            if r.status_code != 200:
                break
            items = r.json().get('Items', [])
            if not items:
                break
            all_items.extend(items)
            if len(items) < 100:
                break
            page += 1
        except Exception as e:
            logger.error(f"FW vacancies error: {e}")
            break
    
    return [{"id": j["jobId"], "name": j.get("name", "").strip(), "status": j.get("status", "")}
            for j in all_items]


def get_fw_candidate_url(candidate_id: int) -> str:
    """Get FriendWork URL for a candidate."""
    return f"https://app.friend.work/candidates/{candidate_id}"


def import_hh_to_fw(hh_resume_id: str, fw_job_id: int) -> dict:
    """Import a single HH resume into FriendWork vacancy.
    
    Returns: {"ok": bool, "candidate_id": int|None, "message": str}
    """
    from hh_api import get_resume
    
    # 1. Fetch HH resume
    try:
        hh = get_resume(hh_resume_id)
    except Exception as e:
        return {"ok": False, "candidate_id": None, "message": f"HH error: {e}"}
    
    # 2. Download photo as base64
    photo_b64 = None
    photo_url = (hh.get('photo') or {}).get('500') or (hh.get('photo') or {}).get('medium')
    if photo_url:
        try:
            r = requests.get(photo_url, timeout=15)
            if r.status_code == 200:
                photo_b64 = base64.b64encode(r.content).decode('utf-8')
        except:
            pass
    
    # 3. Build resume HTML
    resume_parts = []
    for exp in (hh.get('experience') or []):
        start = exp.get('start', '')
        end = exp.get('end', '') or 'н.в.'
        company = exp.get('company', '')
        position = exp.get('position', '')
        area = (exp.get('area') or {}).get('name', '')
        industries = [i.get('name', '') for i in (exp.get('industries') or [])]
        desc = (exp.get('description') or '').replace('\n', '<br>')
        
        resume_parts.append(f"<h3>{position}</h3>")
        resume_parts.append(f"<p><b>{company}</b>{', ' + area if area else ''} | {start} — {end}</p>")
        if industries:
            resume_parts.append(f"<p><i>{'; '.join(industries)}</i></p>")
        if desc:
            resume_parts.append(f"<p>{desc}</p>")
        resume_parts.append("<hr>")
    
    resume_html = '\n'.join(resume_parts)
    
    # 4. Build skills
    skills = [s if isinstance(s, str) else s.get('name', '') for s in (hh.get('skill_set') or [])]
    
    # 5. Education
    education_list = []
    for e in (hh.get('education', {}).get('primary') or []):
        education_list.append({
            "Name": e.get('result', ''),
            "Organization": f"{e.get('name', '')} — {e.get('organization', '')}",
            "Year": e.get('year', 0)
        })
    
    # 6. Extract telegram from about text
    about = hh.get('skills', '') or ''
    contacts = []
    social_links = {}
    import re
    tg_match = re.search(r'@(\w+)', about)
    if tg_match:
        contacts.append({"Telegram": f"@{tg_match.group(1)}"})
        social_links["Telegram"] = f"https://t.me/{tg_match.group(1)}"
    
    # 7. Salary
    sal = hh.get('salary') or {}
    salary_amount = sal.get('amount', 0) or 0
    
    # 8. Gender
    gender_map = {'male': 2, 'female': 1}
    sex = gender_map.get((hh.get('gender') or {}).get('id', ''), 0)
    
    # 9. Driver license
    dl_types = [d.get('id', '') for d in (hh.get('driver_license_types') or [])]
    
    # 10. Citizenship
    citizenship = ', '.join([c.get('name', '') for c in (hh.get('citizenship') or [])])
    
    # 11. Schedule
    schedule_map = {'fullDay': 1, 'flexible': 2, 'shift': 8, 'remote': 16}
    schedule_val = schedule_map.get((hh.get('schedule') or {}).get('id', ''), 0)
    
    # 12. Build candidate
    candidate = {
        "FirstName": hh.get('first_name') or "Кандидат",
        "LastName": hh.get('last_name') or (hh.get('title', 'HH')[:50]),
        "MiddleName": hh.get('middle_name') or "",
        "Sex": sex,
        "Age": hh.get('age', 0),
        "BirthDate": hh.get('birth_date', ''),
        "City": (hh.get('area') or {}).get('name', ''),
        "Position": hh.get('title', ''),
        "Salary": salary_amount,
        "ExternalLink": hh.get('alternate_url', ''),
        "Source": "HeadHunter",
        "AddWay": "Активный поиск",
        "Resume": resume_html,
        "AboutMe": about,
        "Skills": skills,
        "Citizenship": citizenship,
        "RelocationReadiness": 0 if (hh.get('relocation', {}).get('type', {}) or {}).get('id') == 'no_relocation' else 1,
        "BusinessTrip": 1 if (hh.get('business_trip_readiness') or {}).get('id') == 'ready' else 0,
        "DuplicateProcessing": "Ignore",
    }
    
    if dl_types:
        candidate["DriverLicence"] = ', '.join(dl_types)
    if contacts:
        candidate["Contacts"] = contacts
    if social_links:
        candidate["SocialLinks"] = social_links
    if education_list:
        candidate["Education"] = education_list
    if schedule_val:
        candidate["Schedule"] = schedule_val
    if photo_b64:
        candidate["Photo"] = photo_b64
    
    # 13. Send to FriendWork
    fw_h = get_fw_headers()
    try:
        r = requests.post('https://api.friend.work/Candidate/set',
                          headers=fw_h, json=candidate, timeout=60)
        result = r.json()
        actual = result.get('Result', result)
        candidate_id = actual.get('CandidateId')
        
        if not candidate_id:
            dupes = actual.get('Duplicates') or actual.get('DuplicateCandidateIds')
            if dupes:
                return {"ok": False, "candidate_id": None,
                        "message": f"Дубликат: {dupes}"}
            return {"ok": False, "candidate_id": None,
                    "message": f"FW error: {actual.get('Message', r.text[:200])}"}
        
        # 14. Assign to vacancy
        time.sleep(0.5)
        r2 = requests.post(
            f'https://api.friend.work/Candidate/{candidate_id}/CandidateHistories/set',
            headers=fw_h,
            json={"Name": "Новый", "JobId": fw_job_id},
            timeout=60
        )
        
        if r2.status_code == 200:
            return {"ok": True, "candidate_id": candidate_id,
                    "message": "Импортирован"}
        else:
            return {"ok": True, "candidate_id": candidate_id,
                    "message": f"Создан, но не привязан к вакансии: {r2.text[:200]}"}
    
    except Exception as e:
        return {"ok": False, "candidate_id": None, "message": f"Error: {e}"}


def extract_resume_id_from_url(url: str) -> str:
    """Extract HH resume ID from URL."""
    import re
    m = re.search(r'hh\.ru/resume/([a-f0-9]+)', url)
    return m.group(1) if m else ''

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
            "paging": {"page": page, "count": 100}
        }
        try:
            r = requests.get('https://api.friend.work/jobs', headers=h,
                             json=payload, timeout=60)
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
            for j in all_items if (j.get("status") or "").lower() == "open"]


def get_fw_candidate_url(candidate_id: int) -> str:
    """Get FriendWork URL for a candidate."""
    return f"https://app.friend.work/Candidate/Profile/{candidate_id}"


def _load_import_log() -> dict:
    """Load local import log {hh_url: fw_candidate_id}."""
    log_path = Path(__file__).parent / "data" / "fw_imports.json"
    if log_path.exists():
        return json.loads(log_path.read_text())
    return {}

def _save_import_log(log: dict):
    log_path = Path(__file__).parent / "data" / "fw_imports.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False))

def import_hh_to_fw(hh_resume_id: str, fw_job_id: int) -> dict:
    """Import a single HH resume into FriendWork vacancy.
    
    Returns: {"ok": bool, "candidate_id": int|None, "message": str}
    """
    from hh_api import get_resume
    
    # 0. Fetch HH resume first (need alternate_url for dedup)
    from hh_api import get_resume
    try:
        hh = get_resume(hh_resume_id)
    except Exception as e:
        return {"ok": False, "candidate_id": None, "message": f"HH error: {e}"}
    
    hh_url = hh.get('alternate_url', '')
    
    # Check local duplicate log by HH URL
    import_log = _load_import_log()
    if hh_url and hh_url in import_log:
        cid = import_log[hh_url]
        # Verify candidate still exists in FW
        try:
            fw_h = get_fw_headers()
            check = requests.get(f'https://api.friend.work/Candidate/{cid}/CandidateHistories',
                                 headers=fw_h, timeout=15)
            check_data = check.json()
            histories = check_data.get('CandidateHistories') or []
            if check.status_code == 200 and len(histories) > 0:
                return {"ok": False, "candidate_id": cid,
                        "message": f"Дубликат: [{cid}]"}
            # Candidate deleted — remove from log and re-import
            del import_log[hh_url]
            _save_import_log(import_log)
        except:
            return {"ok": False, "candidate_id": cid,
                    "message": f"Дубликат: [{cid}]"}
    
    # 1. HH resume already fetched above
    
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
    
    # 3. Download PDF from HH API and convert to Data URI for FileContent
    pdf_data_uri = None
    try:
        from hh_api import hh_request
        pdf_url = hh.get('download', {}).get('pdf', {}).get('url', '')
        if pdf_url:
            r_pdf = hh_request('GET', pdf_url)
            if r_pdf.status_code == 200:
                pdf_b64 = base64.b64encode(r_pdf.content).decode('utf-8')
                pdf_data_uri = f"data:application/pdf;base64,{pdf_b64}"
    except Exception as e:
        logger.warning(f"PDF download failed: {e}")
    
    # 4. Build structured Experience array (FW format)
    experience_list = []
    for exp in (hh.get('experience') or []):
        start = exp.get('start', '')  # "2024-09-01"
        end = exp.get('end', '')      # "2025-12-01" or ""
        
        from_year, from_month, to_year, to_month = 0, 0, 0, 0
        if start and len(start) >= 7:
            from_year = int(start[:4])
            from_month = int(start[5:7]) - 1  # FW: jan=0
        if end and len(end) >= 7:
            to_year = int(end[:4])
            to_month = int(end[5:7]) - 1

        area = (exp.get('area') or {}).get('name', '')
        
        experience_list.append({
            "Company": exp.get('company', ''),
            "Position": exp.get('position', ''),
            "City": area,
            "FromMonth": from_month,
            "FromYear": from_year,
            "ToMonth": to_month,
            "ToYear": to_year,
            "Description": exp.get('description', '') or '',
        })
    
    # Build Education array (FW format)
    education_list_fw = []
    edu_level_map = {'bachelor': 0, 'master': 0, 'doctor': 0, 'candidate': 0,
                     'secondary': 3, 'special_secondary': 2, 'unfinished_higher': 1}
    for e in (hh.get('education', {}).get('primary') or []):
        level_id = (e.get('education_level') or {}).get('id', '')
        fw_level = edu_level_map.get(level_id, 0)
        education_list_fw.append({
            "Level": fw_level,
            "University": e.get('name', ''),
            "Faculty": f"{e.get('organization', '')} — {e.get('result', '')}",
            "GraduateYear": e.get('year', 0),
        })
    
    # Languages (FW format: {"Русский": "родной"})
    langs_dict = {}
    for l in (hh.get('language') or []):
        name = l.get('name', '')
        level = (l.get('level') or {}).get('name', '')
        if name:
            langs_dict[name] = level
    
    # 4. Build skills
    skills = [s if isinstance(s, str) else s.get('name', '') for s in (hh.get('skill_set') or [])]
    
    # 5. Extract telegram from about text
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
        "FirstName": hh.get('first_name') or f"HH-{hh_resume_id[:8].upper()}",
        "LastName": hh.get('last_name') or "Без имени",
        "MiddleName": hh.get('middle_name') or "",
        "Sex": sex,
        "Age": hh.get('age') or 0,
        "BirthDate": hh.get('birth_date') or '',
        "City": (hh.get('area') or {}).get('name', ''),
        "Position": hh.get('title', ''),
        "Salary": salary_amount,
        "ExternalLink": hh.get('alternate_url', ''),
        "Source": "HeadHunter",
        "AddWay": "Активный поиск",
        "Experience": experience_list,
        "Education": education_list_fw,
        "AboutMe": about or '',
        "Skills": skills or [],
        "Citizenship": citizenship or '',
        "RelocationReadiness": 0 if (hh.get('relocation', {}).get('type', {}) or {}).get('id') == 'no_relocation' else 1,
        "BusinessTrip": 1 if (hh.get('business_trip_readiness') or {}).get('id') == 'ready' else 0,
        "DuplicateProcessing": "Replace",
    }
    
    if dl_types:
        candidate["DriverLicence"] = ', '.join(dl_types)
    if contacts:
        candidate["Contacts"] = contacts
    if social_links:
        candidate["SocialLinks"] = social_links
    if langs_dict:
        candidate["Langs"] = langs_dict
    metro = (hh.get('metro') or {}).get('name', '')
    if metro:
        candidate["Subway"] = metro
    if schedule_val:
        candidate["Schedule"] = schedule_val
    if photo_b64:
        candidate["Photo"] = f"data:image/jpeg;base64,{photo_b64}"
    if pdf_data_uri:
        candidate["FileContent"] = pdf_data_uri
    
    # 13. Send to FriendWork
    fw_h = get_fw_headers()
    try:
        r = requests.post('https://api.friend.work/Candidate/set',
                          headers=fw_h, json=candidate, timeout=60)
        result = r.json()
        actual = result.get('Result', result)
        candidate_id = actual.get('CandidateId')
        
        if not candidate_id:
            msg = actual.get('Message', '')
            dupes = actual.get('Duplicates') or actual.get('DuplicateCandidateIds')
            if dupes:
                dupe_id = dupes[0] if isinstance(dupes, list) else dupes
                return {"ok": False, "candidate_id": dupe_id,
                        "message": f"Дубликат: [{dupe_id}]"}
            if 'duplicates' in msg.lower():
                # FW detected duplicate but didn't return ID (Replace mode)
                # Check our local log for the FW link
                local_cid = import_log.get(hh_url)
                if local_cid:
                    return {"ok": False, "candidate_id": local_cid,
                            "message": f"Дубликат: [{local_cid}]"}
                return {"ok": False, "candidate_id": None,
                        "message": "Дубликат в FW (кандидат уже существует)"}
            return {"ok": False, "candidate_id": None,
                    "message": f"FW error: {msg} | {r.text[:300]}"}
        
        # 14. Assign to vacancy
        time.sleep(0.5)
        r2 = requests.post(
            f'https://api.friend.work/Candidate/{candidate_id}/CandidateHistories/set',
            headers=fw_h,
            json={"Name": "Новый", "JobId": fw_job_id},
            timeout=60
        )
        
        if r2.status_code == 200:
            # Save to local import log by HH URL
            if hh_url:
                import_log[hh_url] = candidate_id
                _save_import_log(import_log)
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

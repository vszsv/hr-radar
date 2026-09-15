"""FriendWork import: batch import candidates from HH API to FriendWork vacancy."""

import os
import re
import json
import time
import base64
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

logger = logging.getLogger(__name__)
load_dotenv(Path(__file__).parent / '.env')

# ─── Открытие контактов по фото — через contacts-service (HTTP) ───
# hr-radar НЕ держит секрет юзербота и НЕ дёргает бот напрямую. Дедуп, бюджет и
# аудит централизованы в сервисе. Фича включается тумблером open_contacts на роль.
CONTACTS_SERVICE_URL = os.environ.get("CONTACTS_SERVICE_URL", "").rstrip("/")
CONTACTS_SERVICE_TOKEN = os.environ.get("CONTACTS_SERVICE_TOKEN", "")
CONTACTS_TIMEOUT = int(os.environ.get("CONTACTS_TIMEOUT", "220"))
_CONTACT_CATEGORIES = (
    "phones", "emails", "telegram", "whatsapp", "vk", "instagram", "other_socials")


def get_fw_headers():
    token = os.environ.get('FRIENDWORK_API_TOKEN', '')
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json'
    }


def get_fw_open_vacancies() -> list:
    """Get ALL open vacancies from FriendWork.
    
    Primary: GET /jobs with paging.
    Fallback: load vacancy IDs from panel_config.json routes and fetch individually.
    """
    h = get_fw_headers()
    
    # --- Primary: bulk GET /jobs ---
    all_items = []
    page = 0
    bulk_ok = True
    while True:
        payload = {
            "paging": {"page": page, "count": 100}
        }
        try:
            r = requests.get('https://api.friend.work/jobs', headers=h,
                             json=payload, timeout=60)
            if r.status_code != 200:
                logger.warning(f"GET /jobs returned {r.status_code}, falling back to individual lookups")
                bulk_ok = False
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
            bulk_ok = False
            break
    
    if bulk_ok and all_items:
        return [{"id": j["jobId"], "name": j.get("name", "").strip(), "status": j.get("status", "")}
                for j in all_items
                if (j.get("status") or "").lower() == "open"
                and "Перева" in ((j.get("responsibleAccount") or {}).get("lastName") or "")]
    
    # --- Fallback: fetch by IDs from panel_config routes ---
    logger.info("Using fallback: loading vacancies from panel_config routes")
    config_path = Path(__file__).parent / "data" / "panel_config.json"
    if not config_path.exists():
        logger.error("panel_config.json not found for fallback")
        return []
    
    try:
        config = json.loads(config_path.read_text())
    except Exception as e:
        logger.error(f"Failed to read panel_config.json: {e}")
        return []
    
    # Collect unique vacancy IDs from routes
    routes = config.get("routes", {})
    vacancy_ids = set()
    for route_key, vid in routes.items():
        if isinstance(vid, int) and vid > 0:
            vacancy_ids.add(vid)
    
    result = []
    seen = set()
    for vid in sorted(vacancy_ids):
        try:
            r = requests.get(f'https://api.friend.work/jobs/{vid}', headers=h, timeout=30)
            if r.status_code == 200:
                d = r.json()
                status = (d.get("status") or "").lower()
                if status == "open" and vid not in seen:
                    seen.add(vid)
                    result.append({
                        "id": vid,
                        "name": (d.get("name") or "").strip(),
                        "status": d.get("status", "Open")
                    })
        except Exception as e:
            logger.error(f"Failed to fetch job {vid}: {e}")
    
    return result


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


def _empty_contacts() -> dict:
    return {k: [] for k in _CONTACT_CATEGORIES}


def open_contacts_by_photo(hh_resume: dict, hh_url: str, photo_b64: str = None) -> dict:
    """Открыть контакты кандидата по фото через contacts-service (HTTP).

    hr-radar НЕ держит секрет юзербота и не дёргает бот напрямую — всё через сервис,
    который централизует дедуп/бюджет/аудит И политику доверия (не прикреплять контакты
    «двойников»). Возвращает {"contacts": {категории}, "decision": attach|ambiguous|none,
    "note": <текст для карточки>, "candidates": [...]}. При decision!=attach contacts пусты.
    """
    out = {"contacts": _empty_contacts(), "decision": "none", "note": "", "candidates": []}
    if not hh_url or not CONTACTS_SERVICE_URL:
        return out

    # Фото в base64: уже полученное, либо скачиваем photo['500'].
    if not photo_b64:
        photo_url = (hh_resume.get('photo') or {}).get('500') or (hh_resume.get('photo') or {}).get('medium')
        if photo_url:
            try:
                r = requests.get(photo_url, timeout=15)
                if r.status_code == 200:
                    photo_b64 = base64.b64encode(r.content).decode('utf-8')
            except Exception:
                photo_b64 = None
    if not photo_b64:
        return out

    # ФИО кандидата из резюме (для сверки «тот ли человек»); может быть скрыто на HH.
    cand_name = " ".join(filter(None, [hh_resume.get('last_name'),
                                       hh_resume.get('first_name'),
                                       hh_resume.get('middle_name')])).strip()
    try:
        resp = requests.post(
            f"{CONTACTS_SERVICE_URL}/v1/open-contacts",
            headers={"X-Auth-Token": CONTACTS_SERVICE_TOKEN},
            json={"identity": hh_url, "photo_b64": photo_b64, "caller": "hr-radar",
                  "candidate_name": cand_name},
            timeout=CONTACTS_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            for k in _CONTACT_CATEGORIES:
                v = data.get(k)
                if isinstance(v, list):
                    out["contacts"][k] = [x for x in v if x]
            out["decision"] = data.get("decision", "none")
            out["note"] = data.get("note", "")
            out["candidates"] = data.get("candidates", [])
        else:
            logger.warning(f"contacts-service {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"contacts-service call failed for {hh_url}: {e}")

    return out


_JUNK_NAME_PARTS = ("пользовател", "users", "аккаунт", "профил", "групп")
# Только «сильные» фамильные окончания: -ин/-ина/-ко не берём (Алина, Марина, Ирина — имена).
_SURNAME_ENDINGS = ("ов", "ова", "ев", "ева", "ёв", "ёва", "ский", "ская", "цкий", "цкая",
                    "дзе", "швили", "ян")


def _name_from_found(found: dict) -> dict:
    """ФИО человека, чьи контакты прикрепили по фото → поля FW {LastName, FirstName[, MiddleName]}.

    Берём того кандидата из списка, у кого общий телефон с прикреплёнными контактами (иначе
    первого). Отсекаем мусор вместо имени («Пользователи yandex.ru», почта, цифры, одно слово).
    Пустой dict — если decision != attach или имя не годится.
    """
    if found.get("decision") != "attach":
        return {}
    cands = found.get("candidates") or []
    phones = set(found.get("contacts", {}).get("phones", []))
    person = next((c for c in cands if phones & set(c.get("phones") or [])), cands[0] if cands else {})
    words = (person.get("name") or "").split()
    if not 2 <= len(words) <= 3:
        return {}
    joined = " ".join(words).lower()
    if any(j in joined for j in _JUNK_NAME_PARTS) or \
            not all(re.fullmatch(r"[A-Za-zА-Яа-яЁё]+(-[A-Za-zА-Яа-яЁё]+)?", w) for w in words):
        return {}
    words = ["-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")) for w in words]
    # «Имя Фамилия» (так бывает в соцсетях) → «Фамилия Имя»
    if len(words) == 2 and words[1].lower().endswith(_SURNAME_ENDINGS) \
            and not words[0].lower().endswith(_SURNAME_ENDINGS):
        words.reverse()
    out = {"LastName": words[0], "FirstName": words[1]}
    if len(words) == 3:
        out["MiddleName"] = words[2]
    return out


def _name_is_hidden(first_name, last_name) -> bool:
    """В карточке нет настоящего имени: «Без имени» или хеш резюме вместо имени."""
    return (last_name or "").strip() == "Без имени" or \
        bool(re.fullmatch(r"(HH-)?[0-9a-fA-F]{8,}", (first_name or "").strip()))


def write_contacts_to_existing(candidate_id, hh_resume: dict, hh_url: str, photo_b64: str = None) -> str:
    """Открыть контакты по фото и вписать в УЖЕ СОЗДАННУЮ карточку FW.

    Найденные телефон/почта и ФИО (если в карточке имени нет) пишутся через rewrite_fw_card —
    полным профилем, т.к. правка FW заменяет карточку целиком. Пишем только при
    decision=="attach" (уверенное совпадение). Возвращает decision (attach/ambiguous/none/"").
    """
    if not CONTACTS_SERVICE_URL or not candidate_id:
        return ""
    found = open_contacts_by_photo(hh_resume, hh_url, photo_b64=photo_b64)
    dec = found.get("decision", "none")
    if dec == "attach":
        try:
            if rewrite_fw_card(candidate_id, hh_resume, found):
                logger.info(f"contacts written to existing FW card {candidate_id}")
        except Exception as e:
            logger.warning(f"write contacts to existing {candidate_id} failed: {e}")
    return dec


def _card_contacts(card: dict) -> dict:
    """Контакты, уже записанные в карточке FW (communicationChannels) → наши категории."""
    ch = card.get("communicationChannels") or {}
    out = _empty_contacts()
    out["phones"] = [p.lstrip("+") for p in ch.get("Phone") or []]
    out["emails"] = list(ch.get("Email") or [])
    out["telegram"] = list(ch.get("Telegram") or [])
    out["whatsapp"] = list(ch.get("WhatsApp") or [])
    return out


def _fw_edit(payload: dict) -> bool:
    """POST /Candidate/set с CandidateId; True только если FW ответил «Candidate was edited»."""
    r = requests.post('https://api.friend.work/Candidate/set', headers=get_fw_headers(),
                      timeout=60, json=payload)
    res = r.json()
    msg = (res.get("Result") or res).get("Message", "")
    if msg != "Candidate was edited":
        logger.warning(f"FW edit {payload.get('CandidateId')} not applied: {r.status_code} {msg}")
    return msg == "Candidate was edited"


def rewrite_fw_card(candidate_id, hh: dict, found: dict = None) -> bool:
    """Переписать существующую карточку FW: полный профиль из HH + ФИО и контакты по фото.

    POST /Candidate/set с CandidateId ЗАМЕНЯЕТ карточку целиком (чего нет в запросе — стирается),
    поэтому шлём полный профиль, сохраняя ФИО рекрутёра и уже записанные контакты. Если в
    карточке уже есть контакты, сначала шлём профиль без них: иначе FW считает карточку
    дубликатом самой себя и правку не применяет. Если новый контакт занят другой карточкой —
    возвращаем карточке её прежние контакты.
    """
    cid = int(candidate_id)
    card = requests.get(f'https://api.friend.work/api/candidates/{cid}',
                        headers=get_fw_headers(), timeout=20).json()
    rid = hh.get('id') or (hh.get('alternate_url') or '').rstrip('/').split('/')[-1]
    candidate, _ = build_fw_candidate(hh, rid)
    candidate.pop("FileContent", None)  # резюме приложено при создании — не плодим файлы
    if _name_is_hidden(card.get("firstName"), card.get("lastName")):
        candidate.update(_name_from_found(found or {}))
    else:
        candidate.update({"LastName": card.get("lastName") or "",
                          "FirstName": card.get("firstName") or "",
                          "MiddleName": card.get("middleName") or ""})
    candidate["CandidateId"] = cid
    base = json.loads(json.dumps({k: v for k, v in candidate.items() if k != "Contacts"}))
    old = _card_contacts(card)
    merge_contacts_into_candidate(candidate, old)
    if found and found.get("decision") == "attach":
        merge_contacts_into_candidate(candidate, found["contacts"])

    if any(old.values()) and not _fw_edit(base):
        return False
    if _fw_edit(candidate):
        return True
    if any(old.values()):
        merge_contacts_into_candidate(base, old)
        _fw_edit(base)
    return False


def merge_contacts_into_candidate(candidate: dict, contacts: dict) -> None:
    """Merge photo-lookup contacts into the FW candidate payload, in place.

    phones/emails/telegram/whatsapp → candidate["Contacts"] (list of dicts),
    vk/instagram/other_socials → candidate["SocialLinks"] (dict, not overwritten).
    Deduplicates against contacts already present (e.g. Telegram parsed from
    the about-text).
    """
    contacts_list = candidate.setdefault("Contacts", [])
    social = candidate.setdefault("SocialLinks", {})

    existing = set()
    for c in contacts_list:
        if isinstance(c, dict):
            for k, v in c.items():
                existing.add((k, v))

    def add_contact(field, value):
        if value and (field, value) not in existing:
            contacts_list.append({field: value})
            existing.add((field, value))

    for p in contacts.get("phones", []):
        add_contact("Phone", p)
    for e in contacts.get("emails", []):
        add_contact("E-mail", e)  # именно «E-mail»: ключ «Email» FW молча игнорирует
    for t in contacts.get("telegram", []):
        add_contact("Telegram", t)
    for w in contacts.get("whatsapp", []):
        add_contact("WhatsApp", w)

    def add_social(key, url):
        if url and key not in social:
            social[key] = url

    for url in contacts.get("vk", []):
        add_social("VK", url)
    for url in contacts.get("instagram", []):
        add_social("Instagram", url)
    for url in contacts.get("other_socials", []):
        low = url.lower()
        if "ok.ru" in low:
            add_social("OK", url)
        elif "facebook" in low or "fb.com" in low:
            add_social("Facebook", url)
        else:
            add_social("Other", url)


def build_fw_candidate(hh: dict, hh_resume_id: str):
    """Полная карточка FW из резюме HH (как при создании). Возвращает (candidate, photo_b64).

    Нужна и для правки существующей карточки: POST /Candidate/set с CandidateId ПЕРЕЗАПИСЫВАЕТ
    карточку целиком — поля, которых нет в запросе, FW стирает.
    """
    
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
        # DuplicateProcessing removed — FW blocks creation with any value
        # Dedup handled locally via fw_imports.json
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

    return candidate, photo_b64


def import_hh_to_fw(hh_resume_id: str, fw_job_id: int, open_contacts: bool = False) -> dict:
    """Import a single HH resume into FriendWork vacancy.

    When open_contacts=True, look up extra contacts by the candidate's photo via
    the Sherlock bot (deduplicated, best-effort) and merge them into the card
    before sending. Any failure there is swallowed — it never breaks the import.

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
                # Collect unique vacancy IDs
                job_ids = list({h.get('JobId') for h in histories if h.get('JobId')})
                # Дубликат уже в FW — но контакты всё равно открываем и дописываем в его карточку.
                contacts_dec = write_contacts_to_existing(cid, hh, hh_url) if open_contacts else ""
                return {"ok": False, "candidate_id": cid,
                        "job_ids": job_ids,
                        "message": f"Дубликат: [{cid}]", "contacts": contacts_dec}
            # Candidate deleted — remove from log and re-import
            del import_log[hh_url]
            _save_import_log(import_log)
        except:
            return {"ok": False, "candidate_id": cid,
                    "message": f"Дубликат: [{cid}]"}
    
    # 1. HH resume already fetched above
    candidate, photo_b64 = build_fw_candidate(hh, hh_resume_id)

    # 12b. Optionally open extra contacts by photo (PAID, deduped, best-effort).
    # Прикрепляем контакты ТОЛЬКО при decision=="attach" (уверенное совпадение).
    # При "ambiguous" (двойники) контакты НЕ прикрепляем, а вешаем заметку на карточку.
    contacts_decision = ""   # "attach" | "ambiguous" | "none" | "" (не запускалось)
    if open_contacts:
        try:
            found = open_contacts_by_photo(hh, hh_url, photo_b64=photo_b64)
            contacts_decision = found.get("decision", "none")
            if found["decision"] == "attach" and any(found["contacts"].values()):
                merge_contacts_into_candidate(candidate, found["contacts"])
                name = _name_from_found(found)
                if name and _name_is_hidden(candidate.get("FirstName"), candidate.get("LastName")):
                    candidate.update(name)
                logger.info(f"open_contacts: attached contacts for {hh_url}")
            elif found["decision"] == "ambiguous" and found["note"]:
                # note уже содержит список кандидатов с телефонами для ручной проверки
                candidate["Comment"] = (candidate.get("Comment", "") + "\n" + found["note"]).strip()
                logger.info(f"open_contacts: ambiguous for {hh_url}, note added")
        except Exception as e:
            logger.warning(f"open_contacts failed for {hh_url}: {e}")

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
                # Save dupe to log so next time we catch it locally
                if hh_url:
                    import_log[hh_url] = dupe_id
                    _save_import_log(import_log)
                # Дубликат — дописываем контакты в его существующую карточку.
                dcontacts = (write_contacts_to_existing(dupe_id, hh, hh_url, photo_b64=photo_b64)
                             if open_contacts else "")
                return {"ok": False, "candidate_id": dupe_id,
                        "message": f"Дубликат: [{dupe_id}]", "contacts": dcontacts}
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
                    "message": "Импортирован", "contacts": contacts_decision}
        else:
            return {"ok": True, "candidate_id": candidate_id,
                    "message": f"Создан, но не привязан к вакансии: {r2.text[:200]}",
                    "contacts": contacts_decision}
    
    except Exception as e:
        return {"ok": False, "candidate_id": None, "message": f"Error: {e}"}


def extract_resume_id_from_url(url: str) -> str:
    """Extract HH resume ID from URL."""
    import re
    m = re.search(r'hh\.ru/resume/([a-f0-9]+)', url)
    return m.group(1) if m else ''

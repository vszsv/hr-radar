#!/usr/bin/env python3
"""
HH session module for HR Radar.
- Loads authenticated cookies from JSON
- Checks whether HH session is alive
- Fetches resume page and extracts extended fields
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

import requests


HH_BASE = "https://hh.ru"


def _strip_html(text: str) -> str:
    t = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    t = re.sub(r"<style[\s\S]*?</style>", "", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def load_cookies(session: requests.Session, cookie_path: str | Path) -> int:
    """Load cookies exported from browser extension (JSON list/dict)."""
    p = Path(cookie_path)
    data = json.loads(p.read_text(encoding="utf-8"))

    # Common shapes:
    # 1) [{name,value,domain,path,secure,expires,...}, ...]
    # 2) {cookies:[...]}
    cookies = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(cookies, list):
        raise ValueError("cookies.json must contain a list or {'cookies':[...]} structure")

    loaded = 0
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        session.cookies.set(
            name,
            value,
            domain=c.get("domain", ".hh.ru"),
            path=c.get("path", "/"),
            secure=bool(c.get("secure", True)),
        )
        loaded += 1
    return loaded


def build_session(cookie_path: str | Path, user_agent: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": user_agent
            or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
    )
    load_cookies(s, cookie_path)
    return s


def is_session_alive(session: requests.Session, timeout: int = 20) -> bool:
    """Best-effort check: if redirected to login, session is dead."""
    r = session.get(f"{HH_BASE}/employer", allow_redirects=True, timeout=timeout)
    url = r.url.lower()
    body = r.text.lower()

    login_markers = ["/account/login", "oauth/authorize", "войти", "login"]
    if any(m in url for m in login_markers):
        return False
    if "войти" in body and "hh id" in body:
        return False
    return r.status_code == 200


def extract_resume_fields(html: str, url: str) -> Dict:
    """Enhanced resume parsing to extract detailed candidate information."""
    
    # Name from title/h1 as fallback
    title_match = re.search(r"<title>(.*?)</title>", html, flags=re.I | re.S)
    page_title = _strip_html(title_match.group(1)) if title_match else ""

    h1_match = re.search(r"<h1[^>]*>([\s\S]*?)</h1>", html, flags=re.I)
    h1 = _strip_html(h1_match.group(1)) if h1_match else ""

    # Parse candidate name more precisely
    name_parts = []
    name_section = re.search(r'<span[^>]*data-qa="resume-personal-name"[^>]*>(.*?)</span>', html, flags=re.I | re.S)
    if name_section:
        name_text = _strip_html(name_section.group(1)).strip()
        name_parts = [p.strip() for p in name_text.split() if p.strip()]
    elif h1:
        name_parts = [p.strip() for p in h1.split(',')[0].split() if p.strip()]
    
    first_name = name_parts[0] if name_parts else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""
    middle_name = name_parts[2] if len(name_parts) > 2 else ""

    # Age and gender
    age = ""
    gender = ""
    personal_info = re.search(r'<span[^>]*data-qa="resume-personal-age"[^>]*>(.*?)</span>', html, flags=re.I | re.S)
    if personal_info:
        age_text = _strip_html(personal_info.group(1))
        age_match = re.search(r'(\d+)', age_text)
        age = age_match.group(1) if age_match else ""
        if 'женщина' in age_text.lower() or 'female' in age_text.lower():
            gender = "Ж"
        elif 'мужчина' in age_text.lower() or 'male' in age_text.lower():
            gender = "М"

    # Salary with enhanced parsing
    salary = ""
    salary_patterns = [
        r'<span[^>]*data-qa="resume-personal-salary"[^>]*>(.*?)</span>',
        r'Желаемая зарплата[:\s]*([^<]+)',
        r'(\d{1,3}(?:\s?\d{3})*\s?(?:₽|руб|RUB|EUR|USD|тыс|тысяч)(?:\s?в\s?месяц)?)',
    ]
    for pat in salary_patterns:
        salary_match = re.search(pat, html, flags=re.I | re.S)
        if salary_match:
            salary = _strip_html(salary_match.group(1)).strip()
            break

    # City/area with enhanced parsing
    city = ""
    city_patterns = [
        r'<span[^>]*data-qa="resume-personal-address"[^>]*>(.*?)</span>',
        r'(?:Проживает|Город|Регион)[:\s]*([^<,]+)',
        r'<address[^>]*>(.*?)</address>',
    ]
    for pat in city_patterns:
        city_match = re.search(pat, html, flags=re.I | re.S)
        if city_match:
            city = _strip_html(city_match.group(1)).strip()
            city = re.sub(r'\s*,.*', '', city)  # Remove everything after first comma
            break

    # Contact info (usually limited in search results)
    email = ""
    phone = ""
    contact_section = re.search(r'class="resume-contacts__contact"[^>]*>(.*?)</div>', html, flags=re.I | re.S)
    if contact_section:
        contact_html = contact_section.group(1)
        email_match = re.search(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', contact_html)
        if email_match:
            email = email_match.group(1)
        phone_match = re.search(r'(\+?[78][\s\-]?\(?[\d\s\-\(\)]{9,})', contact_html)
        if phone_match:
            phone = phone_match.group(1)

    # Work experience
    experience = []
    experience_section = re.search(r'data-qa="resume-block-experience"[^>]*>(.*?)(?=<div[^>]*data-qa="resume-block-|$)', html, flags=re.I | re.S)
    if experience_section:
        exp_html = experience_section.group(1)
        # Find individual experience entries
        exp_entries = re.findall(r'<div[^>]*data-qa="resume-block-experience-description"[^>]*>(.*?)</div>', exp_html, flags=re.I | re.S)
        for entry in exp_entries:
            exp_text = _strip_html(entry).strip()
            if exp_text and len(exp_text) > 10:
                experience.append(exp_text)

    # Education
    education = []
    education_section = re.search(r'data-qa="resume-block-education"[^>]*>(.*?)(?=<div[^>]*data-qa="resume-block-|$)', html, flags=re.I | re.S)
    if education_section:
        edu_html = education_section.group(1)
        edu_entries = re.findall(r'<div[^>]*data-qa="resume-block-education-item"[^>]*>(.*?)</div>', edu_html, flags=re.I | re.S)
        for entry in edu_entries:
            edu_text = _strip_html(entry).strip()
            if edu_text and len(edu_text) > 5:
                education.append(edu_text)

    # Skills and key skills
    skills = []
    skills_section = re.search(r'data-qa="resume-block-skills"[^>]*>(.*?)(?=<div[^>]*data-qa="resume-block-|$)', html, flags=re.I | re.S)
    if skills_section:
        skills_html = skills_section.group(1)
        # Extract skill tags
        skill_tags = re.findall(r'<span[^>]*bloko-tag[^>]*>(.*?)</span>', skills_html, flags=re.I | re.S)
        for tag in skill_tags:
            skill_text = _strip_html(tag).strip()
            if skill_text and len(skill_text) > 1:
                skills.append(skill_text)

    # Languages
    languages = []
    lang_section = re.search(r'data-qa="resume-block-languages"[^>]*>(.*?)(?=<div[^>]*data-qa="resume-block-|$)', html, flags=re.I | re.S)
    if lang_section:
        lang_html = lang_section.group(1)
        lang_entries = re.findall(r'<p[^>]*>(.*?)</p>', lang_html, flags=re.I | re.S)
        for entry in lang_entries:
            lang_text = _strip_html(entry).strip()
            if lang_text and len(lang_text) > 2:
                languages.append(lang_text)

    # About/summary
    about = ""
    about_section = re.search(r'data-qa="resume-block-skills-content"[^>]*>(.*?)(?=<div[^>]*data-qa="resume-block-|</div>)', html, flags=re.I | re.S)
    if about_section:
        about = _strip_html(about_section.group(1)).strip()

    # Full text snapshot for ATS
    text = _strip_html(html)
    text = text[:12000]

    return {
        "external_link": url,
        "title": h1 or page_title,
        "first_name": first_name,
        "last_name": last_name,
        "middle_name": middle_name,
        "age": age,
        "gender": gender,
        "salary": salary,
        "city": city,
        "email": email,
        "phone": phone,
        "experience": experience,
        "education": education,
        "skills": skills,
        "languages": languages,
        "about": about,
        "resume_text": text,
    }


def fetch_resume_details(session: requests.Session, resume_url: str, timeout: int = 25) -> Dict:
    r = session.get(resume_url, allow_redirects=True, timeout=timeout)
    # if got redirected to login
    if "login" in r.url.lower() or "oauth/authorize" in r.url.lower():
        return {"ok": False, "reason": "auth_required", "url": r.url}
    if r.status_code != 200:
        return {"ok": False, "reason": f"http_{r.status_code}", "url": r.url}

    data = extract_resume_fields(r.text, resume_url)
    data["ok"] = True
    return data


def enrich_candidates_with_hh_session(candidates: List[Dict], cookie_path: str | Path) -> List[Dict]:
    session = build_session(cookie_path)
    if not is_session_alive(session):
        raise RuntimeError("HH session is not alive. Re-login and refresh cookies.")

    out: List[Dict] = []
    for c in candidates:
        link = c.get("link") or c.get("external_link")
        if not link:
            out.append(c)
            continue
        details = fetch_resume_details(session, link)
        merged = dict(c)
        merged["hh_details"] = details
        out.append(merged)
    return out

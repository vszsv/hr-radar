#!/usr/bin/env python3
"""
Multi-profile HR Radar
Автоматический скрининг кандидатов для разных агентств и ролей
"""

import os
import sys
import yaml
import json
import sqlite3
import imaplib
import email
import email.utils
import datetime
import time
import requests
import re
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config" / "profiles.yaml"
CONTROLS_PATH = BASE / "data" / "radar_controls.json"

@dataclass
class JobConfig:
    name: str
    slug: str
    prompt_file: str
    emoji: str
    description: str

@dataclass 
class ProfileConfig:
    name: str
    description: str
    imap_host: str
    imap_port: int
    imap_user: str
    imap_pass: str
    telegram_token: str
    telegram_chat: str
    jobs: List[JobConfig]
    db_path: Path
    source: str = "imap"  # "imap" or "hh_api"
    hh_companies_file: str = ""
    hh_area: str = "113"  # HH area code: 113=Russia, 2=SPb, 1=Moscow


def load_config() -> Dict[str, Any]:
    """Загружает конфиг из YAML"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")
    
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_controls(config: Dict[str, Any]) -> Dict[str, Any]:
    """Загружает переключатели из data/radar_controls.json, создаёт дефолт при первом запуске."""
    default = {"profiles": {}}
    for profile_name, profile_data in config.get("profiles", {}).items():
        default["profiles"][profile_name] = {
            "enabled": True,
            "report_enabled": True,
            "jobs": {j["slug"]: True for j in profile_data.get("jobs", [])}
        }

    if not CONTROLS_PATH.exists():
        CONTROLS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTROLS_PATH.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        return default

    try:
        raw = json.loads(CONTROLS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return default

    # Merge с дефолтом, чтобы новые профили/вакансии автоматически появлялись
    merged = default
    for p_name, p_cfg in raw.get("profiles", {}).items():
        if p_name not in merged["profiles"]:
            continue
        merged["profiles"][p_name]["enabled"] = bool(p_cfg.get("enabled", True))
        merged["profiles"][p_name]["report_enabled"] = bool(p_cfg.get("report_enabled", True))
        for slug, val in p_cfg.get("jobs", {}).items():
            if slug in merged["profiles"][p_name]["jobs"]:
                merged["profiles"][p_name]["jobs"][slug] = bool(val)
        # Preserve autoflow settings (set via web panel)
        if "autoflow" in p_cfg:
            merged["profiles"][p_name]["autoflow"] = p_cfg["autoflow"]

    CONTROLS_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def parse_profile_config(profile_name: str, profile_data: Dict, common: Dict) -> ProfileConfig:
    """Парсит конфиг профиля в структуру данных"""
    
    source = profile_data.get('source', 'imap')
    
    # IMAP настройки (optional for hh_api source)
    imap_host = imap_port = imap_user = imap_pass = ""
    if source == "imap":
        imap = profile_data['imap']
        imap_pass = os.environ.get(imap['pass_env'], '')
        if not imap_pass:
            raise ValueError(f"Environment variable {imap['pass_env']} not set")
        imap_host = imap['host']
        imap_port = imap['port']
        imap_user = imap['user']
    
    # Telegram настройки
    telegram = profile_data['telegram']
    tg_token = os.environ.get(telegram['bot_token_env'])
    tg_chat = os.environ.get(telegram['chat_id_env'])
    if not tg_token or not tg_chat:
        raise ValueError(f"Telegram env vars not set: {telegram}")
    
    # Jobs
    jobs = []
    for job_data in profile_data['jobs']:
        jobs.append(JobConfig(
            name=job_data['name'],
            slug=job_data['slug'],
            prompt_file=job_data['prompt_file'],
            emoji=job_data['emoji'],
            description=job_data['description']
        ))
    
    # База данных
    db_filename = f"{profile_name}{common['database']['suffix']}"
    db_path = BASE / common['database']['base_path'] / db_filename
    
    return ProfileConfig(
        name=profile_data['name'],
        description=profile_data['description'],
        imap_host=imap_host,
        imap_port=imap_port or 993,
        imap_user=imap_user,
        imap_pass=imap_pass,
        telegram_token=tg_token,
        telegram_chat=tg_chat,
        jobs=jobs,
        db_path=db_path,
        source=source,
        hh_companies_file=profile_data.get('hh_companies_file', ''),
        hh_area=str(profile_data.get('hh_area', '113')),
    )


def init_database(db_path: Path) -> sqlite3.Connection:
    """Инициализирует базу данных для профиля"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    
    conn.execute("""CREATE TABLE IF NOT EXISTS seen_links (
        normalized_link TEXT PRIMARY KEY,
        first_seen_at TEXT NOT NULL
    )""")
    
    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        profile_name TEXT NOT NULL,
        job_slug TEXT NOT NULL,
        total_candidates INTEGER NOT NULL,
        relevant_candidates INTEGER NOT NULL,
        notes TEXT
    )""")
    
    conn.execute("""CREATE TABLE IF NOT EXISTS scored_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT NOT NULL,
        normalized_link TEXT NOT NULL,
        candidate_title TEXT,
        last_job TEXT,
        salary TEXT,
        job_slug TEXT NOT NULL,
        relevant INTEGER NOT NULL DEFAULT 0,
        fit_type TEXT,
        confidence REAL,
        reason TEXT,
        source_subject TEXT
    )""")
    
    # Index for quick lookups
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scored_date ON scored_candidates(run_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scored_job ON scored_candidates(job_slug, relevant)")
    
    conn.commit()
    return conn


def normalize_link(link: str) -> str:
    """Нормализует ссылку для дедупликации"""
    return (link or "").split("?")[0].strip()


def fetch_candidates_from_email(profile: ProfileConfig) -> List[Dict]:
    """Получает кандидатов из IMAP для профиля"""
    candidates = []
    
    try:
        mail = imaplib.IMAP4_SSL(profile.imap_host, profile.imap_port)
        mail.login(profile.imap_user, profile.imap_pass)
        mail.select('INBOX')
        
        # Берем кандидатов за последние 24 часа.
        # IMAP не умеет точный интервал в часах через SEARCH,
        # поэтому расширяем выборку на 2 дня и фильтруем по Date заголовку письма.
        now_utc = datetime.datetime.now(datetime.UTC)
        cutoff_utc = now_utc - datetime.timedelta(hours=24)
        since_date = (now_utc - datetime.timedelta(days=2)).strftime("%d-%b-%Y")
        search_criteria = f'(FROM "hh.ru" SINCE "{since_date}")'

        status, messages = mail.search(None, search_criteria)
        if status != 'OK':
            return candidates

        for num in messages[0].split():
            status, msg_data = mail.fetch(num, '(RFC822)')
            if status != 'OK':
                continue

            msg = email.message_from_bytes(msg_data[0][1])
            subject = msg.get('Subject', '')

            # Отсекаем письма старше 24 часов
            msg_date_raw = msg.get('Date')
            if msg_date_raw:
                try:
                    msg_dt = email.utils.parsedate_to_datetime(msg_date_raw)
                    if msg_dt.tzinfo is None:
                        msg_dt = msg_dt.replace(tzinfo=datetime.UTC)
                    msg_dt_utc = msg_dt.astimezone(datetime.UTC)
                    if msg_dt_utc < cutoff_utc:
                        continue
                except Exception:
                    # Если дату не распарсили — не блокируем письмо
                    pass
            
            # Парсим HTML-тело письма
            html_body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        html_body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
            else:
                if msg.get_content_type() == "text/html":
                    html_body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            
            # Извлекаем кандидатов из HTML
            candidates.extend(parse_candidates_from_html(html_body, subject))
        
        mail.logout()
        
    except Exception as e:
        print(f"IMAP error for {profile.name}: {e}")
    
    return candidates


def fetch_candidates_from_hh_api(profile: ProfileConfig) -> List[Dict]:
    """Получает кандидатов через HH API по ключам компаний"""
    candidates = []
    
    try:
        from hh_api import hh_request
        import time as _time
        from datetime import datetime, timedelta
        
        companies_path = BASE / "data" / profile.hh_companies_file
        if not companies_path.exists():
            print(f"  ⚠️ Companies file not found: {companies_path}")
            return candidates
        
        companies = json.loads(companies_path.read_text())
        enabled = [c for c in companies if c.get("enabled", True)]
        print(f"  📡 HH API: {len(enabled)} компаний включено")
        
        # Only fetch resumes updated in the last 24 hours
        import datetime as _dt_mod
        date_from = (_dt_mod.datetime.now(_dt_mod.timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
        
        # Industry filter: 11 = СМИ, маркетинг, реклама, BTL, PR, дизайн, продюсирование
        # Note: for resume search, industry filters by candidate's experience industries.
        # Since WORKPLACE_ORGANIZATION queries are already company-specific, industry filter
        # is optional — enable if needed to reduce noise.
        use_industry_filter = False
        industry_id = "11"
        
        for comp in enabled:
            hh_key = comp.get("hh_key", "")
            if not hh_key:
                continue
            
            try:
                # Fetch resumes updated in last 24h, Russia only
                for page in range(5):
                    params = {
                        "text": hh_key,
                        "per_page": "20",
                        "page": str(page),
                        "order_by": "publication_time",
                        "date_from": date_from,
                        "area": profile.hh_area,  # 113=Russia, 2=SPb, 1=Moscow
                    }
                    if use_industry_filter:
                        params["industry"] = industry_id
                    
                    r = hh_request("GET", "/resumes", params=params)
                    data = r.json()
                    items = data.get("items", [])
                    if page == 0:
                        # Save 24h count for panel (how many updated in last 24h)
                        comp["count_24h"] = data.get("found", 0)
                    
                    for item in items:
                        link = item.get("alternate_url", "")
                        title = item.get("title", "")
                        
                        # Build resume text from brief card
                        exp_list = item.get("experience", [])
                        exp_text = ""
                        for e in exp_list[:3]:
                            exp_text += f"{e.get('position','')} в {e.get('company','')} ({e.get('start','')}-{e.get('end','н.в.')}). "
                        
                        salary = item.get("salary")
                        sal_str = f"{salary['amount']} {salary['currency']}" if salary else ""
                        area = (item.get("area") or {}).get("name", "")
                        total_exp = item.get("total_experience", {})
                        months = total_exp.get("months", 0) if total_exp else 0
                        skills = ", ".join(item.get("skill_set", [])[:10])
                        
                        resume_text = f"Должность: {title}\nГород: {area}\nОпыт: {months // 12} лет {months % 12} мес\nЗарплата: {sal_str}\nНавыки: {skills}\nОпыт работы: {exp_text}"
                        
                        last_job = f"{exp_list[0].get('company','')} — {exp_list[0].get('position','')}" if exp_list else ""
                        
                        candidates.append({
                            "link": link,
                            "title": title,
                            "resume_text": resume_text,
                            "salary_str": sal_str,
                            "lastJob": last_job,
                            "source_company": comp.get("name", ""),
                        })
                    
                    if page >= data.get("pages", 1) - 1 or not items:
                        break
                    _time.sleep(0.5)
                
            except Exception as e:
                print(f"  ⚠️ HH API error for {comp.get('name','')}: {e}")
            
            _time.sleep(0.3)
        
        # Persist updated 24h counts for panel visibility
        try:
            companies_path.write_text(json.dumps(companies, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"  ⚠️ Failed to save companies counts: {e}")

        print(f"  📡 HH API: получено {len(candidates)} резюме")
        
    except Exception as e:
        print(f"  ❌ HH API error: {e}")
    
    return candidates


def parse_candidates_from_html(html: str, subject: str) -> List[Dict]:
    """Парсит кандидатов из HTML письма"""
    candidates = []
    text = html or ""
    
    # Ищем ссылки на резюме
    for match in re.finditer(r'<a[^>]+href=["\']([^"\']*hh\.ru/resume/[^"\']*)["\'][^>]*>([\s\S]*?)</a>', 
                             text, flags=re.I):
        link = match.group(1)
        anchor_text = match.group(2)
        title = re.sub(r"<[^>]+>", "", anchor_text).replace("&nbsp;", " ")
        title = re.sub(r"\s+", " ", title).strip()
        
        if not title:
            continue
        
        # Контекст вокруг ссылки для дополнительной инфы
        start, end = match.span()
        context_start = max(0, start - 1200)
        context = text[context_start: min(len(text), end + 1200)]
        anchor_pos = start - context_start

        # Ищем ближайшее к ссылке значение поля (а не первое в контексте),
        # иначе при плотной верстке HH может подтянуться значение от соседнего кандидата.
        def nearest_field(pattern: str) -> str:
            best = None
            best_dist = None
            for m_field in re.finditer(pattern, context, flags=re.I):
                dist = abs(m_field.start() - anchor_pos)
                if best is None or dist < best_dist:
                    best = m_field
                    best_dist = dist
            if not best:
                return ""
            return re.sub(r"\s+", " ", best.group(1).replace("&nbsp;", " ")).strip()

        last_job = nearest_field(r"Последнее место работы[:\s]*([^<]+)")
        salary = nearest_field(r"Уровень дохода[:\s]*([^<]+)")
        
        description = f"Автопоиск: {subject}"
        if last_job:
            description += f" | Последнее место: {last_job}"
        if salary:
            description += f" | Доход: {salary}"
        
        candidates.append({
            "title": title,
            "link": link,
            "normalized_link": normalize_link(link),
            "description": description,
            "lastJob": last_job,
            "salary": salary,
            "source_subject": subject
        })
    
    return candidates


def score_candidates_for_job(candidates: List[Dict], job: JobConfig, openai_config: Dict) -> List[Dict]:
    """Скоринг кандидатов для конкретной роли через LLM"""
    
    api_key = os.environ.get(openai_config['api_key_env'])
    if not api_key:
        print(f"No OpenAI API key, using fallback scoring")
        return [fallback_score(c) for c in candidates]
    
    # Загружаем промпт для роли
    prompt_path = BASE / "config" / "prompts" / job.prompt_file
    if not prompt_path.exists():
        print(f"Prompt file not found: {prompt_path}")
        return [fallback_score(c) for c in candidates]
    
    system_prompt = prompt_path.read_text(encoding="utf-8")
    schema_hint = (
        "Верни только JSON-массив объектов в порядке входа: "
        "[{\"index\":1,\"relevant\":true/false,\"fit_type\":\"target|near_target|not_fit\",\"confidence\":0..1,\"suggested_action\":\"...\"}]"
    )
    
    def score_batch(batch: List[Dict]) -> List[Dict]:
        listing = []
        for i, c in enumerate(batch, start=1):
            listing.append(f"[{i}] {c['title']} | {c.get('description', c.get('resume_text', ''))}")
        
        user_text = f"КАНДИДАТЫ НА РОЛЬ {job.name.upper()}:\n\n" + "\n".join(listing)
        
        payload = {
            "model": openai_config['model'],
            "messages": [
                {"role": "system", "content": system_prompt + "\n\n" + schema_hint},
                {"role": "user", "content": user_text},
            ],
            "temperature": openai_config.get('temperature', 0.1),
        }
        
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=openai_config.get('timeout', 60),
        )
        response.raise_for_status()
        
        content = response.json()["choices"][0]["message"]["content"]
        json_match = re.search(r"\[[\s\S]*\]", content)
        scores_array = json.loads(json_match.group(0) if json_match else content)
        
        results = []
        for i, candidate in enumerate(batch, start=1):
            score = next((s for s in scores_array if s.get("index") == i), 
                        scores_array[i-1] if i-1 < len(scores_array) else {})
            
            item = {
                "relevant": bool(score.get("relevant", False)),
                "fit_type": score.get("fit_type", "not_fit"),
                "confidence": float(score.get("confidence", 0)),
                "reason": score.get("suggested_action", ""),
                "job_slug": job.slug,
                "job_name": job.name
            }
            if job.slug == 'project_manager':
                item = apply_project_manager_postfilter(candidate, item)
            results.append(item)
        
        return results
    
    # Обработка батчами
    batch_size = openai_config.get('batch_size', 15)
    all_scores = []
    total_batches = (len(candidates) + batch_size - 1) // batch_size
    
    for i in range(0, len(candidates), batch_size):
        batch_num = i // batch_size + 1
        batch = candidates[i:i + batch_size]
        
        print(f"[{job.name}] Batch {batch_num}/{total_batches} ({len(batch)} candidates)...", flush=True)
        
        try:
            scores = score_batch(batch)
            all_scores.extend(scores)
            print(f"[{job.name}] Batch {batch_num} done.", flush=True)
        except Exception as e:
            print(f"[{job.name}] Batch {batch_num} FAILED: {e}", flush=True)
            all_scores.extend([fallback_score(c, job) for c in batch])
    
    return all_scores


def apply_project_manager_postfilter(candidate: Dict, score: Dict) -> Dict:
    """Жёсткие пост-правила для PM (стабилизируют спорные кейсы LLM)."""
    text = " ".join([
        candidate.get('title', ''),
        candidate.get('lastJob', ''),
        candidate.get('description', ''),
    ]).lower()

    force_not_fit_keywords = [
        'няня', 'гуверн', 'воспитател',
        'финансовый директор',
        'студенческий союз', 'мирэа',
        'senior account manager', 'аккаунт менеджер', 'account manager',
        'руководитель event-отдел', 'руководитель event отдела',
        'руководитель event-департамент', 'руководитель event департамента',
        'event team leader', 'head of events', 'team lead',
    ]
    if any(k in text for k in force_not_fit_keywords):
        return {
            **score,
            'relevant': False,
            'fit_type': 'not_fit',
            'confidence': max(float(score.get('confidence', 0)), 0.8),
            'reason': 'PM post-filter: role mismatch/overqualified/not-fit'
        }

    return score


def fallback_score(candidate: Dict, job: Optional[JobConfig] = None) -> Dict:
    """Fallback скоринг по ключевым словам если LLM не работает"""
    title = candidate.get('title', '').lower()
    description = candidate.get('description', '').lower()
    text = f"{title} {description}"
    
    # Базовые ключевые слова для event/BTL
    positive_keywords = [
        'менеджер', 'директор', 'руководитель', 'управляющий', 'координатор',
        'event', 'проект', 'клиент', 'агентство', 'маркетинг', 'реклама',
        'организация', 'мероприятие', 'продажи', 'развитие'
    ]
    
    score = sum(1 for keyword in positive_keywords if keyword in text)
    confidence = min(score * 0.15, 0.8)  # Максимум 0.8 для fallback
    
    return {
        "relevant": confidence > 0.5,
        "fit_type": "target" if confidence > 0.6 else "near_target" if confidence > 0.5 else "not_fit",
        "confidence": confidence,
        "reason": f"Fallback scoring: {score} keywords matched",
        "job_slug": job.slug if job else "unknown",
        "job_name": job.name if job else "Unknown"
    }


def _get_hh_extra(link: str) -> dict:
    """Fetch extra candidate info from HH API. Returns empty dict on failure."""
    if not link or 'hh.ru/resume/' not in link:
        return {}
    try:
        m = re.search(r'hh\.ru/resume/([a-f0-9]+)', link)
        if not m:
            return {}
        resume_id = m.group(1)
        
        from hh_api import get_resume
        data = get_resume(resume_id)
        
        sal = data.get('salary') or {}
        sal_amount = sal.get('amount')
        
        total_exp = (data.get('total_experience') or {}).get('months')
        
        work_formats = [wf.get('name', '') for wf in (data.get('work_format') or [])]
        
        return {
            'age': data.get('age'),
            'city': (data.get('area') or {}).get('name', ''),
            'salary': sal_amount,
            'total_exp_months': total_exp,
            'work_format': ', '.join(work_formats) if work_formats else '',
        }
    except Exception as e:
        print(f"HH API enrich failed for {link}: {e}")
        return {}


def send_telegram_report(profile: ProfileConfig, job_results: Dict[str, List[Dict]], jobs_for_report: Optional[List[JobConfig]] = None):
    """Отправляет отчёт в Telegram"""
    
    url = f"https://api.telegram.org/bot{profile.telegram_token}/sendMessage"
    
    # Формируем сводку по профилю
    total_candidates = sum(len(candidates) for candidates in job_results.values())
    total_relevant = sum(len([c for c in candidates if c.get('relevant', False)]) 
                        for candidates in job_results.values())
    
    if total_candidates == 0:
        text = (
            f"📭 <b>HR Radar: {profile.name}</b>\n"
            f"Новых кандидатов не найдено\n"
            f"📧 {profile.imap_user}"
        )
    else:
        header = (
            f"🔥 <b>HR Radar: {profile.name}</b>\n"
            f"Всего проанализировано: {total_candidates}\n"
            f"Релевантных найдено: {total_relevant}\n"
            f"📧 {profile.imap_user}\n\n"
        )
        
        job_summaries = []
        for job in (jobs_for_report or profile.jobs):
            candidates = job_results.get(job.slug, [])
            relevant_candidates = [c for c in candidates if c.get('relevant', False)]
            
            if relevant_candidates:
                job_summaries.append(
                    f"{job.emoji} <b>{job.name}</b>: {len(relevant_candidates)} кандидат(ов)"
                )
        
        if job_summaries:
            text = header + "\n".join(job_summaries)
        else:
            text = header + "Подходящих кандидатов не найдено 😔"
    
    # Отправляем сводку
    requests.post(url, json={
        "chat_id": profile.telegram_chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }, timeout=30).raise_for_status()
    
    # Отправляем детали по каждой роли с релевантными кандидатами
    for job in (jobs_for_report or profile.jobs):
        candidates = job_results.get(job.slug, [])
        relevant_candidates = [c for c in candidates if c.get('relevant', False)]
        
        if not relevant_candidates:
            continue
        
        # Сортируем по confidence
        relevant_candidates.sort(key=lambda x: x.get('confidence', 0), reverse=True)
        
        # Группируем по fit_type
        targets = [c for c in relevant_candidates if c.get('fit_type') == 'target' and c.get('confidence', 0) >= 0.75]
        near_targets = [c for c in relevant_candidates if c not in targets]
        
        # Детальные списки кандидатов не отправляем в Telegram — 
        # всё доступно в веб-панели
        # send_job_candidates(profile, job, targets[:15], "🎯 Целевые кандидаты")
        # send_job_candidates(profile, job, near_targets[:10], "🟡 Близкие к целевым")


def send_job_candidates(profile: ProfileConfig, job: JobConfig, candidates: List[Dict], title: str):
    """Отправляет список кандидатов для конкретной роли с кнопкой импорта в FW"""
    
    if not candidates:
        return
    
    url = f"https://api.telegram.org/bot{profile.telegram_token}/sendMessage"
    
    header = f"<b>{job.emoji} {job.name} - {title}</b>\n\n"
    
    chunks = []
    current_chunk = header
    
    for i, candidate in enumerate(candidates, 1):
        conf_pct = round(candidate.get('confidence', 0) * 100)
        
        candidate_block = (
            f"<b>{i}. {candidate['title']}</b>\n"
            f"Соответствие: {conf_pct}%\n"
            f"🏢 {candidate.get('lastJob', '-')}\n"
            f"💰 {candidate.get('salary', '-')}\n"
            f"🔗 <a href=\"{candidate['link']}\">Резюме на HH</a>\n"
            "─────────────────────\n"
        )
        
        # Проверяем лимит длины сообщения
        if len(current_chunk + candidate_block) > 3800:
            chunks.append(current_chunk)
            current_chunk = candidate_block
        else:
            current_chunk += candidate_block
    
    if current_chunk.strip():
        chunks.append(current_chunk)
    
    # Collect HH resume links for import button
    hh_links = []
    for c in candidates:
        link = c.get('link', '')
        if 'hh.ru/resume/' in link:
            import re
            m = re.search(r'hh\.ru/resume/([a-f0-9]+)', link)
            if m:
                hh_links.append(m.group(1))
    
    # Save candidate batch for import
    import hashlib
    batch_id = hashlib.md5(json.dumps(hh_links, sort_keys=True).encode()).hexdigest()[:8]
    batch_file = Path(__file__).parent / "data" / f"batch_{batch_id}.json"
    batch_file.parent.mkdir(parents=True, exist_ok=True)
    batch_file.write_text(json.dumps({
        "resume_ids": hh_links,
        "job_name": job.name,
        "title": title,
        "profile": profile.name,
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }, ensure_ascii=False), encoding="utf-8")
    
    # Build import button
    import_button = None
    if hh_links:
        fit_label = "целевых" if "Целев" in title else "кандидатов"
        import_button = {
            "inline_keyboard": [[
                {"text": f"📥 Импорт {len(hh_links)} {fit_label} в FriendWork",
                 "callback_data": f"fw_pick|{batch_id}"}
            ]]
        }
    
    # Отправляем чанки (кнопку — на последний)
    for i, chunk in enumerate(chunks):
        payload = {
            "chat_id": profile.telegram_chat,
            "text": chunk,
            "parse_mode": "HTML", 
            "disable_web_page_preview": True
        }
        # Attach button to last chunk
        if i == len(chunks) - 1 and import_button:
            payload["reply_markup"] = import_button
        
        requests.post(url, json=payload, timeout=30).raise_for_status()


def process_profile(profile_name: str, config_data: Dict, controls: Optional[Dict[str, Any]] = None):
    """Обрабатывает один профиль агентства"""
    
    print(f"\n🚀 Processing profile: {profile_name}")
    
    # Парсим конфиг
    try:
        profile = parse_profile_config(profile_name, config_data['profiles'][profile_name], config_data['common'])
    except Exception as e:
        print(f"❌ Config error for {profile_name}: {e}")
        return

    profile_controls = ((controls or {}).get("profiles", {}).get(profile_name, {}))
    if not profile_controls.get("enabled", True):
        print(f"⏭️ Profile {profile_name} disabled via controls")
        return

    enabled_jobs = profile_controls.get("jobs", {})
    active_jobs = [j for j in profile.jobs if enabled_jobs.get(j.slug, True)]
    report_enabled = profile_controls.get("report_enabled", True)

    if not active_jobs:
        print(f"⏭️ No active jobs for profile {profile_name}")
        return
    
    # Инициализируем БД
    conn = init_database(profile.db_path)
    
    # Получаем уже виденные ссылки
    seen_links = {row[0] for row in conn.execute("SELECT normalized_link FROM seen_links")}
    
    # Получаем кандидатов
    if profile.source == "hh_api":
        print(f"📡 Fetching candidates via HH API")
        api_candidates = fetch_candidates_from_hh_api(profile)
        # Convert to expected format
        raw_candidates = []
        for c in api_candidates:
            norm = normalize_link(c.get("link", ""))
            raw_candidates.append({
                "link": c["link"],
                "normalized_link": norm,
                "title": c.get("title", ""),
                "resume_text": c.get("resume_text", ""),
                "description": c.get("resume_text", ""),  # alias for scoring
                "lastJob": c.get("source_company", ""),
                "salary": c.get("salary_str", ""),
                "source_subject": f"HH API: {c.get('source_company', '')}",
                "source_company": c.get("source_company", ""),
            })
    else:
        print(f"📧 Fetching candidates from {profile.imap_user}")
        raw_candidates = fetch_candidates_from_email(profile)
    
    # Дедупликация
    unique_candidates = []
    current_run_links = set()
    
    for candidate in raw_candidates:
        normalized = candidate['normalized_link']
        if not normalized or normalized in seen_links or normalized in current_run_links:
            continue
        unique_candidates.append(candidate)
        current_run_links.add(normalized)
    
    print(f"📊 Found {len(raw_candidates)} raw, {len(unique_candidates)} unique new candidates")
    
    if not unique_candidates:
        # Отправляем уведомление о том, что новых кандидатов нет
        if report_enabled:
            send_telegram_report(profile, {}, active_jobs)
        return
    
    # Скоринг для каждой роли
    started = datetime.datetime.now(datetime.UTC).isoformat()
    openai_config = config_data['common']['openai']
    job_results = {}
    
    for job in active_jobs:
        print(f"\n🧠 Scoring candidates for {job.name}")
        
        # Скоринг кандидатов для этой роли
        scores = score_candidates_for_job(unique_candidates, job, openai_config)
        
        # Мержим оценки с кандидатами
        scored_candidates = []
        for candidate, score in zip(unique_candidates, scores):
            merged = {**candidate, **score}
            scored_candidates.append(merged)
        
        job_results[job.slug] = scored_candidates
        
        # Сохраняем статистику в БД
        relevant_count = len([c for c in scored_candidates if c.get('relevant', False)])
        conn.execute(
            "INSERT INTO runs(started_at,profile_name,job_slug,total_candidates,relevant_candidates,notes) VALUES(?,?,?,?,?,?)",
            (started, profile_name, job.slug, len(unique_candidates), relevant_count, "ok")
        )
        
        # Сохраняем детали скоринга
        for sc in scored_candidates:
            conn.execute(
                "INSERT INTO scored_candidates(run_date,normalized_link,candidate_title,last_job,salary,job_slug,relevant,fit_type,confidence,reason,source_subject) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (started, sc.get('normalized_link',''), sc.get('title',''), sc.get('lastJob',''),
                 sc.get('salary',''), job.slug, int(sc.get('relevant', False)),
                 sc.get('fit_type',''), sc.get('confidence',0), sc.get('reason',''),
                 sc.get('source_subject',''))
            )
    
    # Сохраняем виденные ссылки
    for candidate in unique_candidates:
        conn.execute(
            "INSERT OR IGNORE INTO seen_links(normalized_link,first_seen_at) VALUES(?,?)",
            (candidate['normalized_link'], started)
        )
    
    conn.commit()
    conn.close()
    
    # Отправляем результаты в Telegram
    if report_enabled:
        print(f"📱 Sending Telegram report for {profile.name}")
        send_telegram_report(profile, job_results, active_jobs)
    else:
        print(f"📵 Report disabled for {profile.name}")
    
    # ═══ AUTOFLOW: automatic deep scoring + FW import ═══
    autoflow_cfg = profile_controls.get("autoflow", {})
    panel_config = {}
    panel_config_path = BASE / "data" / "panel_config.json"
    if panel_config_path.exists():
        try:
            panel_config = json.loads(panel_config_path.read_text())
        except:
            pass
    
    for job in active_jobs:
        af = autoflow_cfg.get(job.slug, {})
        if not af.get("enabled", False):
            continue
        
        relevant = [c for c in job_results.get(job.slug, []) if c.get("relevant", False)]
        # Sort by confidence and limit to top-30 for deep scoring
        relevant.sort(key=lambda x: x.get('confidence', 0), reverse=True)
        relevant = relevant[:30]
        if not relevant:
            print(f"⚡ Autoflow [{job.name}]: нет релевантных, пропускаю")
            continue
        
        links = [c["link"] for c in relevant if c.get("link")]
        if not links:
            continue
        
        print(f"⚡ Autoflow [{job.name}]: {len(links)} кандидатов")
        
        # Per-job thresholds
        thresholds = af.get("thresholds", {})
        t_approve = thresholds.get("approve", panel_config.get("score_threshold_approve", 7))
        t_reject = thresholds.get("reject", panel_config.get("score_threshold_reject", 4))
        
        # Step 1: Deep scoring (if enabled)
        deep_results = []
        if af.get("deep_scoring", True):
            deep_results = run_autoflow_deep_scoring(
                links, job, openai_config, t_approve, t_reject, panel_config, profile_name
            )
            print(f"  ⚡ Deep scoring done: {len(deep_results)} scored")
        
        # Step 2: FW import (if enabled and route configured)
        routes = panel_config.get("routes", {})
        route_key = f"{profile_name}.{job.slug}"
        fw_vacancy_id = routes.get(route_key)
        
        if fw_vacancy_id and deep_results:
            imported, dupes, errors, contacts_opened, contacts_manual = run_autoflow_fw_import(
                deep_results, int(fw_vacancy_id), af, t_approve, t_reject, panel_config.get("default_model", openai_config.get("model", "gpt-5.2"))
            )
            print(f"  ⚡ FW import: {imported} new, {dupes} dupes, {errors} errors | contacts: {contacts_opened} open, {contacts_manual} manual")
            
            # Send autoflow summary to Telegram
            approved_count = len([r for r in deep_results if r.get("score", 0) >= t_approve])
            reviewed_count = len([r for r in deep_results if t_reject <= r.get("score", 0) < t_approve])
            rejected_count = len([r for r in deep_results if 0 < r.get("score", 0) < t_reject])
            
            # Save autoflow last run log
            try:
                # Per-candidate details for panel display
                candidates_detail = []
                for r in deep_results:
                    entry = {
                        "name": r.get("candidateName", ""),
                        "link": r.get("link", ""),
                        "score": r.get("score", 0),
                        "status": r.get("fw_status", ""),
                        "reason": r.get("reason", "")[:150],
                    }
                    if r.get("fw_link"):
                        entry["fw_link"] = r["fw_link"]
                    candidates_detail.append(entry)
                candidates_detail.sort(key=lambda x: x.get("score", 0), reverse=True)

                last_run = {
                    "date": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M"),
                    "primary": len(relevant),
                    "approved": approved_count,
                    "reviewed": reviewed_count,
                    "rejected": rejected_count,
                    "imported": imported,
                    "dupes": dupes,
                    "errors": errors,
                    "contacts_opened": contacts_opened,
                    "contacts_manual": contacts_manual,
                    "candidates": candidates_detail,
                }
                af["last_run"] = last_run
                # Re-save controls with updated last_run
                controls_raw = json.loads(CONTROLS_PATH.read_text(encoding="utf-8")) if CONTROLS_PATH.exists() else controls
                controls_raw.setdefault("profiles", {}).setdefault(profile_name, {}).setdefault("autoflow", {})[job.slug] = af
                CONTROLS_PATH.write_text(json.dumps(controls_raw, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                print(f"  ⚠️ Failed to save autoflow log: {e}")
            
            summary = (
                f"⚡ <b>Autoflow [{profile.name}]: {job.emoji} {job.name}</b>\n\n"
                f"📋 Первичный отбор: {len(relevant)} релевантных\n"
                f"🤖 Глубокий скоринг: ✅{approved_count} 👁{reviewed_count} ❌{rejected_count}\n"
                f"📤 Импорт в FW: {imported} новых, {dupes} дубликатов"
            )
            if af.get("open_contacts"):
                summary += f"\n🔍 Контакты: ✅{contacts_opened} открыто, 👁{contacts_manual} на проверку"
            try:
                requests.post(
                    f"https://api.telegram.org/bot{profile.telegram_token}/sendMessage",
                    json={"chat_id": profile.telegram_chat, "text": summary, "parse_mode": "HTML"},
                    timeout=30
                )
            except:
                pass
        elif not fw_vacancy_id and deep_results:
            print(f"  ⚠️ Autoflow: нет привязанной вакансии FW для {route_key}")
    
    print(f"✅ Profile {profile_name} completed")


def run_autoflow_deep_scoring(links: List[str], job: JobConfig, openai_config: Dict,
                              t_approve: int, t_reject: int, panel_config: Dict, profile_name: str) -> List[Dict]:
    """Run deep scoring synchronously for autoflow."""
    api_key = os.environ.get(openai_config['api_key_env'])
    if not api_key:
        print("  ⚠️ No OpenAI key for deep scoring")
        return []
    
    # Load prompt
    prompt_key = f"{profile_name}.{job.slug}"
    route_prompts = panel_config.get("route_prompts", {})
    prompt_name = route_prompts.get(prompt_key, job.prompt_file.replace(".txt", ""))
    prompt_path = BASE / "web_panel" / "prompts" / f"{prompt_name}.txt"
    if not prompt_path.exists():
        prompt_path = BASE / "config" / "prompts" / job.prompt_file
    prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else "Оцени кандидата."
    
    model = panel_config.get("default_model", openai_config.get("model", "gpt-5.2"))
    
    # Try to use HH API
    hh_get_resume = None
    try:
        from hh_api import get_resume as _hh_get
        hh_get_resume = _hh_get
    except:
        pass
    
    # Cache dir
    hh_cache_dir = BASE / "data" / "hh_resumes"
    hh_cache_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        from openai import OpenAI
        oai = OpenAI(api_key=api_key)
    except Exception as e:
        print(f"  ⚠️ OpenAI init failed: {e}")
        return []
    
    json_fmt = 'Ответь СТРОГО JSON без markdown (формат — см. системный промпт).'
    results = []
    
    for link in links:
        resume_id = link.rstrip("/").split("/")[-1].split("?")[0]
        cand_text = ""
        cand_name = ""
        
        # Load from cache or HH API
        cache_path = hh_cache_dir / f"{resume_id}.json"
        resume = None
        if cache_path.exists():
            try:
                resume = json.loads(cache_path.read_text())
            except:
                pass
        if not resume and hh_get_resume:
            try:
                resume = hh_get_resume(resume_id)
                if resume and not resume.get("errors"):
                    cache_path.write_text(json.dumps(resume, ensure_ascii=False, indent=2))
                else:
                    resume = None
            except:
                resume = None
        
        if resume and not resume.get("errors"):
            try:
                title = resume.get("title", "")
                area = resume.get("area", {}).get("name", "")
                salary = resume.get("salary")
                sal_text = f"{salary['amount']} {salary.get('currency','')}" if salary else "не указана"
                total_exp = resume.get("total_experience") or {}
                exp_months = total_exp.get("months", 0)
                fn = resume.get('first_name') or ''
                ln = resume.get('last_name') or ''
                cand_name = f"{ln} {fn}".strip()
                if not cand_name or cand_name in ('None None', 'None', ''):
                    cand_name = title or f"HH-{resume_id[:8]}"
                cand_text = f"Кандидат: {cand_name}\nДолжность: {title}\nГород: {area}\nЗарплата: {sal_text}\nОпыт: {exp_months//12} лет {exp_months%12} мес\n"
                for exp in resume.get("experience", [])[:5]:
                    cand_text += f"\nОпыт: {exp.get('company','')} — {exp.get('position','')} ({exp.get('start','')}-{exp.get('end','н.в.')})\n"
                    if exp.get("description"):
                        cand_text += f"  {exp['description'][:300]}\n"
                skills = resume.get("skill_set", [])
                if skills:
                    sk = [s if isinstance(s, str) else s.get("name","") for s in skills]
                    cand_text += f"\nНавыки: {', '.join(sk[:15])}\n"
            except Exception as e:
                cand_text = f"Ошибка: {e}"
        
        if not cand_text:
            cand_text = f"Резюме HH: {link}"
        
        # Score with LLM
        try:
            resp = oai.chat.completions.create(
                model=model, temperature=0.1, max_completion_tokens=500,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Оцени кандидата:\n\n{cand_text}\n\n{json_fmt}"}
                ]
            )
            raw = resp.choices[0].message.content.strip()
            parsed = json.loads(raw.replace('```json','').replace('```','').strip())
            # LLM may return a list instead of dict
            result = parsed[0] if isinstance(parsed, list) else parsed
        except Exception as e:
            result = {"verdict": "error", "score": 0, "reason": str(e)[:80]}
        
        score = result.get("score", 0) if isinstance(result, dict) else 0
        if score >= t_approve:
            fw_status = "Одобрен ИИ"
        elif 0 < score < t_reject:
            fw_status = "Отказ ИИ"
        elif 0 < score:
            fw_status = "Просмотрен ИИ"
        else:
            fw_status = "Новый"
        
        result["link"] = link
        result["candidateName"] = cand_name or f"HH-{resume_id[:8]}"
        result["fw_status"] = fw_status
        result["resume_id"] = resume_id
        results.append(result)
        
        time.sleep(0.5)  # rate limit
    
    return results


def run_autoflow_fw_import(deep_results: List[Dict], fw_vacancy_id: int,
                           autoflow_cfg: Dict, t_approve: int, t_reject: int,
                           model: str) -> tuple:
    """Import scored candidates to FriendWork based on autoflow config.
    Returns (imported, duplicates, errors, contacts_opened, contacts_manual)."""
    try:
        from fw_import import import_hh_to_fw, get_fw_headers
    except ImportError:
        print("  ⚠️ fw_import not available")
        return (0, 0, 0, 0, 0)

    headers = get_fw_headers()
    imported = 0
    dupes = 0
    errors = 0
    contacts_opened = 0   # контакты авто-прикреплены (attach)
    contacts_manual = 0   # найдены, но на ручную проверку (ambiguous)

    for r in deep_results:
        score = r.get("score", 0)
        fw_status = r.get("fw_status", "Новый")
        link = r.get("link", "")
        resume_id = r.get("resume_id", "")
        
        # Check if this status category is enabled for import
        should_import = False
        if fw_status == "Одобрен ИИ" and autoflow_cfg.get("fw_import_approved", True):
            should_import = True
        elif fw_status == "Просмотрен ИИ" and autoflow_cfg.get("fw_import_reviewed", False):
            should_import = True
        elif fw_status == "Отказ ИИ" and autoflow_cfg.get("fw_import_rejected", False):
            should_import = True
        
        if not should_import or not resume_id:
            continue
        
        try:
            res = import_hh_to_fw(resume_id, fw_vacancy_id,
                                  open_contacts=autoflow_cfg.get("open_contacts", False))
            cid = res.get("candidate_id")
            
            # Set AI status in FW
            if cid and fw_status != "Новый":
                comment = f"[AI {model}] Оценка: {score}/10 — {r.get('reason', '')}"
                try:
                    requests.post(
                        f"https://api.friend.work/Candidate/{cid}/CandidateHistories/set",
                        headers=headers, timeout=15,
                        json={"Name": fw_status, "JobId": fw_vacancy_id, "Description": comment}
                    )
                except:
                    pass
            
            # Save FW candidate ID back to result for panel display
            if cid:
                r["fw_link"] = f"https://app.friend.work/Candidate/Profile/{cid}"

            if res.get("ok"):
                imported += 1
            else:
                dupes += 1
            # контакты считаем в обоих случаях: и для новых, и для дубликатов (их карточки тоже правим)
            dec = res.get("contacts", "")
            if dec == "attach":
                contacts_opened += 1
            elif dec == "ambiguous":
                contacts_manual += 1
        except Exception as e:
            print(f"  ⚠️ FW import error for {resume_id}: {e}")
            errors += 1

        time.sleep(1)  # rate limit

    return (imported, dupes, errors, contacts_opened, contacts_manual)


def main():
    """Основная функция - обрабатывает все профили или указанный"""
    
    if len(sys.argv) > 1:
        target_profile = sys.argv[1]
    else:
        target_profile = None
    
    # Загружаем конфиг
    try:
        config = load_config()
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        return 1
    
    controls = load_controls(config)

    # Обрабатываем профили
    profiles_to_process = [target_profile] if target_profile else list(config['profiles'].keys())
    
    for profile_name in profiles_to_process:
        if profile_name not in config['profiles']:
            print(f"❌ Profile '{profile_name}' not found in config")
            continue
        
        try:
            process_profile(profile_name, config, controls)
        except Exception as e:
            print(f"❌ Error processing {profile_name}: {e}")
    
    print("\n🎉 Multi-radar completed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
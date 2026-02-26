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

    CONTROLS_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def parse_profile_config(profile_name: str, profile_data: Dict, common: Dict) -> ProfileConfig:
    """Парсит конфиг профиля в структуру данных"""
    
    # IMAP настройки
    imap = profile_data['imap']
    imap_pass = os.environ.get(imap['pass_env'])
    if not imap_pass:
        raise ValueError(f"Environment variable {imap['pass_env']} not set")
    
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
        imap_host=imap['host'],
        imap_port=imap['port'],
        imap_user=imap['user'],
        imap_pass=imap_pass,
        telegram_token=tg_token,
        telegram_chat=tg_chat,
        jobs=jobs,
        db_path=db_path
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
            listing.append(f"[{i}] {c['title']} | {c['description']}")
        
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
        "relevant": confidence > 0.3,
        "fit_type": "target" if confidence > 0.6 else "near_target" if confidence > 0.3 else "not_fit",
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
        
        send_job_candidates(profile, job, targets, "🎯 Целевые кандидаты")
        if near_targets:
            send_job_candidates(profile, job, near_targets, "🟡 Близкие к целевым")


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
    
    # Получаем кандидатов из email
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
    
    print(f"✅ Profile {profile_name} completed")


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
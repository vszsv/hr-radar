#!/usr/bin/env python3
"""
Full HR Radar pipeline with enhanced resume parsing and FriendWork import.
This script combines:
1. Daily email parsing (existing logic from run_daily.py)
2. Enhanced HH resume parsing via browser session
3. FriendWork API integration for candidate import
"""

import os
import sys
from pathlib import Path

# Add current directory to path for local imports
sys.path.insert(0, str(Path(__file__).parent))

# Load environment variables from .env file
def load_env_vars():
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()

# Load environment first
load_env_vars()

# Import existing modules
from run_daily import (
    db, fetch_today_candidates, llm_score, send_tg,
    normalize_link, PROMPT_PATH
)
from hh_session import enrich_candidates_with_hh_session
from friendwork_api import import_candidates_to_friendwork

import datetime
import json


def main():
    """Enhanced daily run with full resume parsing and FriendWork import."""
    
    print("🚀 Starting enhanced HR Radar pipeline...")
    
    # 1. Initialize database and get today's candidates (existing logic)
    conn = db()
    started = datetime.datetime.now(datetime.UTC).isoformat()
    seen = {row[0] for row in conn.execute("SELECT normalized_link FROM seen_links")}
    
    print("📧 Fetching candidates from email...")
    raw_candidates = fetch_today_candidates()
    
    # Filter unique candidates
    uniq_candidates = []
    in_run = set()
    for c in raw_candidates:
        nl = c["normalized_link"]
        if not nl or nl in seen or nl in in_run:
            continue
        uniq_candidates.append(c)
        in_run.add(nl)
    
    if not uniq_candidates:
        msg = "📭 HR Radar (Enhanced)\nНовых кандидатов за сегодня не найдено."
        send_tg(msg)
        conn.execute("INSERT INTO runs(started_at,total_candidates,relevant_candidates,notes) VALUES(?,?,?,?)", 
                    (started, 0, 0, "no candidates"))
        conn.commit()
        return
    
    print(f"📊 Found {len(uniq_candidates)} unique candidates, analyzing relevance...")
    
    # 2. LLM scoring (existing logic)
    scores = llm_score(uniq_candidates)
    relevant_candidates = []
    for c, s in zip(uniq_candidates, scores):
        c.update(s)
        if c["relevant"]:
            relevant_candidates.append(c)
    
    # Save to database
    for c in uniq_candidates:
        conn.execute("INSERT OR IGNORE INTO seen_links(normalized_link,first_seen_at) VALUES(?,?)", 
                    (c["normalized_link"], started))
    
    if not relevant_candidates:
        msg = (f"📭 HR Radar (Enhanced)\n"
               f"Проанализировано: {len(uniq_candidates)}\n"
               "Подходящих на Account Director не найдено.")
        send_tg(msg)
        conn.execute("INSERT INTO runs(started_at,total_candidates,relevant_candidates,notes) VALUES(?,?,?,?)", 
                    (started, len(uniq_candidates), 0, "no relevant"))
        conn.commit()
        return
    
    print(f"✅ Found {len(relevant_candidates)} relevant candidates")
    
    # 3. Enhanced HH resume parsing
    print("🔍 Fetching detailed resume information from HH...")
    try:
        hh_cookies_path = os.getenv('HH_COOKIES_PATH', '/opt/hr-radar/data/hh_cookies.json')
        enhanced_candidates = enrich_candidates_with_hh_session(relevant_candidates, hh_cookies_path)
        print(f"📄 Enhanced {len(enhanced_candidates)} candidates with full resume data")
    except Exception as e:
        print(f"⚠️ Warning: HH session enrichment failed: {e}")
        enhanced_candidates = relevant_candidates
    
    # 4. FriendWork import
    print("📤 Importing candidates to FriendWork...")
    try:
        import_result = import_candidates_to_friendwork(
            enhanced_candidates, 
            job_keywords=['Account Director', 'Group Head', 'Директор по работе с клиентами'],
            status='Новый'
        )
        
        print(f"📊 FriendWork import results:")
        print(f"  - Total: {import_result['total']}")
        print(f"  - Imported: {import_result['imported']}")
        print(f"  - Failed: {import_result['failed']}")
        print(f"  - Job ID: {import_result['job_id']}")
        print(f"  - Candidate IDs: {import_result['candidate_ids']}")
        
        if import_result['errors']:
            print(f"  - Errors: {import_result['errors'][:3]}")  # Show first 3 errors
    
    except Exception as e:
        print(f"⚠️ Warning: FriendWork import failed: {e}")
        import_result = {
            'imported': 0,
            'failed': len(enhanced_candidates),
            'candidate_ids': [],
            'errors': [str(e)]
        }
    
    # 5. Update database with results
    notes = {
        'enhanced_parsing': len(enhanced_candidates),
        'friendwork_imported': import_result.get('imported', 0),
        'friendwork_failed': import_result.get('failed', 0),
        'candidate_ids': import_result.get('candidate_ids', [])
    }
    
    conn.execute("INSERT INTO runs(started_at,total_candidates,relevant_candidates,notes) VALUES(?,?,?,?)", 
                (started, len(uniq_candidates), len(relevant_candidates), json.dumps(notes)))
    conn.commit()
    
    # 6. Send enhanced Telegram notification
    send_enhanced_telegram_report(relevant_candidates, import_result)
    
    print("✅ Enhanced HR Radar pipeline completed!")


def send_enhanced_telegram_report(candidates, import_result):
    """Send enhanced Telegram report with FriendWork import results."""
    
    # Separate candidates by classification
    targets = [c for c in candidates if c.get("fit_type") == "target" and c.get("confidence", 0) >= 0.75]
    doubtful = [c for c in candidates if c not in targets]
    
    fit_type_label = {
        "target": "🎯 Целевой",
        "near_target": "🟡 Близкий к целевому", 
        "not_fit": "⚪ Нецелевой",
    }
    
    def format_candidate_list(title: str, candidate_list, show_friendwork_ids=False):
        if not candidate_list:
            return ""
        
        chunks = []
        current_chunk = f"<b>{title}</b>\n\n"
        
        for i, c in enumerate(candidate_list, start=1):
            conf = round(c.get("confidence", 0) * 100)
            label = fit_type_label.get(c.get("fit_type", "target"), "⚪ Нецелевой")
            
            # Enhanced details from HH session
            hh_details = c.get('hh_details', {})
            city = hh_details.get('city', '') or c.get('city', '')
            salary = hh_details.get('salary', '') or c.get('salary', '')
            
            # Format candidate block
            block = (
                f"<b>{i}. {c['title']}</b>\n"
                f"{label} | {conf}%\n"
            )
            
            if city:
                block += f"🏙️ {city}\n"
            
            block += f"🏢 {c.get('lastJob', '-')}\n"
            
            if salary:
                block += f"💰 {salary}\n"
            else:
                block += f"💰 {c.get('salary', '-')}\n"
            
            # Enhanced info from full parsing
            if hh_details.get('skills'):
                skills_preview = ', '.join(hh_details['skills'][:3])
                if len(hh_details['skills']) > 3:
                    skills_preview += f" и ещё {len(hh_details['skills']) - 3}"
                block += f"🛠️ {skills_preview}\n"
            
            block += f"🔗 <a href=\"{c['link']}\">Резюме на HH</a>\n"
            
            # Add FriendWork ID if imported successfully
            if show_friendwork_ids and import_result.get('candidate_ids'):
                if i-1 < len(import_result['candidate_ids']):
                    fw_id = import_result['candidate_ids'][i-1]
                    block += f"📋 FriendWork ID: {fw_id}\n"
            
            block += "─────────────────────\n"
            
            # Check if adding this block would exceed Telegram's limit
            if len(current_chunk) + len(block) > 3800:
                chunks.append(current_chunk)
                current_chunk = block
            else:
                current_chunk += block
        
        if current_chunk.strip():
            chunks.append(current_chunk)
        
        return chunks
    
    # Send summary first
    total_candidates = len(candidates)
    imported_count = import_result.get('imported', 0) 
    failed_count = import_result.get('failed', 0)
    
    summary = (
        f"🔥 <b>HR Radar (Enhanced)</b>\n\n"
        f"📊 Анализ: {total_candidates} релевантных из {total_candidates + failed_count}\n"
        f"🎯 Целевых: {len(targets)}\n"
        f"🟡 Сомнительных: {len(doubtful)}\n\n"
        f"📤 FriendWork импорт:\n"
        f"✅ Загружено: {imported_count}\n"
        f"❌ Ошибок: {failed_count}"
    )
    
    if import_result.get('job_id'):
        summary += f"\n📋 Job ID: {import_result['job_id']}"
    
    send_tg(summary)
    
    # Send target candidates
    target_chunks = format_candidate_list(
        "🎯 Целевые кандидаты (импортированы в FriendWork)", 
        targets, 
        show_friendwork_ids=True
    )
    for chunk in target_chunks:
        send_tg(chunk)
    
    # Send doubtful candidates
    doubtful_chunks = format_candidate_list(
        "⚠️ Сомнительные кандидаты (требуют проверки)", 
        doubtful,
        show_friendwork_ids=True
    )
    for chunk in doubtful_chunks:
        send_tg(chunk)
    
    # Send import errors if any
    if import_result.get('errors'):
        error_msg = "❌ <b>Ошибки импорта:</b>\n\n" + '\n'.join(import_result['errors'][:5])
        if len(import_result['errors']) > 5:
            error_msg += f"\n\n... и ещё {len(import_result['errors']) - 5} ошибок"
        send_tg(error_msg)


if __name__ == "__main__":
    main()
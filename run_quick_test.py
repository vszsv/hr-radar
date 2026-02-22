#!/usr/bin/env python3
"""Quick test run with limited candidates for debugging."""

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

from run_daily import (
    db, fetch_today_candidates, llm_score, send_tg,
    normalize_link, PROMPT_PATH
)
from hh_session import enrich_candidates_with_hh_session
from friendwork_api import import_candidates_to_friendwork

import datetime
import json


def main():
    """Quick test run with enhanced parsing."""
    
    print("🚀 Quick test run with enhanced parsing...")
    
    # 1. Get today's candidates
    conn = db()
    started = datetime.datetime.now(datetime.UTC).isoformat()
    seen = {row[0] for row in conn.execute("SELECT normalized_link FROM seen_links")}
    
    print("📧 Fetching candidates from email...")
    raw_candidates = fetch_today_candidates()
    print(f"Found {len(raw_candidates)} total candidates")
    
    # Filter unique candidates
    uniq_candidates = []
    in_run = set()
    for c in raw_candidates:
        nl = c["normalized_link"]
        if not nl or nl in seen or nl in in_run:
            continue
        uniq_candidates.append(c)
        in_run.add(nl)
    
    print(f"Found {len(uniq_candidates)} unique candidates")
    
    if not uniq_candidates:
        print("❌ No new candidates found")
        return
    
    # Limit to first 5 for testing
    test_candidates = uniq_candidates[:5]
    print(f"🧪 Testing with {len(test_candidates)} candidates")
    
    # 2. LLM scoring
    print("🤖 Running LLM analysis...")
    try:
        scores = llm_score(test_candidates)
        relevant_candidates = []
        for c, s in zip(test_candidates, scores):
            c.update(s)
            if c["relevant"]:
                relevant_candidates.append(c)
        
        print(f"✅ Found {len(relevant_candidates)} relevant candidates")
        
        if not relevant_candidates:
            print("❌ No relevant candidates for import")
            return
            
    except Exception as e:
        print(f"❌ LLM scoring failed: {e}")
        return
    
    # 3. Enhanced HH parsing
    print("🔍 Enhanced HH parsing...")
    try:
        hh_cookies_path = '/opt/hr-radar/data/hh_cookies.json'
        enhanced_candidates = enrich_candidates_with_hh_session(relevant_candidates, hh_cookies_path)
        
        # Show enhancement results
        for i, c in enumerate(enhanced_candidates):
            details = c.get('hh_details', {})
            print(f"  [{i+1}] {c.get('title', 'Unknown')}")
            if details.get('ok'):
                print(f"      ✅ Enhanced: {details.get('first_name', '')} {details.get('last_name', '')}")
                print(f"      City: {details.get('city', 'Unknown')}")
                print(f"      Skills: {len(details.get('skills', []))} items")
            else:
                print(f"      ❌ Enhancement failed: {details.get('reason', 'Unknown')}")
        
    except Exception as e:
        print(f"❌ HH enhancement failed: {e}")
        enhanced_candidates = relevant_candidates
    
    # 4. FriendWork import
    print("📤 FriendWork import...")
    try:
        import_result = import_candidates_to_friendwork(
            enhanced_candidates, 
            job_keywords=['Account Director', 'Group Head', 'Директор по работе с клиентами'],
            status='Новый'
        )
        
        print(f"📊 Import results:")
        print(f"  - Total: {import_result['total']}")
        print(f"  - Imported: {import_result['imported']}")
        print(f"  - Failed: {import_result['failed']}")
        print(f"  - Job ID: {import_result['job_id']}")
        print(f"  - Candidate IDs: {import_result['candidate_ids']}")
        
        if import_result['errors']:
            print(f"  - First 3 errors: {import_result['errors'][:3]}")
            
        # Send telegram notification
        if import_result['imported'] > 0:
            msg = (
                f"🔥 <b>HR Radar (Quick Test)</b>\n\n"
                f"📊 Результаты:\n"
                f"✅ Импортировано: {import_result['imported']} из {import_result['total']}\n"
                f"📋 Job ID: {import_result['job_id']}\n"
                f"👥 Candidate IDs: {', '.join(map(str, import_result['candidate_ids']))}"
            )
            send_tg(msg)
    
    except Exception as e:
        print(f"❌ FriendWork import failed: {e}")
    
    print("✅ Quick test completed!")


if __name__ == "__main__":
    main()
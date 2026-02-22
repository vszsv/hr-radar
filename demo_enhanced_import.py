#!/usr/bin/env python3
"""Demo enhanced import with existing candidates."""

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

from run_daily import fetch_today_candidates, llm_score, send_tg
from hh_session import enrich_candidates_with_hh_session
from friendwork_api import import_candidates_to_friendwork


def main():
    """Demo run with enhanced parsing on fresh candidates."""
    
    print("🎭 Demo: Enhanced HR Radar Import")
    
    # Get fresh candidates (ignore seen status for demo)
    print("📧 Getting candidates from email...")
    raw_candidates = fetch_today_candidates()
    print(f"Found {len(raw_candidates)} candidates")
    
    if not raw_candidates:
        print("❌ No candidates found")
        return
    
    # Take first 3 for demo
    demo_candidates = raw_candidates[:3]
    print(f"🎯 Demo with {len(demo_candidates)} candidates:")
    
    for i, c in enumerate(demo_candidates, 1):
        print(f"  {i}. {c.get('title', 'Unknown')}")
        print(f"     Link: {c.get('link', 'No link')[:80]}...")
    
    # LLM scoring
    print("\n🤖 Running LLM relevance analysis...")
    try:
        scores = llm_score(demo_candidates)
        relevant_candidates = []
        for c, s in zip(demo_candidates, scores):
            c.update(s)
            fit_label = {'target': '🎯 Target', 'near_target': '🟡 Near', 'not_fit': '❌ Not fit'}
            print(f"  - {c.get('title', 'Unknown')[:50]}: {fit_label.get(s.get('fit_type', 'unknown'), '❓')} ({int(s.get('confidence', 0)*100)}%)")
            if c["relevant"]:
                relevant_candidates.append(c)
        
        print(f"\n✅ {len(relevant_candidates)} relevant for import")
        
        if not relevant_candidates:
            print("❌ No relevant candidates")
            return
            
    except Exception as e:
        print(f"❌ LLM scoring failed: {e}")
        # Use fallback for demo
        relevant_candidates = demo_candidates
        for c in relevant_candidates:
            c.update({'relevant': True, 'fit_type': 'target', 'confidence': 0.85})
    
    # Enhanced HH parsing
    print(f"\n🔍 Enhanced HH parsing for {len(relevant_candidates)} candidates...")
    try:
        enhanced_candidates = enrich_candidates_with_hh_session(
            relevant_candidates, 
            '/opt/hr-radar/data/hh_cookies.json'
        )
        
        print("📊 Enhancement results:")
        for i, c in enumerate(enhanced_candidates, 1):
            details = c.get('hh_details', {})
            title = c.get('title', 'Unknown')[:40]
            
            if details.get('ok'):
                name = f"{details.get('first_name', '')} {details.get('last_name', '')}".strip()
                city = details.get('city', 'Unknown')
                skills = len(details.get('skills', []))
                exp = len(details.get('experience', []))
                print(f"  ✅ [{i}] {title}")
                print(f"      Name: {name or 'Not parsed'}")
                print(f"      City: {city}")
                print(f"      Experience: {exp} records")
                print(f"      Skills: {skills} items")
            else:
                reason = details.get('reason', 'Unknown')
                print(f"  ❌ [{i}] {title} - Failed: {reason}")
                
    except Exception as e:
        print(f"❌ HH enhancement failed: {e}")
        enhanced_candidates = relevant_candidates
    
    # FriendWork import
    print(f"\n📤 Importing {len(enhanced_candidates)} candidates to FriendWork...")
    try:
        import_result = import_candidates_to_friendwork(
            enhanced_candidates,
            job_keywords=['Account Director', 'Group Head'],
            status='Новый'
        )
        
        print("\n🎉 IMPORT RESULTS:")
        print(f"  📊 Total processed: {import_result['total']}")
        print(f"  ✅ Successfully imported: {import_result['imported']}")
        print(f"  ❌ Failed: {import_result['failed']}")
        print(f"  🎯 Target job ID: {import_result['job_id']}")
        print(f"  👥 New candidate IDs: {import_result['candidate_ids']}")
        
        if import_result['errors']:
            print(f"  ⚠️ Errors: {len(import_result['errors'])}")
            for error in import_result['errors'][:2]:  # Show first 2 errors
                print(f"     - {error}")
        
        # Send notification if successful
        if import_result['imported'] > 0:
            msg = (
                f"🎭 <b>HR Radar DEMO</b>\n\n"
                f"✅ Импортировано: {import_result['imported']} кандидатов\n"
                f"📋 Job ID: {import_result['job_id']}\n"
                f"👥 Candidate IDs: {', '.join(map(str, import_result['candidate_ids']))}\n\n"
                f"🔥 <b>Теперь с расширенным парсингом резюме!</b>"
            )
            send_tg(msg)
            
        print("\n✨ Demo completed! Check FriendWork for imported candidates.")
            
    except Exception as e:
        print(f"❌ FriendWork import failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
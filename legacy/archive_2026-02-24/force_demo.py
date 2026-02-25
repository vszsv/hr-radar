#!/usr/bin/env python3
"""Force demo with Account/Manager candidates."""

import os
import sys
from pathlib import Path

# Add current directory to path
sys.path.insert(0, str(Path(__file__).parent))

# Load environment
def load_env_vars():
    env_path = Path(__file__).parent / '.env'
    if env_path.exists():
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()

load_env_vars()

from run_daily import fetch_today_candidates, send_tg
from hh_session import enrich_candidates_with_hh_session
from friendwork_api import import_candidates_to_friendwork


def main():
    print("🚀 FORCE DEMO: Account/Manager Candidates")
    
    # Get all candidates
    candidates = fetch_today_candidates()
    print(f"📊 Found {len(candidates)} total candidates")
    
    # Filter for Account/Manager/Director
    relevant_keywords = ['account', 'director', 'менеджер', 'manager']
    demo_candidates = []
    
    for c in candidates:
        title = c.get('title', '').lower()
        if any(keyword in title for keyword in relevant_keywords):
            demo_candidates.append(c)
            if len(demo_candidates) >= 5:  # Limit to 5 for demo
                break
    
    print(f"🎯 Selected {len(demo_candidates)} relevant candidates:")
    for i, c in enumerate(demo_candidates, 1):
        title = c.get('title', 'Unknown')
        print(f"  {i}. {title}")
        print(f"     Last job: {c.get('lastJob', 'Unknown')}")
        print(f"     Salary: {c.get('salary', 'Unknown')}")
        print()
    
    if not demo_candidates:
        print("❌ No relevant candidates found")
        return
    
    # Force mark as relevant and target
    for c in demo_candidates:
        c.update({
            'relevant': True,
            'fit_type': 'target',
            'confidence': 0.80,
            'reason': 'Force demo - Account/Manager keywords'
        })
    
    print(f"✅ Force marked {len(demo_candidates)} as relevant targets")
    
    # Enhanced HH parsing
    print(f"\n🔍 Enhanced HH parsing...")
    try:
        enhanced_candidates = enrich_candidates_with_hh_session(
            demo_candidates, 
            '/opt/hr-radar/data/hh_cookies.json'
        )
        
        print("📊 Enhancement results:")
        enhanced_count = 0
        for i, c in enumerate(enhanced_candidates, 1):
            details = c.get('hh_details', {})
            title = c.get('title', 'Unknown')[:50]
            
            if details.get('ok'):
                enhanced_count += 1
                name = f"{details.get('first_name', '')} {details.get('last_name', '')}".strip()
                city = details.get('city', 'Не указан')
                skills = details.get('skills', [])
                exp = details.get('experience', [])
                education = details.get('education', [])
                
                print(f"  ✅ [{i}] {title}")
                print(f"      👤 Name: {name or 'Не распознано'}")
                print(f"      🏙️ City: {city}")
                print(f"      💼 Experience: {len(exp)} records")
                print(f"      🛠️ Skills: {len(skills)} items")
                print(f"      🎓 Education: {len(education)} records")
                
                if skills:
                    print(f"      🔹 Top skills: {', '.join(skills[:3])}")
                print()
            else:
                print(f"  ❌ [{i}] {title[:50]} - Enhancement failed")
                print(f"      Reason: {details.get('reason', 'Unknown')}")
                print()
        
        print(f"📈 Enhanced: {enhanced_count}/{len(enhanced_candidates)} candidates")
                
    except Exception as e:
        print(f"❌ HH enhancement failed: {e}")
        enhanced_candidates = demo_candidates
    
    # FriendWork import
    print(f"\n📤 IMPORTING TO FRIENDWORK...")
    print(f"Target candidates: {len(enhanced_candidates)}")
    
    try:
        import_result = import_candidates_to_friendwork(
            enhanced_candidates,
            job_keywords=['Account Director', 'Group Head'],
            status='Новый'
        )
        
        print("\n🎉 ===== FINAL RESULTS =====")
        print(f"📊 Total processed: {import_result['total']}")
        print(f"✅ Successfully imported: {import_result['imported']}")
        print(f"❌ Failed imports: {import_result['failed']}")
        print(f"🎯 Target job ID: {import_result['job_id']}")
        print(f"👥 New candidate IDs: {import_result['candidate_ids']}")
        
        if import_result['errors']:
            print(f"\n⚠️ Import errors ({len(import_result['errors'])}):")
            for i, error in enumerate(import_result['errors'][:3], 1):
                print(f"  {i}. {error}")
        
        # Success notification
        if import_result['imported'] > 0:
            candidates_text = '\\n'.join([
                f"• {c.get('title', 'Unknown')}" 
                for c in enhanced_candidates[:import_result['imported']]
            ])
            
            msg = (
                f"🚀 <b>HR Radar Enhanced Import</b>\\n\\n"
                f"✅ Импортировано: {import_result['imported']} кандидатов\\n"
                f"📋 Job ID: {import_result['job_id']}\\n"
                f"👥 IDs: {', '.join(map(str, import_result['candidate_ids']))}\\n\\n"
                f"<b>Импортированы:</b>\\n{candidates_text}\\n\\n"
                f"🔥 <b>С расширенным парсингом резюме!</b>"
            )
            send_tg(msg)
            
        print("\\n🎊 DEMO COMPLETED!")
        print(f"Check FriendWork for {import_result['imported']} new enhanced candidates")
            
    except Exception as e:
        print(f"❌ FriendWork import failed: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""Test script for enhanced HH parsing and FriendWork integration."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from hh_session import build_session, fetch_resume_details, is_session_alive
from friendwork_api import map_hh_candidate_to_friendwork, FriendWorkAPI
import json


def test_hh_parsing():
    """Test HH resume parsing with actual session."""
    
    cookies_path = "/opt/hr-radar/data/hh_cookies.json"
    
    print("🔍 Testing HH session...")
    session = build_session(cookies_path)
    
    if not is_session_alive(session):
        print("❌ HH session is not alive")
        return False
    
    print("✅ HH session is alive")
    
    # Test with a sample resume URL (replace with actual URL for testing)
    test_url = "https://hh.ru/resume/12345678"  # This would need to be a real URL
    
    print(f"🔍 Testing resume parsing for: {test_url}")
    result = fetch_resume_details(session, test_url)
    
    if result.get('ok'):
        print("✅ Resume parsing successful")
        print("📊 Parsed fields:")
        for key, value in result.items():
            if key != 'resume_text':  # Don't print full text
                print(f"  - {key}: {value}")
    else:
        print(f"❌ Resume parsing failed: {result}")
    
    return result.get('ok', False)


def test_friendwork_mapping():
    """Test FriendWork data mapping."""
    
    print("🔍 Testing FriendWork mapping...")
    
    # Sample candidate data
    sample_candidate = {
        'title': 'Иванов Иван Иванович, Account Director',
        'link': 'https://hh.ru/resume/12345678',
        'salary': '150000 руб',
        'lastJob': 'ООО "Рекламное агентство"',
        'description': 'Автопоиск: Account Director',
        'fit_type': 'target',
        'confidence': 0.85,
        'hh_details': {
            'first_name': 'Иван',
            'last_name': 'Иванов',
            'middle_name': 'Иванович',
            'city': 'Москва',
            'age': '32',
            'gender': 'М',
            'salary': '150 000 - 200 000 руб',
            'experience': ['Account Manager в ООО "Медиа Групп", 2020-2023', 'Junior Account в ООО "Креатив", 2018-2020'],
            'education': ['МГУ, Факультет журналистики, 2018'],
            'skills': ['Account management', 'Client relations', 'Project management', 'English'],
            'languages': ['Русский — родной', 'Английский — B2'],
            'about': 'Опытный аккаунт-менеджер с 5-летним опытом работы в рекламных агентствах',
            'resume_text': 'Полный текст резюме...'
        }
    }
    
    mapped = map_hh_candidate_to_friendwork(sample_candidate)
    
    print("✅ Mapping successful")
    print("📊 FriendWork format:")
    print(json.dumps(mapped, ensure_ascii=False, indent=2))
    
    return True


def test_friendwork_api():
    """Test FriendWork API connection (if configured)."""
    
    print("🔍 Testing FriendWork API...")
    
    api = FriendWorkAPI()
    
    if not api.base_url or not api.auth_token:
        print("⚠️ FriendWork API not configured (missing URL or token)")
        return False
    
    print(f"🌐 API URL: {api.base_url}")
    
    try:
        jobs = api.get_jobs(5)
        print(f"✅ API connection successful, found {len(jobs)} jobs:")
        for job in jobs[:3]:
            print(f"  - {job.get('id')}: {job.get('title', job.get('name', 'Unknown'))}")
        return True
    except Exception as e:
        print(f"❌ API connection failed: {e}")
        return False


def main():
    print("🧪 Testing Enhanced HR Radar Components\n")
    
    # Test 1: HH Session and Parsing
    # test_hh_parsing()  # Commented out as it needs real resume URL
    
    # Test 2: Data Mapping
    print("\n" + "="*50)
    test_friendwork_mapping()
    
    # Test 3: FriendWork API
    print("\n" + "="*50)
    test_friendwork_api()
    
    print("\n✅ Testing completed!")


if __name__ == "__main__":
    main()
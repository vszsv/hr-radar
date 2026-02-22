#!/usr/bin/env python3
"""
Test script to discover FriendWork API endpoints and structure.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from friendwork_api import FriendWorkAPI
import requests


def test_api_discovery():
    """Test various API endpoints to understand structure."""
    
    api = FriendWorkAPI()
    
    if not api.base_url or not api.auth_token:
        print("❌ API credentials not configured")
        return False
    
    print(f"🔍 Testing FriendWork API:")
    print(f"  Base URL: {api.base_url}")
    print(f"  Token: {api.auth_token[:20]}...")
    
    # Common API endpoints to test
    test_endpoints = [
        '/api/v1/jobs',
        '/api/jobs', 
        '/v1/jobs',
        '/jobs',
        '/api/v1/candidates',
        '/api/candidates',
        '/v1/candidates', 
        '/candidates',
        '/api/v1/vacancies',
        '/api/vacancies',
        '/vacancies',
        '/',
        '/api',
        '/api/v1',
        '/health',
        '/status'
    ]
    
    working_endpoints = []
    
    for endpoint in test_endpoints:
        url = f"{api.base_url}{endpoint}"
        try:
            print(f"Testing: {endpoint}...", end=' ')
            response = api.session.get(url, timeout=10)
            
            if response.status_code == 200:
                print(f"✅ {response.status_code}")
                working_endpoints.append(endpoint)
                
                # Try to parse JSON
                try:
                    data = response.json()
                    if isinstance(data, dict) and len(str(data)) < 500:
                        print(f"  Data: {data}")
                    elif isinstance(data, list) and len(data) > 0:
                        print(f"  Found {len(data)} items, first: {data[0] if data else 'None'}")
                except:
                    print(f"  Non-JSON response, length: {len(response.text)}")
                    
            elif response.status_code == 401:
                print(f"🔑 {response.status_code} (Auth required)")
            elif response.status_code == 403:
                print(f"🚫 {response.status_code} (Forbidden)")
            elif response.status_code == 404:
                print(f"❌ {response.status_code} (Not Found)")
            else:
                print(f"⚠️ {response.status_code}")
                
        except requests.exceptions.Timeout:
            print("⏰ Timeout")
        except requests.exceptions.RequestException as e:
            print(f"💥 Error: {e}")
    
    print(f"\n✅ Working endpoints: {working_endpoints}")
    
    return len(working_endpoints) > 0


def test_candidate_creation():
    """Test creating a candidate with minimal data."""
    
    api = FriendWorkAPI()
    
    # Sample candidate data (minimal for testing)
    test_candidate = {
        'FirstName': 'Тест',
        'LastName': 'Кандидат',
        'Email': 'test@example.com',
        'Position': 'Test Position',
        'Source': 'HR Radar Test',
        'Resume': 'Тестовое резюме для проверки API'
    }
    
    # Try different candidate creation endpoints
    candidate_endpoints = ['/api/v1/candidates', '/api/candidates', '/candidates']
    
    for endpoint in candidate_endpoints:
        try:
            print(f"🧪 Testing candidate creation: {endpoint}")
            url = f"{api.base_url}{endpoint}"
            
            response = api.session.post(url, json=test_candidate, timeout=15)
            print(f"  Response: {response.status_code}")
            
            if response.status_code in [200, 201]:
                try:
                    result = response.json()
                    print(f"  ✅ Success: {result}")
                    return result
                except:
                    print(f"  ✅ Success (non-JSON): {response.text[:200]}")
                    return {'ok': True}
            else:
                print(f"  ❌ Failed: {response.text[:200]}")
                
        except Exception as e:
            print(f"  💥 Error: {e}")
    
    return None


if __name__ == '__main__':
    print("🧪 FriendWork API Discovery\n")
    
    # Test 1: Discover working endpoints
    print("="*50)
    if test_api_discovery():
        print("\n" + "="*50)
        # Test 2: Try creating a test candidate
        result = test_candidate_creation()
        if result:
            print(f"\n✅ Candidate creation works!")
        else:
            print(f"\n❌ Candidate creation failed")
    else:
        print("\n❌ No working endpoints found")
    
    print("\n✅ Discovery completed!")
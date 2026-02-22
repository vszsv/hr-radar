#!/usr/bin/env python3
"""
FriendWork API integration module for HR Radar.
- Manages authentication and API calls to FriendWork
- Maps HH candidate data to FriendWork fields
- Handles candidate creation and job application
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime, UTC

import requests


def _load_env_from_file(env_path: str = '/opt/hr-radar/.env') -> Dict:
    """Load environment variables from .env file."""
    env_vars = {}
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    env_vars[key.strip()] = value.strip()
    except Exception:
        pass
    return env_vars


class FriendWorkAPI:
    """FriendWork API client for importing candidates."""
    
    def __init__(self, base_url: str = None, auth_token: str = None):
        # Load from environment or fallback to .env file
        env_vars = _load_env_from_file()
        
        self.base_url = (
            base_url or 
            os.getenv('FRIENDWORK_API_URL') or 
            env_vars.get('FRIENDWORK_API_URL', '')
        ).rstrip('/')
        
        self.auth_token = (
            auth_token or 
            os.getenv('FRIENDWORK_API_TOKEN') or 
            env_vars.get('FRIENDWORK_API_TOKEN', '')
        )
        
        self.session = requests.Session()
        
        if self.auth_token:
            self.session.headers.update({
                'Authorization': f'Bearer {self.auth_token}',
                'Content-Type': 'application/json',
                'User-Agent': 'HR-Radar/1.0'
            })
    
    def get_jobs(self, limit: int = 50) -> List[Dict]:
        """Get list of active jobs/vacancies."""
        if not self.base_url:
            return []
        
        try:
            r = self.session.get(f'{self.base_url}/api/jobs', timeout=30)
            r.raise_for_status()
            jobs_data = r.json()
            # API returns array directly, not wrapped in 'jobs' key
            if isinstance(jobs_data, list):
                return jobs_data[:limit] if limit else jobs_data
            return jobs_data.get('jobs', [])
        except Exception as e:
            print(f"Error fetching jobs: {e}")
            return []
    
    def find_job_by_title(self, title_keywords: List[str]) -> Optional[Dict]:
        """Find job by title keywords (case-insensitive)."""
        jobs = self.get_jobs()
        for job in jobs:
            # FriendWork API uses 'name' field for job title
            job_title = job.get('name', '').lower()
            if any(keyword.lower() in job_title for keyword in title_keywords):
                return job
        return None
    
    def create_candidate(self, candidate_data: Dict) -> Dict:
        """Create new candidate in FriendWork."""
        if not self.base_url:
            return {'ok': False, 'reason': 'API not configured'}
        
        try:
            r = self.session.post(f'{self.base_url}/api/candidates', 
                                json=candidate_data, timeout=30)
            r.raise_for_status()
            result = r.json()
            
            # API returns {'Candidates': [created_candidates], ...}
            if 'Candidates' in result and result['Candidates']:
                candidate = result['Candidates'][0]  # First created candidate
                return {'ok': True, 'candidate_id': candidate.get('candidateId'), 'data': candidate}
            
            return {'ok': True, 'candidate_id': result.get('candidateId'), 'data': result}
        except Exception as e:
            print(f"Error creating candidate: {e}")
            return {'ok': False, 'reason': str(e)}
    
    def create_candidate_with_job(self, candidate_data: Dict, job_id: int, description: str = '') -> Dict:
        """Create candidate and assign to job in one call."""
        if not self.base_url:
            return {'ok': False, 'reason': 'API not configured'}
        
        try:
            # Add job assignment to candidate data
            payload = dict(candidate_data)
            payload.update({
                'jobId': job_id,
                'description': description or 'Импорт из HR Radar (расширенный парсинг)'
            })
            
            r = self.session.post(f'{self.base_url}/api/candidates', 
                                json=payload, timeout=30)
            r.raise_for_status()
            result = r.json()
            
            if 'Candidates' in result and result['Candidates']:
                candidate = result['Candidates'][0]
                return {
                    'ok': True, 
                    'candidate_id': candidate.get('candidateId'),
                    'job_id': job_id,
                    'data': candidate
                }
            
            return {'ok': True, 'candidate_id': result.get('candidateId'), 'data': result}
        except Exception as e:
            print(f"Error creating candidate with job: {e}")
            return {'ok': False, 'reason': str(e)}


def map_hh_candidate_to_friendwork(hh_candidate: Dict, hh_details: Dict = None) -> Dict:
    """
    Map HH candidate data to FriendWork API format.
    
    Expected FriendWork fields:
    - FirstName, LastName, MiddleName
    - Email, Phone, City
    - Position (роль)
    - Salary 
    - Experience (опыт работы)
    - Education (образование)
    - Skills (навыки)
    - Languages (языки)
    - AboutMe (дополнительная информация)
    - Resume (текст резюме)
    - ExternalLink (ссылка на HH)
    - Source (источник)
    """
    
    # Basic info from email parsing
    title = hh_candidate.get('title', '')
    link = hh_candidate.get('link', '')
    salary_basic = hh_candidate.get('salary', '')
    last_job = hh_candidate.get('lastJob', '')
    description = hh_candidate.get('description', '')
    
    # Extended info from full resume parsing
    details = hh_details or hh_candidate.get('hh_details', {}) or {}
    
    # Use enhanced parsing data if available, fallback to basic
    first_name = details.get('first_name', '')
    last_name = details.get('last_name', '')
    middle_name = details.get('middle_name', '')
    
    # If no enhanced name data, parse from title
    if not first_name and title:
        name_parts = title.split(',')[0].strip().split()
        first_name = name_parts[0] if name_parts else ''
        last_name = name_parts[1] if len(name_parts) > 1 else ''
        middle_name = name_parts[2] if len(name_parts) > 2 else ''
    
    # Contact and location info
    email = details.get('email', '')
    phone = details.get('phone', '')
    city = details.get('city', '')
    salary = details.get('salary', '') or salary_basic
    
    # Enhanced fields from full parsing
    experience_list = details.get('experience', [])
    education_list = details.get('education', [])
    skills_list = details.get('skills', [])
    languages_list = details.get('languages', [])
    about_text = details.get('about', '')
    resume_text = details.get('resume_text', '')
    
    # Format structured data for FriendWork
    experience_formatted = '\n\n'.join(experience_list) if experience_list else last_job
    education_formatted = '\n'.join(education_list) if education_list else ''
    skills_formatted = ', '.join(skills_list) if skills_list else ''
    languages_formatted = '\n'.join(languages_list) if languages_list else ''
    
    # Enhanced AboutMe with classification and additional info
    about_parts = []
    about_parts.append(f"Классификация: {hh_candidate.get('fit_type', 'unknown')} "
                      f"(уверенность {int(hh_candidate.get('confidence', 0) * 100)}%)")
    about_parts.append(f"Источник: {description}")
    
    if details.get('age'):
        gender_str = f" ({details.get('gender', '')})" if details.get('gender') else ""
        about_parts.append(f"Возраст: {details['age']}{gender_str}")
    
    if about_text:
        about_parts.append(f"О себе: {about_text}")
    
    # Build enhanced resume text
    enhanced_resume = []
    if first_name or last_name:
        full_name = ' '.join(filter(None, [first_name, middle_name, last_name]))
        enhanced_resume.append(f"=== {full_name} ===")
    
    if salary:
        enhanced_resume.append(f"Зарплата: {salary}")
    
    if city:
        enhanced_resume.append(f"Город: {city}")
    
    if experience_formatted:
        enhanced_resume.append(f"\n=== ОПЫТ РАБОТЫ ===\n{experience_formatted}")
    
    if education_formatted:
        enhanced_resume.append(f"\n=== ОБРАЗОВАНИЕ ===\n{education_formatted}")
    
    if skills_formatted:
        enhanced_resume.append(f"\n=== НАВЫКИ ===\n{skills_formatted}")
    
    if languages_formatted:
        enhanced_resume.append(f"\n=== ЯЗЫКИ ===\n{languages_formatted}")
    
    if about_text:
        enhanced_resume.append(f"\n=== О СЕБЕ ===\n{about_text}")
    
    resume_final = '\n\n'.join(enhanced_resume) if enhanced_resume else (
        resume_text or f"{title}\n\n{last_job}\n\n{salary_basic}"
    )
    
    # Build FriendWork payload (based on API structure)
    friendwork_data = {
        'firstName': first_name or 'Кандидат',
        'lastName': last_name or title.split(',')[0].strip() if title else 'HH Кандидат',
        'middleName': middle_name or '',
        'position': title,
        'salary': int(re.search(r'(\d+)', salary.replace(' ', '').replace(',', '')).group(1)) if salary and re.search(r'(\d+)', salary.replace(' ', '').replace(',', '')) else 0,
        'city': city or '',
        'sourceLink': link,
        'SiteName': 'HeadHunter',
        'addWay': 'Активный поиск',
        'additional': '\n\n'.join([
            f"=== РАСШИРЕННЫЕ ДАННЫЕ ===",
            f"Источник: HR Radar (расширенный парсинг)",
            f"Классификация: {hh_candidate.get('fit_type', 'unknown')} (уверенность {int(hh_candidate.get('confidence', 0) * 100)}%)",
            f"",
            f"=== ОПЫТ РАБОТЫ ===",
            experience_formatted if experience_formatted else "Не указан",
            f"",
            f"=== ОБРАЗОВАНИЕ ===", 
            education_formatted if education_formatted else "Не указано",
            f"",
            f"=== НАВЫКИ ===",
            skills_formatted if skills_formatted else "Не указаны",
            f"",
            f"=== ЯЗЫКИ ===",
            languages_formatted if languages_formatted else "Не указаны",
            f"",
            f"=== О СЕБЕ ===",
            about_text if about_text else "Не указано"
        ]).strip(),
        'communicationChannels': {
            'Email': [email] if email else [],
            'Phone': [phone] if phone else []
        } if email or phone else {}
    }
    
    # Clean up empty fields
    friendwork_data = {k: v for k, v in friendwork_data.items() if v or k in ['salary', 'middleName', 'city', 'communicationChannels']}
    
    return friendwork_data


def import_candidates_to_friendwork(candidates: List[Dict], job_keywords: List[str] = None, 
                                  status: str = 'Новый') -> Dict:
    """
    Import list of candidates to FriendWork.
    
    Returns:
        {
            'total': int,
            'imported': int, 
            'failed': int,
            'job_id': int|None,
            'candidate_ids': List[int],
            'errors': List[str]
        }
    """
    
    api = FriendWorkAPI()
    if not api.base_url or not api.auth_token:
        return {
            'total': len(candidates),
            'imported': 0,
            'failed': len(candidates),
            'job_id': None,
            'candidate_ids': [],
            'errors': ['FriendWork API not configured (missing URL or token)']
        }
    
    # Find target job
    job_keywords = job_keywords or ['Account Director', 'Group Head']
    target_job = api.find_job_by_title(job_keywords)
    if not target_job:
        return {
            'total': len(candidates),
            'imported': 0,
            'failed': len(candidates),
            'job_id': None,
            'candidate_ids': [],
            'errors': [f'Job not found by keywords: {job_keywords}']
        }
    
    job_id = target_job.get('jobId')  # FriendWork uses 'jobId' field
    imported = 0
    failed = 0
    candidate_ids = []
    errors = []
    
    for candidate in candidates:
        try:
            # Map to FriendWork format
            fw_data = map_hh_candidate_to_friendwork(candidate)
            
            # Build description with classification info
            fit_type = candidate.get('fit_type', 'unknown')
            confidence = int(candidate.get('confidence', 0) * 100)
            description = f"Импорт из HR Radar (расширенный парсинг) - {fit_type} (уверенность {confidence}%)"
            
            # Create candidate and assign to job
            create_result = api.create_candidate_with_job(fw_data, job_id, description)
            if not create_result.get('ok'):
                failed += 1
                errors.append(f"Failed to create candidate {candidate.get('title', 'Unknown')}: {create_result.get('reason')}")
                continue
            
            candidate_id = create_result.get('candidate_id')
            candidate_ids.append(candidate_id)
            imported += 1
            
            time.sleep(0.1)  # Rate limiting
            
        except Exception as e:
            failed += 1
            errors.append(f"Error processing candidate {candidate.get('title', 'Unknown')}: {str(e)}")
    
    return {
        'total': len(candidates),
        'imported': imported,
        'failed': failed,
        'job_id': job_id,
        'candidate_ids': candidate_ids,
        'errors': errors
    }


if __name__ == '__main__':
    # Test mode
    api = FriendWorkAPI()
    jobs = api.get_jobs(5)
    print(f"Found {len(jobs)} jobs")
    for job in jobs:
        print(f"- {job.get('id')}: {job.get('title', job.get('name', 'Unknown'))}")
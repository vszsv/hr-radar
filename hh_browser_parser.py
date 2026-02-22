#!/usr/bin/env python3
"""
HH Resume Parser using Playwright (browser-based).
Solves the 403 problem by using a real browser instead of HTTP requests.
Loads cookies from hh_cookies.json and navigates to resume pages.
"""

from __future__ import annotations

import json
import re
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout


COOKIES_PATH = Path(os.getenv('HH_COOKIES_PATH', '/opt/hr-radar/data/hh_cookies.json'))
PLAYWRIGHT_PATH = os.getenv('PLAYWRIGHT_BROWSERS_PATH', '/opt/hr-radar/ms-playwright')


def _strip_html(text: str) -> str:
    t = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.I)
    t = re.sub(r"<style[\s\S]*?</style>", "", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def parse_resume_page(page) -> Dict:
    """Extract all resume fields from a loaded Playwright page."""
    result = {
        'ok': True,
        'first_name': '',
        'last_name': '',
        'middle_name': '',
        'age': '',
        'gender': '',
        'city': '',
        'salary': '',
        'position': '',
        'experience': [],
        'education': [],
        'skills': [],
        'languages': [],
        'about': '',
        'email': '',
        'phone': '',
        'photo_url': '',
    }

    try:
        # Name
        try:
            name_el = page.locator('[data-qa="resume-personal-name"]').first
            name_text = name_el.text_content(timeout=3000) or ''
            parts = name_text.strip().split()
            if len(parts) >= 1:
                result['last_name'] = parts[0]
            if len(parts) >= 2:
                result['first_name'] = parts[1]
            if len(parts) >= 3:
                result['middle_name'] = parts[2]
        except Exception:
            # Try h1 as fallback
            try:
                h1 = page.locator('h1').first
                h1_text = h1.text_content(timeout=2000) or ''
                result['position'] = h1_text.strip()
            except Exception:
                pass

        # Title/position from h2 or data-qa
        try:
            title_el = page.locator('[data-qa="resume-block-title-position"]').first
            result['position'] = title_el.text_content(timeout=2000).strip()
        except Exception:
            pass

        # Age and gender
        try:
            age_el = page.locator('[data-qa="resume-personal-age"]').first
            age_text = age_el.text_content(timeout=2000) or ''
            age_match = re.search(r'(\d+)', age_text)
            if age_match:
                result['age'] = age_match.group(1)
            if 'женщина' in age_text.lower():
                result['gender'] = 'Ж'
            elif 'мужчина' in age_text.lower():
                result['gender'] = 'М'
        except Exception:
            pass

        # City
        try:
            city_el = page.locator('[data-qa="resume-personal-address"]').first
            result['city'] = city_el.text_content(timeout=2000).strip()
        except Exception:
            pass

        # Salary
        try:
            salary_el = page.locator('[data-qa="resume-block-salary"]').first
            result['salary'] = salary_el.text_content(timeout=2000).strip()
        except Exception:
            pass

        # Photo
        try:
            photo_el = page.locator('[data-qa="resume-photo"] img, .resume-photo img').first
            result['photo_url'] = photo_el.get_attribute('src', timeout=2000) or ''
        except Exception:
            pass

        # Experience
        try:
            exp_items = page.locator('[data-qa="resume-block-experience-position"]').all()
            for item in exp_items[:10]:  # Max 10 positions
                try:
                    # Get parent block for full context
                    parent = item.locator('..').locator('..')
                    
                    position_text = item.text_content(timeout=1500) or ''
                    
                    # Try to get company name
                    company = ''
                    try:
                        company_el = parent.locator('[class*="organization"]').first
                        company = company_el.text_content(timeout=1000) or ''
                    except Exception:
                        pass
                    
                    # Try to get period
                    period = ''
                    try:
                        period_el = parent.locator('[class*="period"]').first
                        period = period_el.text_content(timeout=1000) or ''
                    except Exception:
                        pass
                    
                    # Try to get description
                    description = ''
                    try:
                        desc_el = parent.locator('[data-qa="resume-block-experience-description"]').first
                        description = desc_el.text_content(timeout=1000) or ''
                    except Exception:
                        pass
                    
                    entry = f"{position_text}"
                    if company:
                        entry += f" | {company}"
                    if period:
                        entry += f" ({period})"
                    if description:
                        entry += f"\n{description.strip()[:500]}"
                    
                    result['experience'].append(entry.strip())
                except Exception:
                    continue
        except Exception:
            pass

        # Education  
        try:
            edu_items = page.locator('[data-qa*="resume-block-education"]').all()
            for item in edu_items[:5]:
                try:
                    edu_text = item.text_content(timeout=1500) or ''
                    edu_text = re.sub(r'\s+', ' ', edu_text).strip()
                    if edu_text and len(edu_text) > 5:
                        result['education'].append(edu_text)
                except Exception:
                    continue
        except Exception:
            pass

        # Skills
        try:
            skill_tags = page.locator('[data-qa="bloko-tag__text"], [data-qa="skills-table"] [data-qa="bloko-tag__text"]').all()
            for tag in skill_tags[:30]:
                try:
                    skill_text = tag.text_content(timeout=1000) or ''
                    if skill_text.strip() and len(skill_text.strip()) > 1:
                        result['skills'].append(skill_text.strip())
                except Exception:
                    continue
        except Exception:
            pass

        # Languages
        try:
            lang_items = page.locator('[data-qa="resume-block-languages"] p, [data-qa="resume-block-language-item"]').all()
            for item in lang_items[:5]:
                try:
                    lang_text = item.text_content(timeout=1000) or ''
                    if lang_text.strip() and len(lang_text.strip()) > 2:
                        result['languages'].append(lang_text.strip())
                except Exception:
                    continue
        except Exception:
            pass

        # About / key skills description
        try:
            about_el = page.locator('[data-qa="resume-block-skills-content"]').first
            result['about'] = about_el.text_content(timeout=2000).strip()[:2000]
        except Exception:
            pass

    except Exception as e:
        result['parse_error'] = str(e)

    return result


def fetch_resume_with_browser(resume_url: str, cookies_path: str = None) -> Dict:
    """Fetch and parse a single resume using Playwright browser."""
    
    cookies_path = cookies_path or str(COOKIES_PATH)
    
    # Load cookies
    try:
        cookies_data = json.loads(Path(cookies_path).read_text(encoding='utf-8'))
        cookies = cookies_data.get('cookies') if isinstance(cookies_data, dict) else cookies_data
    except Exception as e:
        return {'ok': False, 'reason': f'cookies_load_error: {e}'}
    
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = PLAYWRIGHT_PATH
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = browser.new_context(
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            )
            
            # Load cookies into context
            browser_cookies = []
            for c in cookies:
                cookie = {
                    'name': c.get('name', ''),
                    'value': c.get('value', ''),
                    'domain': c.get('domain', '.hh.ru'),
                    'path': c.get('path', '/'),
                }
                # Only add valid cookies
                if cookie['name'] and cookie['value']:
                    browser_cookies.append(cookie)
            
            if browser_cookies:
                context.add_cookies(browser_cookies)
            
            page = context.new_page()
            
            # Navigate to resume
            page.goto(resume_url, wait_until='domcontentloaded', timeout=30000)
            
            # Check if redirected to login
            current_url = page.url.lower()
            if 'login' in current_url or 'oauth' in current_url:
                browser.close()
                return {'ok': False, 'reason': 'auth_required'}
            
            # Wait for content to load
            time.sleep(2)
            
            # Parse the page
            result = parse_resume_page(page)
            result['external_link'] = resume_url
            result['page_url'] = page.url
            
            browser.close()
            return result
            
    except PwTimeout as e:
        return {'ok': False, 'reason': f'timeout: {e}'}
    except Exception as e:
        return {'ok': False, 'reason': f'browser_error: {e}'}


def enrich_candidates_with_browser(candidates: List[Dict], cookies_path: str = None, 
                                    max_candidates: int = 20, delay_sec: float = 2.0) -> List[Dict]:
    """
    Enrich multiple candidates with full resume data using Playwright browser.
    Opens browser ONCE and navigates to each resume page.
    """
    
    cookies_path = cookies_path or str(COOKIES_PATH)
    
    # Load cookies
    try:
        cookies_data = json.loads(Path(cookies_path).read_text(encoding='utf-8'))
        cookies = cookies_data.get('cookies') if isinstance(cookies_data, dict) else cookies_data
    except Exception as e:
        print(f"Error loading cookies: {e}")
        return candidates
    
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = PLAYWRIGHT_PATH
    
    enriched = []
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            context = browser.new_context(
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
            )
            
            # Load cookies
            browser_cookies = []
            for c in cookies:
                cookie = {
                    'name': c.get('name', ''),
                    'value': c.get('value', ''),
                    'domain': c.get('domain', '.hh.ru'),
                    'path': c.get('path', '/'),
                }
                if cookie['name'] and cookie['value']:
                    browser_cookies.append(cookie)
            
            if browser_cookies:
                context.add_cookies(browser_cookies)
            
            page = context.new_page()
            
            for i, candidate in enumerate(candidates[:max_candidates]):
                link = candidate.get('link') or candidate.get('external_link')
                if not link:
                    enriched.append(candidate)
                    continue
                
                print(f"  [{i+1}/{min(len(candidates), max_candidates)}] Parsing: {candidate.get('title', 'Unknown')[:50]}...")
                
                try:
                    page.goto(link, wait_until='domcontentloaded', timeout=30000)
                    
                    # Check for login redirect
                    if 'login' in page.url.lower() or 'oauth' in page.url.lower():
                        print(f"    ❌ Auth required")
                        candidate['hh_details'] = {'ok': False, 'reason': 'auth_required'}
                        enriched.append(candidate)
                        continue
                    
                    # Wait for content
                    time.sleep(delay_sec)
                    
                    # Parse
                    details = parse_resume_page(page)
                    details['external_link'] = link
                    details['page_url'] = page.url
                    
                    candidate['hh_details'] = details
                    
                    skills_count = len(details.get('skills', []))
                    exp_count = len(details.get('experience', []))
                    name = f"{details.get('first_name', '')} {details.get('last_name', '')}".strip()
                    print(f"    ✅ {name or 'Unknown'} | exp:{exp_count} skills:{skills_count}")
                    
                except PwTimeout:
                    print(f"    ⏰ Timeout")
                    candidate['hh_details'] = {'ok': False, 'reason': 'timeout'}
                except Exception as e:
                    print(f"    ❌ Error: {e}")
                    candidate['hh_details'] = {'ok': False, 'reason': str(e)}
                
                enriched.append(candidate)
                
                # Rate limiting between requests
                if i < len(candidates) - 1:
                    time.sleep(delay_sec)
            
            browser.close()
    
    except Exception as e:
        print(f"Browser session error: {e}")
        # Return whatever we have
        return enriched + candidates[len(enriched):]
    
    # Add remaining candidates without enrichment
    if len(enriched) < len(candidates):
        enriched.extend(candidates[len(enriched):])
    
    return enriched


if __name__ == '__main__':
    # Quick test
    test_url = 'https://hh.ru/resume/9421d8df0005234c030045769b387339426469'
    print(f"Testing browser parser with: {test_url}")
    result = fetch_resume_with_browser(test_url)
    print(f"Result: ok={result.get('ok')}")
    if result.get('ok'):
        print(f"  Name: {result.get('first_name', '')} {result.get('last_name', '')}")
        print(f"  City: {result.get('city', '')}")
        print(f"  Position: {result.get('position', '')}")
        print(f"  Skills: {len(result.get('skills', []))}")
        print(f"  Experience: {len(result.get('experience', []))}")
        print(f"  Education: {len(result.get('education', []))}")
        print(f"  Languages: {len(result.get('languages', []))}")
    else:
        print(f"  Reason: {result.get('reason')}")
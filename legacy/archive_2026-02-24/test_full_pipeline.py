#!/usr/bin/env python3
"""Test full HR Radar pipeline with enhanced parsing."""

import sys
import os
from pathlib import Path

# Add current directory to path
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

from run_daily import fetch_today_candidates, llm_score
from hh_session import enrich_candidates_with_hh_session
from friendwork_api import import_candidates_to_friendwork, map_hh_candidate_to_friendwork


def test_full_pipeline():
    print('🧪 Тест полного пайплайна HR Radar с расширенным парсингом')
    
    # 1. Получаем кандидатов из email
    print('\n📧 Получение кандидатов из email...')
    try:
        candidates = fetch_today_candidates()
        print(f'Найдено {len(candidates)} кандидатов из писем')
    except Exception as e:
        print(f'❌ Ошибка получения кандидатов: {e}')
        return False
    
    if not candidates:
        print('⚠️ Нет новых кандидатов для тестирования, создаем тестового...')
        # Создаем тестового кандидата
        candidates = [{
            'title': 'Тестовый Account Director',
            'link': 'https://hh.ru/resume/test123',
            'salary': '150 000 руб.',
            'lastJob': 'Рекламное агентство TEST',
            'description': 'Автопоиск: Account Director TEST',
            'normalized_link': 'https://hh.ru/resume/test123'
        }]
    
    # Берем первого для теста
    test_candidate = candidates[0]
    print(f'\n🧪 Тестируем с кандидатом: {test_candidate.get("title", "Unknown")}')
    print(f'Ссылка: {test_candidate.get("link", "No link")}')
    
    # 2. LLM скоринг
    print('\n🤖 LLM анализ релевантности...')
    try:
        scored = llm_score([test_candidate])
        if scored:
            test_candidate.update(scored[0])
            print(f'Релевантен: {test_candidate.get("relevant", False)}')
            print(f'Тип: {test_candidate.get("fit_type", "unknown")}')
            print(f'Уверенность: {int(test_candidate.get("confidence", 0) * 100)}%')
        else:
            print('⚠️ LLM скоринг недоступен, используем fallback')
            test_candidate.update({
                'relevant': True,
                'fit_type': 'target',
                'confidence': 0.85,
                'reason': 'test fallback'
            })
    except Exception as e:
        print(f'⚠️ Ошибка LLM скоринга: {e}')
        test_candidate.update({
            'relevant': True,
            'fit_type': 'target', 
            'confidence': 0.85,
            'reason': 'test fallback'
        })
    
    # 3. Расширенный парсинг HH (только если есть реальная ссылка)
    print('\n🔍 Расширенный парсинг резюме из HH...')
    if test_candidate.get('link', '').startswith('https://hh.ru/resume/') and 'test' not in test_candidate.get('link', ''):
        try:
            enhanced = enrich_candidates_with_hh_session([test_candidate], '/opt/hr-radar/data/hh_cookies.json')
            if enhanced and enhanced[0].get('hh_details'):
                details = enhanced[0]['hh_details']
                print('✅ Расширенные данные получены:')
                print(f'  - Имя: {details.get("first_name", "")} {details.get("last_name", "")}')
                print(f'  - Город: {details.get("city", "Не указан")}')
                print(f'  - Опыт: {len(details.get("experience", []))} записей')
                print(f'  - Образование: {len(details.get("education", []))} записей')  
                print(f'  - Навыки: {len(details.get("skills", []))} штук')
                print(f'  - Языки: {len(details.get("languages", []))} штук')
                test_candidate = enhanced[0]
            else:
                print('⚠️ Не удалось получить расширенные данные')
        except Exception as e:
            print(f'❌ Ошибка парсинга: {e}')
    else:
        print('⚠️ Пропускаем парсинг (тестовая ссылка)')
        # Добавляем тестовые расширенные данные
        test_candidate['hh_details'] = {
            'first_name': 'Тест',
            'last_name': 'Кандидатов',
            'city': 'Москва',
            'age': '30',
            'salary': '150 000 - 200 000 руб',
            'experience': ['Account Manager в ООО "Тест", 2020-2024', 'Junior в ООО "Тест2", 2018-2020'],
            'education': ['МГУ, Факультет экономики, 2018'],
            'skills': ['Account management', 'Client relations', 'Project management'],
            'languages': ['Русский — родной', 'Английский — B2'],
            'about': 'Опытный account manager с 5+ лет опыта'
        }
    
    # 4. Тест маппинга для FriendWork
    print('\n📤 Тест маппинга данных для FriendWork...')
    try:
        fw_data = map_hh_candidate_to_friendwork(test_candidate)
        print('✅ Маппинг успешен:')
        print(f'  - firstName: {fw_data.get("firstName")}')
        print(f'  - lastName: {fw_data.get("lastName")}')
        print(f'  - position: {fw_data.get("position")}')
        print(f'  - salary: {fw_data.get("salary")}')
        print(f'  - additional: {len(fw_data.get("additional", ""))} символов')
    except Exception as e:
        print(f'❌ Ошибка маппинга: {e}')
        return False
    
    # 5. Тест импорта в FriendWork
    print('\n📤 ВНИМАНИЕ: Тестовый импорт в FriendWork будет выполнен!')
    if True:  # Всегда выполняем тест
        print('\n📤 Тестовый импорт в FriendWork...')
        try:
            result = import_candidates_to_friendwork([test_candidate], status='Новый')
            print('✅ Импорт завершен:')
            print(f'  - Обработано: {result["total"]}')
            print(f'  - Импортировано: {result["imported"]}')
            print(f'  - Ошибок: {result["failed"]}')
            print(f'  - Job ID: {result["job_id"]}')
            print(f'  - Candidate IDs: {result["candidate_ids"]}')
            if result['errors']:
                print(f'  - Ошибки: {result["errors"][:3]}')
        except Exception as e:
            print(f'❌ Ошибка импорта: {e}')
    else:
        print('⚠️ Импорт пропущен')
    
    print('\n✅ Тест полного пайплайна завершен!')
    return True


if __name__ == '__main__':
    test_full_pipeline()
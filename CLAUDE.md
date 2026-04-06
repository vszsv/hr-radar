# HR Radar

## Описание

Автоматизированный сервис мониторинга резюме кандидатов с HeadHunter. Читает резюме через HH API (или IMAP), скорит релевантность через GPT-4o по нескольким профилям вакансий, отправляет отчёты в Telegram, импортирует лучших кандидатов в CRM FriendWork. Двухэтапный скоринг: первичный (batch GPT-4o) + глубокий autoflow (GPT-5.2/GPT-4o, 0-10 баллов).

## Стек технологий

- **Backend:** Python 3, FastAPI (веб-панель), SQLite3
- **AI:** OpenAI GPT-4o (первичный скоринг), GPT-5.2 (глубокий autoflow)
- **Интеграции:** HeadHunter API, Telegram Bot API, FriendWork CRM API
- **Frontend:** Jinja2 + Alpine.js + TailwindCSS
- **Автоматизация:** Playwright (HH-сессии), systemd timers/services
- **Зависимости:** requests, playwright, PyYAML (см. requirements.txt)

## Архитектура

```
HH API / IMAP  →  Дедупликация (seen_links)  →  GPT-4o Batch-скоринг
                                                        ↓
                                               SQLite (scored_candidates)
                                                        ↓
                                               Telegram-отчёт
                                                        ↓
                                               [Autoflow] Глубокий скоринг → FW Import
                                                        ↓
                                               Web Panel (аналитика)
```

### Профили (независимые пайплайны)

| Профиль | Источник | Роли |
|---------|----------|------|
| event_agencies | HH API | Account Director, AM, PM, New Business |
| btl_agencies | HH API | Account Director, AM, PM, New Business |
| btl_spb | HH API (СПб) | AD, AM, PM (BTL-промпты) |
| outsource_agencies | HH API | Outsource Manager |

Каждый профиль имеет свою БД (`data/{profile}.db`), свои seen_links, свой Telegram-канал.

## Ключевые файлы

| Файл | Назначение |
|------|-----------|
| `run_multi_radar.py` | Главный пайплайн: fetch → dedup → scoring → DB → Telegram → autoflow |
| `bot_control.py` | Telegram-бот: /radar меню, toggle профилей/вакансий |
| `hh_api.py` | Клиент HH API: OAuth, поиск резюме, rate limiting |
| `friendwork_api.py` | Клиент FriendWork CRM: создание кандидатов, привязка к вакансиям |
| `fw_import.py` | Batch-импорт из HH в FW с полными данными |
| `config/profiles.yaml` | Конфигурация всех профилей: источники, вакансии, токены, модели |
| `config/prompts/*.txt` | Промпты для каждой роли (7 файлов) |
| `data/radar_controls.json` | Runtime-состояние: enabled/disabled профили и вакансии |
| `data/{profile}.db` | SQLite: seen_links, runs, scored_candidates |
| `data/candidate_journey.db` | Трекинг полного пути кандидата (скоринг → FW-импорт) |
| `web_panel/app.py` | FastAPI: дашборд, кандидаты, autoflow-настройки, интервью-анализ |
| `web_panel/templates/index.html` | SPA-дашборд (Alpine.js) |

## Схема БД

**{profile}.db:**
- `seen_links` — дедупликация (normalized_link PK)
- `runs` — лог запусков (профиль, вакансия, кол-во кандидатов)
- `scored_candidates` — результаты скоринга (fit_type: target/near_target/not_fit, confidence 0-1)

**candidate_journey.db:**
- `journey` — полный путь: primary scoring → deep scoring (0-10) → FW import

## Точки входа

| Скрипт | Запуск | Назначение |
|--------|--------|-----------|
| `run_multi_radar.py` | systemd timer (09:00 UTC) | Основной пайплайн |
| `bot_control.py` | systemd service (постоянно) | Telegram-бот |
| `web_panel/app.py` | uvicorn :8000 | Веб-панель |
| `hh_session_check.py` | systemd timer (каждые 6ч) | Обновление OAuth-токена HH |

## Правила работы с кодом

- **Не удалять данные:** никогда не удалять файлы из `data/`, `backups/`, БД. Перед миграциями — бэкап.
- **Секреты:** `.env` не коммитить. Все токены через переменные окружения.
- **Коммиты:** формат `feat:` / `fix:` / `chore:` / `refactor:`
- **Тестирование:** перед деплоем — `python run_multi_radar.py {profile}` на одном профиле. Проверить Telegram-отчёт.
- **Промпты:** при изменении промптов (`config/prompts/`) — тестировать на 5-10 кандидатах, сравнивать fit_type.
- **profiles.yaml:** менять осторожно — влияет на все профили. Бэкапить перед изменением.
- **radar_controls.json:** не редактировать вручную — управлять через Telegram-бот.
- **Web Panel:** доступ по ключу (`?key=`), не менять авторизацию без согласования.
- **Rate limits:** HH API — 0.3-1с между запросами, OpenAI — batch по 15 кандидатов.

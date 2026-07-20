# HRM Ars Magna — Спецификация системы

> **Версия:** 1.0 draft
> **Дата:** 2026-04-10
> **Автор:** Claude Code + Василий Свеженцев
> **Статус:** На утверждении

---

## 1. Резюме проекта

### Что строим
Полноценную HRM-систему (Applicant Tracking System + Talent CRM) для коммуникационного агентства Ars Magna, заменяющую FriendWork CRM. Система объединяет AI-агента (архитектора подбора), веб-интерфейс для HR-менеджеров и руководителей, и интеграции с внешними платформами.

### Зачем
- FriendWork не настраивается под процессы агентства, данные размазаны, видеоинтервью не интегрировано
- Нет единого интерфейса: HH + FW + Telegram + Google Sheets
- AI-скоринг приходится строить поверх FW через API-костыли
- Нет контроля SLA для HR
- Нет talent mapping / кадрового резерва

### Для кого
| Роль | Частота | Основные задачи |
|------|---------|----------------|
| **HR-менеджер** | Ежедневно | Обзвон, переписка, ведение кандидатов по воронке, отчёты |
| **Руководитель департамента** | По необходимости | Создание заявок на вакансии, просмотр/одобрение кандидатов, собеседования |
| **AI-агент** | Автоматически | Поиск на HH, скоринг резюме, маршрутизация задач HR, talent mapping |
| **Администратор** | Редко | Настройка системы, профили вакансий, промпты |

### Масштаб
- Агентство до 50 человек
- ~2,000-3,000 кандидатов в базе
- 3-10 активных вакансий одновременно
- 1-3 HR-менеджера
- Целевой рынок: коммуникационные агентства (BTL, event, digital)

---

## 2. Архитектурные принципы

### 2.1 Одна база = источник правды
Вся информация о кандидатах, вакансиях, коммуникациях хранится в одной системе. Никаких "посмотри в FW" или "проверь в Google Sheets".

### 2.2 AI-агент = центр управления
Агент не просто скорит резюме — он **управляет процессом подбора**: сам ищет, сам оценивает, сам ставит задачи HR. HR = "кожаный интерфейс" для звонков и переписки.

### 2.3 Модель Lever/Greenhouse
- **Pipeline/воронка** — каждая вакансия имеет настраиваемые этапы
- **Scorecards** — структурированная оценка на каждом этапе (не свободный текст)
- **Collaborative feedback** — несколько участников оценивают кандидата
- **Talent mapping** — непрерывный мониторинг рынка даже без открытых вакансий

### 2.4 Экономия AI-бюджета
- Первичный скоринг — дешёвая модель (GPT-4o-mini / Haiku)
- Глубокий анализ — дорогая модель (GPT-5.2 / Opus) только для прошедших порог
- Видеоинтервью — отдельный pipeline (AssemblyAI + Claude)

### 2.5 Стек технологий
| Компонент | Технология | Обоснование |
|-----------|-----------|-------------|
| Backend | Python 3, FastAPI | Уже используется в HR Radar |
| БД | PostgreSQL | Реляционные данные, JSONB для гибкости, полнотекстовый поиск |
| Frontend | Jinja2 + Alpine.js + TailwindCSS | Уже используется, быстрый SSR |
| AI | OpenAI API (GPT-4o/5.2) + Anthropic (Claude) | Мультимодельный подход |
| Очереди | Celery + Redis | Фоновые задачи (скоринг, рассылки, парсинг) |
| Файлы | Локальная FS + S3 (опционально) | Резюме, видео, документы |
| Деплой | systemd + nginx | Уже настроено на Hetzner |

---

## 3. Модель данных

### 3.1 Основные сущности

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  Vacancy    │────<│ Application  │>────│   Candidate      │
│  (Вакансия) │     │ (Заявка на   │     │   (Кандидат)     │
│             │     │  кандидата)  │     │                  │
└─────────────┘     └──────────────┘     └─────────────────┘
       │                   │                      │
       │                   │                      │
       ▼                   ▼                      ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│ VacancyStage│     │  Activity    │     │ CandidateProfile │
│ (Этап       │     │  (Действие/  │     │ (Резюме, опыт,  │
│  воронки)   │     │   событие)   │     │  образование)   │
└─────────────┘     └──────────────┘     └─────────────────┘
                           │
                           ▼
                    ┌──────────────┐
                    │  Scorecard   │
                    │  (Оценка)    │
                    └──────────────┘
```

### 3.2 Candidate (Кандидат)

Центральная сущность. Один кандидат может быть привязан к нескольким вакансиям.

```sql
CREATE TABLE candidates (
    id              SERIAL PRIMARY KEY,
    -- Идентификация
    first_name      VARCHAR(100) NOT NULL,
    last_name       VARCHAR(100) NOT NULL,
    middle_name     VARCHAR(100),
    birth_date      DATE,
    sex             SMALLINT,           -- 0=unknown, 1=female, 2=male
    photo_url       TEXT,
    
    -- Контакты
    phone           VARCHAR(20),
    phone2          VARCHAR(20),
    email           VARCHAR(200),
    telegram        VARCHAR(100),
    whatsapp        VARCHAR(20),
    
    -- Профессиональное
    position        VARCHAR(200),       -- Целевая позиция
    salary_min      INTEGER,
    salary_max      INTEGER,
    currency        VARCHAR(3) DEFAULT 'RUB',
    experience_months INTEGER,
    city            VARCHAR(100),
    relocation_ready BOOLEAN DEFAULT FALSE,
    
    -- Источник
    source          VARCHAR(50),        -- hh, linkedin, referral, cold_search, response
    source_link     TEXT,               -- URL на HH/LinkedIn
    source_details  TEXT,               -- Откуда именно (какой поиск, какая компания)
    
    -- AI-данные
    psycho_profile  JSONB,              -- Психопортрет (из видеоинтервью + резюме + соцсети)
    ai_summary      TEXT,               -- Краткая AI-характеристика
    ai_tags         TEXT[],             -- AI-теги для поиска
    
    -- Системное
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    created_by      INTEGER REFERENCES users(id),
    
    -- Внешние ID (для миграции)
    fw_candidate_id INTEGER,            -- ID из FriendWork
    hh_resume_id    VARCHAR(100),       -- ID резюме на HH
    
    -- Полнотекстовый поиск
    search_vector   TSVECTOR
);
```

### 3.3 CandidateResume (Резюме / Опыт)

Полное резюме кандидата — опыт работы, образование, курсы, навыки, языки.

```sql
-- Опыт работы
CREATE TABLE candidate_experience (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    company         VARCHAR(200),
    position        VARCHAR(200),
    city            VARCHAR(100),
    date_from       DATE,
    date_to         DATE,               -- NULL = текущее место
    description     TEXT,               -- Обязанности
    achievements    TEXT,               -- Результаты
    sort_order      INTEGER DEFAULT 0
);

-- Образование
CREATE TABLE candidate_education (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    level           VARCHAR(50),        -- Высшее, Среднее, MBA, etc.
    institution     VARCHAR(300),
    faculty         VARCHAR(300),
    specialization  VARCHAR(300),
    graduate_year   INTEGER
);

-- Курсы и сертификаты
CREATE TABLE candidate_courses (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    name            VARCHAR(300),
    institution     VARCHAR(300),
    year            INTEGER
);

-- Языки
CREATE TABLE candidate_languages (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    language        VARCHAR(50),
    level           VARCHAR(20)         -- A1, A2, B1, B2, C1, C2, Native
);

-- Навыки (теги)
CREATE TABLE candidate_skills (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    skill           VARCHAR(100),
    source          VARCHAR(20)         -- manual, ai_extracted, hh
);

-- Дополнительная информация
CREATE TABLE candidate_additional (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
    citizenship     VARCHAR(100),
    work_permit     TEXT,
    schedule        VARCHAR(100),       -- Полная, Частичная, Проектная
    work_format     VARCHAR(100),       -- Офис, Удаленно, Гибрид
    driver_license  VARCHAR(20),
    business_trips  BOOLEAN
);
```

### 3.4 Vacancy (Вакансия)

```sql
CREATE TABLE vacancies (
    id              SERIAL PRIMARY KEY,
    title           VARCHAR(200) NOT NULL,
    department_id   INTEGER REFERENCES departments(id),
    
    -- Описание
    description     TEXT,               -- Полное описание
    requirements    TEXT,               -- Требования
    conditions      TEXT,               -- Условия работы
    
    -- Параметры
    city            VARCHAR(100),
    salary_min      INTEGER,
    salary_max      INTEGER,
    experience_min  INTEGER,            -- Мин. опыт (месяцы)
    work_format     VARCHAR(50),        -- office, remote, hybrid
    employment_type VARCHAR(50),        -- full, part, project
    
    -- Управление
    status          VARCHAR(20) DEFAULT 'draft',  -- draft, open, paused, closed, archived
    is_public       BOOLEAN DEFAULT TRUE,         -- Открытая / закрытая вакансия
    is_urgent       BOOLEAN DEFAULT FALSE,        -- Оперативная (влияет на сценарий контакта)
    priority        SMALLINT DEFAULT 2,           -- 1=low, 2=normal, 3=high
    
    -- AI
    scoring_prompt  TEXT,               -- Промпт для AI-скоринга кандидатов
    scoring_model   VARCHAR(50) DEFAULT 'gpt-4o-mini',  -- Модель для первичного скоринга
    deep_model      VARCHAR(50) DEFAULT 'gpt-5.2',      -- Модель для глубокого анализа
    
    -- Бриф заказчика
    brief           JSONB,              -- Структурированный бриф
    screening_questions TEXT[],          -- Базовые вопросы для скрининга
    has_test_task   BOOLEAN DEFAULT FALSE,
    test_task       TEXT,               -- Описание тестового задания
    
    -- Метаданные
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    opened_at       TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    created_by      INTEGER REFERENCES users(id),  -- Инициатор
    responsible_hr  INTEGER REFERENCES users(id),   -- Ответственный HR
    
    -- Внешние
    hh_vacancy_id   VARCHAR(50),        -- ID вакансии на HH (если опубликована)
    fw_job_id       INTEGER             -- ID из FriendWork (миграция)
);
```

### 3.5 VacancyStage (Этапы воронки)

Настраиваемые этапы для каждой вакансии (по модели Lever).

```sql
CREATE TABLE vacancy_stages (
    id              SERIAL PRIMARY KEY,
    vacancy_id      INTEGER REFERENCES vacancies(id) ON DELETE CASCADE,
    name            VARCHAR(100) NOT NULL,
    stage_type      VARCHAR(30) NOT NULL,  -- см. enum ниже
    sort_order      INTEGER NOT NULL,
    is_active       BOOLEAN DEFAULT TRUE,
    
    -- SLA
    sla_hours       INTEGER,            -- Макс. время на этапе (часы)
    
    -- Автоматизация
    auto_action     JSONB,              -- Авто-действие при входе на этап
    scorecard_template_id INTEGER REFERENCES scorecard_templates(id)
);

-- Типы этапов (из бизнес-процессов):
-- new              Новый (автоматически при добавлении)
-- ai_screening     Просмотрен ИИ / Одобрен ИИ / Отказ ИИ
-- messenger        Мессенджер (первичный контакт)
-- call             Звонок
-- waiting          Ждём ответа
-- phone_interview  Телефонное интервью
-- hr_review        Просмотрен заказчиком / Одобрен / Не одобрен
-- hr_interview     Собеседование с рекрутером
-- client_interview Собеседование с заказчиком
-- test_task        Тестовое задание
-- security_check   Проверка СБ
-- offer            Отправка оффера
-- onboarding       Выход на работу
-- probation        Испытательный срок
-- hired            Сотрудник
-- reserve          Кадровый резерв
-- rejected_company Отказ компании
-- rejected_candidate Отказ кандидата
-- no_response      Нет ответа
-- risk             Имеются риски
```

### 3.6 Application (Привязка кандидата к вакансии)

```sql
CREATE TABLE applications (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER REFERENCES candidates(id),
    vacancy_id      INTEGER REFERENCES vacancies(id),
    current_stage_id INTEGER REFERENCES vacancy_stages(id),
    
    -- Статус
    status          VARCHAR(20) DEFAULT 'active',  -- active, hired, rejected, withdrawn, reserve
    rejection_reason TEXT,
    rejection_side  VARCHAR(20),        -- company, candidate
    
    -- AI-оценка
    primary_score   REAL,               -- 0-1, первичный скоринг
    primary_fit     VARCHAR(20),        -- target, near_target, not_fit
    deep_score      REAL,               -- 0-10, глубокий скоринг
    ai_comment      TEXT,               -- AI-обоснование
    
    -- Даты
    applied_at      TIMESTAMPTZ DEFAULT NOW(),
    stage_entered_at TIMESTAMPTZ DEFAULT NOW(),
    closed_at       TIMESTAMPTZ,
    
    -- SLA
    sla_deadline    TIMESTAMPTZ,        -- Дедлайн на текущем этапе
    is_overdue      BOOLEAN DEFAULT FALSE,
    
    UNIQUE(candidate_id, vacancy_id)
);
```

### 3.7 Activity (Лента событий)

Полный лог всех действий по кандидату — аналог "ленты событий" FriendWork.

```sql
CREATE TABLE activities (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER REFERENCES candidates(id),
    vacancy_id      INTEGER REFERENCES vacancies(id),  -- NULL = общее действие
    application_id  INTEGER REFERENCES applications(id),
    
    -- Что произошло
    activity_type   VARCHAR(30) NOT NULL,
    -- types: status_change, comment, call, message, email, 
    --        interview_scheduled, interview_completed, 
    --        scorecard_submitted, ai_scoring, file_uploaded,
    --        offer_sent, offer_accepted, offer_rejected,
    --        task_created, task_completed
    
    -- Детали
    description     TEXT,
    metadata        JSONB,              -- Доп. данные (старый/новый статус, результат звонка, etc.)
    
    -- Кто сделал
    user_id         INTEGER REFERENCES users(id),  -- NULL = AI-агент
    is_ai_action    BOOLEAN DEFAULT FALSE,
    
    -- Когда
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    
    -- Миграция
    fw_history_id   INTEGER             -- candidateHistoryId из FriendWork
);

CREATE INDEX idx_activities_candidate ON activities(candidate_id, created_at DESC);
CREATE INDEX idx_activities_vacancy ON activities(vacancy_id, created_at DESC);
```

### 3.8 Scorecard (Структурированная оценка)

По модели Lever — каждый участник оценивает кандидата по заранее определённым критериям.

```sql
-- Шаблоны скоркарт (привязываются к этапу воронки)
CREATE TABLE scorecard_templates (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(200),
    criteria        JSONB NOT NULL      -- [{name, description, weight, scale}]
    -- scale: 1-5 или yes/no
);

-- Заполненные скоркарты
CREATE TABLE scorecards (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER REFERENCES applications(id),
    stage_id        INTEGER REFERENCES vacancy_stages(id),
    template_id     INTEGER REFERENCES scorecard_templates(id),
    
    -- Кто оценивал
    evaluator_id    INTEGER REFERENCES users(id),
    
    -- Оценки
    scores          JSONB NOT NULL,     -- [{criterion_name, score, comment}]
    overall_rating  SMALLINT,           -- 1-5 общая оценка
    recommendation  VARCHAR(20),        -- strong_yes, yes, neutral, no, strong_no
    comment         TEXT,               -- Общий комментарий
    
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    
    UNIQUE(application_id, stage_id, evaluator_id)
);
```

### 3.9 Interview (Собеседование / Интервью)

```sql
CREATE TABLE interviews (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER REFERENCES applications(id),
    stage_id        INTEGER REFERENCES vacancy_stages(id),
    
    -- Параметры
    interview_type  VARCHAR(30),        -- phone, video, in_person
    scheduled_at    TIMESTAMPTZ,
    duration_minutes INTEGER DEFAULT 60,
    location        TEXT,               -- Адрес / ссылка на Zoom/Meet
    
    -- Участники
    interviewers    INTEGER[],          -- user IDs
    
    -- Результат
    status          VARCHAR(20) DEFAULT 'scheduled',  -- scheduled, completed, cancelled, no_show
    
    -- AI-анализ (видеоинтервью)
    video_url       TEXT,
    transcript      TEXT,
    ai_analysis     JSONB,              -- Структурированный анализ из AssemblyAI + Claude
    ai_summary      TEXT,
    
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### 3.10 Task (Задачи для HR)

AI-агент создаёт задачи, HR выполняет.

```sql
CREATE TABLE tasks (
    id              SERIAL PRIMARY KEY,
    
    -- Привязка
    candidate_id    INTEGER REFERENCES candidates(id),
    vacancy_id      INTEGER REFERENCES vacancies(id),
    application_id  INTEGER REFERENCES applications(id),
    
    -- Задача
    task_type       VARCHAR(30) NOT NULL,
    -- types: call, message, schedule_interview, send_offer,
    --        review_resume, send_test_task, follow_up, custom
    
    title           VARCHAR(300) NOT NULL,
    description     TEXT,
    
    -- Исполнитель
    assigned_to     INTEGER REFERENCES users(id),
    
    -- Сроки
    due_at          TIMESTAMPTZ,
    priority        SMALLINT DEFAULT 2,  -- 1=low, 2=normal, 3=high, 4=urgent
    
    -- Статус
    status          VARCHAR(20) DEFAULT 'pending',  -- pending, in_progress, done, skipped
    completed_at    TIMESTAMPTZ,
    result          TEXT,               -- Результат выполнения
    
    -- Кто создал
    created_by_ai   BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_tasks_assigned ON tasks(assigned_to, status, due_at);
```

### 3.11 Communication (Коммуникации)

Лог всех коммуникаций: звонки, сообщения, email.

```sql
CREATE TABLE communications (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER REFERENCES candidates(id),
    application_id  INTEGER REFERENCES applications(id),
    
    channel         VARCHAR(20) NOT NULL,  -- phone, telegram, whatsapp, email, sms
    direction       VARCHAR(10) NOT NULL,  -- outbound, inbound
    
    -- Содержимое
    subject         VARCHAR(300),       -- Тема (для email)
    body            TEXT,               -- Текст сообщения / заметки о звонке
    
    -- Результат (для звонков)
    call_result     VARCHAR(20),        -- answered, no_answer, busy, voicemail
    call_duration   INTEGER,            -- Секунды
    
    -- Файлы
    attachments     JSONB,              -- [{filename, url, size}]
    
    -- Метаданные
    sent_by         INTEGER REFERENCES users(id),
    sent_at         TIMESTAMPTZ DEFAULT NOW(),
    
    -- Внешние ID
    telegram_msg_id BIGINT,
    email_message_id VARCHAR(200)
);
```

### 3.12 Users & Departments

```sql
CREATE TABLE departments (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    head_id         INTEGER REFERENCES users(id)
);

CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    email           VARCHAR(200) UNIQUE NOT NULL,
    password_hash   VARCHAR(200),
    first_name      VARCHAR(100),
    last_name       VARCHAR(100),
    role            VARCHAR(20) NOT NULL,  -- admin, hr, manager, viewer
    department_id   INTEGER REFERENCES departments(id),
    telegram_id     BIGINT,             -- Для нотификаций
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    
    -- Внешние
    fw_account_id   INTEGER             -- ID из FriendWork (миграция)
);
```

### 3.13 TalentPool (Кадровый резерв / Talent Mapping)

```sql
CREATE TABLE talent_pool (
    id              SERIAL PRIMARY KEY,
    candidate_id    INTEGER REFERENCES candidates(id),
    
    -- Категория
    pool_type       VARCHAR(30),        -- reserve, passive, competitor, alumni
    target_roles    TEXT[],             -- Подходящие роли
    notes           TEXT,
    
    -- Автообновление
    last_hh_check   TIMESTAMPTZ,        -- Когда последний раз проверяли обновление резюме на HH
    hh_updated_at   TIMESTAMPTZ,        -- Когда резюме обновлялось на HH
    
    -- AI
    ai_readiness    REAL,               -- 0-1, AI-оценка готовности к переходу
    ai_match_roles  JSONB,              -- [{vacancy_title, match_score}]
    
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 4. Воронка и статусы

### 4.1 Дефолтная воронка (из бизнес-процессов Ars Magna)

```
 1. Новый
 2. Просмотрен ИИ → Одобрен ИИ / Отказ ИИ
 3. Первичный контакт (Мессенджер / Звонок)
 4. Ждём ответа → Нет ответа [выбыл]
 5. Телефонное интервью
 6. Просмотрен заказчиком → Одобрен / Не одобрен
 7. Собеседование с рекрутером
 8. Тестовое задание (опционально)
 9. Собеседование с заказчиком
10. Проверка СБ → Имеются риски [выбыл]
11. Отправка оффера → Отказ кандидата [выбыл] / Отказ компании [выбыл]
12. Выход на работу
13. Испытательный срок
14. Сотрудник [нанят]
```

**Боковые статусы (не в основном потоке):**
- Кадровый резерв — кандидат хороший, но нет вакансии
- Не состоялось — собеседование/этап отменён

### 4.2 Маппинг статусов FriendWork → HRM

| FW Status ID | FW Name | HRM Stage Type |
|-------------|---------|----------------|
| 188586 | Новый | `new` |
| 188587 | Скрининг | `ai_screening` |
| 214980 | Просмотрен ИИ | `ai_screening` |
| 214981 | Одобрен ИИ | `ai_screening` |
| 214982 | Отказ ИИ | `ai_screening` |
| 188588 | Телефонное интервью | `phone_interview` |
| 250560 | Первичный контакт | `messenger` |
| 253073 | Звонок | `call` |
| — | Мессенджер | `messenger` |
| — | Ждём ответа | `waiting` |
| — | Нет ответа | `no_response` |
| — | Одобрен | `hr_review` |
| — | Не одобрен | `hr_review` |
| — | Просмотрен Заказчиком | `hr_review` |
| — | Собеседование с рекрутером | `hr_interview` |
| — | Собеседование с Заказчиком | `client_interview` |
| — | Тестовое задание | `test_task` |
| — | Проверка СБ | `security_check` |
| — | Отправка оффера | `offer` |
| — | Выход на работу | `onboarding` |
| — | Оформление в штат | `onboarding` |
| — | Адаптация | `probation` |
| — | Испытательный срок | `probation` |
| — | Сотрудник | `hired` |
| — | Резерв | `reserve` |
| — | Отказ компании | `rejected_company` |
| — | Отказ кандидата | `rejected_candidate` |
| — | Имеются риски | `risk` |

### 4.3 Правила переходов

Воронка **не жёсткая** — можно перескочить этапы (например, руководитель сам нашёл кандидата → сразу на собеседование). Но система предупреждает о пропущенных этапах и логирует каждый переход.

**Автоматические переходы:**
- Кандидат добавлен → `new`
- AI-скоринг завершён → `ai_screening` (Одобрен/Отказ ИИ)
- SLA истёк на этапе "Ждём ответа" → уведомление HR
- Оффер принят → `onboarding`
- Испытательный срок завершён → `hired`

**Возвратные потоки (из БП):**
- Отказ на любом этапе → возврат вакансии в поиск (не кандидата)
- Кандидат из "Кадрового резерва" может быть повторно привязан к новой вакансии

---

## 5. AI-агент

### 5.1 Роли агента

| Функция | Триггер | Модель | Описание |
|---------|---------|--------|----------|
| **Auto-search** | По расписанию / по запросу | — | Поиск резюме на HH API по параметрам вакансии |
| **Primary scoring** | Новый кандидат | GPT-4o-mini | Быстрая оценка релевантности (target/near/not_fit) |
| **Deep scoring** | Score > порога | GPT-5.2 / Opus | Глубокий анализ: баллы 0-10, обоснование, риски |
| **Resume enrichment** | Кандидат добавлен | GPT-4o-mini | Извлечение навыков, тегов, саммари из резюме |
| **Psycho-profiling** | Видеоинтервью + резюме | Claude Opus | Психопортрет: soft skills, мотивация, red flags |
| **Task routing** | Кандидат одобрен | — | Создание задач HR (кому позвонить, когда, о чём) |
| **Talent mapping** | По расписанию | GPT-4o-mini | Мониторинг обновлений резюме в кадровом резерве |
| **Base re-scan** | Новая вакансия | GPT-4o-mini | Пересмотр базы кандидатов с отказами/резервом |

### 5.2 Pipeline скоринга

```
Кандидат найден (HH API / отклик / ручное добавление)
    │
    ▼
[Дедупликация] — проверка по ФИО + телефону + HH-ссылке
    │
    ▼
[Primary Scoring] — GPT-4o-mini, batch по 15
    │ fit_type: target / near_target / not_fit
    │ confidence: 0.0 - 1.0
    │
    ├── not_fit (confidence > 0.8) → Отказ ИИ + комментарий
    │
    ├── near_target → Просмотрен ИИ (ждёт ручного решения)
    │
    └── target → Одобрен ИИ
                    │
                    ▼
              [Deep Scoring] — GPT-5.2/Opus, индивидуально
                    │ score: 0-10
                    │ detailed_analysis: strengths, risks, fit_rationale
                    │
                    ▼
              [Task Creation] → HR получает задачу "Позвонить кандидату X"
```

### 5.3 Talent Mapping (кадровый резерв)

```
[По расписанию: 1 раз в неделю]
    │
    ▼
[Проверка обновлений резюме на HH] — по sourceLink кандидатов в резерве
    │
    ├── Резюме обновлено → пересмотр AI-оценки
    │                       → если есть подходящая вакансия → уведомление HR
    │
    └── Резюме не обновлено → skip
    
[При открытии новой вакансии]
    │
    ▼
[Пересмотр базы] — AI сканирует кандидатов в статусах:
    │ - кадровый резерв
    │ - отказ кандидата (>3 мес назад)
    │ - нет ответа (>1 мес назад)
    │
    ▼
[Рекомендации] → HR видит список "Кандидаты из базы для этой вакансии"
```

---

## 6. Интерфейс

### 6.1 Навигация

```
┌─────────────────────────────────────────────────────┐
│  HRM Ars Magna          [Задачи 5] [🔔 3]  [User] │
├──────────┬──────────────────────────────────────────┤
│          │                                          │
│ Дашборд  │   [Основная рабочая область]             │
│ Вакансии │                                          │
│ Кандидаты│                                          │
│ Задачи   │                                          │
│ Интервью │                                          │
│ Аналитика│                                          │
│ Настройки│                                          │
│          │                                          │
└──────────┴──────────────────────────────────────────┘
```

### 6.2 Экраны

#### Дашборд (HR)
- Мои задачи на сегодня (звонки, follow-up, интервью)
- Просроченные задачи (SLA)
- Активные вакансии с количеством кандидатов на каждом этапе
- Последние действия

#### Дашборд (Руководитель)
- Мои вакансии и их статус
- Кандидаты на просмотр (ждут одобрения)
- Запланированные собеседования
- Статистика закрытия

#### Вакансия — Kanban-доска
```
| Новый(12) | ИИ(5) | Контакт(3) | Интервью(2) | Собес(1) | Оффер(1) |
|  [card]   | [card] |  [card]    |   [card]    |  [card]  |  [card]  |
|  [card]   | [card] |  [card]    |   [card]    |          |          |
|  [card]   |        |            |             |          |          |
```
- Drag & drop между этапами
- Фильтры: по HR, по score, по дате
- Bulk actions: переместить, отклонить, назначить HR

#### Карточка кандидата
- Профиль: фото, контакты, позиция, зарплата
- Вакансии: список привязанных вакансий с текущим этапом
- Лента событий: все действия, статусы, комментарии, AI-оценки (аналог FW)
- Резюме: полный опыт, образование, курсы, навыки
- Скоркарты: оценки от всех участников
- Файлы: резюме PDF, видео, тестовые задания
- AI: психопортрет, саммари, теги, рекомендации
- Коммуникации: история звонков, сообщений, email

#### Задачи (HR)
- Список задач с фильтрами: сегодня / просрочено / все
- Группировка по кандидату или по вакансии
- Quick actions: позвонил (результат), написал, перенёс

#### Аналитика
- Воронка по вакансии (конверсия между этапами)
- Time-to-hire по вакансиям
- SLA: % просрочек по HR
- Источники: откуда лучшие кандидаты
- AI-метрики: точность скоринга vs финальный результат

---

## 7. Интеграции

### 7.1 HeadHunter API
| Функция | Реализация | Статус |
|---------|-----------|--------|
| Поиск резюме | HH API (OAuth) | Уже работает в HR Radar |
| Парсинг резюме | HH API + scraping | Уже работает |
| Обновление резюме | Периодическая проверка sourceLink | Новое |
| Публикация вакансий | HH Employer API | Будущее |

### 7.2 Telegram
| Функция | Реализация | Статус |
|---------|-----------|--------|
| Уведомления HR | Telegram Bot API | Уже работает |
| Отчёты по скорингу | Telegram Bot API | Уже работает |
| Команды бота (/radar) | Telegram Bot API | Уже работает |
| Уведомления руководителям | Новый функционал | Новое |

### 7.3 WhatsApp (фаза 2)
- Отправка сообщений кандидатам
- Шаблоны сообщений
- Логирование переписки в ленту событий

### 7.4 Email (фаза 2)
- Отправка офферов
- Шаблоны писем
- Подтягивание откликов из почты (IMAP)

### 7.5 Календарь (фаза 2)
- Создание встреч для собеседований
- Синхронизация с Google Calendar

---

## 8. SLA и контроль HR

### 8.1 Метрики SLA

| Метрика | Порог | Действие при превышении |
|---------|-------|------------------------|
| Время реакции на нового кандидата | 4 часа | Уведомление HR + руководителю |
| Время на этапе "Ждём ответа" | 24 часа | Автозадача: повторный контакт |
| Время на этапе "Телефонное интервью" | 48 часов | Уведомление |
| Просмотр заказчиком | до 12:00 ежедневно | Напоминание заказчику |
| Обратная связь после собеседования | 24 часа | Уведомление |
| Закрытие вакансии (общее) | 30 дней | Эскалация |

### 8.2 Дашборд SLA для руководителя

```
HR Елена Перева:
  ✅ Задач выполнено сегодня: 12
  ⚠️ Просрочено: 2 (кандидат X — звонок, кандидат Y — follow-up)
  📊 SLA за неделю: 94%
  📊 SLA за месяц: 91%
```

---

## 9. Миграция из FriendWork

### 9.1 Данные для миграции

| Данные | Источник | Объём | Статус |
|--------|----------|-------|--------|
| Кандидаты (профили) | FW API export | 2,461 | Выгружено |
| Вакансии | FW API export | 67 | Выгружено |
| История событий | FW API export | ~15,000+ | Выгружается |
| Аккаунты (пользователи) | FW API export | 17 | Выгружено |
| Статусы (словарь) | FW API export | 70 (47 с именами) | Выгружено |
| Резюме (опыт, образование) | HH API по sourceLink | ~2,000 | Нужно дособрать |

### 9.2 План миграции

1. **Создать схему PostgreSQL** — все таблицы
2. **Импорт вакансий** — jobs.json → vacancies + vacancy_stages (дефолтная воронка)
3. **Импорт пользователей** — accounts.json → users
4. **Импорт кандидатов** — candidates.json → candidates + candidate_additional
5. **Импорт истории** — candidate_histories.json → activities + applications
6. **Маппинг статусов** — status_map.json → vacancy_stages + текущий этап applications
7. **Обогащение резюме** — через HH API по sourceLink → candidate_experience, education, etc.
8. **Валидация** — проверка полноты данных, дубликатов, битых связей

### 9.3 Что не мигрируется
- Файлы/документы из FW (нет доступа через API)
- Фото кандидатов (есть photoId, но нет эндпоинта скачивания)
- Настройки FW (воронки, права) — создаём заново

---

## 10. Фазы реализации

### Фаза 1: MVP (2-3 недели)
**Цель:** Заменить FriendWork для ежедневной работы HR

- [ ] Схема БД (PostgreSQL)
- [ ] Миграция данных из FW
- [ ] Карточка кандидата (профиль + лента событий + вакансии)
- [ ] Список кандидатов с поиском и фильтрами
- [ ] Вакансии: CRUD + Kanban-доска
- [ ] Смена статусов (drag & drop + кнопки)
- [ ] Комментарии и заметки
- [ ] Базовая авторизация (ключ → роль)
- [ ] Интеграция существующего AI-скоринга

### Фаза 2: Полноценный ATS (3-4 недели)
**Цель:** Закрыть весь цикл подбора

- [ ] Скоркарты (шаблоны + заполнение)
- [ ] Задачи для HR (создание AI-агентом)
- [ ] SLA-мониторинг
- [ ] Интервью-модуль (видео → расшифровка → AI-анализ)
- [ ] Email-интеграция (отправка + IMAP-импорт)
- [ ] Telegram-уведомления для руководителей
- [ ] Аналитика: воронка, time-to-hire, источники

### Фаза 3: AI-агент + Talent CRM (4-6 недель)
**Цель:** AI управляет процессом, HR выполняет

- [ ] AI task routing (агент ставит задачи HR)
- [ ] Talent mapping (мониторинг кадрового резерва)
- [ ] Auto-search по новым вакансиям
- [ ] Пересмотр базы при открытии вакансии
- [ ] WhatsApp-интеграция
- [ ] Календарь (Google Calendar)
- [ ] Психопортреты (комплексный AI-анализ)

### Фаза 4: Масштабирование (ongoing)
- [ ] Мультитенантность (для продажи другим агентствам)
- [ ] Публичный API
- [ ] Мобильная версия
- [ ] Интеграция с другими job-сайтами (SuperJob, LinkedIn)

---

## 11. Что делаем / Что НЕ делаем

### Делаем
- Единую базу кандидатов с полным жизненным циклом
- AI-скоринг и глубокий анализ резюме
- Kanban-воронку по модели Lever
- SLA-контроль для HR
- Ленту событий (как в FW, но лучше)
- Миграцию данных из FW
- Интеграцию с HH, Telegram
- Задачи от AI-агента к HR

### НЕ делаем (сейчас)
- Мобильное приложение
- CRM для клиентов агентства (это HR-система, не клиентская CRM)
- Бухгалтерию / зарплаты / кадровый учёт (1С и аналоги)
- Интеграцию с LinkedIn (нет API для РФ)
- Чат с кандидатами в реальном времени
- Видеозвонки внутри системы (используем внешние: Zoom, Meet)

---

## 12. Критерии готовности (DoD)

### Фаза 1 считается готовой когда:
1. Все 2,461 кандидата из FW доступны в новой системе с полной историей
2. HR может вести кандидата по воронке без FriendWork
3. Руководитель может просматривать и одобрять кандидатов
4. AI-скоринг работает через новую систему (не через FW API)
5. Лента событий показывает всю историю (включая мигрированную из FW)

### Общие критерии провала:
- HR тратит больше времени на новую систему, чем на FW
- Потеря данных при миграции
- AI-агент ставит нерелевантные задачи (>30% ложных)
- Система падает чаще 1 раза в неделю

---

## 13. Технические риски

| Риск | Вероятность | Влияние | Митигация |
|------|-------------|---------|-----------|
| Потеря данных при миграции | Низкая | Высокое | Бэкап FW export, валидация после импорта |
| HH API блокировка | Средняя | Среднее | Rate limiting, ротация токенов, IMAP fallback |
| Рост AI-затрат | Средняя | Среднее | Двухуровневый скоринг, batch-обработка |
| PostgreSQL производительность | Низкая | Низкое | При 3K кандидатов — не проблема |
| Пользователи не перейдут с FW | Средняя | Высокое | MVP должен быть не хуже FW по UX |

---

*Спецификация подготовлена на основе: grill me сессии, 7 BPMN бизнес-процессов, 312 статей документации FW, экспорта данных FW (2,461 кандидат, 67 вакансий, 70 статусов), модели Lever/Greenhouse.*

---
slug: multi-video-interview
prd: docs/features/multi-video-interview.md
status: reviewed
generated: 2026-06-16
committed_ids: {}
---

# Задачи: Мульти-видео интервью

> Документ собран скиллом `prd-to-beads` из [PRD](./multi-video-interview.md).
> Каждая задача — **вертикальный срез** (UI + Domain) по User Story.
>
> **Как ревьюить:**
> 1. Прочитай каждую T-N. Проверь что описание классов/функций совпадает с реальной структурой кода.
> 2. Поправь Gherkin-сценарии и edge cases по своему вкусу.
> 3. Когда готов — поменяй `status: draft` на `status: reviewed` и попроси «залить задачи в beads».

---

## T-1. UI: список URL с добавлением и удалением частей

**User Story:** US-1 + US-2 — Как HR-менеджер, я хочу вставить несколько ссылок на части интервью и управлять списком, чтобы не склеивать видео вручную.

**Связанные FR:** FR-1, FR-2, FR-9

**Приоритет:** P1

### Затрагиваемые классы и функции

**UI:**
- `[EDIT] web_panel/templates/interview.html::Alpine data.videoUrls` — заменить `videoUrl: ''` на `videoUrls: ['']` (массив строк)
- `[NEW] web_panel/templates/interview.html::Alpine data.addVideoUrl` — метод, добавляет пустую строку в `videoUrls`
- `[NEW] web_panel/templates/interview.html::Alpine data.removeVideoUrl(i)` — метод, удаляет элемент по индексу (только если `videoUrls.length > 1`)
- `[EDIT] web_panel/templates/interview.html::Alpine data.startAnalysis` — при сборке FormData: вместо `video_url` добавлять все непустые `videoUrls` как повторяющиеся поля `video_urls`
- `[EDIT] web_panel/templates/interview.html::video-url-input block` — заменить одиночный `<input>` на `x-for` список с кнопками «+ Добавить часть» и «×»

### Контракты на границах слоёв

```javascript
// FormData при отправке:
// video_urls = videoUrls.filter(u => u.trim()) // только непустые
// Повторяющиеся поля: formData.append('video_urls', url) для каждого
```

### Acceptance — Happy Path (Gherkin)

```gherkin
Сценарий: HR-менеджер добавляет две ссылки на части интервью
  Дано пользователь открыл форму анализа интервью
    И выбран режим «Видео»
  Когда пользователь вводит первую ссылку в поле URL
    И нажимает «+ Добавить часть»
    И вводит вторую ссылку в новое поле
    И нажимает «Анализировать интервью»
  Тогда форма отправляет два поля video_urls в запросе
    И кнопка «×» не отображается рядом с последним оставшимся полем
```

### Edge cases

- Если все поля URL пустые → кнопка «Анализировать» заблокирована (disabled)
- Одно поле → кнопка «×» скрыта (нельзя удалить единственное поле)
- Пробелы в URL → фильтруются перед отправкой (trim + filter)
- Добавление 5+ полей → UI не ломается, скролл
- При переключении режима (video → audio → video) → `videoUrls` сбрасывается в `['']`

### Ссылки на PRD

- US: [§4 User Stories](./multi-video-interview.md#4-user-stories)
- FR: [§5 Функциональные требования](./multi-video-interview.md#5-функциональные-требования)
- Tech: [§6.5](./multi-video-interview.md#65-затрагиваемые-классы-и-функции)

### Реализация

Выполняется через `/senior-developer` (TDD-цикл, conventional commits).

---

## T-2. Backend: скачка нескольких URL, склейка аудио, передача в пайплайн

**User Story:** US-3 — Как HR-менеджер, я хочу получить один PDF-отчёт на всё интервью, чтобы анализ отражал полную картину.

**Связанные FR:** FR-3, FR-4, FR-5, FR-6, FR-7, FR-8, FR-9

**Приоритет:** P1

**Зависит от:** T-1

### Затрагиваемые классы и функции

**Domain:**
- `[EDIT] web_panel/app.py::api_interview_analyze` — заменить `video_url: str = Form("")` на `video_urls: List[str] = Form([])`, обновить валидацию (хотя бы один непустой URL в video-режиме)
- `[NEW] web_panel/app.py::_merge_audio_parts(audio_paths: list[str], output_path: str) -> str` — ffmpeg concat нескольких аудио-файлов в один
- `[EDIT] web_panel/app.py::_run_interview_pipeline` — добавить параметр `video_urls: list[str] = []`, цикл скачки каждого URL, вызов `_merge_audio_parts` если частей > 1, иначе использовать напрямую

### Контракты на границах слоёв

```python
def _merge_audio_parts(audio_paths: list[str], output_path: str) -> str:
    """
    Принимает список путей к аудио-файлам (mp3/m4a/wav).
    Склеивает через ffmpeg concat demuxer в output_path.
    Возвращает output_path.
    Инварианты:
    - audio_paths непустой (len >= 1)
    - если len == 1: копирует файл в output_path без запуска ffmpeg
    - при ошибке ffmpeg: raise RuntimeError с stderr
    - временный filelist.txt удаляется после завершения
    - при разных кодеках: fallback на -c:a libmp3lame (ре-энкодинг)
    """

# api_interview_analyze:
# video_urls: List[str] = Form([])  — повторяющиеся поля из формы
# валидация: if not any(u.strip() for u in video_urls) → 400
```

### Acceptance — Happy Path (Gherkin)

```gherkin
Сценарий: Система обрабатывает два URL и возвращает один анализ
  Дано форма отправила два поля video_urls с валидными ссылками
    И выбрана вакансия и модель анализа
  Когда сервер получает запрос POST /api/interview/analyze
  Тогда скачиваются оба видео последовательно
    И из каждого извлекается аудио
    И аудио-части склеиваются в merged_audio.mp3
    И транскрипция запускается на merged_audio.mp3
    И возвращается task_id для отслеживания прогресса
    И финальный результат содержит полный анализ обеих частей
```

### Edge cases

- Один URL → поведение идентично текущему (обратная совместимость), `_merge_audio_parts` просто копирует
- Второй URL недоступен (404/timeout) → задача завершается ошибкой с сообщением «Не удалось скачать: <url>»
- Части в разных кодеках → ffmpeg делает ре-энкодинг (`-c:a libmp3lame`) вместо `-c copy`
- Суммарный размер > 1 GB → скачка займёт время, прогресс-стадия «скачивание» уже есть в UI
- `video_urls` пустой список или все строки пустые → 400 «Нужна ссылка на видео»
- Старые запросы с `video_url` (одиночная строка) → обратная совместимость через fallback

### Ссылки на PRD

- US: [§4 User Stories](./multi-video-interview.md#4-user-stories)
- FR: [§5 Функциональные требования](./multi-video-interview.md#5-функциональные-требования)
- Tech: [§6.5](./multi-video-interview.md#65-затрагиваемые-классы-и-функции)
- API: [§6.2](./multi-video-interview.md#62-api)
- Риски: [§9](./multi-video-interview.md#9-риски-и-ограничения)

### Реализация

Выполняется через `/senior-developer` (TDD-цикл, conventional commits).

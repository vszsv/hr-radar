# Управление HR Radar через Telegram

## Команда
- `/radar` — открыть меню управления

## Что можно переключать
- Профиль целиком (Event/BTL)
- Отправку отчётов по профилю
- Отдельные вакансии в профиле

Состояния:
- ✅ включено
- ❌ выключено
- 📣 отчёт включен
- 🔕 отчёт выключен

## Где хранится
- `data/radar_controls.json`

Пример структуры:
```json
{
  "profiles": {
    "event_agencies": {
      "enabled": true,
      "report_enabled": true,
      "jobs": {
        "account_director": true,
        "account_manager": true,
        "project_manager": true
      }
    }
  }
}
```

## Принцип работы
1. `bot_control.py` принимает нажатие кнопки
2. Обновляет `radar_controls.json`
3. `run_multi_radar.py` при запуске читает этот файл и применяет флаги

То есть поведение системы управляется без правок кода.

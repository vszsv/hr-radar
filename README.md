# HR Radar

Сервис автоматического скрининга кандидатов из HH-автопоиска (email) с LLM-оценкой и отправкой отчётов в Telegram.

## Что делает
- Читает письма HH из IMAP-ящиков
- Парсит ссылки на резюме
- Оценивает релевантность кандидатов по ролям (GPT)
- Отправляет отчёты и списки кандидатов в Telegram
- Поддерживает несколько профилей (например, Event и BTL)

## Ключевые файлы
- `run_multi_radar.py` — основной пайплайн
- `bot_control.py` — Telegram-меню `/radar` с кнопками вкл/выкл
- `config/profiles.yaml` — профили, роли, расписания, общие настройки
- `data/radar_controls.json` — текущие переключатели из Telegram-меню
- `config/prompts/*.txt` — промпты по ролям
- `data/*.db` — базы запусков и seen links

## Быстрый старт
```bash
cd /opt/hr-radar
source venv/bin/activate
set -a; source .env; set +a
python run_multi_radar.py              # все профили
python run_multi_radar.py btl_agencies # только BTL
```

## Управление через Telegram
В чате бота отправь:
- `/radar` — открыть меню управления

Доступно:
- Включать/выключать профиль
- Включать/выключать отчёт по профилю
- Включать/выключать отдельные вакансии

## Systemd
- `hr-radar-multi.timer` → `hr-radar-multi.service` (ежедневный запуск пайплайна)
- `hr-radar-control.service` (бот управления кнопками)
- `hr-radar-hh-session.timer` (обновление HH-сессии)

## Где смотреть логи
```bash
journalctl -u hr-radar-multi.service -n 200 --no-pager
journalctl -u hr-radar-control.service -n 200 --no-pager
```

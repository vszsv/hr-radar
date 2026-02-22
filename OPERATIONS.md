# OPERATIONS

## Повседневные команды

### Ручной запуск
```bash
cd /opt/hr-radar
source venv/bin/activate
set -a; source .env; set +a
python run_multi_radar.py
```

### Только один профиль
```bash
python run_multi_radar.py event_agencies
python run_multi_radar.py btl_agencies
```

### Проверить сервисы
```bash
systemctl status hr-radar-multi.timer --no-pager
systemctl status hr-radar-multi.service --no-pager
systemctl status hr-radar-control.service --no-pager
```

### Логи
```bash
journalctl -u hr-radar-multi.service -f
journalctl -u hr-radar-control.service -f
```

## Типовые проблемы

### Нет новых кандидатов
- Проверь IMAP-доступ в `.env`
- Проверь, что письма HH пришли за текущий день
- Проверь дедуп (возможно уже в `seen_links`)

### Нет отчёта в Telegram
- Проверь `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Проверь `report_enabled` и тумблеры вакансий через `/radar`

### Медленно идёт скоринг
- Уменьши `batch_size` в `profiles.yaml`
- Смени модель в `common.openai.model`

## Безопасность
- `.env` не коммитить
- токены и пароли держать только в env
- доступ к управлению ботом ограничен `TELEGRAM_CHAT_ID`

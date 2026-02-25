#!/bin/bash
# Service healthcheck — alerts to Telegram if something is down
source /opt/hr-radar/.env

SERVICES=("hr-radar-control" "hr-radar-multi.timer" "hr-radar-hh-session.timer")
FAILED=()

for svc in "${SERVICES[@]}"; do
    if ! systemctl is-active --quiet "$svc"; then
        FAILED+=("$svc")
    fi
done

if [ ${#FAILED[@]} -gt 0 ]; then
    MSG="⚠️ <b>HR Radar Healthcheck Alert</b>%0A%0AСервисы упали:%0A"
    for f in "${FAILED[@]}"; do
        MSG+="❌ $f%0A"
    done
    MSG+="%0AСервер: $(hostname), $(date '+%Y-%m-%d %H:%M UTC')"
    
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage?chat_id=${TELEGRAM_CHAT_ID}&text=${MSG}&parse_mode=HTML" > /dev/null
fi

# Disk space check (alert if <2GB free)
FREE_KB=$(df /opt/hr-radar --output=avail | tail -1 | tr -d ' ')
if [ "$FREE_KB" -lt 2097152 ]; then
    DISK_MSG="⚠️ <b>Disk Space Alert</b>%0A%0AОсталось менее 2GB свободного места!%0AСвободно: $((FREE_KB/1024))MB"
    curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage?chat_id=${TELEGRAM_CHAT_ID}&text=${DISK_MSG}&parse_mode=HTML" > /dev/null
fi

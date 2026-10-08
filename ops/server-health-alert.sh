#!/bin/sh
# Следит за местом на диске, оперативкой и swap. При выходе за пороги шлёт
#alert в админ-чат парсера ботом @fruntend_errors_bot.
#
# Токен и chat_id НЕ хранятся здесь: читаются из .env парсера, чтобы после
# ротации токена ничего не нужно было править в двух местах.
#
# Антиспам: пока состояние не изменилось, повторный алерт уходит не чаще
# раза в сутки. Возврат в норму тоже сообщается — один раз.

set -u

ENV_FILE=/var/www/python-parser/services/fe-articles/.env
STATE_FILE=/var/lib/server-health-alert.state
REPEAT_AFTER=86400          # повтор того же алерта не чаще раза в сутки

DISK_WARN=80                # % занятого /
MEM_AVAIL_MIN_MB=300        # сколько MiB доступной памяти считаем минимумом
SWAP_USED_MAX_PCT=75        # % занятого swap

HOST=$(hostname)

# ---------- измерения ----------
DISK_PCT=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
DISK_FREE=$(df -Ph / | awk 'NR==2 {print $4}')
MEM_AVAIL_MB=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)
MEM_TOTAL_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
SWAP_TOTAL_KB=$(awk '/SwapTotal/ {print $2}' /proc/meminfo)
SWAP_FREE_KB=$(awk '/SwapFree/ {print $2}' /proc/meminfo)
if [ "${SWAP_TOTAL_KB:-0}" -gt 0 ]; then
    SWAP_PCT=$(( (SWAP_TOTAL_KB - SWAP_FREE_KB) * 100 / SWAP_TOTAL_KB ))
else
    SWAP_PCT=0
fi

# ---------- проверка порогов ----------
PROBLEMS=""
[ "$DISK_PCT" -ge "$DISK_WARN" ] && \
    PROBLEMS="${PROBLEMS}• диск / занят на <b>${DISK_PCT}%</b> (свободно ${DISK_FREE})\n"
[ "$MEM_AVAIL_MB" -lt "$MEM_AVAIL_MIN_MB" ] && \
    PROBLEMS="${PROBLEMS}• доступной памяти <b>${MEM_AVAIL_MB} МБ</b> из ${MEM_TOTAL_MB} МБ\n"
[ "$SWAP_PCT" -ge "$SWAP_USED_MAX_PCT" ] && \
    PROBLEMS="${PROBLEMS}• swap занят на <b>${SWAP_PCT}%</b>\n"

if [ -n "$PROBLEMS" ]; then NOW_STATE="alert"; else NOW_STATE="ok"; fi

# ---------- антиспам ----------
PREV_STATE=ok
PREV_TS=0
if [ -r "$STATE_FILE" ]; then
    PREV_STATE=$(cut -d' ' -f1 "$STATE_FILE" 2>/dev/null || echo ok)
    PREV_TS=$(cut -d' ' -f2 "$STATE_FILE" 2>/dev/null || echo 0)
fi
NOW_TS=$(date +%s)

SEND=no
MSG=""
if [ "$NOW_STATE" = "alert" ]; then
    if [ "$PREV_STATE" != "alert" ] || [ $((NOW_TS - PREV_TS)) -ge "$REPEAT_AFTER" ]; then
        SEND=yes
        MSG="⚠️ <b>${HOST}: ресурсы на пределе</b>\n\n${PROBLEMS}\nТоп по месту в /var/lib/docker и /var/log — смотреть руками."
    fi
elif [ "$PREV_STATE" = "alert" ]; then
    SEND=yes
    MSG="✅ <b>${HOST}: ресурсы вернулись в норму</b>\n\nДиск ${DISK_PCT}% (свободно ${DISK_FREE}), память ${MEM_AVAIL_MB} МБ из ${MEM_TOTAL_MB} МБ, swap ${SWAP_PCT}%."
fi

# ---------- отправка ----------
if [ "$SEND" = "yes" ]; then
    TOKEN=$(grep -m1 '^TG_BOT_TOKEN_FOR_LOGS=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
    CHAT=$(grep -m1 '^TG_CHAT_ID_FOR_LOGS=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
    if [ -n "${TOKEN:-}" ] && [ -n "${CHAT:-}" ]; then
        printf '%s %s\n' "$(date -Is)" "отправка: $NOW_STATE" >&2
        curl -s --max-time 15 -o /dev/null \
            "https://api.telegram.org/bot${TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${CHAT}" \
            --data-urlencode "text=$(printf "%b" "$MSG")" \
            --data-urlencode "parse_mode=HTML" \
            --data-urlencode "disable_web_page_preview=true"
    else
        echo "$(date -Is) НЕ НАЙДЕНЫ TG-креды в $ENV_FILE" >&2
    fi
fi

# состояние обновляем только когда реально слали алерт либо состояние сменилось
if [ "$SEND" = "yes" ] || [ "$NOW_STATE" != "$PREV_STATE" ]; then
    if [ "$NOW_STATE" = "alert" ] && [ "$SEND" = "no" ]; then
        printf '%s %s\n' "$NOW_STATE" "$PREV_TS" > "$STATE_FILE"
    else
        printf '%s %s\n' "$NOW_STATE" "$NOW_TS" > "$STATE_FILE"
    fi
fi

printf '%s диск=%s%% свободно=%s память=%sМБ swap=%s%% состояние=%s\n' \
    "$(date -Is)" "$DISK_PCT" "$DISK_FREE" "$MEM_AVAIL_MB" "$SWAP_PCT" "$NOW_STATE"

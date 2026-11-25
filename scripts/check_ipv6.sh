#!/usr/bin/env bash
# Готов ли сервер принимать вебхуки Яндекс Форм по IPv6.
# Запускать НА СЕРВЕРЕ: bash scripts/check_ipv6.sh [порт]
# Код возврата: 0 — всё критичное на месте, 1 — есть блокирующая проблема.

set -u

PORT="${1:-8000}"
FAIL=0
WARN=0

green() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
red()   { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
warn()  { printf '  \033[33m!\033[0m %s\n' "$1"; WARN=$((WARN + 1)); }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

printf 'Проверка IPv6 для приёмника на порту %s\n' "$PORT"

# ---------------------------------------------------------------- адрес
head_ "1. Адрес"
ADDR=""
if command -v ip >/dev/null 2>&1; then
    ADDR=$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/{print $2; exit}')
fi

if [ -n "$ADDR" ]; then
    green "глобальный IPv6 есть: ${ADDR}"
else
    red "глобального IPv6 нет — Яндекс не сможет доставить запрос"
fi

if ip -6 route show default 2>/dev/null | grep -q '^default'; then
    green "маршрут по умолчанию для IPv6 есть"
else
    red "нет default-маршрута для IPv6 — внешний трафик не уйдёт"
fi

# ---------------------------------------------------------------- порт
head_ "2. Сокет"
LISTEN6=$(ss -6 -lnt 2>/dev/null | grep -c "]:${PORT}\|:${PORT} " || true)
LISTEN4=$(ss -4 -lnt 2>/dev/null | grep -c ":${PORT} " || true)

if [ "${LISTEN6:-0}" -gt 0 ]; then
    green "кто-то слушает ${PORT} по IPv6:"
    ss -6 -lnt 2>/dev/null | grep "]:${PORT}\|:${PORT} " | sed 's/^/      /'
elif [ "${LISTEN4:-0}" -gt 0 ]; then
    red "порт ${PORT} слушается только по IPv4 — IPv6-запросы не примутся"
    warn "если приложение запущено без Docker: uvicorn ... --host ::"
else
    red "на порту ${PORT} никто не слушает"
fi

# ---------------------------------------------------------------- приложение
head_ "3. Ответ по IPv6 (loopback)"
CODE=$(curl -6 -s -o /dev/null -m 3 -w '%{http_code}' "http://[::1]:${PORT}/api/health" 2>/dev/null || true)
if [ "$CODE" = "200" ]; then
    green "приложение отвечает по IPv6 на [::1] (проверка только локальная)"
else
    warn "по [::1] ответ ${CODE:-нет} — проверь, что сервис жив и слушает v6"
fi

# ---------------------------------------------------------------- исходящий
head_ "4. Исходящий IPv6"
OUT=$(curl -6 -s -m 5 https://api6.ipify.org 2>/dev/null || true)
if [ -n "$OUT" ]; then
    green "исходящий IPv6 работает, внешний адрес: ${OUT}"
    if [ -n "$ADDR" ] && [ "${OUT}" != "${ADDR%%/*}" ]; then
        warn "внешний адрес не совпадает с локальным (NAT/прокси) — для входящих это нормально"
    fi
else
    warn "исходящего IPv6 нет (для приёма не критично, но проверь маршрут)"
fi

# ---------------------------------------------------------------- файрвол
head_ "5. Файрвол"
FOUND=0

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    FOUND=1
    if ufw status 2>/dev/null | grep -qE "^${PORT}(/tcp)?\s+ALLOW"; then
        green "ufw активен, ${PORT}/tcp разрешён (правило действует и для IPv4, и для IPv6)"
    else
        red "ufw активен, ${PORT}/tcp не разрешён"
        warn "разреши: ufw allow ${PORT}/tcp"
    fi
fi

# Политика DROP сама по себе — норма: ufw так и работает, разрешая порты точечно.
# Важно, есть ли ACCEPT именно на наш порт.
if [ "$FOUND" -eq 0 ] && command -v ip6tables >/dev/null 2>&1; then
    FOUND=1
    if ip6tables -S 2>/dev/null | grep -q -- "--dport ${PORT} .*ACCEPT"; then
        green "ip6tables: есть ACCEPT на ${PORT}/tcp"
    else
        POLICY=$(ip6tables -S INPUT 2>/dev/null | awk '/^-P INPUT/{print $3}')
        if [ "${POLICY}" = "DROP" ] || [ "${POLICY}" = "REJECT" ]; then
            red "политика INPUT = ${POLICY}, ACCEPT на ${PORT}/tcp не найден"
        else
            warn "правило для ${PORT}/tcp не найдено, политика ${POLICY:-неизвестна}"
        fi
    fi
fi

if [ "$FOUND" -eq 0 ] && command -v nft >/dev/null 2>&1; then
    FOUND=1
    if nft list ruleset 2>/dev/null | grep -q "ip6"; then
        warn "есть nftables-правила для ip6 — проверь вручную, что ${PORT}/tcp разрешён"
    fi
fi

[ "$FOUND" -eq 0 ] && warn "ufw/nft/ip6tables не найдены — вероятно, фильтрация во внешнем облаке"

# ---------------------------------------------------------------- адрес для формы
head_ "Итог"
if [ -n "$ADDR" ] && [ "$FAIL" -eq 0 ]; then
    printf '  URL для интеграции Яндекс Форм:\n'
    printf '  \033[32mhttp://[%s]:%s/api/ingest\033[0m\n' "${ADDR%%/*}" "$PORT"
    printf '\n  Дальше: отправить тестовый ответ и убедиться, что он появился в /api/ingest/log\n'
else
    printf '  Блокирующих проблем: %s, предупреждений: %s\n' "$FAIL" "$WARN"
fi

printf '\n  Важно: облачный файрвол (security group) отсюда не проверить —\n'
printf '  проверь в панели провайдера, что входящий TCP %s разрешён для IPv6.\n' "$PORT"

exit "$([ "$FAIL" -eq 0 ] && echo 0 || echo 1)"

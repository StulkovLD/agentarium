#!/usr/bin/env bash
# demo/chaos.sh <агент> — авария посреди обработки. Контракт: spec/50 «Сценарий-доказательство».
#
# Момент важен: убить агента нужно ПОСРЕДИ обработки, иначе сцена ничего не доказывает. Скрипт ждёт
# по Management API момента messages_unacknowledged=1 на очереди жертвы (конверт взят, не подтверждён)
# и только тогда бьёт: kill -9 рабочего python ВНУТРИ контейнера (не PID 1 — там tini, init:true).
# Внешний docker kill не годится (ручная остановка, unless-stopped её не чинит); kill -9 1 изнутри
# ядро игнорирует. После убийства: система жива (шлюз отвечает), контейнер рестартует, конверт
# невозмутимо лежит в очереди → дообработан → очередь дренится в 0. Заявка доходит до финала.
#
# Запуск (посреди `make demo &`):  demo/chaos.sh knowledge
set -euo pipefail

AGENT="${1:?использование: demo/chaos.sh <агент>   (например: knowledge)}"
QUEUE="agentarium.${AGENT}"
MGMT_URL="${MGMT_URL:-http://localhost:15672}"
MGMT_AUTH="${MGMT_AUTH:-agentarium:agentarium}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"
VHOST="${VHOST:-%2F}"
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.agents.yml"
WAIT_S="${WAIT_S:-120}"

say() { printf '\033[1m[chaos]\033[0m %s\n' "$*"; }

# messages_unacknowledged очереди через Management API (python3 разбирает JSON — надёжнее sed по полю).
q_field() {
  curl -sf -u "$MGMT_AUTH" "$MGMT_URL/api/queues/$VHOST/$QUEUE" \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('$1', 0))"
}

wait_until() {  # wait_until <field> <value> <секунд> — крутит, пока q_field(field) != value
  local field="$1" want="$2" limit="$3" waited=0
  while [ "$(q_field "$field" 2>/dev/null || echo -1)" != "$want" ]; do
    sleep 1; waited=$((waited + 1))
    [ "$waited" -ge "$limit" ] && { say "таймаут ожидания $field=$want на $QUEUE"; return 1; }
  done
}

CID="$($COMPOSE ps -q "$AGENT")"
[ -n "$CID" ] || { say "контейнер агента '$AGENT' не найден — поднята ли система (make up)?"; exit 1; }
say "жертва: $AGENT (контейнер ${CID:0:12}), очередь $QUEUE"

say "жду messages_unacknowledged=1 — конверт взят в обработку и не подтверждён…"
wait_until messages_unacknowledged 1 "$WAIT_S"
say "конверт в обработке. Бью: kill -9 рабочего python внутри контейнера."

# PID рабочего python внутри контейнера (не tini/PID 1): comm == 'python'. procps в slim нет —
# читаем /proc напрямую (sh/grep/sed есть в базовом образе).
PID="$(docker exec "$CID" sh -c "grep -l '^python' /proc/[0-9]*/comm 2>/dev/null | head -n1 | sed 's#/proc/##; s#/comm##'")"
[ -n "$PID" ] || { say "не нашёл python-процесс в контейнере"; exit 1; }
docker exec "$CID" kill -9 "$PID"
say "python (pid $PID) убит. Контейнер должен упасть по-настоящему и подняться restart-политикой."

# Система жива: шлюз отвечает, пока жертва мертва (падение одного агента не трогает остальных, spec/50).
if curl -sf "$GATEWAY_URL/health" >/dev/null; then
  say "шлюз жив (200 /health) — падение агента не уронило систему."
else
  say "ВНИМАНИЕ: шлюз не отвечает — это регрессия изоляции."; exit 1
fi

say "жду восстановления: конверт дожидается в очереди, restart поднимает агента, обработка доводится…"
wait_until messages_unacknowledged 0 "$WAIT_S"   # переобработка началась/завершилась
wait_until messages 0 "$WAIT_S"                  # очередь дренирована — заявка доехала до финала
say "очередь $QUEUE пуста: конверт пережил аварию и дообработан. Заявка дошла до финала. ✓"

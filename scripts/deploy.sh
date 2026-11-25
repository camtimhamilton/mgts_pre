#!/usr/bin/env bash
# Обновление кода на сервере: git archive → ssh tar → пересборка контейнера.
# Берётся только закоммиченное состояние, рабочая копия не уезжает.
#
# Использование: scripts/deploy.sh root@77.222.47.41 [/opt/pulse]

set -euo pipefail

TARGET="${1:?укажи цель, например root@77.222.47.41}"
DIR="${2:-/opt/pulse}"

cd "$(dirname "$0")/.."
REV="$(git rev-parse --short HEAD)"

echo "→ $(git log -1 --pretty=%s) [$REV] на $TARGET:$DIR"

ssh "$TARGET" "mkdir -p $DIR/data && chown -R 1000:1000 $DIR/data"
git archive HEAD | ssh "$TARGET" "tar -x -C $DIR"
ssh "$TARGET" "cd $DIR && docker compose up --build -d"

echo "→ жду ответа"
sleep 5
ssh "$TARGET" "curl -fsS localhost:8000/api/health" || {
    echo "сервис не ответил, смотри: ssh $TARGET 'cd $DIR && docker compose logs --tail=50'"
    exit 1
}
echo
echo "готово"

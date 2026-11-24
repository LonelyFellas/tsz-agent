#!/usr/bin/env bash
# Verify the exact candidate with a fresh Agent-only database and no provider egress.
set -euo pipefail
image=$1
probe="$(cd "$(dirname "$0")" && pwd -P)/probe.py"
name="tsz-agent-smoke-$$-$RANDOM"
temp=$(mktemp -d)
network="" db="" api="" worker=""
cleanup() {
  local result=$? id
  trap - EXIT
  for id in "$worker" "$api" "$db"; do
    if [[ -n "$id" ]] && ! docker rm --force --volumes "$id" > /dev/null; then result=1; fi
  done
  if [[ -n "$network" ]] && ! docker network rm "$network" > /dev/null; then result=1; fi
  if [[ -f "$temp/settings.env" ]]; then unlink "$temp/settings.env" || result=1; fi
  rmdir "$temp" || result=1
  exit "$result"
}
trap cleanup EXIT
umask 077
cat > "$temp/settings.env" <<'ENV'
TSZ_AGENT_DATABASE_URL=postgresql+psycopg://agent_test@postgres:5432/tsz_agent_test
TSZ_AGENT_API_TOKEN=agent-smoke-only-token-xxxxxxxxxxxxxxx
TSZ_AGENT_TSZ_BASE_URL=http://127.0.0.1:8383
TSZ_AGENT_TSZ_SERVICE_TOKEN=agent-smoke-only-rust-token-xxxxxxxxxx
TSZ_AGENT_CHATGPT_ENABLED=false
DEEPSEEK_API_KEY=agent-smoke-only-no-provider-access
ENV

network=$(docker network create --internal "$name")
db=$(docker run -d --name "$name-db" --network "$network" --network-alias postgres \
  -e POSTGRES_USER=agent_test -e POSTGRES_DB=tsz_agent_test -e POSTGRES_HOST_AUTH_METHOD=trust \
  postgres:18-alpine)
for ((i=0; i<30; i++)); do
  if docker exec "$db" pg_isready -h 127.0.0.1 -U agent_test -d tsz_agent_test > /dev/null; then break; fi
  sleep 2
done
docker exec "$db" pg_isready -h 127.0.0.1 -U agent_test -d tsz_agent_test > /dev/null

runtime=(--network "$network" --env-file "$temp/settings.env")
docker run --rm "${runtime[@]}" "$image" alembic upgrade head > /dev/null 2>&1 \
  || { echo "Isolated Agent migration failed" >&2; exit 1; }
docker run --rm -i "${runtime[@]}" "$image" python - schema tsz_agent_test < "$probe"
api=$(docker run -d --name "$name-api" "${runtime[@]}" "$image")
worker=$(docker run -d --name "$name-worker" "${runtime[@]}" "$image" python -m tsz_agent.worker)
expected=$(docker image inspect --format '{{.Id}}' "$image")
passes=0
for ((i=0; i<30; i++)); do
  if [[ "$(docker inspect --format '{{.Image}} {{.State.Status}} {{.RestartCount}}' "$worker")" == "$expected running 0" ]] \
    && [[ "$(docker inspect --format '{{.Image}} {{.State.Status}} {{.RestartCount}}' "$api")" == "$expected running 0" ]] \
    && docker exec -i "$api" python - ready < "$probe" > /dev/null 2>&1; then
    passes=$((passes + 1))
    if (( passes == 2 )); then
      echo "Candidate API is ready and worker is running on the same image"
      exit 0
    fi
  else
    passes=0
  fi
  sleep 2
done
echo "Isolated Agent smoke failed" >&2
exit 1

#!/usr/bin/env bash
# Run on tshb-test with the verified image archive and probe.py staged beside this script.
set -euo pipefail
umask 077

fail() {
  printf 'Agent deployment: %s\n' "$1" >&2
  exit 1
}

[[ $# -eq 8 || $# -eq 9 ]] || fail "invalid release arguments"
root=${1%/}
project=$2
database=$3
sha=$4
run_id=$5
run_number=$6
attempt=$7
archive_digest=$8
compose_files=${9:--}
stage="$(cd "$(dirname "$0")" && pwd -P)"
archive="$stage/image.tar.gz"
probe="$stage/probe.py"

[[ "$root" =~ ^/[a-zA-Z0-9._/-]+$ && "$root" != */../* && "$root" != */.. ]] \
  || fail "invalid deployment directory"
[[ "$project" =~ ^[a-z0-9][a-z0-9_-]*$ && "$database" =~ ^[a-zA-Z0-9_]+$ ]] \
  || fail "invalid Compose project or database name"
[[ "$sha" =~ ^[0-9a-f]{40}$ && "$archive_digest" =~ ^[0-9a-f]{64}$ ]] \
  || fail "invalid image identity"
[[ "$run_id" =~ ^[1-9][0-9]*$ && "$run_number" =~ ^[1-9][0-9]*$ && "$attempt" =~ ^[1-9][0-9]*$ ]] \
  || fail "invalid CI run identity"
[[ -f "$root/.env" && -f "$probe" && -f "$archive" ]] || fail "missing deployment or staged files"
if [[ "$compose_files" == "-" ]]; then
  compose_files="$root/deploy/compose.yaml"
fi
[[ "$compose_files" =~ ^/[a-zA-Z0-9._/-]+(,/[a-zA-Z0-9._/-]+)*$ ]] \
  || fail "invalid Compose file list"

compose_args=(--project-name "$project" --env-file "$root/.env")
IFS=, read -r -a files <<< "$compose_files"
for file in "${files[@]}"; do
  [[ "$file" == "$root/"* && "$file" != */../* && -f "$file" ]] \
    || fail "Compose files must exist under the deployment directory"
  compose_args+=(-f "$file")
done

mkdir -p "$root/.agent-cd"
exec 9>"$root/.agent-cd/lock"
flock -n 9 || fail "another Agent release owns the deployment lock"
receipt="$root/.agent-cd/state"
if [[ -f "$receipt" ]]; then
  previous_run=$(awk -F= '$1 == "run_number" { print $2 }' "$receipt")
  [[ "$previous_run" =~ ^[1-9][0-9]*$ ]] || fail "invalid host release state"
  previous_attempt=$(awk -F= '$1 == "attempt" { print $2 }' "$receipt")
  [[ "$previous_attempt" =~ ^[1-9][0-9]*$ ]] || fail "invalid host release state"
  (( run_number > previous_run || (run_number == previous_run && attempt >= previous_attempt) )) \
    || fail "a newer candidate already reached this host"
fi

[[ "$(sha256sum "$archive" | cut -d ' ' -f1)" == "$archive_digest" ]] \
  || fail "image archive checksum mismatch"
docker load --input "$archive" > /dev/null 2>&1 || fail "unable to load image"
image="tsz-agent:sha-$sha"
identity=$(docker image inspect --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")
[[ "$identity" =~ ^(sha256:[0-9a-f]{64})[[:space:]]([0-9a-f]{40})$ && "${BASH_REMATCH[2]}" == "$sha" ]] \
  || fail "loaded image does not match this CI commit"
image_id=${BASH_REMATCH[1]}

compose() {
  TSZ_AGENT_IMAGE="$image" docker compose "${compose_args[@]}" "$@"
}
candidate() {
  compose run --rm --no-deps -T --workdir /app api python - "$@" < "$probe"
}

compose config --quiet > /dev/null 2>&1 || fail "invalid Agent Compose configuration"
schema=$(candidate schema "$database") || fail "unable to validate the independent Agent database"
[[ "$schema" =~ ^([a-zA-Z0-9_]+)[[:space:]]([a-zA-Z0-9_]+)$ ]] || fail "invalid schema check response"
current=${BASH_REMATCH[1]}
head=${BASH_REMATCH[2]}
write_state() {
  local next=$1 temp
  temp=$(mktemp "$root/.agent-cd/state.XXXXXXXX")
  printf 'status=%s\nsha=%s\nimage_id=%s\narchive_sha256=%s\nrun_id=%s\nrun_number=%s\nattempt=%s\nfrom_revision=%s\nto_revision=%s\n' \
    "$next" "$sha" "$image_id" "$archive_digest" "$run_id" "$run_number" "$attempt" "$current" "$head" > "$temp"
  mv "$temp" "$receipt"
  printf 'Agent deployment: %s (%s)\n' "$next" "$sha"
}

wait_ready() {
  local expected=$1 api worker i passes=0
  for ((i=0; i<30; i++)); do
    api=$(compose ps -q api)
    worker=$(compose ps -q worker)
    if [[ -n "$api" && -n "$worker" ]] \
      && [[ "$(docker inspect --format '{{.Image}} {{.State.Status}} {{.RestartCount}}' "$api" 2>/dev/null)" == "$expected running 0" ]] \
      && [[ "$(docker inspect --format '{{.Image}} {{.State.Status}} {{.RestartCount}}' "$worker" 2>/dev/null)" == "$expected running 0" ]] \
      && compose exec -T api python - ready < "$probe" > /dev/null 2>&1; then
      passes=$((passes + 1))
      (( passes < 2 )) || return 0
    else
      passes=0
    fi
    sleep 2
  done
  return 1
}

api_before=$(compose ps -q api)
worker_before=$(compose ps -q worker)
old_id=""
if [[ -n "$api_before" && -n "$worker_before" ]]; then
  old_id=$(docker inspect --format '{{.Image}}' "$api_before")
  if [[ "$old_id" != "$(docker inspect --format '{{.Image}}' "$worker_before")" ]]; then
    old_id=""
  fi
fi

if [[ "$current" == "$head" && "$old_id" == "$image_id" ]] && wait_ready "$image_id"; then
  write_state accepted
  exit 0
fi

rollback_image=""
if [[ "$current" == "$head" && "$old_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  rollback_image="tsz-agent:rollback-$project"
  docker image tag "$old_id" "$rollback_image" || fail "unable to retain previous image"
fi

write_state stopping
compose stop api worker > /dev/null 2>&1 || fail "unable to stop the existing Agent services"
if [[ "$current" != "$head" ]]; then
  write_state migrating
  if [[ "$(candidate migrate "$database" "$current")" != "$head" ]]; then
    write_state migration_failed
    fail "migration failed; services remain stopped for recovery"
  fi
fi
write_state starting
if compose up -d --no-deps --pull never --force-recreate api worker > /dev/null 2>&1 \
  && wait_ready "$image_id"; then
  write_state accepted
  exit 0
fi

if [[ -n "$rollback_image" ]]; then
  image="$rollback_image"
  if compose up -d --no-deps --pull never --force-recreate api worker > /dev/null 2>&1 \
    && wait_ready "$old_id"; then
    write_state rolled_back
    fail "candidate failed readiness; previous image restored"
  fi
fi
if compose stop api worker > /dev/null 2>&1; then
  write_state needs_recovery
  fail "candidate failed readiness; services remain stopped for recovery"
fi
write_state stop_failed
fail "candidate failed readiness and service stop failed; check the host"

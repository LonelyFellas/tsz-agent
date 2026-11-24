#!/usr/bin/env bash
# Runs on Actions. Transfer only the verified image and the release entry points.
set -euo pipefail
umask 077

[[ "$DEPLOY_HOST" =~ ^[a-zA-Z0-9.:-]+$ && "$DEPLOY_USER" =~ ^[a-z_][a-z0-9_-]*$ ]]
[[ "$DEPLOY_ROOT" =~ ^/[a-zA-Z0-9._/-]+$ && "$DEPLOY_ROOT" != */../* && "$DEPLOY_ROOT" != */.. ]]
[[ "$DEPLOY_PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ && "$DEPLOY_DATABASE" =~ ^[a-zA-Z0-9_]+$ ]]
[[ -n "$DEPLOY_KEY" && -n "$DEPLOY_KNOWN_HOSTS" ]]

root=$(cd "$(dirname "$0")" && pwd -P)
temp=$(mktemp -d)
config="$temp/ssh.conf"
remote_stage=""
started=false
cleanup() {
  local result=$?
  trap - EXIT
  if [[ -n "$remote_stage" ]]; then
    if [[ "$started" == false ]]; then
      ssh -F "$config" agent-tshb-test \
        "for f in '$remote_stage'/image.tar.gz '$remote_stage'/publish.sh '$remote_stage'/probe.py; do if test -f \"\$f\"; then unlink \"\$f\" || exit 1; fi; done; rmdir '$remote_stage'" \
        || result=1
    elif (( result == 255 )); then
      printf 'SSH was interrupted. Check the recorded host state and stage at %s before retrying.\n' "$remote_stage" >&2
    fi
  fi
  local file
  for file in key known_hosts image.tar.gz ssh.conf; do
    if [[ -f "$temp/$file" ]]; then unlink "$temp/$file" || result=1; fi
  done
  rmdir "$temp" || result=1
  exit "$result"
}
trap cleanup EXIT
printf '%s\n' "$DEPLOY_KEY" > "$temp/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$temp/known_hosts"
unset DEPLOY_KEY DEPLOY_KNOWN_HOSTS
cat > "$config" <<SSH
Host agent-tshb-test
  HostName $DEPLOY_HOST
  User $DEPLOY_USER
  IdentityFile $temp/key
  IdentitiesOnly yes
  UserKnownHostsFile $temp/known_hosts
  StrictHostKeyChecking yes
  BatchMode yes
  ConnectTimeout 10
  ServerAliveInterval 15
  ServerAliveCountMax 3
SSH

obsolete() {
  local code
  if bash "$root/check-ci.sh" "$CI_SHA" "$CI_RUN_ID" "$CI_RUN_NUMBER" "$CI_ATTEMPT"; then
    return 1
  else
    code=$?
    if [[ "$code" == 3 ]]; then return 0; fi
    exit "$code"
  fi
}

image="tsz-agent:sha-$CI_SHA"
docker save "$image" | gzip -1 > "$temp/image.tar.gz"
digest=$(sha256sum "$temp/image.tar.gz" | cut -d ' ' -f1)
if obsolete; then exit 0; fi

staged=$(ssh -F "$config" agent-tshb-test \
  "umask 077; mkdir -p '$DEPLOY_ROOT/.agent-cd' && mktemp -d '$DEPLOY_ROOT/.agent-cd/incoming.XXXXXXXX'")
[[ "$staged" == "$DEPLOY_ROOT/.agent-cd/incoming."* && "$staged" =~ ^/[a-zA-Z0-9._/-]+$ ]]
remote_stage=$staged
scp -F "$config" -q "$temp/image.tar.gz" "$root/publish.sh" "$root/probe.py" "agent-tshb-test:$remote_stage/"
if obsolete; then exit 0; fi

started=true
ssh -F "$config" agent-tshb-test bash -s -- "$remote_stage" "$DEPLOY_ROOT" "$DEPLOY_PROJECT" \
  "$DEPLOY_DATABASE" "$CI_SHA" "$CI_RUN_ID" "$CI_RUN_NUMBER" "$CI_ATTEMPT" "$digest" <<'REMOTE'
set -euo pipefail
stage=$1
shift
cleanup_stage() {
  result=$?
  for file in image.tar.gz publish.sh probe.py; do
    test ! -f "$stage/$file" || unlink "$stage/$file" || result=1
  done
  rmdir "$stage" || result=1
  exit "$result"
}
trap cleanup_stage EXIT
bash "$stage/publish.sh" "$@"
REMOTE

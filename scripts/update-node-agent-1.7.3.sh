#!/bin/sh
set -eu

target_ref='ghcr.io/case211/remnawave-admin-node-agent@sha256:1b74e53b9587be9fd3bddf2c0fed6a935fa3f92af666d32011de0f68bc345ed6'
target_digest='sha256:1b74e53b9587be9fd3bddf2c0fed6a935fa3f92af666d32011de0f68bc345ed6'
expected_version='1.7.3'
install_dir='/opt/remnawave-node-agent'
container='remnawave-node-agent'
service='node-agent'
backup_root='/root/remnawave-node-agent-backups'
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="${backup_root}/${stamp}-pre-agent-1.7.3"
unit="remnawave-agent-173-${stamp}"

resolved="$(readlink -f -- "$install_dir")"
[ "$resolved" = "$install_dir" ] || {
  echo "ERROR target_path=$resolved"
  exit 10
}
[ -d "$install_dir" ] || {
  echo 'ERROR install_dir_missing'
  exit 11
}
cd "$install_dir"
[ -f docker-compose.yml ] || {
  echo 'ERROR compose_missing'
  exit 12
}
[ -f .env ] || {
  echo 'ERROR env_missing'
  exit 13
}
[ "$(docker compose config --services | wc -l | tr -d ' ')" = '1' ] || {
  echo 'ERROR unexpected_service_count'
  exit 14
}
docker compose config --services | grep -Fx "$service" >/dev/null || {
  echo 'ERROR node_agent_service_missing'
  exit 15
}
[ "$(grep -Ec '^[[:space:]]*image:' docker-compose.yml)" = '1' ] || {
  echo 'ERROR unexpected_image_lines'
  exit 16
}

override_has_image=false
if [ -f docker-compose.override.yml ]; then
  override_image_lines="$(grep -Ec '^[[:space:]]*image:' docker-compose.override.yml || true)"
  case "$override_image_lines" in
    0) ;;
    1) override_has_image=true ;;
    *)
      echo 'ERROR unexpected_override_image_lines'
      exit 26
      ;;
  esac
fi

old_resolved="$(docker compose config --images)"
[ "$(printf '%s\n' "$old_resolved" | wc -l | tr -d ' ')" = '1' ] || {
  echo 'ERROR unexpected_resolved_images'
  exit 17
}
docker inspect "$container" >/dev/null 2>&1 || {
  echo 'ERROR container_missing'
  exit 18
}
command -v systemd-run >/dev/null 2>&1 || {
  echo 'ERROR systemd_run_missing'
  exit 27
}
docker compose up --help 2>&1 | grep -Eq -- '--pull([ =]|$)' || {
  echo 'ERROR compose_pull_policy_unsupported'
  exit 28
}

umask 077
mkdir -p "$backup_dir"
chmod 700 "$backup_root" "$backup_dir"
cp -a docker-compose.yml "$backup_dir/docker-compose.yml"
[ ! -f docker-compose.override.yml ] || cp -a docker-compose.override.yml "$backup_dir/docker-compose.override.yml"
cp -a .env "$backup_dir/.env"
docker inspect "$container" > "$backup_dir/container-inspect.json"
old_image_id="$(docker inspect "$container" --format '{{.Image}}')"
old_config_image="$(docker inspect "$container" --format '{{.Config.Image}}')"
snapshot="agellar/remnawave-admin-node-agent:snapshot-${stamp}"
docker tag "$old_image_id" "$snapshot"
printf 'old_config_image=%s\nold_image_id=%s\nsnapshot=%s\ntarget=%s\n' \
  "$old_config_image" "$old_image_id" "$snapshot" "$target_ref" > "$backup_dir/manifest.txt"

restore_compose() {
  cp "$backup_dir/docker-compose.yml" docker-compose.yml
  if [ -f "$backup_dir/docker-compose.override.yml" ]; then
    cp "$backup_dir/docker-compose.override.yml" docker-compose.override.yml
  fi
}

compose_mutated=false
detached_scheduled=false
cleanup() {
  rc=$?
  trap - 0 1 2 3 15
  if [ "$rc" -ne 0 ] && [ "$compose_mutated" = 'true' ] && [ "$detached_scheduled" != 'true' ]; then
    restore_compose || true
  fi
  exit "$rc"
}
trap cleanup 0 1 2 3 15

available_kb="$(df -Pk /var/lib/docker 2>/dev/null | awk 'NR==2 {print $4}')"
[ -n "$available_kb" ] || available_kb=0
[ "$available_kb" -ge 1048576 ] || {
  echo "ERROR insufficient_docker_space_kb=$available_kb"
  exit 19
}

docker pull "$target_ref"
docker image inspect "$target_ref" --format '{{join .RepoDigests "\n"}}' | grep -F "$target_digest" >/dev/null || {
  echo 'ERROR digest_not_present'
  exit 20
}
[ "$(docker run --rm --entrypoint python "$target_ref" -c 'from src.version import AGENT_VERSION; print(AGENT_VERSION)')" = "$expected_version" ] || {
  echo 'ERROR version_mismatch'
  exit 21
}
docker run --rm --entrypoint sh "$target_ref" -c 'command -v nDPId >/dev/null && command -v nDPIsrvd >/dev/null' || {
  echo 'ERROR ndpi_binaries_missing'
  exit 22
}

sed -E "s|^([[:space:]]*image:).*|\1 ${target_ref}|" docker-compose.yml > "$backup_dir/docker-compose.yml.new"
chmod --reference=docker-compose.yml "$backup_dir/docker-compose.yml.new"
compose_mutated=true
cp "$backup_dir/docker-compose.yml.new" docker-compose.yml
if [ "$override_has_image" = 'true' ]; then
  sed -E "s|^([[:space:]]*image:).*|\1 ${target_ref}|" docker-compose.override.yml > "$backup_dir/docker-compose.override.yml.new"
  chmod --reference=docker-compose.override.yml "$backup_dir/docker-compose.override.yml.new"
  cp "$backup_dir/docker-compose.override.yml.new" docker-compose.override.yml
fi
docker compose config -q || {
  restore_compose
  echo 'ERROR compose_validation_failed'
  exit 23
}
[ "$(docker compose config --images)" = "$target_ref" ] || {
  restore_compose
  echo 'ERROR compose_target_not_effective'
  exit 24
}

runner="$backup_dir/apply-and-verify.sh"
cat > "$runner" <<'RUNNER'
#!/bin/sh
set -u

backup_dir="$1"
target_ref="$2"
expected_version="$3"
old_image_id="$4"
old_config_image="$5"
snapshot="$6"
install_dir='/opt/remnawave-node-agent'
container='remnawave-node-agent'
service='node-agent'
status_file="$backup_dir/status.txt"

case "$backup_dir" in
  /root/remnawave-node-agent-backups/*-pre-agent-1.7.3) ;;
  *) exit 90 ;;
esac
[ "$(readlink -f -- "$install_dir")" = "$install_dir" ] || exit 91
cd "$install_dir" || exit 92
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

rollback() {
  reason="$1"
  cp "$backup_dir/docker-compose.yml" docker-compose.yml
  if [ -f "$backup_dir/docker-compose.override.yml" ]; then
    cp "$backup_dir/docker-compose.override.yml" docker-compose.override.yml
  fi
  docker compose config -q || {
    printf 'result=rollback_failed\nreason=%s\nstage=old_compose_invalid\n' "$reason" > "$status_file"
    return 1
  }
  docker image inspect "$snapshot" >/dev/null 2>&1 || {
    printf 'result=rollback_failed\nreason=%s\nstage=snapshot_missing\n' "$reason" > "$status_file"
    return 1
  }
  case "$old_config_image" in
    *@sha256:*) ;;
    *) docker tag "$snapshot" "$old_config_image" || {
      printf 'result=rollback_failed\nreason=%s\nstage=old_tag_restore_failed\n' "$reason" > "$status_file"
      return 1
    } ;;
  esac
  rb_started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  docker compose up -d --no-deps --force-recreate --pull never "$service" || {
    printf 'result=rollback_failed\nreason=%s\nstage=old_recreate_failed\n' "$reason" > "$status_file"
    return 1
  }
  n=0
  while [ "$n" -lt 30 ]; do
    state="$(docker inspect "$container" --format '{{.State.Status}}' 2>/dev/null || true)"
    health="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true)"
    if [ "$state" = 'running' ] && [ "$health" = 'healthy' ] \
       && docker logs --since "$rb_started" "$container" 2>&1 | grep -Fq 'Collector API OK' \
       && docker logs --since "$rb_started" "$container" 2>&1 | grep -Fq 'Agent v2 WS connected' \
       && docker exec "$container" python -c 'import httpx; from src.config import Settings; s=Settings(); r=httpx.get(f"{s.collector_url.rstrip(chr(47))}/api/v2/collector/health", headers={"Authorization": f"Bearer {s.auth_token}"}, timeout=20); r.raise_for_status()' >/dev/null 2>&1; then
      actual="$(docker inspect "$container" --format '{{.Config.Image}}')"
      image_id="$(docker inspect "$container" --format '{{.Image}}')"
      if [ "$image_id" = "$old_image_id" ]; then
        printf 'result=rolled_back\nreason=%s\nimage=%s\nimage_id=%s\nhealth=%s\ncollector_ok=true\nws_connected=true\n' \
          "$reason" "$actual" "$image_id" "$health" > "$status_file"
        return 0
      fi
      printf 'result=rollback_failed\nreason=%s\nstage=old_image_id_mismatch\nexpected_image_id=%s\nactual_image_id=%s\n' \
        "$reason" "$old_image_id" "$image_id" > "$status_file"
      return 1
    fi
    sleep 4
    n=$((n + 1))
  done
  printf 'result=rollback_failed\nreason=%s\nstage=old_health_or_ws_timeout\n' "$reason" > "$status_file"
  return 1
}

if ! docker compose up -d --no-deps --force-recreate --pull never "$service"; then
  rollback 'target_recreate_failed'
  exit 42
fi

n=0
while [ "$n" -lt 30 ]; do
  state="$(docker inspect "$container" --format '{{.State.Status}}' 2>/dev/null || true)"
  health="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true)"
    if [ "$state" = 'running' ] && [ "$health" = 'healthy' ] \
     && docker logs --since "$started" "$container" 2>&1 | grep -Fq 'Collector API OK' \
     && docker logs --since "$started" "$container" 2>&1 | grep -Fq 'Agent v2 WS connected' \
     && docker exec "$container" python -c 'import httpx; from src.config import Settings; s=Settings(); r=httpx.get(f"{s.collector_url.rstrip(chr(47))}/api/v2/collector/health", headers={"Authorization": f"Bearer {s.auth_token}"}, timeout=20); r.raise_for_status()' >/dev/null 2>&1; then
    actual="$(docker inspect "$container" --format '{{.Config.Image}}')"
    image_id="$(docker inspect "$container" --format '{{.Image}}')"
    version="$(docker exec "$container" python -c 'from src.version import AGENT_VERSION; print(AGENT_VERSION)' 2>/dev/null || true)"
    if [ "$actual" = "$target_ref" ] && [ "$version" = "$expected_version" ]; then
      printf 'result=success\nimage=%s\nimage_id=%s\nversion=%s\nhealth=%s\ncollector_ok=true\nws_connected=true\n' \
        "$actual" "$image_id" "$version" "$health" > "$status_file"
      exit 0
    fi
    rollback 'target_identity_or_version_mismatch'
    exit 43
  fi
  case "$state" in exited|dead) break ;; esac
  sleep 4
  n=$((n + 1))
done

rollback 'target_health_or_connection_timeout'
exit 44
RUNNER
chmod 700 "$runner"

scheduled=false
if systemd-run --unit="$unit" --collect --property=Type=oneshot \
     /bin/sh -c "sleep 3; exec '$runner' '$backup_dir' '$target_ref' '$expected_version' '$old_image_id' '$old_config_image' '$snapshot'" >/dev/null 2>&1; then
  scheduled=true
fi
if [ "$scheduled" != 'true' ]; then
  restore_compose
  echo 'ERROR detached_runner_unavailable'
  exit 25
fi
detached_scheduled=true

printf 'scheduled=true\nbackup=%s\ntarget=%s\nold_image=%s\n' \
  "$backup_dir" "$target_ref" "$old_config_image"

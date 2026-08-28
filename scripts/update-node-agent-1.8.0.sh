#!/bin/sh
set -eu

target_ref='ghcr.io/case211/remnawave-admin-node-agent@sha256:ebee9822755f26cbf88e8ac96de995d83449b29428509d7c5d5905af9c0fef14'
target_digest='sha256:ebee9822755f26cbf88e8ac96de995d83449b29428509d7c5d5905af9c0fef14'
expected_version='1.8.0'
install_dir='/opt/remnawave-node-agent'
container='remnawave-node-agent'
service='node-agent'
backup_root='/root/remnawave-node-agent-backups'
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_dir="${backup_root}/${stamp}-pre-agent-1.8.0"
unit="remnawave-agent-180-${stamp}"

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
require_regular_config_files() {
  [ -f docker-compose.yml ] && [ ! -L docker-compose.yml ] || {
    echo 'ERROR compose_not_regular_file'
    return 12
  }
  [ -f .env ] && [ ! -L .env ] || {
    echo 'ERROR env_not_regular_file'
    return 13
  }
  if [ -e docker-compose.override.yml ] || [ -L docker-compose.override.yml ]; then
    [ -f docker-compose.override.yml ] && [ ! -L docker-compose.override.yml ] || {
      echo 'ERROR override_not_regular_file'
      return 36
    }
  fi
}
require_regular_config_files
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

require_safe_current_agent() {
  require_regular_config_files || return "$?"
  compose_container_id="$(docker compose ps -q "$service" 2>/dev/null || true)"
  actual_container_id="$(docker inspect "$container" --format '{{.Id}}' 2>/dev/null || true)"
  [ -n "$compose_container_id" ] \
    && [ "$(printf '%s\n' "$compose_container_id" | wc -l | tr -d ' ')" = '1' ] \
    && [ "$compose_container_id" = "$actual_container_id" ] || {
    echo 'ERROR compose_container_mismatch'
    return 34
  }
  # Never emit resolved Compose: credentials pass directly into a silent,
  # allowlisted check. An absent key must have a confirmed false source default.
  docker compose config --format json 2>/dev/null | docker exec -i "$container" python -c 'import json, sys; from src.config import Settings; cfg=json.load(sys.stdin); env=cfg["services"]["node-agent"].get("environment") or {}; assert isinstance(env, dict); assert str(env.get("AGENT_NDPI_ENABLED")).strip().lower() in ("0", "false", "no", "off", "f", "n") if "AGENT_NDPI_ENABLED" in env else Settings.model_fields["ndpi_enabled"].default is False' >/dev/null 2>&1 || {
    echo 'ERROR effective_compose_ndpi_not_confirmed_off'
    return 35
  }
  live_state="$(docker inspect "$container" --format '{{.State.Status}}' 2>/dev/null || true)"
  live_health="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true)"
  live_image_id="$(docker inspect "$container" --format '{{.Image}}' 2>/dev/null || true)"
  if [ "$live_state" != 'running' ] || [ "$live_health" != 'healthy' ] || [ "$live_image_id" != "$old_image_id" ]; then
    echo 'ERROR live_agent_unhealthy_or_changed'
    return 29
  fi
  # Active reachability must still work now, not merely in an earlier preflight.
  # The health endpoint is anonymous; this is not an auth-token validation.
  docker exec "$container" python -c 'import httpx; from src.config import Settings; s=Settings(); r=httpx.get(f"{s.collector_url.rstrip(chr(47))}/api/v2/collector/health", headers={"Authorization": f"Bearer {s.auth_token}"}, timeout=20); r.raise_for_status()' >/dev/null 2>&1 || {
    echo 'ERROR live_collector_probe_failed'
    return 30
  }
  docker exec "$container" python -c 'from src.config import Settings; assert not Settings().ndpi_enabled' >/dev/null 2>&1 \
    && docker exec "$container" sh -c 'command -v pgrep >/dev/null || exit 2; for process in nDPId nDPIsrvd; do pgrep -x "$process" >/dev/null; [ "$?" = 1 ] || exit 1; done' >/dev/null 2>&1 || {
    echo 'ERROR live_ndpi_not_confirmed_off'
    return 31
  }
}

old_image_id="$(docker inspect "$container" --format '{{.Image}}')"
old_config_image="$(docker inspect "$container" --format '{{.Config.Image}}')"
[ "$old_resolved" = "$old_config_image" ] || {
  echo 'ERROR compose_container_image_drift'
  exit 32
}
require_safe_current_agent

docker_root="$(docker info --format '{{.DockerRootDir}}')"
case "$docker_root" in /*) ;; *) echo 'ERROR invalid_docker_root'; exit 37 ;; esac
[ -d "$docker_root" ] || { echo 'ERROR docker_root_missing'; exit 38; }
available_kb="$(df -Pk -- "$docker_root" 2>/dev/null | awk 'NR==2 {print $4}')"
[ -n "$available_kb" ] || available_kb=0
[ "$available_kb" -ge 1048576 ] || {
  echo "ERROR insufficient_docker_space_kb=$available_kb"
  exit 19
}

umask 077
mkdir -p "$backup_root"
[ "$(readlink -f -- "$backup_root")" = "$backup_root" ] || {
  echo 'ERROR backup_root_path_mismatch'
  exit 33
}
# Do not overwrite a previous backup when two invocations share a timestamp.
mkdir "$backup_dir"
chmod 700 "$backup_root" "$backup_dir"
cp -a docker-compose.yml "$backup_dir/docker-compose.yml"
[ ! -f docker-compose.override.yml ] || cp -a docker-compose.override.yml "$backup_dir/docker-compose.override.yml"
cp -a .env "$backup_dir/.env"
docker inspect "$container" > "$backup_dir/container-inspect.json"
snapshot="agellar/remnawave-admin-node-agent:snapshot-${stamp}"
docker tag "$old_image_id" "$snapshot"
printf 'old_config_image=%s\nold_image_id=%s\nsnapshot=%s\ntarget=%s\n' \
  "$old_config_image" "$old_image_id" "$snapshot" "$target_ref" > "$backup_dir/manifest.txt"

restore_compose() {
  cp "$backup_dir/docker-compose.yml" docker-compose.yml || return 1
  if [ -f "$backup_dir/docker-compose.override.yml" ]; then
    cp "$backup_dir/docker-compose.override.yml" docker-compose.override.yml || return 1
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
trap cleanup 0
trap 'exit 129' 1
trap 'exit 130' 2
trap 'exit 131' 3
trap 'exit 143' 15

docker pull "$target_ref"
docker image inspect "$target_ref" --format '{{join .RepoDigests "\n"}}' | grep -F "$target_digest" >/dev/null || {
  echo 'ERROR digest_not_present'
  exit 20
}
target_image_id="$(docker image inspect "$target_ref" --format '{{.Id}}')"
[ "$(docker run --rm --entrypoint python "$target_ref" -c 'from src.version import AGENT_VERSION; from src.config import Settings; assert Settings.model_fields["ndpi_enabled"].default is False; print(AGENT_VERSION)')" = "$expected_version" ] || {
  echo 'ERROR version_mismatch'
  exit 21
}
docker run --rm --entrypoint sh "$target_ref" -c 'command -v nDPId >/dev/null && command -v nDPIsrvd >/dev/null' || {
  echo 'ERROR ndpi_binaries_missing'
  exit 22
}

# Pull/validation can take minutes. Recheck before changing Compose, then again
# in the detached runner before stopping the existing management channel.
require_safe_current_agent
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
target_image_id="$7"
install_dir='/opt/remnawave-node-agent'
container='remnawave-node-agent'
service='node-agent'
status_file="$backup_dir/status.txt"

case "$backup_dir" in
  /root/remnawave-node-agent-backups/*-pre-agent-1.8.0) ;;
  *) exit 90 ;;
esac
[ "$(readlink -f -- "$install_dir")" = "$install_dir" ] || exit 91
cd "$install_dir" || exit 92

restore_compose() {
  cp "$backup_dir/docker-compose.yml" docker-compose.yml || return 1
  if [ -f "$backup_dir/docker-compose.override.yml" ]; then
    cp "$backup_dir/docker-compose.override.yml" docker-compose.override.yml || return 1
  fi
  docker compose config -q
}

ndpi_is_off() {
  docker exec "$container" python -c 'from src.config import Settings; assert not Settings().ndpi_enabled' >/dev/null 2>&1 \
    && docker exec "$container" sh -c 'command -v pgrep >/dev/null || exit 2; for process in nDPId nDPIsrvd; do pgrep -x "$process" >/dev/null; [ "$?" = 1 ] || exit 1; done' >/dev/null 2>&1
}

collector_is_reachable() {
  docker exec "$container" python -c 'import httpx; from src.config import Settings; s=Settings(); r=httpx.get(f"{s.collector_url.rstrip(chr(47))}/api/v2/collector/health", headers={"Authorization": f"Bearer {s.auth_token}"}, timeout=5); r.raise_for_status()' >/dev/null 2>&1
}

compose_controls_container() {
  compose_container_id="$(docker compose ps -q "$service" 2>/dev/null || true)"
  actual_container_id="$(docker inspect "$container" --format '{{.Id}}' 2>/dev/null || true)"
  [ -n "$compose_container_id" ] \
    && [ "$(printf '%s\n' "$compose_container_id" | wc -l | tr -d ' ')" = '1' ] \
    && [ "$compose_container_id" = "$actual_container_id" ]
}

effective_ndpi_is_off() {
  docker compose config --format json 2>/dev/null | docker exec -i "$container" python -c 'import json, sys; from src.config import Settings; cfg=json.load(sys.stdin); env=cfg["services"]["node-agent"].get("environment") or {}; assert isinstance(env, dict); assert str(env.get("AGENT_NDPI_ENABLED")).strip().lower() in ("0", "false", "no", "off", "f", "n") if "AGENT_NDPI_ENABLED" in env else Settings.model_fields["ndpi_enabled"].default is False' >/dev/null 2>&1
}

state="$(docker inspect "$container" --format '{{.State.Status}}' 2>/dev/null || true)"
health="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true)"
live_image_id="$(docker inspect "$container" --format '{{.Image}}' 2>/dev/null || true)"
if [ "$state" != 'running' ] || [ "$health" != 'healthy' ] || [ "$live_image_id" != "$old_image_id" ] \
   || ! compose_controls_container || ! effective_ndpi_is_off || ! ndpi_is_off || ! collector_is_reachable; then
  if restore_compose; then
    printf 'result=aborted\nreason=pre_restart_gate_failed\nexisting_container_preserved=true\n' > "$status_file"
  else
    printf 'result=rollback_failed\nreason=pre_restart_gate_failed\nstage=compose_restore_failed\n' > "$status_file"
  fi
  exit 45
fi
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

rollback() {
  reason="$1"
  restore_compose || {
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
  # The failed target may already be stopped. Validate the restored effective
  # flag in an isolated pinned image, not by exec-ing into that failed agent.
  docker compose config --format json 2>/dev/null | docker run --rm --pull never --network none -i --entrypoint python "$target_ref" -c 'import json, sys; from src.config import Settings; cfg=json.load(sys.stdin); env=cfg["services"]["node-agent"].get("environment") or {}; assert isinstance(env, dict); assert str(env.get("AGENT_NDPI_ENABLED")).strip().lower() in ("0", "false", "no", "off", "f", "n") if "AGENT_NDPI_ENABLED" in env else Settings.model_fields["ndpi_enabled"].default is False' >/dev/null 2>&1 || {
    printf 'result=rollback_failed\nreason=%s\nstage=effective_ndpi_not_off\n' "$reason" > "$status_file"
    return 1
  }
  docker compose up -d --no-deps --force-recreate --pull never "$service" || {
    printf 'result=rollback_failed\nreason=%s\nstage=old_recreate_failed\n' "$reason" > "$status_file"
    return 1
  }
  compose_controls_container || {
    printf 'result=rollback_failed\nreason=%s\nstage=old_compose_container_mismatch\n' "$reason" > "$status_file"
    return 1
  }
  n=0
  while [ "$n" -lt 30 ]; do
    state="$(docker inspect "$container" --format '{{.State.Status}}' 2>/dev/null || true)"
    health="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true)"
    if [ "$state" = 'running' ] && [ "$health" = 'healthy' ] \
       && docker logs --since "$rb_started" "$container" 2>&1 | grep -Fq 'Collector API OK' \
       && docker logs --since "$rb_started" "$container" 2>&1 | grep -Fq 'Agent v2 WS connected' \
       && collector_is_reachable && ndpi_is_off && compose_controls_container; then
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
if ! compose_controls_container; then
  rollback 'target_compose_container_mismatch'
  exit 46
fi

n=0
while [ "$n" -lt 30 ]; do
  state="$(docker inspect "$container" --format '{{.State.Status}}' 2>/dev/null || true)"
  health="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null || true)"
  if [ "$state" = 'running' ] && [ "$health" = 'healthy' ] \
     && docker logs --since "$started" "$container" 2>&1 | grep -Fq 'Collector API OK' \
     && docker logs --since "$started" "$container" 2>&1 | grep -Fq 'Agent v2 WS connected' \
     && collector_is_reachable && ndpi_is_off && compose_controls_container; then
    actual="$(docker inspect "$container" --format '{{.Config.Image}}')"
    image_id="$(docker inspect "$container" --format '{{.Image}}')"
    version="$(docker exec "$container" python -c 'from src.version import AGENT_VERSION; print(AGENT_VERSION)' 2>/dev/null || true)"
    if [ "$actual" = "$target_ref" ] && [ "$image_id" = "$target_image_id" ] && [ "$version" = "$expected_version" ]; then
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
# Type=oneshot waits for the whole job unless --no-block is explicit. The caller
# must acknowledge scheduling before its own container is replaced.
if systemd-run --no-block --unit="$unit" --collect --property=Type=oneshot \
     /bin/sh -c 'sleep 3; exec "$@"' sh "$runner" "$backup_dir" "$target_ref" "$expected_version" "$old_image_id" "$old_config_image" "$snapshot" "$target_image_id" >/dev/null 2>&1; then
  scheduled=true
fi
if [ "$scheduled" != 'true' ]; then
  restore_compose
  echo 'ERROR detached_runner_unavailable'
  exit 25
fi
detached_scheduled=true

printf 'scheduled=true\nbackup=%s\ntarget=%s\ntarget_image_id=%s\nold_image=%s\n' \
  "$backup_dir" "$target_ref" "$target_image_id" "$old_config_image"

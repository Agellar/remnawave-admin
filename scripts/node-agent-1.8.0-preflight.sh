#!/bin/sh
set -eu

install_dir='/opt/remnawave-node-agent'
container='remnawave-node-agent'
service='node-agent'

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
[ -f docker-compose.yml ] && [ ! -L docker-compose.yml ] || {
  echo 'ERROR compose_not_regular_file'
  exit 12
}
[ -f .env ] && [ ! -L .env ] || {
  echo 'ERROR env_not_regular_file'
  exit 13
}
if [ -e docker-compose.override.yml ] || [ -L docker-compose.override.yml ]; then
  [ -f docker-compose.override.yml ] && [ ! -L docker-compose.override.yml ] || {
    echo 'ERROR override_not_regular_file'
    exit 21
  }
fi
docker compose config -q
[ "$(docker compose config --services | wc -l | tr -d ' ')" = '1' ] || {
  echo 'ERROR unexpected_service_count'
  exit 14
}
docker compose config --services | grep -Fx "$service" >/dev/null || {
  echo 'ERROR node_agent_service_missing'
  exit 15
}
docker inspect "$container" >/dev/null 2>&1 || {
  echo 'ERROR container_missing'
  exit 16
}
compose_container_id="$(docker compose ps -q "$service")"
actual_container_id="$(docker inspect "$container" --format '{{.Id}}')"
[ -n "$compose_container_id" ] \
  && [ "$(printf '%s\n' "$compose_container_id" | wc -l | tr -d ' ')" = '1' ] \
  && [ "$compose_container_id" = "$actual_container_id" ] || {
  echo 'ERROR compose_container_mismatch'
  exit 19
}
# Read only this flag from the effective configuration. The full JSON, including
# credentials, stays inside the pipe and is never printed or written to disk.
docker compose config --format json 2>/dev/null | docker exec -i "$container" python -c 'import json, sys; from src.config import Settings; cfg=json.load(sys.stdin); env=cfg["services"]["node-agent"].get("environment") or {}; assert isinstance(env, dict); assert str(env.get("AGENT_NDPI_ENABLED")).strip().lower() in ("0", "false", "no", "off", "f", "n") if "AGENT_NDPI_ENABLED" in env else Settings.model_fields["ndpi_enabled"].default is False' >/dev/null 2>&1 || {
  echo 'ERROR effective_compose_ndpi_not_confirmed_off'
  exit 20
}
command -v systemd-run >/dev/null 2>&1 || {
  echo 'ERROR systemd_run_missing'
  exit 17
}
docker compose up --help 2>&1 | grep -Eq -- '--pull([ =]|$)' || {
  echo 'ERROR compose_pull_policy_unsupported'
  exit 18
}

compose_image="$(docker compose config --images)"
actual_image="$(docker inspect "$container" --format '{{.Config.Image}}')"
image_id="$(docker inspect "$container" --format '{{.Image}}')"
state="$(docker inspect "$container" --format '{{.State.Status}}')"
health="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
version="$(docker exec "$container" python -c 'from src.version import AGENT_VERSION; print(AGENT_VERSION)')"
docker_root="$(docker info --format '{{.DockerRootDir}}')"
case "$docker_root" in /*) ;; *) echo 'ERROR invalid_docker_root'; exit 22 ;; esac
[ -d "$docker_root" ] || { echo 'ERROR docker_root_missing'; exit 23; }
available_kb="$(df -Pk -- "$docker_root" 2>/dev/null | awk 'NR==2 {print $4}')"
[ -n "$available_kb" ] || available_kb=0
ws_log=false
collector_log=false
docker logs --since 30m "$container" 2>&1 | grep -Fq 'Agent v2 WS connected' && ws_log=true
docker logs --since 30m "$container" 2>&1 | grep -Fq 'Collector API OK' && collector_log=true
collector_probe=false
# This endpoint proves reachability, not token validity. Fresh batches and the
# existing command channel are verified separately by the rollout operator.
docker exec "$container" python -c 'import httpx; from src.config import Settings; s=Settings(); r=httpx.get(f"{s.collector_url.rstrip(chr(47))}/api/v2/collector/health", headers={"Authorization": f"Bearer {s.auth_token}"}, timeout=20); r.raise_for_status()' \
  >/dev/null 2>&1 && collector_probe=true
ndpi_off=false
if docker exec "$container" python -c 'from src.config import Settings; assert not Settings().ndpi_enabled' >/dev/null 2>&1 \
   && docker exec "$container" sh -c 'command -v pgrep >/dev/null || exit 2; for process in nDPId nDPIsrvd; do pgrep -x "$process" >/dev/null; [ "$?" = 1 ] || exit 1; done' >/dev/null 2>&1; then
  ndpi_off=true
fi

printf 'compose_image=%s\nactual_image=%s\nimage_id=%s\nstate=%s\nhealth=%s\nversion=%s\navailable_kb=%s\nws_log=%s\ncollector_log=%s\ncollector_probe=%s\nndpi_off=%s\nsystemd_run=true\npull_never=true\n' \
  "$compose_image" "$actual_image" "$image_id" "$state" "$health" "$version" "$available_kb" "$ws_log" "$collector_log" "$collector_probe" "$ndpi_off"

[ "$compose_image" = "$actual_image" ]
[ "$state" = 'running' ]
[ "$health" = 'healthy' ]
[ "$collector_probe" = 'true' ]
[ "$ndpi_off" = 'true' ]
[ "$available_kb" -ge 1048576 ]
printf 'compose_container_match=true\neffective_compose_ndpi_off=true\npreflight=pass\n'

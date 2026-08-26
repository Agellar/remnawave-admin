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
[ -f docker-compose.yml ] || {
  echo 'ERROR compose_missing'
  exit 12
}
[ -f .env ] || {
  echo 'ERROR env_missing'
  exit 13
}
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
available_kb="$(df -Pk /var/lib/docker 2>/dev/null | awk 'NR==2 {print $4}')"
[ -n "$available_kb" ] || available_kb=0
ws_log=false
collector_log=false
docker logs --since 30m "$container" 2>&1 | grep -Fq 'Agent v2 WS connected' && ws_log=true
docker logs --since 30m "$container" 2>&1 | grep -Fq 'Collector API OK' && collector_log=true
collector_probe=false
docker exec "$container" python -c 'import httpx; from src.config import Settings; s=Settings(); r=httpx.get(f"{s.collector_url.rstrip(chr(47))}/api/v2/collector/health", headers={"Authorization": f"Bearer {s.auth_token}"}, timeout=20); r.raise_for_status()' \
  >/dev/null 2>&1 && collector_probe=true

printf 'preflight=pass\ncompose_image=%s\nactual_image=%s\nimage_id=%s\nstate=%s\nhealth=%s\nversion=%s\navailable_kb=%s\nws_log=%s\ncollector_log=%s\ncollector_probe=%s\nsystemd_run=true\npull_never=true\n' \
  "$compose_image" "$actual_image" "$image_id" "$state" "$health" "$version" "$available_kb" "$ws_log" "$collector_log" "$collector_probe"

[ "$state" = 'running' ]
[ "$health" = 'healthy' ]
[ "$collector_probe" = 'true' ]
[ "$available_kb" -ge 1048576 ]

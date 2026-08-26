#!/bin/sh
set -eu

target_ref='ghcr.io/case211/remnawave-admin-node-agent@sha256:1b74e53b9587be9fd3bddf2c0fed6a935fa3f92af666d32011de0f68bc345ed6'
target_digest='sha256:1b74e53b9587be9fd3bddf2c0fed6a935fa3f92af666d32011de0f68bc345ed6'
install_dir='/opt/remnawave-node-agent'
container='remnawave-node-agent'

[ "$(readlink -f -- "$install_dir")" = "$install_dir" ] || exit 10
cd "$install_dir"
docker compose config -q
compose_image="$(docker compose config --images)"
actual_image="$(docker inspect "$container" --format '{{.Config.Image}}')"
image_id="$(docker inspect "$container" --format '{{.Image}}')"
state="$(docker inspect "$container" --format '{{.State.Status}}')"
health="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
version="$(docker exec "$container" python -c 'from src.version import AGENT_VERSION; print(AGENT_VERSION)')"
repo_digest=false
docker image inspect "$target_ref" --format '{{join .RepoDigests "\n"}}' | grep -F "$target_digest" >/dev/null && repo_digest=true
binaries=false
docker exec "$container" sh -c 'command -v nDPId >/dev/null && command -v nDPIsrvd >/dev/null' && binaries=true
ndpi_running=false
docker exec "$container" sh -c 'pgrep -x nDPId >/dev/null || pgrep -x nDPIsrvd >/dev/null' && ndpi_running=true
ws_log=false
collector_log=false
docker logs --since 30m "$container" 2>&1 | grep -Fq 'Agent v2 WS connected' && ws_log=true
docker logs --since 30m "$container" 2>&1 | grep -Fq 'Collector API OK' && collector_log=true
collector_probe=false
docker exec "$container" python -c 'import httpx; from src.config import Settings; s=Settings(); r=httpx.get(f"{s.collector_url.rstrip(chr(47))}/api/v2/collector/health", headers={"Authorization": f"Bearer {s.auth_token}"}, timeout=20); r.raise_for_status()' \
  >/dev/null 2>&1 && collector_probe=true
status='missing'
backup_dir='missing'
latest="$(find /root/remnawave-node-agent-backups -mindepth 1 -maxdepth 1 -type d -name '*-pre-agent-1.7.3' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)"
if [ -n "$latest" ] && [ -f "$latest/status.txt" ]; then
  status="$(sed -n 's/^result=//p' "$latest/status.txt" | head -1)"
  backup_dir="$(basename "$latest")"
fi
printf 'compose_image=%s\nactual_image=%s\nimage_id=%s\nstate=%s\nhealth=%s\nversion=%s\nrepo_digest=%s\nndpi_binaries=%s\nndpi_running=%s\nws_log=%s\ncollector_log=%s\ncollector_probe=%s\nrollout_status=%s\nbackup_dir=%s\n' \
  "$compose_image" "$actual_image" "$image_id" "$state" "$health" "$version" "$repo_digest" "$binaries" "$ndpi_running" "$ws_log" "$collector_log" "$collector_probe" "$status" "$backup_dir"
[ "$compose_image" = "$target_ref" ]
[ "$actual_image" = "$target_ref" ]
[ "$state" = 'running' ]
[ "$health" = 'healthy' ]
[ "$version" = '1.7.3' ]
[ "$repo_digest" = 'true' ]
[ "$binaries" = 'true' ]
[ "$ndpi_running" = 'false' ]
[ "$ws_log" = 'true' ]
[ "$collector_log" = 'true' ]
[ "$collector_probe" = 'true' ]
[ "$status" = 'success' ]
case "$backup_dir" in *-pre-agent-1.7.3) ;; *) exit 30 ;; esac

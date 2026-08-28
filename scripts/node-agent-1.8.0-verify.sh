#!/bin/sh
set -eu

target_ref='ghcr.io/case211/remnawave-admin-node-agent@sha256:ebee9822755f26cbf88e8ac96de995d83449b29428509d7c5d5905af9c0fef14'
target_digest='sha256:ebee9822755f26cbf88e8ac96de995d83449b29428509d7c5d5905af9c0fef14'
install_dir='/opt/remnawave-node-agent'
container='remnawave-node-agent'
service='node-agent'

[ "$(readlink -f -- "$install_dir")" = "$install_dir" ] || exit 10
cd "$install_dir"
docker compose config -q
compose_container_id="$(docker compose ps -q "$service")"
actual_container_id="$(docker inspect "$container" --format '{{.Id}}')"
[ -n "$compose_container_id" ] \
  && [ "$(printf '%s\n' "$compose_container_id" | wc -l | tr -d ' ')" = '1' ] \
  && [ "$compose_container_id" = "$actual_container_id" ] || {
  echo 'ERROR compose_container_mismatch'
  exit 31
}
docker compose config --format json 2>/dev/null | docker exec -i "$container" python -c 'import json, sys; from src.config import Settings; cfg=json.load(sys.stdin); env=cfg["services"]["node-agent"].get("environment") or {}; assert isinstance(env, dict); values=[value for key, value in env.items() if key.casefold() == "agent_ndpi_enabled"]; assert len(values) <= 1; assert str(values[0]).strip().casefold() in ("0", "false", "no", "off", "f", "n") if values else Settings.model_fields["ndpi_enabled"].default is False' >/dev/null 2>&1 || {
  echo 'ERROR effective_compose_ndpi_not_confirmed_off'
  exit 32
}
compose_image="$(docker compose config --images)"
actual_image="$(docker inspect "$container" --format '{{.Config.Image}}')"
image_id="$(docker inspect "$container" --format '{{.Image}}')"
expected_image_id="$(docker image inspect "$target_ref" --format '{{.Id}}')"
state="$(docker inspect "$container" --format '{{.State.Status}}')"
health="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')"
version="$(docker exec "$container" python -c 'from src.version import AGENT_VERSION; print(AGENT_VERSION)')"
repo_digest=false
docker image inspect "$target_ref" --format '{{join .RepoDigests "\n"}}' | grep -F "$target_digest" >/dev/null && repo_digest=true
binaries=false
docker exec "$container" sh -c 'command -v nDPId >/dev/null && command -v nDPIsrvd >/dev/null' && binaries=true
ndpi_running=false
docker exec "$container" sh -c 'pgrep -x nDPId >/dev/null || pgrep -x nDPIsrvd >/dev/null' && ndpi_running=true
ndpi_off=false
if docker exec "$container" python -c 'from src.config import Settings; assert not Settings().ndpi_enabled' >/dev/null 2>&1 \
   && docker exec "$container" sh -c 'command -v pgrep >/dev/null || exit 2; for process in nDPId nDPIsrvd; do pgrep -x "$process" >/dev/null; [ "$?" = 1 ] || exit 1; done' >/dev/null 2>&1; then
  ndpi_off=true
fi
ws_log=false
collector_log=false
docker logs --since 30m "$container" 2>&1 | grep -Fq 'Agent v2 WS connected' && ws_log=true
docker logs --since 30m "$container" 2>&1 | grep -Fq 'Collector API OK' && collector_log=true
collector_probe=false
docker exec "$container" python -c 'import httpx; from src.config import Settings; s=Settings(); r=httpx.get(f"{s.collector_url.rstrip(chr(47))}/api/v2/collector/health", headers={"Authorization": f"Bearer {s.auth_token}"}, timeout=20); r.raise_for_status()' \
  >/dev/null 2>&1 && collector_probe=true
status='missing'
backup_dir='missing'
latest="$(find /root/remnawave-node-agent-backups -mindepth 1 -maxdepth 1 -type d -name '*-pre-agent-1.8.0' -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)"
if [ -n "$latest" ] && [ -f "$latest/status.txt" ]; then
  status="$(sed -n 's/^result=//p' "$latest/status.txt" | head -1)"
  backup_dir="$(basename "$latest")"
fi
printf 'compose_image=%s\nactual_image=%s\nimage_id=%s\nexpected_image_id=%s\nstate=%s\nhealth=%s\nversion=%s\nrepo_digest=%s\nndpi_binaries=%s\nndpi_running=%s\nndpi_off=%s\nws_log=%s\ncollector_log=%s\ncollector_probe=%s\nrollout_status=%s\nbackup_dir=%s\n' \
  "$compose_image" "$actual_image" "$image_id" "$expected_image_id" "$state" "$health" "$version" "$repo_digest" "$binaries" "$ndpi_running" "$ndpi_off" "$ws_log" "$collector_log" "$collector_probe" "$status" "$backup_dir"
[ "$compose_image" = "$target_ref" ]
[ "$actual_image" = "$target_ref" ]
[ "$image_id" = "$expected_image_id" ]
[ "$state" = 'running' ]
[ "$health" = 'healthy' ]
[ "$version" = '1.8.0' ]
[ "$repo_digest" = 'true' ]
[ "$binaries" = 'true' ]
[ "$ndpi_running" = 'false' ]
[ "$ndpi_off" = 'true' ]
[ "$ws_log" = 'true' ]
[ "$collector_log" = 'true' ]
[ "$collector_probe" = 'true' ]
[ "$status" = 'success' ]
case "$backup_dir" in *-pre-agent-1.8.0) ;; *) exit 30 ;; esac
printf 'compose_container_match=true\neffective_compose_ndpi_off=true\n'

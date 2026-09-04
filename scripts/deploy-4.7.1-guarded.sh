#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

version=4.7.1-agellar.1
repo=/opt/remnawave-admin
backend_container=remnawave-web-backend
frontend_container=remnawave-web-frontend
db_container=remnawave-admin-db
secret_target=.env.backend-secrets

die() {
    echo "release guard: $*" >&2
    exit 64
}

is_immutable_image() {
    [[ "$1" =~ ^[a-z0-9][a-z0-9._/-]*:sha256-[0-9a-f]{64}$ ]]
}

# Preserve the backed-up local override content except for the backend image.
# The previous 4.6.3 app must know Alembic head 0105 after migration.
make_rollback_override() {
    local source_file="$1" destination="$2" backend_image="$3"
    local partial="${destination}.partial"
    rm -f -- "$partial"
    awk -v image="$backend_image" '
        /^  web-backend:[[:space:]]*(#.*)?$/ { inside = 1 }
        inside && /^  [A-Za-z0-9_.-]+:[[:space:]]*(#.*)?$/ && $0 !~ /^  web-backend:/ { inside = 0 }
        inside && /^    image:[[:space:]]*/ {
            print "    image: " image
            replaced++
            next
        }
        { print }
        END { if (replaced != 1) exit 42 }
    ' "$source_file" > "$partial" || {
        rm -f -- "$partial"
        echo "expected exactly one web-backend image" >&2
        return 42
    }
    mv -- "$partial" "$destination"
}

override_has_bot_zero() {
    awk '
        /^  bot:[[:space:]]*(#.*)?$/ { inside = 1 }
        inside && /^  [A-Za-z0-9_.-]+:[[:space:]]*(#.*)?$/ && $0 !~ /^  bot:/ { inside = 0 }
        inside && /^      replicas:[[:space:]]*0[[:space:]]*(#.*)?$/ { found++ }
        END { exit(found == 1 ? 0 : 1) }
    ' "$1"
}

self_test() {
    is_immutable_image 'repo/component:sha256-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    ! is_immutable_image 'repo/component:latest'
    local td
    td="$(mktemp -d)"
    trap 'rm -rf -- "$td"' RETURN
    printf '%s\n' \
        'services:' \
        '  bot:' \
        '    image: keep/bot:sha256-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' \
        '    deploy:' \
        '      replicas: 0' \
        '  web-backend:' \
        '    image: old/backend:sha256-cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc' \
        '    ports: !reset []' \
        '  web-frontend:' \
        '    image: keep/frontend:sha256-dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd' \
        > "$td/in.yml"
    make_rollback_override "$td/in.yml" "$td/out.yml" \
        'rollback/backend:sha256-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
    grep -Fq 'rollback/backend:sha256-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee' "$td/out.yml"
    grep -Fq 'keep/frontend:sha256-dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd' "$td/out.yml"
    ! grep -Fq 'old/backend:sha256-' "$td/out.yml"
    override_has_bot_zero "$td/out.yml"
    echo 'DEPLOY_GUARD_SELF_TEST=PASS'
}

if [ "${1:-}" = "--self-test" ]; then
    self_test
    exit 0
fi

[ "$#" -ge 8 ] && [ "$#" -le 9 ] || die \
    "usage: $0 IMAGE_SOURCE_HEAD DEPLOY_HEAD OLD_HEAD PRE_BACKUP NEW_BACKEND NEW_FRONTEND ROLLBACK_BACKEND OLD_FRONTEND [STAGED_BACKEND_SECRET]"

image_source_head="$1"
deploy_head="$2"
old_head="$3"
pre_backup="$4"
new_backend="$5"
new_frontend="$6"
rollback_backend="$7"
old_frontend="$8"
secret_source="${9:-}"

[[ "$image_source_head" =~ ^[0-9a-f]{40}$ ]] || die "invalid image source commit"
[[ "$deploy_head" =~ ^[0-9a-f]{40}$ ]] || die "invalid deploy commit"
[[ "$old_head" =~ ^[0-9a-f]{40}$ ]] || die "invalid previous commit"
for ref in "$new_backend" "$new_frontend" "$rollback_backend" "$old_frontend"; do
    is_immutable_image "$ref" || die "all release images must use immutable sha256 aliases"
done

[ "$(readlink -f -- "$repo")" = "$repo" ]
cd "$repo"
[ "$(git rev-parse HEAD)" = "$deploy_head" ]
git diff --quiet
git diff --cached --quiet
[ "$(tr -d '\r\n' < VERSION)" = "$version" ]
bash scripts/verify-release-backup.sh "$pre_backup" "$old_head"
pre_backup="$(readlink -f -- "$pre_backup")"
command -v systemd-run >/dev/null
command -v flock >/dev/null
[ "$(df -Pk /var/lib/docker | awk 'NR==2 {print $4}')" -ge 1048576 ]

if [ -n "$secret_source" ]; then
    secret_source="$(readlink -f -- "$secret_source")"
    [ -f "$secret_source" ] && [ ! -L "$secret_source" ]
    [ "$(stat -c '%a' "$secret_source")" = 600 ] || die "staged backend secret must be mode 600"
fi

for ref in "$new_backend" "$new_frontend" "$rollback_backend" "$old_frontend"; do
    docker image inspect "$ref" >/dev/null
    id="$(docker image inspect "$ref" --format '{{.Id}}')"
    [ "$ref" = "${ref%:*}:sha256-${id#sha256:}" ] || die "immutable alias does not match image id"
done
[ "$(docker image inspect "$new_backend" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$image_source_head" ]
[ "$(docker image inspect "$new_backend" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')" = "$version" ]
[ "$(docker image inspect "$new_frontend" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$image_source_head" ]
[ "$(docker image inspect "$new_frontend" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')" = "$version" ]
[ "$(docker run --rm --network none --entrypoint cat "$rollback_backend" /app/VERSION | tr -d '\r\n')" = '4.6.3-agellar.1' ]
docker run --rm --network none --workdir /app --entrypoint python "$rollback_backend" -c \
    'from alembic.config import Config; from alembic.script import ScriptDirectory; assert ScriptDirectory.from_config(Config("/app/alembic.ini")).get_current_head() == "0105"'

grep -Fq "image: $new_backend" docker-compose.override.yml
grep -Fq "image: $new_frontend" docker-compose.override.yml
override_has_bot_zero docker-compose.override.yml
[ "$(docker compose ps -q --status running bot | wc -l)" -eq 0 ]
[ "$(docker inspect "$backend_container" --format '{{range .Config.Env}}{{if eq . "APP_MODE=full"}}yes{{end}}{{end}}')" = yes ]
[ "$(docker exec "$db_container" psql -U remnawave -d remnawave_bot -X -Atqc 'SELECT version_num FROM alembic_version')" = 0102 ]

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
run_dir="/root/remnawave-admin-deploys/${stamp}-4.7.1"
unit="remnawave-admin-471-rollback-${stamp}"
mkdir -m 700 -p "$run_dir"
cp -- docker-compose.override.yml "$run_dir/candidate-compose.override.yml"
make_rollback_override "$pre_backup/docker-compose.override.yml" \
    "$run_dir/rollback-compose.override.yml" "$rollback_backend"
grep -Fq "image: $rollback_backend" "$run_dir/rollback-compose.override.yml"
grep -Fq "image: $old_frontend" "$run_dir/rollback-compose.override.yml"
override_has_bot_zero "$run_dir/rollback-compose.override.yml"

if [ -e "$secret_target" ]; then
    [ -f "$secret_target" ] && [ ! -L "$secret_target" ]
    cp -p -- "$secret_target" "$run_dir/backend-secret.before"
    touch "$run_dir/backend-secret.preexisted"
fi

snapshot_no_send() {
    docker exec -i "$db_container" sh -lc \
        'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -qAt -F "|"' <<'SQL'
SELECT count(*),
       count(*) FILTER (WHERE dry_run),
       count(*) FILTER (WHERE NOT dry_run),
       coalesce(sum(sent), 0),
       count(*) FILTER (WHERE broadcast_id IS NOT NULL),
       (SELECT count(*) FROM retention_radar_sends),
       (SELECT count(*) FROM scheduled_tasks WHERE is_enabled)
FROM retention_radar_campaigns;
SQL
}

snapshot_no_send > "$run_dir/no-send.before"
[[ "$(cat "$run_dir/no-send.before")" =~ ^[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|0$ ]]
docker ps -a --format '{{.Names}} {{.ID}} {{.Image}}' \
    | awk '$1 != "remnawave-web-backend" && $1 != "remnawave-web-frontend"' \
    | sort > "$run_dir/unaffected.before"

cat > "$run_dir/rollback.sh" <<ROLLBACK
#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
run_dir='$run_dir'
repo='$repo'
[ "\$(readlink -f -- "\$run_dir")" = "\$run_dir" ]
exec 9> "\$run_dir/rollback.lock"
flock -n 9 || exit 0
if [ -e "\$run_dir/accepted" ] || [ -e "\$run_dir/rollback.complete" ]; then exit 0; fi
exec > "\$run_dir/rollback.log" 2>&1
cd "\$repo"
cp -- "\$run_dir/rollback-compose.override.yml" docker-compose.override.yml
if [ -e "\$run_dir/backend-secret.preexisted" ]; then
    install -m 600 -- "\$run_dir/backend-secret.before" '$secret_target'
else
    rm -f -- '$secret_target'
fi
docker compose up -d --no-deps --no-build --pull never web-backend web-frontend
test "\$(docker inspect '$backend_container' --format '{{.Image}}')" = '$(docker image inspect "$rollback_backend" --format '{{.Id}}')'
test "\$(docker inspect '$frontend_container' --format '{{.Image}}')" = '$(docker image inspect "$old_frontend" --format '{{.Id}}')'
healthy=false
for _ in \$(seq 1 60); do
    if [ "\$(docker inspect '$backend_container' --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')" = healthy ]; then
        healthy=true
        break
    fi
    sleep 3
done
test "\$healthy" = true
test "\$(docker exec '$db_container' psql -U remnawave -d remnawave_bot -X -Atqc 'SELECT version_num FROM alembic_version')" = 0105
test "\$(docker inspect '$backend_container' --format '{{range .Config.Env}}{{if eq . "APP_MODE=full"}}yes{{end}}{{end}}')" = yes
docker exec -i '$backend_container' python - <<'PY'
import json
import urllib.request
with urllib.request.urlopen("http://127.0.0.1:8081/api/v2/health", timeout=10) as response:
    payload = json.load(response)
assert response.status == 200
assert payload.get("status") == "ok"
assert payload.get("version") == "4.6.3-agellar.1"
PY
docker exec '$frontend_container' wget -q -U 'Mozilla/5.0' -O /dev/null http://127.0.0.1/
docker exec -i '$backend_container' python - <<'PY'
import urllib.request
with urllib.request.urlopen("http://bedolaga-tunnel:8080/health", timeout=10) as response:
    assert response.status == 200
PY
test "\$(docker compose ps -q --status running bot | wc -l)" -eq 0
docker exec -i '$db_container' sh -lc \
    'exec psql -v ON_ERROR_STOP=1 -U "\$POSTGRES_USER" -d "\$POSTGRES_DB" -X -qAt -F "|"' > "\$run_dir/no-send.rollback" <<'SQL'
SELECT count(*),
       count(*) FILTER (WHERE dry_run),
       count(*) FILTER (WHERE NOT dry_run),
       coalesce(sum(sent), 0),
       count(*) FILTER (WHERE broadcast_id IS NOT NULL),
       (SELECT count(*) FROM retention_radar_sends),
       (SELECT count(*) FROM scheduled_tasks WHERE is_enabled)
FROM retention_radar_campaigns;
SQL
cmp -s "\$run_dir/no-send.before" "\$run_dir/no-send.rollback"
printf 'ROLLBACK_PASS backend=4.6.3-schema0105 frontend=previous\n' > "\$run_dir/rollback.complete"
ROLLBACK
chmod 700 "$run_dir/rollback.sh"
systemd-run --quiet --unit="$unit" --on-active=10m \
    --timer-property=AccuracySec=1s --collect "$run_dir/rollback.sh"
printf 'rollback_unit=%s\nrun_dir=%s\n' "$unit" "$run_dir" > "$run_dir/guard.txt"

rollback_on_error() {
    rc=$?
    trap - ERR
    "$run_dir/rollback.sh" || true
    printf 'DEPLOY_FAILED rc=%s path=%s\n' "$rc" "$run_dir" >&2
    exit "$rc"
}
trap rollback_on_error ERR

if [ -n "$secret_source" ]; then
    install -m 600 -- "$secret_source" "$secret_target"
fi
docker compose up -d --no-deps --no-build --pull never web-backend web-frontend
new_backend_id="$(docker image inspect "$new_backend" --format '{{.Id}}')"
new_frontend_id="$(docker image inspect "$new_frontend" --format '{{.Id}}')"
[ "$(docker inspect "$backend_container" --format '{{.Image}}')" = "$new_backend_id" ]
[ "$(docker inspect "$frontend_container" --format '{{.Image}}')" = "$new_frontend_id" ]

healthy=false
for _ in $(seq 1 80); do
    if [ "$(docker inspect "$backend_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')" = healthy ]; then
        healthy=true
        break
    fi
    sleep 3
done
[ "$healthy" = true ]
[ "$(docker inspect "$backend_container" --format '{{.RestartCount}}')" -eq 0 ]
[ "$(docker inspect "$backend_container" --format '{{range .Config.Env}}{{if eq . "APP_MODE=full"}}yes{{end}}{{end}}')" = yes ]
[ "$(docker exec "$db_container" psql -U remnawave -d remnawave_bot -X -Atqc 'SELECT version_num FROM alembic_version')" = 0105 ]

docker exec -i "$backend_container" python - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8081/api/v2/health", timeout=10) as response:
    payload = json.load(response)
assert response.status == 200
assert payload.get("status") == "ok"
assert payload.get("version") == "4.7.1-agellar.1"
print("BACKEND_APPLICATION_HEALTH=PASS")
PY
docker exec "$frontend_container" wget -q -U 'Mozilla/5.0' -O /dev/null http://127.0.0.1/
docker exec -i "$backend_container" python - <<'PY'
import urllib.request

with urllib.request.urlopen("http://bedolaga-tunnel:8080/health", timeout=10) as response:
    assert response.status == 200
print("TUNNEL_APPLICATION_HEALTH=PASS")
PY

docker exec -i "$backend_container" python - <<'PY'
from web.backend.core.plugins import discover_plugins

expected = {"smart_support", "retention_radar", "block_radar", "incident_center", "live_flow"}
found = {item.id: item.version for item in discover_plugins()}
missing = expected - found.keys()
assert not missing, "expected plugin is not discoverable"
print("PLUGIN_DISCOVERY=PASS count=" + str(len(found)))
PY
started_at="$(docker inspect "$backend_container" --format '{{.State.StartedAt}}')"
[ "$(docker logs --since "$started_at" "$backend_container" 2>&1 | grep -Ec 'plugins\.build_failed|plugins\.invalid_parts|plugins\.api_version_mismatch' || true)" -eq 0 ]

history_age="$(docker exec "$db_container" psql -U remnawave -d remnawave_bot -X -Atqc \
    "SELECT coalesce(round(extract(epoch FROM now()-max(request_at)))::bigint,-1) FROM subscription_request_history")"
[[ "$history_age" =~ ^[0-9]+$ ]]
[ "$history_age" -le "${HISTORY_MAX_AGE_SECONDS:-900}" ]
connection_age="$(docker exec "$db_container" psql -U remnawave -d remnawave_bot -X -Atqc \
    "SELECT coalesce(round(extract(epoch FROM now()-max(connected_at)))::bigint,-1) FROM user_connections")"
[[ "$connection_age" =~ ^[0-9]+$ ]]
[ "$connection_age" -le "${HISTORY_MAX_AGE_SECONDS:-900}" ]
retention_fresh=false
retention_age=-1
for _ in $(seq 1 60); do
    retention_age="$(docker exec "$db_container" psql -U remnawave -d remnawave_bot -X -Atqc \
        "SELECT coalesce(round(extract(epoch FROM now()-max(computed_at)))::bigint,-1) FROM retention_radar_daily")"
    if [[ "$retention_age" =~ ^[0-9]+$ ]] \
        && [ "$retention_age" -le "${RETENTION_MAX_AGE_SECONDS:-300}" ]; then
        retention_fresh=true
        break
    fi
    sleep 2
done
[ "$retention_fresh" = true ]

[ "$(docker compose ps -q --status running bot | wc -l)" -eq 0 ]
snapshot_no_send > "$run_dir/no-send.after"
cmp -s "$run_dir/no-send.before" "$run_dir/no-send.after"
docker ps -a --format '{{.Names}} {{.ID}} {{.Image}}' \
    | awk '$1 != "remnawave-web-backend" && $1 != "remnawave-web-frontend"' \
    | sort > "$run_dir/unaffected.after"
cmp -s "$run_dir/unaffected.before" "$run_dir/unaffected.after"

touch "$run_dir/accepted"
systemctl stop "$unit.timer" >/dev/null 2>&1 || true
trap - ERR
printf 'DEPLOY_ACCEPTED version=%s migration=0105 no_send=PASS bot_scaled_zero=PASS app_mode=full subscription_age_seconds=%s connection_age_seconds=%s retention_age_seconds=%s path=%s\n' \
    "$version" "$history_age" "$connection_age" "$retention_age" "$run_dir"

#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
td="$(mktemp -d)"
trap 'rm -rf -- "$td"' EXIT

bash "$root/scripts/deploy-4.7.1-guarded.sh" --self-test

backup="$td/backup"
mkdir -m 700 "$backup"
printf '%040d\n' 0 > "$backup/git-head.txt"
printf '0102\n' > "$backup/migration.txt"
printf '1|1|0|0|0|0\n' > "$backup/no-send-baseline.txt"
printf 'verified\n' > "$backup/verification.txt"
for name in database.dump database.catalog.txt docker-compose.override.yml source.bundle docker-images.tar.gz; do
    printf 'fixture:%s\n' "$name" > "$backup/$name"
done
(
    cd "$backup"
    find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%P\0' \
        | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS
)
VERIFY_BACKUP_ALLOW_ANY_ROOT=1 \
    bash "$root/scripts/verify-release-backup.sh" --quick "$backup" "$(printf '%040d' 0)"

printf 'tampered\n' >> "$backup/database.dump"
if VERIFY_BACKUP_ALLOW_ANY_ROOT=1 \
    bash "$root/scripts/verify-release-backup.sh" --quick "$backup" >/dev/null 2>&1; then
    echo 'tampered manifest was accepted' >&2
    exit 1
fi

grep -Fq 'docker compose up -d --no-deps --no-build --pull never web-backend web-frontend' \
    "$root/scripts/deploy-4.7.1-guarded.sh"
! grep -Fq 'docker compose config' "$root/scripts/deploy-4.7.1-guarded.sh"
grep -Fq 'cmp -s "$run_dir/no-send.before" "$run_dir/no-send.after"' \
    "$root/scripts/deploy-4.7.1-guarded.sh"
grep -Fq 'docker compose ps -q --status running bot' \
    "$root/scripts/deploy-4.7.1-guarded.sh"

# The release-only secret file must stay out of Git/build context and be
# attached to the backend service only.
grep -Fxq '.env.backend-secrets' "$root/.gitignore"
grep -Fxq '.env.*' "$root/.dockerignore"
[ "$(grep -Fc -- '- .env.backend-secrets' "$root/docker-compose.override.yml")" -eq 1 ]
awk '
    /^  web-backend:[[:space:]]*(#.*)?$/ { inside = 1 }
    inside && /^  [A-Za-z0-9_.-]+:[[:space:]]*(#.*)?$/ && $0 !~ /^  web-backend:/ { inside = 0 }
    inside && /- \.env\.backend-secrets[[:space:]]*$/ { found++ }
    END { exit(found == 1 ? 0 : 1) }
' "$root/docker-compose.override.yml"

echo 'RELEASE_GUARDS_TEST=PASS'

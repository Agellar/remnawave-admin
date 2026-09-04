#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Verify the pre-release backup without restoring it.  The normal mode also
# streams the custom-format PostgreSQL dump through pg_restore inside the
# already-running database container; --quick is reserved for the self-test.

usage() {
    echo "usage: $0 [--quick] BACKUP_DIR [EXPECTED_GIT_HEAD]" >&2
    exit 64
}

quick=false
if [ "${1:-}" = "--quick" ]; then
    quick=true
    shift
fi
[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage

backup_dir="$(readlink -f -- "$1")"
expected_head="${2:-}"

[ -d "$backup_dir" ] && [ ! -L "$backup_dir" ]
if [ "${VERIFY_BACKUP_ALLOW_ANY_ROOT:-0}" != 1 ]; then
    case "$backup_dir" in
        /root/remnawave-admin-backups/*) ;;
        *) echo "backup path is outside the release backup root" >&2; exit 65 ;;
    esac
fi

required=(
    SHA256SUMS
    verification.txt
    database.dump
    database.catalog.txt
    docker-images.tar.gz
    docker-compose.override.yml
    git-head.txt
    migration.txt
    no-send-baseline.txt
    source.bundle
)
for name in "${required[@]}"; do
    [ -s "$backup_dir/$name" ] || {
        echo "backup file missing or empty: $name" >&2
        exit 66
    }
    [ ! -L "$backup_dir/$name" ] || {
        echo "backup file must not be a symlink: $name" >&2
        exit 67
    }
done

(cd "$backup_dir" && sha256sum --check --strict --status SHA256SUMS)
if grep -Eq 'FAILED|WARNING|NOT OK' "$backup_dir/verification.txt"; then
    echo "recorded backup verification contains a failure" >&2
    exit 68
fi

head_value="$(tr -d '\r\n' < "$backup_dir/git-head.txt")"
[[ "$head_value" =~ ^[0-9a-f]{40}$ ]]
if [ -n "$expected_head" ]; then
    [[ "$expected_head" =~ ^[0-9a-f]{40}$ ]]
    [ "$head_value" = "$expected_head" ]
fi
[[ "$(tr -d '\r\n' < "$backup_dir/migration.txt")" =~ ^[0-9]{4}$ ]]
[[ "$(tr -d '\r\n' < "$backup_dir/no-send-baseline.txt")" =~ ^[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+$ ]]

if [ "$quick" = false ]; then
    command -v docker >/dev/null
    command -v git >/dev/null
    git bundle verify "$backup_dir/source.bundle" >/dev/null 2>&1
    docker inspect remnawave-admin-db >/dev/null
    docker exec -i remnawave-admin-db pg_restore -l \
        < "$backup_dir/database.dump" >/dev/null
    docker exec -i remnawave-admin-db pg_restore --file=/dev/null \
        < "$backup_dir/database.dump" >/dev/null

    gzip -t "$backup_dir/docker-images.tar.gz"
    tar -tzf "$backup_dir/docker-images.tar.gz" >/dev/null
fi

printf 'BACKUP_VERIFY_PASS path=%s head=%s\n' "$backup_dir" "$head_value"

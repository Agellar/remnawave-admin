"""Isolated shell checks: no Docker daemon, host paths, or network are used."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
TARGET_DIGEST = "sha256:830c5a33a8c5d0bcd784be78264663a278a57dd7469926ea24c5b8966a243e71"
TARGET_REF = f"ghcr.io/case211/remnawave-admin-node-agent@{TARGET_DIGEST}"
OLD_REF = "test/agent:old"
OLD_IMAGE_ID = "sha256:old-test-image"
TARGET_IMAGE_ID = "sha256:target-test-image"


def _effective_compose_guards():
    guards = []
    for name in ("node-agent-1.8.1-preflight.sh", "update-node-agent-1.8.1.sh", "node-agent-1.8.1-verify.sh"):
        for line_number, line in enumerate((SCRIPTS / name).read_text(encoding="utf-8").splitlines(), 1):
            if "cfg=json.load(sys.stdin)" in line:
                guards.append((f"{name}:{line_number}", line.split(" -c '", 1)[1].split("'", 1)[0]))
    assert len(guards) == 5  # Includes the detached runner and isolated rollback guard.
    return guards


def _run_effective_compose_guard(code, environment, monkeypatch):
    config = types.ModuleType("src.config")
    config.Settings = types.SimpleNamespace(
        model_fields={"ndpi_enabled": types.SimpleNamespace(default=False)},
    )
    monkeypatch.setitem(sys.modules, "src.config", config)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "services": {"node-agent": {"environment": {"SECRET": "must-not-be-logged", **environment}}},
    })))
    exec(code, {})


@pytest.mark.parametrize(("script", "code"), _effective_compose_guards())
@pytest.mark.parametrize("environment", [
    {"agent_ndpi_enabled": "true"},
    {"Agent_NdPi_EnAbLeD": "true"},
    {"AGENT_NDPI_ENABLED": "false", "agent_ndpi_enabled": "true"},
    {"AGENT_NDPI_ENABLED": "false", "Agent_NdPi_Enabled": "false"},
])
def test_all_effective_guards_reject_case_variant_enable_and_duplicates(script, code, environment, monkeypatch, capsys):
    with pytest.raises(AssertionError):
        _run_effective_compose_guard(code, environment, monkeypatch)
    output = capsys.readouterr()
    assert "must-not-be-logged" not in output.out + output.err, script


@pytest.mark.parametrize(("script", "code"), _effective_compose_guards())
@pytest.mark.parametrize("key", ["agent_ndpi_enabled", "Agent_NdPi_EnAbLeD"])
def test_all_effective_guards_accept_one_case_variant_false(script, code, key, monkeypatch, capsys):
    _run_effective_compose_guard(code, {key: "false"}, monkeypatch)
    output = capsys.readouterr()
    assert output.out == output.err == "", script


def _bash() -> str:
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    executable = str(git_bash) if git_bash.exists() else shutil.which("bash")
    if not executable:
        pytest.skip("bash is unavailable")
    return executable


def _shell_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt":
        return f"/{value[0].lower()}{value[2:]}"
    return value


FAKE_DOCKER = r'''#!/bin/sh
set -eu
current_ref() { cat "$TEST_STATE/current_ref"; }
up_count() { cat "$TEST_STATE/up_count"; }
container_id() { printf 'test-container-%s\n' "$(up_count)"; }
compose_ref() {
  source=docker-compose.yml
  if [ -f docker-compose.override.yml ] && grep -q 'image:' docker-compose.override.yml; then
    source=docker-compose.override.yml
  fi
  awk '/^[[:space:]]*image:/ {print $2}' "$source"
}
case "$1" in
  info)
    [ "$2" = --format ] && [ "$3" = '{{.DockerRootDir}}' ] || exit 99
    printf '%s\n' "$TEST_DOCKER_ROOT"
    ;;
  compose)
    case "$2" in
      config)
        case "$3" in
          --services) echo 'node-agent' ;;
          --images) compose_ref ;;
          -q) exit 0 ;;
          --format)
            [ "$4" = json ] || exit 97
            effective_ndpi="${TEST_COMPOSE_NDPI:-false}"
            if [ -f "$TEST_STATE/effective_ndpi" ]; then effective_ndpi="$(cat "$TEST_STATE/effective_ndpi")"; fi
            if [ "$effective_ndpi" = absent ]; then
              printf '{"services":{"node-agent":{"environment":{"SECRET":"must-not-be-logged"}}}}'
            else
              printf '{"services":{"node-agent":{"environment":{"AGENT_NDPI_ENABLED":"%s","SECRET":"must-not-be-logged"}}}}' "$effective_ndpi"
            fi
            ;;
          *) exit 97 ;;
        esac
        ;;
      ps)
        [ "$3" = -q ] && [ "$4" = node-agent ] || exit 98
        n="$(cat "$TEST_STATE/binding_count")"
        n=$((n + 1))
        printf '%s\n' "$n" > "$TEST_STATE/binding_count"
        if [ "${TEST_BINDING_FAIL_AT:-0}" -gt 0 ] && [ "$n" -ge "$TEST_BINDING_FAIL_AT" ]; then
          echo 'foreign-container'
        elif [ "${TEST_BINDING_TARGET_WRONG:-0}" = 1 ] && [ "$(current_ref)" = "$TEST_TARGET_REF" ]; then
          echo 'foreign-container'
        else
          case "${TEST_BINDING:-match}" in
            none) ;;
            wrong) echo 'foreign-container' ;;
            multiple) printf 'test-container-0\nforeign-container\n' ;;
            *) container_id ;;
          esac
        fi
        ;;
      up)
        if [ "$3" = '--help' ]; then
          echo ' --pull policy'
          exit 0
        fi
        printf 'compose_up\n' >> "$TEST_STATE/mutations"
        printf '%s\n' "$*" >> "$TEST_STATE/up_args"
        n="$(up_count)"
        printf '%s\n' "$((n + 1))" > "$TEST_STATE/up_count"
        compose_ref > "$TEST_STATE/current_ref"
        ;;
      *) exit 96 ;;
    esac
    ;;
  inspect)
    if [ "$#" -eq 2 ]; then
      echo '[]'
      exit 0
    fi
    case "$4" in
      *Config.Image*) current_ref ;;
      *State.Status*) echo 'running' ;;
      *State.Health*) echo "${TEST_HEALTH:-healthy}" ;;
      '{{.Id}}') container_id ;;
      *Image*)
        if [ "$(current_ref)" = "$TEST_TARGET_REF" ]; then
          echo "${TEST_TARGET_IMAGE_ID:-sha256:target-test-image}"
        elif [ "${TEST_BAD_ROLLBACK_ID:-0}" = 1 ] && [ "$(up_count)" -gt 0 ]; then
          echo 'sha256:wrong-rollback-image'
        else
          echo 'sha256:old-test-image'
        fi
        ;;
      *) exit 95 ;;
    esac
    ;;
  exec)
    if [ "$2" = -i ]; then
      # Execute the real inline JSON guard with only src.config substituted.
      "$TEST_PYTHON" -c 'import os,sys,types; m=types.ModuleType("src.config"); m.Settings=types.SimpleNamespace(model_fields={"ndpi_enabled":types.SimpleNamespace(default=os.getenv("TEST_DEFAULT_NDPI", "false") == "true")}); sys.modules["src.config"]=m; exec(sys.argv[1])' "$6"
      exit "$?"
    fi
    case "$5" in
      *collector/health*)
        n="$(cat "$TEST_STATE/probe_count")"
        n=$((n + 1))
        printf '%s\n' "$n" > "$TEST_STATE/probe_count"
        [ "${TEST_COLLECTOR_FAIL_AT:-0}" -eq 0 ] || [ "$n" -lt "$TEST_COLLECTOR_FAIL_AT" ]
        ;;
      *'assert not Settings().ndpi_enabled'*) [ "${TEST_NDPI_ON:-0}" = 0 ] ;;
      *AGENT_VERSION*)
        if [ "$(current_ref)" = "$TEST_TARGET_REF" ]; then echo '1.8.1'; else echo '1.8.0'; fi
        ;;
      *'for process in nDPId nDPIsrvd'*) [ "${TEST_NDPI_ON:-0}" = 0 ] ;;
      *'pgrep -x nDPId'*'pgrep -x nDPIsrvd'*) [ "${TEST_NDPI_ON:-0}" = 1 ] ;;
      *'command -v nDPId'*) exit 0 ;;
      *) exit 94 ;;
    esac
    ;;
  image)
    if [ "$#" -eq 3 ]; then exit 0; fi
    case "$5" in
      *RepoDigests*) echo "$TEST_TARGET_REF" ;;
      *Id*) echo 'sha256:target-test-image' ;;
      *) exit 93 ;;
    esac
    ;;
  logs) printf 'Collector API OK\nAgent v2 WS connected\n' ;;
  tag) printf 'tag\n' >> "$TEST_STATE/mutations" ;;
  pull) printf 'pull\n' >> "$TEST_STATE/mutations" ;;
  run)
    printf 'run\n' >> "$TEST_STATE/mutations"
    case "$*" in
      *json.load*)
        while [ "$1" != -c ]; do shift; done
        "$TEST_PYTHON" -c 'import os,sys,types; m=types.ModuleType("src.config"); m.Settings=types.SimpleNamespace(model_fields={"ndpi_enabled":types.SimpleNamespace(default=os.getenv("TEST_DEFAULT_NDPI", "false") == "true")}); sys.modules["src.config"]=m; exec(sys.argv[1])' "$2"
        ;;
      *AGENT_VERSION*) echo '1.8.1' ;;
      *) exit 0 ;;
    esac
    ;;
  *) exit 92 ;;
esac
'''


@pytest.fixture
def shell_harness(tmp_path):
    install = tmp_path / "agent"
    backups = tmp_path / "backups"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    docker_root = tmp_path / "docker-data-custom"
    for directory in (install, state, fake_bin, docker_root):
        directory.mkdir()
    original = f"services:\n  node-agent:\n    image: {OLD_REF}\n"
    (install / "docker-compose.yml").write_text(original, encoding="utf-8")
    (install / ".env").write_text("AGENT_NDPI_ENABLED=false\n", encoding="utf-8")
    for name, value in {
        "current_ref": OLD_REF, "up_count": "0", "probe_count": "0", "binding_count": "0", "mutations": "",
    }.items():
        (state / name).write_text(value, encoding="utf-8")
    commands = {
        "docker": FAKE_DOCKER,
        "df": "#!/bin/sh\nprintf '%s\\n' \"$@\" >> \"$TEST_STATE/df_args\"\nprintf 'Filesystem 1024-blocks Used Available Capacity Mounted\\nmock 9999999 1 2097152 1%% /mock\\n'\n",
        "sleep": "#!/bin/sh\nexit 0\n",
        "systemd-run": """#!/bin/sh
[ "${TEST_SYSTEMD_FAIL:-0}" = 0 ] || exit 1
printf '%s\\n' "$@" > "$TEST_STATE/systemd_args"
no_block=false
while [ "$1" != /bin/sh ]; do
  [ "$1" != --no-block ] || no_block=true
  shift
done
# Type=oneshot without --no-block is deliberately rejected; it is not detached.
[ "$no_block" = true ] || exit 91
(
  if [ "${TEST_HOLD_RUNNER:-0}" = 1 ]; then
    while [ ! -f "$TEST_STATE/release_runner" ]; do /bin/sleep 0.02; done
  fi
  "$@" > "$TEST_STATE/runner.log" 2>&1
  printf '%s\\n' "$?" > "$TEST_STATE/runner_exit"
) </dev/null >/dev/null 2>&1 &
printf '%s\\n' "$!" > "$TEST_STATE/runner_pid"
exit 0
""",
    }
    for name, content in commands.items():
        executable = fake_bin / name
        executable.write_text(content, encoding="utf-8", newline="\n")
        executable.chmod(0o755)

    def run(name="update-node-agent-1.8.1.sh", *, wait_for_runner=True, **options):
        script = tmp_path / name
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        source = source.replace("/opt/remnawave-node-agent", _shell_path(install))
        source = source.replace("/root/remnawave-node-agent-backups", _shell_path(backups))
        script.write_text(source, encoding="utf-8", newline="\n")
        env = {
            **os.environ,
            "TEST_STATE": _shell_path(state),
            "TEST_TARGET_REF": TARGET_REF,
            "TEST_PYTHON": _shell_path(Path(sys.executable)),
            "TEST_DOCKER_ROOT": _shell_path(docker_root),
            **{key: str(value) for key, value in options.items()},
        }
        result = subprocess.run(
            [_bash(), "-c", 'export PATH="$1:$PATH"; exec /bin/sh "$2"',
             "isolated-agent-test", _shell_path(fake_bin), _shell_path(script)],
            capture_output=True, text=True, env=env, timeout=45,
        )
        if wait_for_runner and (state / "runner_pid").exists():
            _wait_runner(state)
        return result

    yield install, backups, state, original, run
    (state / "release_runner").touch()
    if (state / "runner_pid").exists():
        _wait_runner(state)


def _wait_runner(state: Path) -> None:
    deadline = time.monotonic() + 35
    while not (state / "runner_exit").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert (state / "runner_exit").exists(), "isolated runner did not finish"


@pytest.mark.parametrize("name", [
    "node-agent-1.8.1-preflight.sh", "update-node-agent-1.8.1.sh", "node-agent-1.8.1-verify.sh",
])
def test_agent_181_scripts_are_lf_and_valid_shell(name):
    raw = (SCRIPTS / name).read_bytes()
    assert b"\r" not in raw
    result = subprocess.run([_bash(), "-n", str(SCRIPTS / name)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("options", "error"), [
    ({"TEST_HEALTH": "unhealthy"}, "live_agent_unhealthy_or_changed"),
    ({"TEST_COLLECTOR_FAIL_AT": 1}, "live_collector_probe_failed"),
    ({"TEST_NDPI_ON": 1}, "live_ndpi_not_confirmed_off"),
])
def test_updater_rechecks_health_collector_and_ndpi_before_any_mutation(shell_harness, options, error):
    install, backups, state, original, run = shell_harness
    result = run(**options)
    assert result.returncode != 0
    assert error in result.stdout
    assert not backups.exists()
    assert (state / "mutations").read_text() == ""
    assert (install / "docker-compose.yml").read_text() == original


def test_preflight_does_not_report_pass_when_collector_fails(shell_harness):
    _, backups, state, _, run = shell_harness
    result = run("node-agent-1.8.1-preflight.sh", TEST_COLLECTOR_FAIL_AT=1)
    assert result.returncode != 0
    assert "collector_probe=false" in result.stdout
    assert "preflight=pass" not in result.stdout
    assert not backups.exists()
    assert (state / "mutations").read_text() == ""


@pytest.mark.parametrize("name", ["node-agent-1.8.1-preflight.sh", "update-node-agent-1.8.1.sh"])
@pytest.mark.parametrize(("filename", "error"), [
    ("docker-compose.yml", "compose_not_regular_file"),
    (".env", "env_not_regular_file"),
    ("docker-compose.override.yml", "override_not_regular_file"),
])
def test_config_symlinks_are_rejected_before_backup(shell_harness, name, filename, error):
    install, backups, state, _, run = shell_harness
    original = install / filename
    destination = install / f"{filename}.actual"
    if original.exists():
        original.rename(destination)
    else:
        destination.write_text(f"services:\n  node-agent:\n    image: {OLD_REF}\n", encoding="utf-8")
    try:
        original.symlink_to(destination)
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"test host cannot create symbolic links: {exc.errno}")
        # Git Bash supports MSYS/Cygwin symlinks even without Windows Developer
        # Mode. The shell's real -L check is exercised, not a mocked predicate.
        # https://www.msys2.org/docs/symlinks/
        created = subprocess.run(
            [_bash(), "-c", 'ln -s "$1" "$2" && test -L "$2"', "make-test-link",
             _shell_path(destination), f"{_shell_path(install)}/{filename}"],
            capture_output=True, text=True, env={**os.environ, "MSYS": "winsymlinks:sys"},
        )
        if created.returncode != 0:
            pytest.skip("test host cannot create native or MSYS symbolic links")
    result = run(name)
    assert result.returncode != 0
    assert error in result.stdout
    assert not backups.exists()
    assert (state / "mutations").read_text() == ""
    is_link = subprocess.run(
        [_bash(), "-c", 'test -L "$1"', "check-test-link", f"{_shell_path(install)}/{filename}"],
        capture_output=True,
    )
    assert is_link.returncode == 0


@pytest.mark.parametrize("name", ["node-agent-1.8.1-preflight.sh", "update-node-agent-1.8.1.sh"])
def test_disk_probe_uses_actual_docker_root_directory(shell_harness, name):
    install, _, state, _, run = shell_harness
    result = run(name)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (state / "df_args").read_text().splitlines() == [
        "-Pk", "--", _shell_path(install.parent / "docker-data-custom"),
    ]


def test_missing_docker_root_fails_before_updater_mutation(shell_harness):
    install, backups, state, _, run = shell_harness
    result = run(TEST_DOCKER_ROOT=_shell_path(install.parent / "missing-docker-data"))
    assert result.returncode != 0
    assert "docker_root_missing" in result.stdout
    assert not backups.exists()
    assert (state / "mutations").read_text() == ""


def test_slow_pull_cannot_bypass_fresh_collector_gate(shell_harness):
    install, backups, state, original, run = shell_harness
    result = run(TEST_COLLECTOR_FAIL_AT=2)
    assert result.returncode != 0
    assert "live_collector_probe_failed" in result.stdout
    assert backups.exists()
    assert "pull" in (state / "mutations").read_text()
    assert "compose_up" not in (state / "mutations").read_text()
    assert (install / "docker-compose.yml").read_text() == original


def test_failed_detach_restores_compose_without_restarting(shell_harness):
    install, _, state, original, run = shell_harness
    result = run(TEST_SYSTEMD_FAIL=1)
    assert result.returncode != 0
    assert "detached_runner_unavailable" in result.stdout
    assert "compose_up" not in (state / "mutations").read_text()
    assert (install / "docker-compose.yml").read_text() == original


def test_no_block_returns_before_oneshot_restarts_the_calling_container(shell_harness):
    install, backups, state, _, run = shell_harness
    result = run(wait_for_runner=False, TEST_HOLD_RUNNER=1)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "scheduled=true" in result.stdout
    assert "--no-block" in (state / "systemd_args").read_text().splitlines()
    assert "--property=Type=oneshot" in (state / "systemd_args").read_text().splitlines()
    assert not (state / "runner_exit").exists()
    assert (state / "up_count").read_text().strip() == "0"
    assert f"image: {TARGET_REF}" in (install / "docker-compose.yml").read_text()
    (state / "release_runner").touch()
    _wait_runner(state)
    assert "result=success" in next(backups.glob("*/status.txt")).read_text()


@pytest.mark.parametrize("name", ["node-agent-1.8.1-preflight.sh", "update-node-agent-1.8.1.sh"])
@pytest.mark.parametrize("binding", ["none", "wrong", "multiple"])
def test_wrong_compose_project_is_rejected_before_mutation(shell_harness, name, binding):
    install, backups, state, original, run = shell_harness
    result = run(name, TEST_BINDING=binding)
    assert result.returncode != 0
    assert "compose_container_mismatch" in result.stdout
    assert not backups.exists()
    assert (state / "mutations").read_text() == ""
    assert (install / "docker-compose.yml").read_text() == original


@pytest.mark.parametrize("name", ["node-agent-1.8.1-preflight.sh", "update-node-agent-1.8.1.sh"])
def test_pending_compose_ndpi_enable_is_rejected_without_leaking_config(shell_harness, name):
    _, backups, state, _, run = shell_harness
    result = run(name, TEST_COMPOSE_NDPI="true")
    assert result.returncode != 0
    assert "effective_compose_ndpi_not_confirmed_off" in result.stdout
    assert "must-not-be-logged" not in result.stdout + result.stderr
    assert not backups.exists()
    assert (state / "mutations").read_text() == ""


@pytest.mark.parametrize("default", ["false", "true"])
def test_missing_ndpi_compose_key_requires_confirmed_false_source_default(shell_harness, default):
    _, _, state, _, run = shell_harness
    result = run("node-agent-1.8.1-preflight.sh", TEST_COMPOSE_NDPI="absent", TEST_DEFAULT_NDPI=default)
    assert (result.returncode == 0) is (default == "false")
    assert "must-not-be-logged" not in result.stdout + result.stderr
    assert (state / "mutations").read_text() == ""


def test_detached_runner_rechecks_effective_env_before_stop(shell_harness):
    install, backups, state, original, run = shell_harness
    result = run(wait_for_runner=False, TEST_HOLD_RUNNER=1)
    assert result.returncode == 0, result.stdout + result.stderr
    (state / "effective_ndpi").write_text("true", encoding="utf-8")
    (state / "release_runner").touch()
    _wait_runner(state)
    assert "result=aborted" in next(backups.glob("*/status.txt")).read_text()
    assert "compose_up" not in (state / "mutations").read_text()
    assert (install / "docker-compose.yml").read_text() == original


def test_detached_runner_rechecks_compose_owner_before_stop(shell_harness):
    install, backups, state, original, run = shell_harness
    result = run(TEST_BINDING_FAIL_AT=3)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "result=aborted" in next(backups.glob("*/status.txt")).read_text()
    assert "compose_up" not in (state / "mutations").read_text()
    assert (install / "docker-compose.yml").read_text() == original


def test_detached_runner_aborts_before_restart_if_collector_changed(shell_harness):
    install, backups, state, original, run = shell_harness
    result = run(TEST_COLLECTOR_FAIL_AT=3)
    assert result.returncode == 0, result.stdout + result.stderr
    status = next(backups.glob("*/status.txt")).read_text()
    assert "result=aborted" in status
    assert "existing_container_preserved=true" in status
    assert "compose_up" not in (state / "mutations").read_text()
    assert (install / "docker-compose.yml").read_text() == original


def test_success_pins_exact_image_and_passes_independent_verify(shell_harness):
    install, backups, state, _, run = shell_harness
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    status = next(backups.glob("*/status.txt")).read_text()
    assert "result=success" in status
    assert f"image={TARGET_REF}" in status
    assert f"image_id={TARGET_IMAGE_ID}" in status
    assert f"image: {TARGET_REF}" in (install / "docker-compose.yml").read_text()
    assert (state / "up_count").read_text().strip() == "1"
    assert "--no-deps --force-recreate --pull never node-agent" in (state / "up_args").read_text()
    verification = run("node-agent-1.8.1-verify.sh")
    assert verification.returncode == 0, verification.stdout + verification.stderr
    assert "ndpi_off=true" in verification.stdout
    assert "rollout_status=success" in verification.stdout


def test_changed_compose_owner_after_recreate_is_not_reported_as_success(shell_harness):
    install, backups, state, original, run = shell_harness
    result = run(TEST_BINDING_TARGET_WRONG=1)
    assert result.returncode == 0, result.stdout + result.stderr
    status = next(backups.glob("*/status.txt")).read_text()
    assert "result=rolled_back" in status
    assert "reason=target_compose_container_mismatch" in status
    assert (install / "docker-compose.yml").read_text() == original
    assert (state / "up_count").read_text().strip() == "2"


def test_independent_verify_rejects_wrong_compose_container(shell_harness):
    _, _, _, _, run = shell_harness
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    verification = run("node-agent-1.8.1-verify.sh", TEST_BINDING="wrong")
    assert verification.returncode != 0
    assert "compose_container_mismatch" in verification.stdout


@pytest.mark.parametrize("wrong_rollback", [False, True])
def test_wrong_target_image_rolls_back_and_validates_actual_old_id(shell_harness, wrong_rollback):
    install, backups, state, original, run = shell_harness
    result = run(TEST_TARGET_IMAGE_ID="sha256:wrong-target-image", TEST_BAD_ROLLBACK_ID=int(wrong_rollback))
    assert result.returncode == 0, result.stdout + result.stderr
    status = next(backups.glob("*/status.txt")).read_text()
    assert (install / "docker-compose.yml").read_text() == original
    assert (state / "up_count").read_text().strip() == "2"
    if wrong_rollback:
        assert "result=rollback_failed" in status
        assert "stage=old_image_id_mismatch" in status
    else:
        assert "result=rolled_back" in status
        assert f"image_id={OLD_IMAGE_ID}" in status

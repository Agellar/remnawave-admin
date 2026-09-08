"""Focused security and load-safety checks for the local Live Flow fork."""
from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import sys
import textwrap
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from rwa_live_flow import FORK_VERSION, UPSTREAM_COMMIT, UPSTREAM_VERSION
from rwa_live_flow import data as D
from rwa_live_flow import poller as P
from rwa_live_flow import routes as R
from rwa_live_flow import scope as S
from rwa_live_flow.module import MODULE_JS
from rwa_live_flow.rate_limit import SlidingWindowLimiter


NODE = "00000000-0000-0000-0000-000000000001"
OTHER_NODE = "00000000-0000-0000-0000-000000000002"
USER = "10000000-0000-0000-0000-000000000001"


class _Logger:
    def warning(self, *_args, **_kwargs):
        pass

    def exception(self, *_args, **_kwargs):
        pass

    def info(self, *_args, **_kwargs):
        pass


@pytest.mark.asyncio
async def test_scope_superadmin_and_legacy_are_explicitly_unrestricted(monkeypatch):
    def should_not_resolve():
        raise AssertionError("scope provider must not run for privileged admins")

    monkeypatch.setattr(S, "_scope_providers", should_not_resolve)
    ctx = SimpleNamespace(db=None, logger=_Logger())

    superadmin = SimpleNamespace(account_id=7, role="superadmin", role_id=1)
    legacy = SimpleNamespace(account_id=None, role="admin", role_id=None)
    assert (await S.resolve_access_scope(ctx, superadmin)).privileged is True
    assert (await S.resolve_access_scope(ctx, legacy)).privileged is True


@pytest.mark.asyncio
async def test_regular_admin_scope_intersects_nodes_and_visible_users(monkeypatch):
    calls = []

    async def get_scope(*args):
        calls.append(("nodes", args))
        return {NODE.upper()}

    async def get_visible(*args):
        calls.append(("users", args))
        return {USER.upper()}

    class DB:
        async def fetch(self, query, *args):
            assert "uuid = ANY($1::uuid[])" in query
            assert args == ([USER],)
            return [{"id": 41}]

    monkeypatch.setattr(S, "_scope_providers", lambda: (get_scope, get_visible))
    admin = SimpleNamespace(account_id=9, role_id=3, role="operator")
    result = await S.resolve_access_scope(SimpleNamespace(db=DB(), logger=_Logger()), admin)

    assert result.node_uuids == frozenset({NODE})
    assert result.user_uuids == frozenset({USER})
    assert result.panel_user_ids == frozenset({"41"})
    assert calls == [
        ("nodes", (9, 3, "operator", "node", "view")),
        ("users", (9, "operator")),
    ]
    assert result.filter_active(
        [
            ("41", {"node_uuid": NODE}),
            ("42", {"node_uuid": NODE}),
            ("41", {"node_uuid": OTHER_NODE}),
        ]
    ) == [("41", {"node_uuid": NODE})]


@pytest.mark.asyncio
async def test_regular_admin_scope_resolution_failure_is_deny_all(monkeypatch):
    async def explode(*_args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(S, "_scope_providers", lambda: (explode, explode))
    admin = SimpleNamespace(account_id=9, role_id=3, role="operator")
    result = await S.resolve_access_scope(SimpleNamespace(db=None, logger=_Logger()), admin)
    assert result == S.AccessScope.deny_all()
    assert result.allows_node(NODE) is False
    assert result.filter_active([("1", {"node_uuid": NODE})]) == []


@pytest.mark.asyncio
async def test_disallowed_node_does_not_probe_database():
    class DB:
        async def fetchrow(self, *_args, **_kwargs):
            raise AssertionError("hidden node lookup leaked through to DB")

    ctx = SimpleNamespace(db=DB(), logger=_Logger())
    result = await D.node_users(ctx, NODE, S.AccessScope.deny_all())
    assert result is None


@pytest.mark.asyncio
async def test_live_node_detail_is_scoped_and_paged_by_users():
    base = datetime.now(timezone.utc)
    active = [
        (str(uid), {"node_uuid": NODE, "online_at": base + timedelta(seconds=uid), "username": f"u{uid}"})
        for uid in range(1, 6)
    ]

    class Poller:
        nodes = {NODE: {"users_online": 999, "name": "visible"}}
        truncated = True

        def active_users(self):
            return active

        def user_bps(self, _uid):
            return 0.0

        def online_ref_iso(self):
            return base.isoformat()

    class DB:
        async def fetch(self, query, *args):
            if "SELECT id, uuid::text AS uuid FROM users" in query:
                return [
                    {"id": uid, "uuid": f"10000000-0000-0000-0000-{uid:012d}"}
                    for uid in args[0]
                ]
            assert "row_number() OVER" in query
            assert args[1] == D.MAX_IPS_PER_USER
            return []

    scope = S.AccessScope(
        node_uuids=frozenset({NODE}),
        user_uuids=frozenset({USER}),
        panel_user_ids=frozenset({"1", "2", "3", "4"}),
    )
    result = await D._node_users_live(
        SimpleNamespace(db=DB()),
        {"uuid": NODE, "name": "visible", "users_online": 999},
        NODE,
        Poller(),
        scope,
        page=2,
        limit=2,
    )
    assert result["count"] == 4
    assert len(result["users"]) == 2
    assert result["node"]["users_online"] == 4
    assert result["node"]["counter_scoped"] is True
    assert result["page"] == 2 and result["has_more"] is False
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_poller_never_requests_or_keeps_past_cap(monkeypatch):
    users = [{"id": uid, "userTraffic": {}} for uid in range(1, 11)]

    class API:
        def __init__(self):
            self.calls = []

        async def get_nodes(self, **_kwargs):
            return {"response": []}

        async def get_users(self, *, start, size, **_kwargs):
            self.calls.append((start, size))
            return {"response": {"users": users[start:start + size], "total": len(users)}}

    api = API()
    for name in ("web", "web.backend", "web.backend.core"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    facade = types.ModuleType("web.backend.core.plugin_api")
    facade.panel_api = lambda: api
    monkeypatch.setitem(sys.modules, "web.backend.core.plugin_api", facade)
    monkeypatch.setattr(P, "USERS_PAGE", 2)
    monkeypatch.setattr(P, "MAX_USERS", 3)

    poller = P.PanelPoller()
    await poller._tick_impl(_Logger())
    assert api.calls == [(0, 2), (2, 1)]
    assert len(poller.users) == 3
    assert poller.truncated is True


def test_poller_env_bounds_and_safe_defaults(monkeypatch):
    name = "RWA_LIVE_FLOW_TEST_BOUND"
    monkeypatch.delenv(name, raising=False)
    assert P._bounded_env_int(name, 5000, 500, 20000) == 5000
    monkeypatch.setenv(name, "1")
    assert P._bounded_env_int(name, 5000, 500, 20000) == 500
    monkeypatch.setenv(name, "999999")
    assert P._bounded_env_int(name, 5000, 500, 20000) == 20000
    monkeypatch.setenv(name, "not-an-int")
    assert P._bounded_env_int(name, 5000, 500, 20000) == 5000
    assert 250 <= P.USERS_PAGE <= 500
    assert 500 <= P.MAX_USERS <= 20000


@pytest.mark.asyncio
async def test_sliding_window_rate_limit_is_deterministic():
    limiter = SlidingWindowLimiter()
    assert await limiter.check("admin:1", "users", limit=2, window_s=1.0, now=0.0) == 0
    assert await limiter.check("admin:1", "users", limit=2, window_s=1.0, now=0.1) == 0
    assert await limiter.check("admin:1", "users", limit=2, window_s=1.0, now=0.2) == pytest.approx(0.8)
    assert await limiter.check("admin:1", "users", limit=2, window_s=1.0, now=1.01) == 0


def test_can_view_users_contract_is_synchronous_and_superadmin_safe():
    assert inspect.iscoroutinefunction(R._can_view_users) is False
    assert R._can_view_users(SimpleNamespace(account_id=None, role="admin")) is True
    assert R._can_view_users(SimpleNamespace(account_id=1, role="superadmin")) is True
    allowed = SimpleNamespace(account_id=2, role="operator", has_permission=lambda r, a: (r, a) == ("live_flow", "view_users"))
    denied = SimpleNamespace(account_id=3, role="operator", has_permission=lambda _r, _a: False)
    assert R._can_view_users(allowed) is True
    assert R._can_view_users(denied) is False


def test_node_uuid_validation_rejects_non_uuid_and_injection_shapes():
    assert R._UUID_RE.fullmatch(NODE)
    for value in ("", "1", NODE + " OR true", "../../etc/passwd", NODE + "/users", "g:all"):
        assert R._UUID_RE.fullmatch(value) is None


def test_personal_data_queries_are_paged_capped_and_parameterized():
    source = inspect.getsource(D)
    assert "LIMIT $4 OFFSET $5" in source
    assert "LIMIT $6" in source
    assert "row_number() OVER" in source
    assert "WHERE c.rn <= $2" in source
    assert "scope.user_sql_arg()" in source
    assert "scope.node_sql_arg()" in source
    assert D.DETAIL_DEFAULT_PAGE_SIZE <= D.DETAIL_MAX_PAGE_SIZE <= 150
    assert D.MAX_IPS_PER_USER <= 6


def test_every_sensitive_route_resolves_scope_and_hides_node_existence():
    source = inspect.getsource(R.build_router)
    assert source.count("resolve_access_scope(ctx, admin)") == 3
    assert "if not scope.allows_node(node_uuid)" in source
    assert "status_code=404" in source
    assert 'headers={**_NO_STORE, "Retry-After"' in source


def test_browser_polling_is_non_overlapping_abortable_and_visibility_aware():
    assert MODULE_JS.count("new AbortController()") == 2
    assert "visibilitychange" in MODULE_JS and "document.hidden" in MODULE_JS
    assert "signal: controller.signal" in MODULE_JS
    assert "setInterval(function () { tick" not in MODULE_JS
    assert "setInterval(function () { if (selected)" not in MODULE_JS
    assert "setTimeout(function () { timer = null; tick(view); }" in MODULE_JS
    assert "?page=" in MODULE_JS and "&limit=" in MODULE_JS
    assert "pd.truncated" in MODULE_JS


def test_large_fleet_ui_is_idempotent_paged_and_unmounted_cleanly():
    assert "ZMIN = 0.5, ZMAX = 4" in MODULE_JS
    assert "var MIN_SCALE = 0.7" in MODULE_JS
    assert "var GRID_ROWS = 20" in MODULE_JS
    assert "var COLUMN_PAGE = 50" in MODULE_JS
    assert "function uniqSinks" in MODULE_JS
    assert "if (html === lastSvgHtml) return" in MODULE_JS
    assert "requestAnimationFrame(update)" in MODULE_JS
    assert "unwatchLive();" in MODULE_JS
    assert "cancelAnimationFrame(raf)" in MODULE_JS
    assert "class=\"lf-panel-pager\"" in MODULE_JS
    assert MODULE_JS.count("class=\"lf-pager\"") == 1
    assert "aria-pressed" in MODULE_JS
    assert "aria-label" in MODULE_JS


def test_browser_normalises_corrupt_prefs_and_resets_empty_filter_pager(tmp_path):
    """Exercise the shipped JS in a DOM, not just by matching source strings."""
    node = shutil.which("node")
    frontend = Path(__file__).resolve().parents[3] / "web" / "frontend"
    if node is None or not (frontend / "node_modules" / "jsdom" / "package.json").is_file():
        pytest.skip("Node.js with the frontend jsdom dependency is required")

    module_path = tmp_path / "live-flow-module.js"
    module_path.write_text(MODULE_JS, encoding="utf-8")
    driver = textwrap.dedent(
        r"""
        const fs = require('fs');
        const { JSDOM } = require('jsdom');
        const source = fs.readFileSync(process.argv[1], 'utf8');
        const dom = new JSDOM('<!doctype html><html><head></head><body><div id="root"></div></body></html>', {
          url: 'https://panel.example/plugins/live-flow',
          runScripts: 'outside-only',
          pretendToBeVisual: true,
        });
        const w = dom.window;
        Object.defineProperty(w.document, 'currentScript', {
          configurable: true,
          value: { src: 'https://panel.example/api/v2/plugins/live_flow/ui-module' },
        });
        w.localStorage.setItem('i18nextLng', 'ru');
        w.localStorage.setItem('lf.prefs', JSON.stringify({
          view: 'broken', q: { nested: true }, onlyActive: 'yes',
          onlyTraffic: 1, page: -4.2,
        }));
        w.requestAnimationFrame = (callback) => w.setTimeout(() => callback(Date.now()), 0);
        w.cancelAnimationFrame = (id) => w.clearTimeout(id);
        w.ResizeObserver = class { observe() {} disconnect() {} };
        const nodes = Array.from({ length: 101 }, (_, index) => ({
          uuid: `00000000-0000-0000-0000-${String(index + 1).padStart(12, '0')}`,
          name: `Node ${index + 1}`,
          connected: true,
          users: 1,
          active: 1,
          vpn_mbps: 1,
          tx_mbps: 1,
          rx_mbps: 1,
          position: index,
          sinks: ['internet'],
          cascades: [],
          inbounds: [],
          profile: 'default',
        }));
        const payload = {
          nodes,
          sinks: [{ tag: 'internet', kind: 'internet', title: 'Internet' }],
          total_active: 101,
          total_users: 101,
          can_view_users: false,
          profiles_available: true,
          live_source: 'panel-live',
        };
        w.fetch = async () => ({ ok: true, json: async () => payload });
        const wait = (ms) => new Promise((resolve) => w.setTimeout(resolve, ms));

        (async () => {
          w.eval(source);
          const root = w.document.getElementById('root');
          w.rwaPluginUI.live_flow.mount(root);
          await wait(80);
          const view = root.querySelector('#suptaz-live-flow-view');
          if (!view || !view.querySelector('svg')) throw new Error('mount/render failed');
          if (view.querySelector('.lf-q').value !== '') throw new Error('q was not normalised');
          if (view.querySelector('.lf-f-active').checked) throw new Error('onlyActive was not normalised');
          if (view.querySelector('.lf-f-traffic').checked) throw new Error('onlyTraffic was not normalised');
          if (view.querySelector('[data-view="column"]').getAttribute('aria-pressed') !== 'true') {
            throw new Error('view was not normalised');
          }

          view.querySelector('[data-view="grid"]').click();
          await wait(30);
          if (view.querySelector('.lf-pg-l').textContent !== '1 из 6') {
            throw new Error('large-fleet pager did not initialise');
          }
          const search = view.querySelector('.lf-q');
          search.value = 'definitely-no-node';
          search.dispatchEvent(new w.Event('input', { bubbles: true }));
          await wait(220);
          if (view.querySelector('.lf-pg-l').textContent !== '1 из 1') {
            throw new Error('empty-filter pager kept stale page count');
          }
          const buttons = view.querySelectorAll('.lf-pg');
          if (!buttons[0].disabled || !buttons[1].disabled) {
            throw new Error('empty-filter pager controls stayed active');
          }
          w.rwaPluginUI.live_flow.unmount();
          dom.window.close();
        })().catch((error) => {
          console.error(error && error.stack ? error.stack : String(error));
          process.exitCode = 1;
        });
        """
    )
    completed = subprocess.run(
        [node, "-e", driver, str(module_path)],
        cwd=frontend,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_browser_still_escapes_user_controlled_html():
    assert "function esc(s)" in MODULE_JS
    assert "replace(/[&<>\"]/g" in MODULE_JS
    for field in ("u.user", "u.tag", "r.as_name", "r.inbound"):
        assert f"esc({field})" in MODULE_JS
    assert "fmtIp(r.ip)" in MODULE_JS
    assert "esc(r.node || '—')" in MODULE_JS
    assert "esc([r.country, r.city].filter(Boolean).join(' · '))" in MODULE_JS


def test_audited_upstream_pin_and_provenance_are_present():
    root = Path(__file__).resolve().parents[1]
    provenance = (root / "UPSTREAM.md").read_text(encoding="utf-8")
    audit = json.loads((root / "AUDIT-PROVENANCE.json").read_text(encoding="utf-8"))
    assert UPSTREAM_VERSION == "0.17.0"
    assert FORK_VERSION == "0.17.0+agellar.3"
    assert UPSTREAM_COMMIT == "c84cadde9aa2f31e70ebbd32bc1ebb0ba3d18b49"
    assert UPSTREAM_COMMIT in provenance
    assert "Remnawave Admin `4.7.2`" in provenance
    assert "805aaba053b9b5aa9ad42070ab731b54bf2472d3eab9f8373bf453c4934a6dfb" in provenance
    assert audit["compatible_admin_release"] == "4.7.2"
    assert audit["fork"]["version"] == FORK_VERSION
    assert (root / "LICENSE.upstream").is_file()

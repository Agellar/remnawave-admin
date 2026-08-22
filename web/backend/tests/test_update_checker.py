"""Tests for GitHub update checker — version fetching and caching."""
import time

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

import web.backend.core.update_checker as uc


@pytest.fixture(autouse=True)
def _reset_cache():
    """Reset module-level cache between tests."""
    uc._cache = {}
    uc._cache_ts = 0
    uc._local_version = None
    uc._history_cache = []
    uc._history_cache_ts = 0
    yield
    uc._cache = {}
    uc._cache_ts = 0
    uc._local_version = None
    uc._history_cache = []
    uc._history_cache_ts = 0


class TestFetchLatestRelease:
    """Tests for _fetch_latest_release."""

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_success(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "tag_name": "v1.2.3",
            "html_url": "https://github.com/Case211/remnawave-admin/releases/tag/v1.2.3",
            "body": "Changelog text",
            "published_at": "2026-02-15T10:00:00Z",
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await uc._fetch_latest_release()
        assert result["tag_name"] == "v1.2.3"

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_404_returns_none(self, mock_client_cls):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await uc._fetch_latest_release()
        assert result is None

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_network_error_returns_none(self, mock_client_cls):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await uc._fetch_latest_release()
        assert result is None


class TestCheckForUpdates:
    """Tests for check_for_updates."""

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="1.0.0")
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_parses_version(self, mock_fetch, _mock_local):
        mock_fetch.return_value = {
            "tag_name": "v2.0.0",
            "html_url": "https://github.com/example/releases/tag/v2.0.0",
            "body": "New release",
            "published_at": "2026-02-15T10:00:00Z",
        }

        result = await uc.check_for_updates()
        assert result["current_version"] == "1.0.0"
        assert result["latest_version"] == "2.0.0"
        assert result["release_url"] == "https://github.com/example/releases/tag/v2.0.0"
        assert result["changelog"] == "New release"

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_fetch_failure_returns_fallback(self, mock_fetch):
        mock_fetch.return_value = None
        result = await uc.check_for_updates()
        assert result["latest_version"] is None
        assert result["update_available"] is False

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_cache_within_ttl(self, mock_fetch):
        mock_fetch.return_value = {"tag_name": "v1.0.0", "html_url": "", "body": "", "published_at": None}

        await uc.check_for_updates()
        await uc.check_for_updates()
        # Only called once due to caching
        assert mock_fetch.call_count == 1

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_cache_expired(self, mock_fetch):
        mock_fetch.return_value = {"tag_name": "v1.0.0", "html_url": "", "body": "", "published_at": None}

        await uc.check_for_updates()

        # Expire cache
        uc._cache_ts = time.time() - 2000

        await uc.check_for_updates()
        assert mock_fetch.call_count == 2


class TestGetLatestVersion:
    """Tests for get_latest_version."""

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="3.1.0")
    async def test_returns_version_string(self, _mock_local):
        version = await uc.get_latest_version()
        assert version == "3.1.0"

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.5.0")
    async def test_returns_cached_version(self, _mock_local):
        version = await uc.get_latest_version()
        assert version == "2.5.0"


# ---------------------------------------------------------------------------
# _detect_local_version
# ---------------------------------------------------------------------------


class TestDetectLocalVersion:
    """Tests for _detect_local_version with various sources."""

    def test_from_env_var(self):
        """APP_VERSION env var takes highest priority."""
        uc._local_version = None
        with patch.dict("os.environ", {"APP_VERSION": "2.8.1"}):
            result = uc._detect_local_version()
        assert result == "2.8.1"

    def test_from_env_var_strips_v_prefix(self):
        uc._local_version = None
        with patch.dict("os.environ", {"APP_VERSION": "v3.0.0"}):
            result = uc._detect_local_version()
        assert result == "3.0.0"

    def test_env_var_ignores_unknown(self):
        """APP_VERSION='unknown' should be skipped."""
        uc._local_version = None
        with patch.dict("os.environ", {"APP_VERSION": "unknown"}, clear=False):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("subprocess.run", side_effect=Exception("no git")):
                    result = uc._detect_local_version()
        assert result == "unknown"  # falls through to fallback

    def test_env_var_ignores_empty(self):
        uc._local_version = None
        with patch.dict("os.environ", {"APP_VERSION": ""}, clear=False):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("subprocess.run", side_effect=Exception("no git")):
                    result = uc._detect_local_version()
        assert result == "unknown"

    def test_from_version_file(self):
        """VERSION file is second priority (after env var)."""
        uc._local_version = None
        with patch.dict("os.environ", {"APP_VERSION": ""}, clear=False):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value="2.7.3\n"):
                    result = uc._detect_local_version()
        assert result == "2.7.3"

    def test_version_file_strips_v_prefix(self):
        uc._local_version = None
        with patch.dict("os.environ", {"APP_VERSION": ""}, clear=False):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("pathlib.Path.read_text", return_value="v2.7.3"):
                    result = uc._detect_local_version()
        assert result == "2.7.3"

    def test_from_git_describe_exact_tag(self):
        """Git describe when exactly on a tag (offset = 0)."""
        uc._local_version = None

        run1 = MagicMock(returncode=0, stdout="2.4.0\n")
        run2 = MagicMock(returncode=0, stdout="2.4.0-0-gabcdef\n")

        with patch.dict("os.environ", {"APP_VERSION": ""}, clear=False):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("subprocess.run", side_effect=[run1, run2]):
                    result = uc._detect_local_version()
        assert result == "2.4.0"

    def test_from_git_describe_with_offset(self):
        """Git describe when ahead of tag -- should include +N suffix."""
        uc._local_version = None

        run1 = MagicMock(returncode=0, stdout="v2.4.0\n")
        run2 = MagicMock(returncode=0, stdout="v2.4.0-3-gabcdef\n")

        with patch.dict("os.environ", {"APP_VERSION": ""}, clear=False):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("subprocess.run", side_effect=[run1, run2]):
                    result = uc._detect_local_version()
        assert result == "2.4.0+3"

    def test_git_describe_long_fails_fallback_to_tag(self):
        """If git describe --long fails, still use tag from --abbrev=0."""
        uc._local_version = None

        run1 = MagicMock(returncode=0, stdout="2.5.0\n")
        run2 = MagicMock(returncode=1, stdout="", stderr="error")

        with patch.dict("os.environ", {"APP_VERSION": ""}, clear=False):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("subprocess.run", side_effect=[run1, run2]):
                    result = uc._detect_local_version()
        assert result == "2.5.0"

    def test_git_describe_exception_fallback(self):
        """Subprocess exception leads to fallback 'unknown'."""
        uc._local_version = None
        with patch.dict("os.environ", {"APP_VERSION": ""}, clear=False):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("subprocess.run", side_effect=OSError("git not found")):
                    result = uc._detect_local_version()
        assert result == "unknown"

    def test_git_describe_abbrev_empty_stdout(self):
        """If git describe --abbrev=0 returns empty stdout, fall through."""
        uc._local_version = None

        run1 = MagicMock(returncode=0, stdout="")

        with patch.dict("os.environ", {"APP_VERSION": ""}, clear=False):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("subprocess.run", side_effect=[run1]):
                    result = uc._detect_local_version()
        assert result == "unknown"

    def test_git_describe_abbrev_nonzero_returncode(self):
        """If git describe --abbrev=0 fails (returncode != 0), fall through."""
        uc._local_version = None

        run1 = MagicMock(returncode=128, stdout="")

        with patch.dict("os.environ", {"APP_VERSION": ""}, clear=False):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("subprocess.run", side_effect=[run1]):
                    result = uc._detect_local_version()
        assert result == "unknown"

    def test_cached_value_returned(self):
        """Once detected, subsequent calls return cached value."""
        uc._local_version = "1.2.3"
        result = uc._detect_local_version()
        assert result == "1.2.3"


# ---------------------------------------------------------------------------
# _parse_version
# ---------------------------------------------------------------------------


class TestParseVersion:
    """Tests for _parse_version."""

    def test_simple_version(self):
        assert uc._parse_version("2.4.1") == (2, 4, 1)

    def test_two_part_version(self):
        assert uc._parse_version("2.4") == (2, 4)

    def test_single_part(self):
        assert uc._parse_version("5") == (5,)

    def test_version_with_plus_suffix(self):
        assert uc._parse_version("2.4+3") == (2, 4)

    def test_version_with_plus_suffix_three_parts(self):
        assert uc._parse_version("2.4.1+7") == (2, 4, 1)

    def test_version_with_vendor_suffix(self):
        assert uc._parse_version("4.5.6-agellar.1e1c0e72") == (4, 5, 6)

    def test_non_numeric_part_stops_parsing(self):
        """A prerelease suffix must not be treated as the stable patch."""
        assert uc._parse_version("2.4.1-beta") == (2, 4)

    def test_empty_string_returns_zero(self):
        assert uc._parse_version("") == (0,)

    def test_completely_non_numeric(self):
        assert uc._parse_version("abc") == (0,)

    def test_comparison_works(self):
        """Parsed tuples should compare correctly for update detection."""
        assert uc._parse_version("2.5.0") > uc._parse_version("2.4.1")
        assert uc._parse_version("2.4.1") == uc._parse_version("2.4.1")
        assert uc._parse_version("2.4.1") < uc._parse_version("3.0.0")
        assert uc._parse_version("2.4.1+5") == uc._parse_version("2.4.1")


# ---------------------------------------------------------------------------
# check_for_updates -- extended
# ---------------------------------------------------------------------------


class TestCheckForUpdatesExtended:
    """Extended tests for check_for_updates: update detection and edge cases."""

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_update_available(self, mock_fetch, _mock_local):
        mock_fetch.return_value = {
            "tag_name": "v2.5.0",
            "html_url": "https://example.com/releases/tag/v2.5.0",
            "body": "New stuff",
            "published_at": "2026-03-01T00:00:00Z",
        }
        result = await uc.check_for_updates()
        assert result["update_available"] is True
        assert result["latest_version"] == "2.5.0"
        assert result["current_version"] == "2.4.0"

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.5.0")
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_no_update_when_same_version(self, mock_fetch, _mock_local):
        mock_fetch.return_value = {
            "tag_name": "v2.5.0",
            "html_url": "",
            "body": "",
            "published_at": None,
        }
        result = await uc.check_for_updates()
        assert result["update_available"] is False

    @pytest.mark.asyncio
    @patch(
        "web.backend.core.update_checker._detect_local_version",
        return_value="4.5.6-agellar.1e1c0e72",
    )
    @patch(
        "web.backend.core.update_checker._fetch_latest_release",
        new_callable=AsyncMock,
    )
    async def test_no_update_when_custom_build_matches_latest_base(
        self, mock_fetch, _mock_local
    ):
        mock_fetch.return_value = {
            "tag_name": "v4.5.6",
            "html_url": "",
            "body": "",
            "published_at": None,
        }
        result = await uc.check_for_updates()
        assert result["update_available"] is False

    @pytest.mark.asyncio
    @patch(
        "web.backend.core.update_checker._detect_local_version",
        return_value="4.5.5-agellar.1e1c0e72",
    )
    @patch(
        "web.backend.core.update_checker._fetch_latest_release",
        new_callable=AsyncMock,
    )
    async def test_update_when_custom_build_has_older_base(
        self, mock_fetch, _mock_local
    ):
        mock_fetch.return_value = {
            "tag_name": "v4.5.6",
            "html_url": "",
            "body": "",
            "published_at": None,
        }
        result = await uc.check_for_updates()
        assert result["update_available"] is True

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="3.0.0")
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_no_update_when_ahead(self, mock_fetch, _mock_local):
        mock_fetch.return_value = {
            "tag_name": "v2.5.0",
            "html_url": "",
            "body": "",
            "published_at": None,
        }
        result = await uc.check_for_updates()
        assert result["update_available"] is False

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="unknown")
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_unknown_version_never_shows_update(self, mock_fetch, _mock_local):
        mock_fetch.return_value = {
            "tag_name": "v9.9.9",
            "html_url": "",
            "body": "",
            "published_at": None,
        }
        result = await uc.check_for_updates()
        assert result["update_available"] is False

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_changelog_truncated_to_2000(self, mock_fetch, _mock_local):
        mock_fetch.return_value = {
            "tag_name": "v2.5.0",
            "html_url": "",
            "body": "A" * 3000,
            "published_at": None,
        }
        result = await uc.check_for_updates()
        assert len(result["changelog"]) == 2000

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_empty_tag_name_yields_none_latest(self, mock_fetch, _mock_local):
        mock_fetch.return_value = {
            "tag_name": "",
            "html_url": "",
            "body": "",
            "published_at": None,
        }
        result = await uc.check_for_updates()
        assert result["latest_version"] is None
        assert result["update_available"] is False

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker._fetch_latest_release", new_callable=AsyncMock)
    async def test_none_changelog_stays_none(self, mock_fetch, _mock_local):
        mock_fetch.return_value = {
            "tag_name": "v2.5.0",
            "html_url": "",
            "body": None,
            "published_at": None,
        }
        result = await uc.check_for_updates()
        assert result["changelog"] is None


# ---------------------------------------------------------------------------
# get_release_history
# ---------------------------------------------------------------------------


def _make_httpx_mock(mock_client_cls, mock_resp):
    """Helper to wire up httpx.AsyncClient mock."""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client_cls.return_value = mock_client
    return mock_client


class TestGetReleaseHistory:
    """Tests for get_release_history."""

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_returns_newer_releases(self, mock_client_cls, _mock_local):
        releases_data = [
            {"tag_name": "v2.6.0", "name": "Release 2.6.0", "body": "Notes 2.6",
             "html_url": "https://example.com/2.6.0", "published_at": "2026-03-10"},
            {"tag_name": "v2.5.0", "name": "Release 2.5.0", "body": "Notes 2.5",
             "html_url": "https://example.com/2.5.0", "published_at": "2026-03-01"},
            {"tag_name": "v2.4.0", "name": "Release 2.4.0", "body": "Notes 2.4",
             "html_url": "https://example.com/2.4.0", "published_at": "2026-02-15"},
            {"tag_name": "v2.3.0", "name": "Release 2.3.0", "body": "Notes 2.3",
             "html_url": "https://example.com/2.3.0", "published_at": "2026-02-01"},
        ]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = releases_data
        _make_httpx_mock(mock_client_cls, mock_resp)

        result = await uc.get_release_history()

        assert len(result) == 2  # Only 2.6.0 and 2.5.0 are newer
        assert result[0]["tag"] == "2.6.0"
        assert result[1]["tag"] == "2.5.0"
        assert result[0]["name"] == "Release 2.6.0"
        assert result[0]["changelog"] == "Notes 2.6"
        assert result[0]["url"] == "https://example.com/2.6.0"

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="unknown")
    async def test_unknown_version_returns_empty(self, _mock_local):
        result = await uc.get_release_history()
        assert result == []

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_api_error_returns_empty(self, mock_client_cls, _mock_local):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        _make_httpx_mock(mock_client_cls, mock_resp)

        result = await uc.get_release_history()
        assert result == []

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_network_exception_returns_empty(self, mock_client_cls, _mock_local):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await uc.get_release_history()
        assert result == []

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_non_list_response_returns_empty(self, mock_client_cls, _mock_local):
        """If GitHub returns something other than a list, return empty."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": "not found"}
        _make_httpx_mock(mock_client_cls, mock_resp)

        result = await uc.get_release_history()
        assert result == []

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_skips_releases_with_empty_tag(self, mock_client_cls, _mock_local):
        releases_data = [
            {"tag_name": "", "name": "Bad release", "body": "", "html_url": "", "published_at": None},
            {"tag_name": "v2.5.0", "name": "Good release", "body": "", "html_url": "", "published_at": None},
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = releases_data
        _make_httpx_mock(mock_client_cls, mock_resp)

        result = await uc.get_release_history()
        assert len(result) == 1
        assert result[0]["tag"] == "2.5.0"

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    async def test_cache_hit(self, _mock_local):
        """Cached history is returned within TTL."""
        cached = [{"tag": "2.5.0", "name": "v2.5.0", "changelog": "", "url": "", "published_at": None}]
        uc._history_cache = cached
        uc._history_cache_ts = time.time()

        result = await uc.get_release_history()
        assert result is cached

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_cache_expired_refetches(self, mock_client_cls, _mock_local):
        uc._history_cache = [{"tag": "old"}]
        uc._history_cache_ts = time.time() - 2000  # expired

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"tag_name": "v2.5.0", "name": "New", "body": "", "html_url": "", "published_at": None}
        ]
        _make_httpx_mock(mock_client_cls, mock_resp)

        result = await uc.get_release_history()
        assert len(result) == 1
        assert result[0]["tag"] == "2.5.0"

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_release_with_missing_name_uses_fallback(self, mock_client_cls, _mock_local):
        """Release without 'name' should default to 'v{tag}'."""
        releases_data = [
            {"tag_name": "v2.5.0", "name": None, "body": None, "html_url": None, "published_at": None},
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = releases_data
        _make_httpx_mock(mock_client_cls, mock_resp)

        result = await uc.get_release_history()
        assert result[0]["name"] == "v2.5.0"
        assert result[0]["changelog"] == ""
        assert result[0]["url"] == ""

    @pytest.mark.asyncio
    @patch("web.backend.core.update_checker._detect_local_version", return_value="2.4.0")
    @patch("web.backend.core.update_checker.httpx.AsyncClient")
    async def test_passes_limit_as_per_page(self, mock_client_cls, _mock_local):
        """Ensure limit parameter is forwarded as per_page query param."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_client = _make_httpx_mock(mock_client_cls, mock_resp)

        await uc.get_release_history(limit=5)
        # Verify per_page was passed
        call_kwargs = mock_client.get.call_args
        assert call_kwargs.kwargs.get("params", {}).get("per_page") == 5


# ---------------------------------------------------------------------------
# get_dependency_versions
# ---------------------------------------------------------------------------


class TestGetDependencyVersions:
    """Tests for get_dependency_versions."""

    @pytest.mark.asyncio
    async def test_returns_python_version(self):
        """Should always include python version."""
        import sys

        mock_db = MagicMock()
        mock_db.is_connected = False

        with patch.dict("sys.modules", {"shared.database": MagicMock(db_service=mock_db)}):
            result = await uc.get_dependency_versions()

        expected = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        assert result["python"] == expected

    @pytest.mark.asyncio
    async def test_postgres_version_from_db(self):
        """Should parse PostgreSQL version from SELECT version() output."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(return_value="PostgreSQL 16.1 on x86_64")

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.acquire = MagicMock()
        mock_db.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_db.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("sys.modules", {"shared.database": MagicMock(db_service=mock_db)}):
            result = await uc.get_dependency_versions()

        assert result["postgresql"] == "16.1"

    @pytest.mark.asyncio
    async def test_postgres_not_connected(self):
        """When DB is not connected, postgresql should be None."""
        mock_db = MagicMock()
        mock_db.is_connected = False

        with patch.dict("sys.modules", {"shared.database": MagicMock(db_service=mock_db)}):
            result = await uc.get_dependency_versions()

        assert result.get("postgresql") is None

    @pytest.mark.asyncio
    async def test_postgres_exception_returns_none(self):
        """DB acquire exception should result in postgresql=None."""
        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.acquire.side_effect = Exception("connection lost")

        with patch.dict("sys.modules", {"shared.database": MagicMock(db_service=mock_db)}):
            result = await uc.get_dependency_versions()

        assert result.get("postgresql") is None

    @pytest.mark.asyncio
    async def test_fastapi_version_included(self):
        """Should include FastAPI version when importable."""
        mock_db = MagicMock()
        mock_db.is_connected = False

        with patch.dict("sys.modules", {"shared.database": MagicMock(db_service=mock_db)}):
            result = await uc.get_dependency_versions()

        import fastapi
        assert result["fastapi"] == fastapi.__version__

    @pytest.mark.asyncio
    async def test_xray_nodes_with_xray_version_column(self):
        """When xray_version column exists, fetch from it directly."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(side_effect=[
            "PostgreSQL 16.1 on x86_64",  # SELECT version()
            True,                          # col_exists check
        ])
        mock_conn.fetch = AsyncMock(return_value=[
            {"name": "node-de-1", "xray_version": "1.8.4"},
            {"name": "node-nl-2", "xray_version": "1.8.3"},
        ])

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.acquire = MagicMock()
        mock_db.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_db.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("sys.modules", {"shared.database": MagicMock(db_service=mock_db)}):
            result = await uc.get_dependency_versions()

        assert result["xray_nodes"] == {"node-de-1": "1.8.4", "node-nl-2": "1.8.3"}

    @pytest.mark.asyncio
    async def test_xray_nodes_from_raw_data_fallback(self):
        """When xray_version column doesn't exist, extract from raw_data JSON."""
        mock_conn = AsyncMock()
        mock_conn.fetchval = AsyncMock(side_effect=[
            "PostgreSQL 16.1 on x86_64",  # SELECT version()
            False,                          # col_exists = False
        ])
        mock_conn.fetch = AsyncMock(return_value=[
            {"name": "node-de-1", "raw_data": {"xray_version": "1.8.4"}},
            {"name": "node-nl-2", "raw_data": {"xrayVersion": "1.8.3"}},
            {"name": "node-empty", "raw_data": {}},
        ])

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.acquire = MagicMock()
        mock_db.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_db.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("sys.modules", {"shared.database": MagicMock(db_service=mock_db)}):
            result = await uc.get_dependency_versions()

        assert result["xray_nodes"] == {"node-de-1": "1.8.4", "node-nl-2": "1.8.3"}

    @pytest.mark.asyncio
    async def test_xray_exception_returns_empty_dict(self):
        """If xray query fails, xray_nodes should be empty dict."""
        mock_conn = AsyncMock()
        # version() succeeds, but col check raises
        mock_conn.fetchval = AsyncMock(side_effect=[
            "PostgreSQL 16.1 on x86_64",
            Exception("query failed"),
        ])

        mock_db = MagicMock()
        mock_db.is_connected = True
        mock_db.acquire = MagicMock()
        mock_db.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_db.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch.dict("sys.modules", {"shared.database": MagicMock(db_service=mock_db)}):
            result = await uc.get_dependency_versions()

        # The second try/except for xray should catch and set empty dict
        assert result.get("xray_nodes") == {} or result.get("xray_nodes") is None

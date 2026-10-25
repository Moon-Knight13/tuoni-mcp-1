from __future__ import annotations

import pytest

from tuoni_mcp.config import MCPMode, Profile, Settings


def _env(**overrides: str) -> dict[str, str]:
    base = {
        "TUONI_URL": "https://example.test:8443",
        "TUONI_USERNAME": "operator",
        "TUONI_PASSWORD": "secret",
    }
    base.update(overrides)
    return base


def test_default_startup_is_native_agent_ops_without_events() -> None:
    cfg = Settings.from_env(_env())
    assert cfg.profiles == frozenset({Profile.AGENT_OPS})
    assert cfg.mode is MCPMode.NATIVE
    assert cfg.events_enabled is False
    assert cfg.verify is True
    assert cfg.request_timeout == 30
    assert "secret" not in repr(cfg)


@pytest.mark.parametrize("url", [
    "ftp://example.test", "https://", "https://user:private@example.test",
    "https://example.test:bad", "https://example.test:70000", "https://example.test:0",
    "https://example.test?token=private", "https://example.test#fragment",
    "https://example.test\n", "https://example.test\\other",
])
def test_invalid_urls_fail_before_startup_without_echoing_credentials(url):
    with pytest.raises(ValueError, match="TUONI_URL") as error:
        Settings.from_env(_env(TUONI_URL=url))
    assert "private" not in str(error.value)


def test_proxy_base_paths_and_ipv6_urls_are_supported():
    assert Settings.from_env(_env(TUONI_URL="https://example.test/tuoni/")).url == "https://example.test/tuoni"
    assert Settings.from_env(_env(TUONI_URL="https://[::1]:8443/")).url == "https://[::1]:8443"


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "-inf"])
def test_invalid_timeouts_fail_at_startup(value):
    with pytest.raises(ValueError, match="finite and greater than zero"):
        Settings.from_env(_env(TUONI_REQUEST_TIMEOUT=value))


def test_profiles_are_additive_and_all_excludes_raw() -> None:
    additive = Settings.from_env(
        _env(TUONI_MCP_PROFILES="agent-ops,artifacts,events")
    )
    assert additive.profiles == {
        Profile.AGENT_OPS,
        Profile.ARTIFACTS,
        Profile.EVENTS,
    }
    assert additive.events_enabled is True

    all_cfg = Settings.from_env(_env(TUONI_MCP_PROFILES="all"))
    assert Profile.RAW not in all_cfg.profiles
    assert Profile.EVENTS in all_cfg.profiles


@pytest.mark.parametrize("value", ("", " ", ",", "agent-ops,"))
def test_empty_profile_values_fail(value: str) -> None:
    with pytest.raises(ValueError, match="TUONI_MCP_PROFILES"):
        Settings.from_env(_env(TUONI_MCP_PROFILES=value))


def test_unknown_profile_and_mode_fail() -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        Settings.from_env(_env(TUONI_MCP_PROFILES="agent-ops,nope"))
    with pytest.raises(ValueError, match="native or dispatch"):
        Settings.from_env(_env(TUONI_MCP_MODE="dynamic"))


def test_unsupported_toolgroups_setting_fails() -> None:
    with pytest.raises(ValueError, match="unsupported.*TUONI_MCP_PROFILES"):
        Settings.from_env(_env(TUONI_MCP_TOOLGROUPS="agents,commands"))


def test_unsupported_inline_download_setting_fails() -> None:
    with pytest.raises(ValueError, match="unsupported.*never inline binary bodies"):
        Settings.from_env(_env(TUONI_MCP_MAX_INLINE_BYTES="5242880"))


def test_raw_is_native_only() -> None:
    with pytest.raises(ValueError, match="raw profile is native-mode-only"):
        Settings.from_env(
            _env(TUONI_MCP_PROFILES="raw", TUONI_MCP_MODE="dispatch")
        )


def test_boolean_values_are_strict() -> None:
    with pytest.raises(ValueError, match="must be true or false"):
        Settings.from_env(_env(TUONI_MCP_READ_ONLY="sometimes"))


def test_download_directory_and_limit_are_explicit_and_bounded(tmp_path) -> None:
    cfg = Settings.from_env(
        _env(
            TUONI_MCP_DOWNLOAD_DIR=str(tmp_path / "downloads"),
            TUONI_MCP_DOWNLOAD_MAX_BYTES="4096",
        )
    )
    assert cfg.download_dir == str(tmp_path / "downloads")
    assert cfg.download_max_bytes == 4096

    with pytest.raises(ValueError, match="absolute path"):
        Settings.from_env(_env(TUONI_MCP_DOWNLOAD_DIR="relative/downloads"))
    with pytest.raises(ValueError, match="filesystem root"):
        Settings.from_env(_env(TUONI_MCP_DOWNLOAD_DIR=tmp_path.anchor))
    with pytest.raises(ValueError, match="at least 1"):
        Settings.from_env(_env(TUONI_MCP_DOWNLOAD_MAX_BYTES="0"))


def test_upload_staging_directory_and_limit_are_explicit(tmp_path) -> None:
    cfg = Settings.from_env(_env(TUONI_MCP_UPLOAD_DIR=str(tmp_path), TUONI_MCP_UPLOAD_MAX_BYTES="4096"))
    assert cfg.upload_dir == str(tmp_path.resolve())
    assert cfg.upload_max_bytes == 4096
    assert Settings.from_env(_env()).upload_dir is None
    assert Settings.from_env(_env()).upload_max_bytes == 16 * 1024 * 1024


def test_invalid_upload_staging_settings_fail_at_startup(tmp_path) -> None:
    file = tmp_path / "file"
    file.write_bytes(b"content")
    for path in ("relative/staging", str(tmp_path / "missing"), str(file), tmp_path.anchor):
        with pytest.raises(ValueError, match="TUONI_MCP_UPLOAD_DIR"):
            Settings.from_env(_env(TUONI_MCP_UPLOAD_DIR=path))
    with pytest.raises(ValueError, match="at least 1"):
        Settings.from_env(_env(TUONI_MCP_UPLOAD_MAX_BYTES="0"))

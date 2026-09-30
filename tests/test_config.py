from __future__ import annotations

from pathlib import Path

import pytest

from nineveh.config import Settings


def test_defaults_target_the_container_layout():
    settings = Settings()
    assert settings.data_dir == Path("/data")
    assert settings.database_path == Path("/state/nineveh.sqlite3")
    assert settings.thumbnail_dir == Path("/state/thumbnails")
    assert settings.page_cache_dir == Path("/state/page-cache")
    assert settings.range_dir == Path("/state/ranges")


def test_range_scratch_is_not_inside_the_page_cache():
    """A generated archive must not be counted or evicted by the page budget."""
    settings = Settings()
    assert settings.range_dir != settings.page_cache_dir
    assert settings.page_cache_dir not in settings.range_dir.parents


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
        ("maybe", False),
    ],
)
def test_boolean_environment_values(monkeypatch, value: str, expected: bool):
    monkeypatch.setenv("NINEVEH_SECURE_COOKIES", value)
    assert Settings.from_env().secure_cookies is expected


def test_an_unset_boolean_keeps_its_default(monkeypatch):
    monkeypatch.delenv("NINEVEH_SECURE_COOKIES", raising=False)
    assert Settings.from_env().secure_cookies is True


def test_from_env_reads_paths_and_numbers(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("NINEVEH_DATA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("NINEVEH_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("NINEVEH_FEED_PAGE_SIZE", "7")
    monkeypatch.setenv("NINEVEH_PAGE_CACHE_MB", "0")
    settings = Settings.from_env()
    assert settings.data_dir == tmp_path / "media"
    assert settings.feed_page_size == 7
    assert settings.page_cache_mb == 0


def test_values_below_their_minimum_are_rejected(monkeypatch):
    monkeypatch.setenv("NINEVEH_FEED_PAGE_SIZE", "0")
    with pytest.raises(ValueError, match="at least 1"):
        Settings.from_env()


def test_worker_ceilings_default_to_two():
    settings = Settings()
    assert (settings.hash_workers, settings.extract_workers) == (2, 2)


def test_worker_ceilings_are_configurable(monkeypatch):
    monkeypatch.setenv("NINEVEH_HASH_WORKERS", "6")
    monkeypatch.setenv("NINEVEH_EXTRACT_WORKERS", "8")
    settings = Settings.from_env()
    assert settings.hash_workers == 6
    assert settings.extract_workers == 8


@pytest.mark.parametrize("name", ["NINEVEH_HASH_WORKERS", "NINEVEH_EXTRACT_WORKERS"])
def test_a_worker_ceiling_of_zero_is_rejected(monkeypatch, name: str):
    """Zero would deadlock every request that needs the slot."""
    monkeypatch.setenv(name, "0")
    with pytest.raises(ValueError, match="at least 1"):
        Settings.from_env()


def test_a_scan_interval_of_zero_is_allowed(monkeypatch):
    monkeypatch.setenv("NINEVEH_SCAN_INTERVAL_SECONDS", "0")
    assert Settings.from_env().scan_interval_seconds == 0


def test_an_empty_public_base_url_becomes_none(monkeypatch):
    monkeypatch.setenv("NINEVEH_PUBLIC_BASE_URL", "")
    assert Settings.from_env().public_base_url is None


def test_the_inline_password_wins_over_the_password_file(tmp_path: Path):
    secret = tmp_path / "password.txt"
    secret.write_text("from-the-file\n", encoding="utf-8")
    settings = Settings(
        bootstrap_admin_password="from-the-env",
        bootstrap_admin_password_file=secret,
    )
    assert settings.admin_password() == "from-the-env"


def test_the_password_file_is_read_and_stripped(tmp_path: Path):
    secret = tmp_path / "password.txt"
    secret.write_text("  from-the-file  \n", encoding="utf-8")
    settings = Settings(bootstrap_admin_password_file=secret)
    assert settings.admin_password() == "from-the-file"


def test_no_configured_password_is_none():
    assert Settings().admin_password() is None


def test_deployment_information_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NINEVEH_MEMORY_LIMIT", "2g")
    monkeypatch.setenv("NINEVEH_RESTART_ENABLED", "true")
    settings = Settings.from_env()
    assert settings.deployment_memory_limit == "2g"
    assert settings.restart_enabled


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"unknown": "1"}, "Unsupported setting"),
        ({"service_title": ""}, "Service title"),
        # Retired from the UI: it is deployment-owned again.
        ({"public_base_url": "https://x.example"}, "Unsupported setting"),
        ({"feed_page_size": "many"}, "must be an integer"),
        ({"feed_page_size": "201"}, "must be between"),
    ],
)
def test_persisted_setting_validation(values: dict[str, str], message: str):
    with pytest.raises(ValueError, match=message):
        Settings().with_overrides(values)


def test_persisted_text_settings_are_normalized():
    assert Settings().with_overrides({"service_title": " Archive "}).service_title == (
        "Archive"
    )


def test_the_public_base_url_is_not_editable_from_the_user_interface():
    """It gates the login origin check, so a bad value locks the admin out."""
    assert "public_base_url" not in Settings().editable_values()


def test_mangabaka_limit_is_editable_but_hard_capped():
    assert (
        Settings()
        .with_overrides({"mangabaka_requests_per_minute": "12"})
        .mangabaka_requests_per_minute
        == 12
    )
    with pytest.raises(ValueError, match="between 1 and 30"):
        Settings().with_overrides({"mangabaka_requests_per_minute": "31"})


def test_split_access_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NINEVEH_PUBLIC_BASE_URL", "https://read.example")
    monkeypatch.setenv(
        "NINEVEH_PRIVATE_BASE_URLS",
        " https://nas.tail.ts.net , https://192.168.1.10:5443,",
    )
    monkeypatch.setenv("NINEVEH_PRIVATE_ALLOW_IPS", "100.64.0.0/10,192.168.1.0/24")
    monkeypatch.setenv("NINEVEH_DOWNLOAD_STREAMS_PER_ACCOUNT", "2")
    settings = Settings.from_env()
    assert settings.private_base_urls == (
        "https://nas.tail.ts.net",
        "https://192.168.1.10:5443",
    )
    assert settings.private_allow_ips == "100.64.0.0/10,192.168.1.0/24"
    assert settings.split_access
    assert settings.download_streams_per_account == 2


def test_split_access_is_off_by_default():
    settings = Settings.from_env()
    assert settings.private_base_urls == ()
    assert not settings.split_access
    assert settings.private_allow_ips == "100.64.0.0/10,fd7a:115c:a1e0::/48"


SAFE_SPLIT = {
    "public_base_url": "https://read.example",
    "private_base_urls": ("https://nas.tail.ts.net",),
}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"public_base_url": None}, "NINEVEH_PUBLIC_BASE_URL is required"),
        ({"public_base_url": "http://read.example"}, "must use https"),
        ({"private_base_urls": ("http://192.168.1.10",)}, "must use https"),
        ({"secure_cookies": False}, "SECURE_COOKIES"),
        ({"forwarded_allow_ips": " * "}, "explicit NINEVEH_FORWARDED_ALLOW_IPS"),
    ],
)
def test_split_access_refuses_settings_that_would_fail_open(overrides, message):
    with pytest.raises(ValueError, match=message):
        Settings(**{**SAFE_SPLIT, **overrides})


def test_an_account_cannot_have_more_downloads_than_everyone():
    with pytest.raises(ValueError, match="cannot exceed"):
        Settings(download_streams=2, download_streams_per_account=3)

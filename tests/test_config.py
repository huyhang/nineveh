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

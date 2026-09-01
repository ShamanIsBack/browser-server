"""Environment parsing and the removal of the stealth flag.

config reads the environment once at import, so each test reloads the module
against a controlled environment.
"""

from __future__ import annotations

import importlib

import pytest

import config


def _reload(monkeypatch, **env: str):
    for key in ("API_KEY", "HOST", "PORT", "HEADLESS", "DEBUG", "STEALTH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Stop a developer's real .env from bleeding into the assertions.
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: False, raising=False)
    module = importlib.reload(config)
    monkeypatch.setattr(module, "load_dotenv", lambda *a, **k: False, raising=False)
    return module


@pytest.fixture(autouse=True)
def _restore_config():
    yield
    importlib.reload(config)


# ── Defaults ──────────────────────────────────────────────────────────────────

def test_defaults_are_safe(monkeypatch) -> None:
    cfg = _reload(monkeypatch)
    assert cfg.API_KEY == ""          # dev mode, warned about at startup
    assert cfg.HOST == "127.0.0.1"    # loopback-only unless explicitly changed
    assert cfg.PORT == 8765
    assert cfg.HEADLESS is True
    assert cfg.DEBUG is False


def test_values_come_from_the_environment(monkeypatch) -> None:
    cfg = _reload(monkeypatch, API_KEY="s3cret", HOST="0.0.0.0", PORT="9000")
    assert cfg.API_KEY == "s3cret"
    assert cfg.HOST == "0.0.0.0"
    assert cfg.PORT == 9000


# ── Boolean flags ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["true", "TRUE", "True", "1", "yes", "YES", "Yes"])
def test_truthy_flag_spellings(monkeypatch, value: str) -> None:
    assert _reload(monkeypatch, DEBUG=value).DEBUG is True


@pytest.mark.parametrize(
    "value", ["false", "FALSE", "0", "no", "off", "", "maybe", "y", "t"]
)
def test_everything_else_is_false(monkeypatch, value: str) -> None:
    """Positive inclusion: only the listed spellings enable a flag."""
    assert _reload(monkeypatch, DEBUG=value).DEBUG is False


def test_headless_defaults_on_but_can_be_turned_off(monkeypatch) -> None:
    assert _reload(monkeypatch, HEADLESS="false").HEADLESS is False
    assert _reload(monkeypatch, HEADLESS="yes").HEADLESS is True


# ── The stealth flag is gone ──────────────────────────────────────────────────

def test_no_stealth_setting_exists(monkeypatch) -> None:
    cfg = _reload(monkeypatch)
    assert not hasattr(cfg, "STEALTH")


def test_setting_stealth_in_the_environment_does_nothing(monkeypatch) -> None:
    """An old .env carrying STEALTH=true must not resurrect the feature."""
    cfg = _reload(monkeypatch, STEALTH="true")
    assert not hasattr(cfg, "STEALTH")

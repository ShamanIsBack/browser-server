"""Application startup and shutdown.

The lifespan is where the browser is launched and where a missing or weak
API_KEY is supposed to be announced loudly. BrowserSession is replaced with a
fake, so no browser starts.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import config
import main


class FakeSession:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def fake_session_class(monkeypatch):
    created: list[FakeSession] = []

    def factory():
        session = FakeSession()
        created.append(session)
        return session

    monkeypatch.setattr(main, "BrowserSession", factory)
    return created


@pytest.fixture(autouse=True)
def _restore_globals():
    """The lifespan assigns module globals; put them back afterwards."""
    session, lock = main._session, main._lock
    yield
    main._session, main._lock = session, lock


async def test_lifespan_starts_and_stops_the_browser(fake_session_class, monkeypatch) -> None:
    monkeypatch.setattr(config, "API_KEY", "0123456789abcdef")

    async with main._lifespan(main.app):
        session = fake_session_class[0]
        assert session.started is True
        assert session.stopped is False
        assert main._session is session
        assert isinstance(main._lock, asyncio.Lock)

    assert session.stopped is True


async def test_missing_api_key_logs_a_warning(fake_session_class, monkeypatch, caplog) -> None:
    monkeypatch.setattr(config, "API_KEY", "")
    with caplog.at_level(logging.WARNING, logger="main"):
        async with main._lifespan(main.app):
            pass

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "an unauthenticated server must say so"
    assert "without authentication" in warnings[0].getMessage()


async def test_short_api_key_logs_an_error(fake_session_class, monkeypatch, caplog) -> None:
    monkeypatch.setattr(config, "API_KEY", "tooshort")
    with caplog.at_level(logging.ERROR, logger="main"):
        async with main._lifespan(main.app):
            pass

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors
    assert "shorter than" in errors[0].getMessage()


async def test_good_api_key_logs_no_warning_or_error(
    fake_session_class, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(config, "API_KEY", "0123456789abcdef0123456789abcdef")
    with caplog.at_level(logging.WARNING, logger="main"):
        async with main._lifespan(main.app):
            pass

    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_startup_log_does_not_mention_the_key(
    fake_session_class, monkeypatch, caplog
) -> None:
    """The secret must never reach the logs."""
    secret = "0123456789abcdef-supersecret"
    monkeypatch.setattr(config, "API_KEY", secret)
    with caplog.at_level(logging.DEBUG, logger="main"):
        async with main._lifespan(main.app):
            pass

    assert all(secret not in r.getMessage() for r in caplog.records)


def test_server_binds_loopback_by_default() -> None:
    assert config.HOST == "127.0.0.1"

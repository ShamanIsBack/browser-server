"""The X-API-Key gate.

Covers the three states the server can be in — no key configured (dev mode),
a key too short to be trusted, and a properly configured key — plus the
_MIN_API_KEY_LENGTH boundary and the constant-time comparison.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

import config
import main

GOOD_KEY = "0123456789abcdef0123456789abcdef"  # 32 chars


class _Req:
    """Minimal stand-in for the Request the dependency reads .url.path from."""

    class _URL:
        def __init__(self, path: str) -> None:
            self.path = path

    def __init__(self, path: str = "/state") -> None:
        self.url = self._URL(path)


async def _check(path: str, provided: str | None) -> None:
    await main._require_api_key(_Req(path), provided)


@pytest.fixture
def configured_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", GOOD_KEY)
    return GOOD_KEY


# ── Happy path ────────────────────────────────────────────────────────────────

async def test_correct_key_is_accepted(configured_key) -> None:
    await _check("/state", GOOD_KEY)  # no exception


async def test_health_needs_no_key(configured_key) -> None:
    await _check("/health", None)
    await _check("/health", "totally-wrong")


# ── Rejections ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "provided",
    [
        None,
        "",
        "wrong-key-but-long-enough-to-pass-length",
        GOOD_KEY[:-1],            # one char short
        GOOD_KEY + "x",           # one char long
        GOOD_KEY.upper(),         # case must match exactly
        " " + GOOD_KEY,           # no trimming
        GOOD_KEY + " ",
    ],
)
async def test_wrong_or_missing_key_is_401(configured_key, provided) -> None:
    with pytest.raises(HTTPException) as exc:
        await _check("/state", provided)
    assert exc.value.status_code == 401
    assert "Invalid or missing API key" in exc.value.detail


async def test_a_prefix_of_the_real_key_is_rejected(configured_key) -> None:
    """Guards against a truncating or prefix-based comparison."""
    for cut in range(1, len(GOOD_KEY)):
        with pytest.raises(HTTPException) as exc:
            await _check("/state", GOOD_KEY[:cut])
        assert exc.value.status_code == 401


async def test_non_ascii_key_is_rejected_not_crashed(configured_key) -> None:
    for provided in ["kluczyk-zażółć-gęślą-jaźń", "🔑🔑🔑🔑🔑🔑🔑🔑", "\x00" * 32]:
        with pytest.raises(HTTPException) as exc:
            await _check("/state", provided)
        assert exc.value.status_code == 401


# ── Dev mode: no key configured ───────────────────────────────────────────────

async def test_unset_key_allows_everything(monkeypatch) -> None:
    monkeypatch.setattr(config, "API_KEY", "")
    await _check("/state", None)
    await _check("/navigate", "anything at all")


# ── The _MIN_API_KEY_LENGTH rule ──────────────────────────────────────────────

def test_minimum_length_is_sixteen() -> None:
    assert main._MIN_API_KEY_LENGTH == 16


@pytest.mark.parametrize("length", [1, 2, 8, 15])
async def test_short_key_is_503_even_when_the_client_sends_it(monkeypatch, length) -> None:
    """A weak secret must not be usable as a credential.

    The server refuses to authenticate anyone rather than accepting a key an
    attacker could brute-force, and says so with 503 (misconfigured) rather
    than 401 (your key is wrong).
    """
    weak = "k" * length
    monkeypatch.setattr(config, "API_KEY", weak)

    with pytest.raises(HTTPException) as exc:
        await _check("/state", weak)          # the *correct* key
    assert exc.value.status_code == 503
    assert exc.value.detail == "Server misconfigured"

    with pytest.raises(HTTPException) as exc:
        await _check("/state", None)          # and no key
    assert exc.value.status_code == 503


async def test_short_key_still_leaves_health_reachable(monkeypatch) -> None:
    monkeypatch.setattr(config, "API_KEY", "tooshort")
    await _check("/health", None)  # liveness must survive a misconfiguration


async def test_exactly_minimum_length_key_is_accepted(monkeypatch) -> None:
    boundary = "k" * main._MIN_API_KEY_LENGTH
    monkeypatch.setattr(config, "API_KEY", boundary)
    await _check("/state", boundary)


async def test_one_below_minimum_length_is_refused(monkeypatch) -> None:
    just_short = "k" * (main._MIN_API_KEY_LENGTH - 1)
    monkeypatch.setattr(config, "API_KEY", just_short)
    with pytest.raises(HTTPException) as exc:
        await _check("/state", just_short)
    assert exc.value.status_code == 503


# ── Comparison is constant-time ───────────────────────────────────────────────

async def test_comparison_uses_hmac_compare_digest(monkeypatch, configured_key) -> None:
    """The check must not fall back to ==, which leaks length and prefix timing."""
    calls: list[tuple[bytes, bytes]] = []
    real = main.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(main.hmac, "compare_digest", spy)
    await _check("/state", GOOD_KEY)

    assert len(calls) == 1
    assert calls[0] == (GOOD_KEY.encode(), GOOD_KEY.encode())


# ── Path allowlist ────────────────────────────────────────────────────────────

def test_only_health_is_unauthenticated() -> None:
    assert main._UNAUTHENTICATED_PATHS == {"/health"}


@pytest.mark.parametrize(
    "path", ["/state", "/navigate", "/click", "/type", "/scroll", "/press_key", "/wait", "/reset"]
)
async def test_every_action_path_requires_a_key(configured_key, path: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await _check(path, None)
    assert exc.value.status_code == 401


@pytest.mark.parametrize("path", ["/health/", "/HEALTH", "/health/../state", "/docs"])
async def test_near_miss_paths_do_not_slip_the_allowlist(configured_key, path: str) -> None:
    """The allowlist is exact-match; anything else must still need a key."""
    with pytest.raises(HTTPException) as exc:
        await _check(path, None)
    assert exc.value.status_code == 401


async def test_a_key_that_cannot_be_encoded_is_401_not_a_crash(configured_key) -> None:
    """The header value is attacker-controlled; a weird type must not 500."""

    class NotAString:
        pass

    for provided in [12345, NotAString(), b"bytes-not-str"]:
        with pytest.raises(HTTPException) as exc:
            await _check("/state", provided)
        assert exc.value.status_code == 401

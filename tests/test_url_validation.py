"""The SSRF guard on /navigate and /reset.

This is the most security-relevant surface in the project: the agent driving
this server chooses the URLs, and a prompt-injected agent choosing them is the
threat being defended against. The guard rejects non-http schemes and IP
literals in ranges that should never be reachable from an agent-driven browser.

The final section documents what the guard deliberately does NOT catch. Those
tests assert today's real behaviour so the limitation stays visible and any
future change to it is a deliberate, test-breaking act. See
docs/DECISIONS.md ADR-004.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import main


# ── Blocked IP literals ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        # Loopback
        "http://127.0.0.1/",
        "http://127.0.0.1:8765/health",
        "http://127.1.2.3/",
        "https://127.0.0.1/admin",
        # Private (RFC 1918)
        "http://10.0.0.1/",
        "http://10.255.255.254/",
        "http://192.168.1.1/",
        "http://192.168.0.254/router",
        "http://172.16.0.1/",
        "http://172.31.255.254/",
        # Link-local — 169.254.169.254 is the cloud instance-metadata endpoint,
        # the single most valuable SSRF target on a cloud host.
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.0.1/",
        # Unspecified
        "http://0.0.0.0/",
        "http://0.0.0.0:8765/",
        # Multicast
        "http://224.0.0.1/",
        "http://239.255.255.250/",
        # Reserved
        "http://240.0.0.1/",
    ],
)
def test_blocked_ipv4_literals(url: str) -> None:
    with pytest.raises(ValueError, match="SSRF"):
        main._validate_http_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://[::1]/",             # loopback
        "http://[::1]:8765/health",
        "http://[::]/",              # unspecified
        "http://[fe80::1]/",         # link-local
        "http://[fe80::dead:beef]/",
        "http://[fc00::1]/",         # unique local (private)
        "http://[fd12:3456::1]/",
        "http://[ff02::1]/",         # multicast
        # IPv4-mapped IPv6 — a classic way to smuggle a v4 loopback past a
        # guard that only reasons about dotted-quad strings.
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://[::ffff:192.168.1.1]/",
    ],
)
def test_blocked_ipv6_literals(url: str) -> None:
    with pytest.raises(ValueError, match="SSRF"):
        main._validate_http_url(url)


# ── Non-http schemes ──────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file://C:/Windows/win.ini",
        "ftp://example.com/",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "about:blank",
        "chrome://settings",
        "view-source:https://example.com",
        "ws://example.com/socket",
        "//example.com/protocol-relative",
        "example.com",
        "not a url",
        "",
    ],
)
def test_only_http_and_https_are_accepted(url: str) -> None:
    with pytest.raises(ValueError, match="http and https"):
        main._validate_http_url(url)


def test_url_without_hostname_is_rejected() -> None:
    with pytest.raises(ValueError, match="hostname"):
        main._validate_http_url("http://")


# ── Allowed ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://example.com/",
        "https://example.com:8443/path?q=1#frag",
        "http://EXAMPLE.COM/",
        "https://sub.domain.example.co.uk/a/b",
        "http://8.8.8.8/",                    # public IP literal
        "http://[2606:4700:4700::1111]/",     # public IPv6 literal
        "https://user:pw@example.com/",
    ],
)
def test_public_http_urls_are_allowed(url: str) -> None:
    assert main._validate_http_url(url) == url


def test_validator_returns_the_url_unmodified() -> None:
    url = "https://example.com/Path?Q=1"
    assert main._validate_http_url(url) is url


# ── Wiring into the request models ────────────────────────────────────────────

def test_navigate_request_enforces_the_guard() -> None:
    assert main.NavigateRequest(url="https://example.com/").url == "https://example.com/"
    with pytest.raises(ValidationError):
        main.NavigateRequest(url="http://169.254.169.254/")


def test_reset_request_enforces_the_guard() -> None:
    assert main.ResetRequest(url="https://example.com/").url == "https://example.com/"
    with pytest.raises(ValidationError):
        main.ResetRequest(url="http://127.0.0.1/")


def test_reset_url_is_optional_and_none_skips_validation() -> None:
    assert main.ResetRequest().url is None
    assert main.ResetRequest(url=None).url is None


# ── The helper in isolation ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "hostname,blocked",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("10.0.0.1", True),
        ("169.254.169.254", True),
        ("8.8.8.8", False),
        ("example.com", False),   # not an IP literal → not classifiable
        ("localhost", False),
        ("", False),
    ],
)
def test_is_blocked_ip(hostname: str, blocked: bool) -> None:
    assert main._is_blocked_ip(hostname) is blocked


# ── Known gaps: asserted so they stay visible ─────────────────────────────────
#
# The guard inspects the hostname as written and never resolves DNS. Everything
# below is therefore ALLOWED today. These are not aspirational tests — they
# pin current behaviour, and they are expected to fail (and be rewritten) the
# day the guard is tightened. See docs/DECISIONS.md ADR-004 for the trade-off.

@pytest.mark.parametrize(
    "url,why_it_slips_through",
    [
        ("http://localhost/", "loopback reached by name, not by literal"),
        ("http://localhost:8765/health", "this server's own default bind address"),
        ("http://metadata.google.internal/", "cloud metadata reached by name"),
        ("http://127.0.0.1.nip.io/", "wildcard DNS resolving to loopback"),
        ("http://2130706433/", "127.0.0.1 as a decimal integer"),
        ("http://0177.0.0.1/", "127.0.0.1 with an octal first octet"),
        ("http://0x7f000001/", "127.0.0.1 in hex"),
        ("http://100.64.0.1/", "CGNAT shared address space (RFC 6598)"),
        ("http://intranet.corp/", "internal hostname on a split-horizon resolver"),
    ],
)
def test_known_gap_hostnames_are_not_blocked(url: str, why_it_slips_through: str) -> None:
    """Documents an accepted limitation, not a desired behaviour.

    The guard is a cheap structural filter, not a complete SSRF defence. It
    cannot close these without resolving DNS, and resolving DNS at validation
    time still loses to a rebinding attack because the browser resolves again
    when it actually navigates. The real containment is the network the server
    runs on.
    """
    assert main._validate_http_url(url) == url

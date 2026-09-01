"""Guards the decision recorded in docs/DECISIONS.md ADR-005.

playwright-stealth exists to defeat bot detection. This project does not ship
it, does not import it, and does not expose a switch for it. These tests exist
so that reintroducing it is a deliberate act that breaks the build, rather than
a plausible-looking convenience someone adds back later.
"""

from __future__ import annotations

import pathlib

import pytest

import browser.session
import config
import main

ROOT = pathlib.Path(__file__).resolve().parent.parent

SOURCE_FILES = sorted(
    p for p in [
        *ROOT.glob("*.py"),
        *(ROOT / "browser").glob("*.py"),
        *[ROOT / "requirements.txt", ROOT / "requirements-dev.txt", ROOT / ".env.example"],
        *(ROOT / "skills").rglob("*.md"),
    ]
    if p.exists()
)


def test_source_files_were_found() -> None:
    """A silent empty glob would make every scan below vacuously pass."""
    names = {p.name for p in SOURCE_FILES}
    assert {"main.py", "config.py", "session.py", "requirements.txt"} <= names


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_no_stealth_references_in_shipped_files(path: pathlib.Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    assert "stealth" not in text, f"{path.name} still mentions stealth"


def test_playwright_stealth_is_not_a_dependency() -> None:
    for name in ("requirements.txt", "requirements-dev.txt"):
        text = (ROOT / name).read_text(encoding="utf-8").lower()
        assert "playwright-stealth" not in text
        assert "playwright_stealth" not in text


def test_session_exposes_no_stealth_api() -> None:
    for attribute in ("set_stealth", "_apply_stealth_to_context", "_stealth"):
        assert not hasattr(browser.session.BrowserSession, attribute)


def test_a_fresh_session_carries_no_stealth_state() -> None:
    assert not hasattr(browser.session.BrowserSession(), "_stealth")


def test_no_stealth_request_model() -> None:
    assert not hasattr(main, "StealthRequest")


def test_no_stealth_endpoint() -> None:
    paths = {r.path for r in main.app.routes if hasattr(r, "path")}
    assert "/stealth" not in paths


def test_no_stealth_config_flag() -> None:
    assert not hasattr(config, "STEALTH")


def test_the_module_that_replaced_humanizer_is_named_pacing() -> None:
    assert (ROOT / "browser" / "pacing.py").exists()
    assert not (ROOT / "browser" / "humanizer.py").exists()


def test_nothing_imports_humanizer() -> None:
    for path in SOURCE_FILES:
        if path.suffix == ".py":
            assert "humanizer" not in path.read_text(encoding="utf-8")


def test_the_skill_tells_the_agent_not_to_work_around_blocks() -> None:
    text = (ROOT / "skills" / "browser" / "SKILL.md").read_text(encoding="utf-8").lower()
    assert "does not hide it" in text or "no fingerprint masking" in text

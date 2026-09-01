"""DOM element marking.

inject_marks is the agent's only view of what is clickable, so its failure
mode matters: it must degrade to "nothing is clickable" rather than raising
and taking the whole /state response down with it.
"""

from __future__ import annotations

import pytest

from browser import dom_processor
from tests.conftest import FakePage

EMPTY = {"marks": [], "total": 0}


async def test_returns_the_payload_from_the_page() -> None:
    page = FakePage()
    page.marks_payload = {
        "marks": [
            {"id": 1, "tag": "a", "text": "Home", "type": None},
            {"id": 2, "tag": "input", "text": "Search", "type": "search"},
        ],
        "total": 9,
    }
    assert await dom_processor.inject_marks(page) == page.marks_payload


async def test_empty_page_is_reported_as_empty() -> None:
    page = FakePage()
    page.marks_payload = {"marks": [], "total": 0}
    assert await dom_processor.inject_marks(page) == EMPTY


@pytest.mark.parametrize(
    "payload",
    [None, [], "marks", 42, {}, {"total": 3}, {"wrong_key": []}],
)
async def test_unexpected_payloads_degrade_to_empty(payload) -> None:
    page = FakePage()
    page.marks_payload = payload
    assert await dom_processor.inject_marks(page) == EMPTY


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("Execution context was destroyed"),
        TimeoutError("evaluate timed out"),
        ValueError("bad script"),
    ],
)
async def test_evaluation_failure_degrades_to_empty(error) -> None:
    page = FakePage()
    page.evaluate_error = error
    assert await dom_processor.inject_marks(page) == EMPTY


# ── The injected script ───────────────────────────────────────────────────────

def test_script_indexes_the_expected_interactive_elements() -> None:
    script = dom_processor.MARKS_SCRIPT
    for selector in [
        "a[href]", "button", "select", "textarea",
        '[role="button"]', '[role="link"]', '[role="checkbox"]',
        '[contenteditable="true"]',
    ]:
        assert selector in script


def test_script_excludes_hidden_inputs() -> None:
    assert 'input:not([type="hidden"])' in dom_processor.MARKS_SCRIPT


def test_script_clears_stale_marks_before_remarking() -> None:
    """Ids must not accumulate across turns — they are per-/state handles."""
    assert "removeAttribute('data-mark-id')" in dom_processor.MARKS_SCRIPT


def test_script_reports_both_viewport_and_total_counts() -> None:
    """The agent needs the total to know whether scrolling would reveal more."""
    script = dom_processor.MARKS_SCRIPT
    assert "allVisible.length" in script
    assert "return { marks, total }" in script


def test_script_truncates_label_text() -> None:
    assert "substring(0, 80)" in dom_processor.MARKS_SCRIPT


def test_script_makes_no_network_calls() -> None:
    """It reads and annotates the DOM; it must not fetch anything."""
    script = dom_processor.MARKS_SCRIPT
    for forbidden in ["fetch(", "XMLHttpRequest", "WebSocket", "import(", "eval("]:
        assert forbidden not in script

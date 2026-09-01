"""Pydantic request models — the input bounds on every endpoint.

These run before anything reaches the browser, so they are the point at which
an oversized, malformed or unexpected payload is supposed to die.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import main


# ── The key-combo pattern ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "key",
    [
        "Enter", "Escape", "Tab", "Backspace", "ArrowDown", "PageUp", "F12",
        "a", "Z", "7", " ", "?", "/", "+",          # single printable chars
        "Control+a", "Shift+Tab", "Alt+F4", "Meta+v",
        "Control+Shift+Tab",
        "Control+Shift+Alt+Delete",                  # three modifiers, the maximum
    ],
)
def test_accepted_keys(key: str) -> None:
    assert main.PressKeyRequest(key=key).key == key


@pytest.mark.parametrize(
    "key",
    [
        # The anchoring cases: \Z rather than $, so a trailing newline is not
        # quietly tolerated at the end of an otherwise valid key.
        "Enter\n",
        "Control+a\n",
        "a\n",
        "\nEnter",
        "Enter\r\n",
        # Injection-shaped payloads
        "Enter; rm -rf /",
        "Enter && whoami",
        "<script>alert(1)</script>",
        "../../etc/passwd",
        "key\x00null",
        "Ünicode",
        "日本語",
        # Structurally wrong combos
        "Control+",
        "+a",
        "Control++a",
        "Control+Shift+Alt+Meta+Delete",   # four modifiers, one too many
        "Control+ArrowDown+Extra+More+Yet",
        "two words",
        "",
    ],
)
def test_rejected_keys(key: str) -> None:
    with pytest.raises(ValidationError):
        main.PressKeyRequest(key=key)


def test_key_pattern_is_anchored_at_both_ends() -> None:
    pattern = main._KEY_PATTERN.pattern
    assert pattern.startswith("^")
    assert pattern.endswith(chr(92) + "Z"), "must anchor with \\Z, not $"


def test_key_length_is_capped_at_64() -> None:
    main.PressKeyRequest(key="A" * 64)
    with pytest.raises(ValidationError):
        main.PressKeyRequest(key="A" * 65)


# ── Typed text bound ──────────────────────────────────────────────────────────

def test_max_type_text_length_is_ten_thousand() -> None:
    assert main._MAX_TYPE_TEXT_LENGTH == 10_000


def test_text_at_the_bound_is_accepted() -> None:
    req = main.TypeRequest(element_id=1, text="x" * main._MAX_TYPE_TEXT_LENGTH)
    assert len(req.text) == main._MAX_TYPE_TEXT_LENGTH


def test_text_one_over_the_bound_is_rejected() -> None:
    with pytest.raises(ValidationError):
        main.TypeRequest(element_id=1, text="x" * (main._MAX_TYPE_TEXT_LENGTH + 1))


def test_grossly_oversized_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        main.TypeRequest(element_id=1, text="x" * 5_000_000)


def test_empty_text_is_allowed() -> None:
    assert main.TypeRequest(element_id=1, text="").text == ""


def test_text_content_is_not_sanitised_only_bounded() -> None:
    """Length is the only rule — the value goes to a keyboard, not a shell."""
    payload = "<script>alert(1)</script> ' OR 1=1 -- \x00 🎉"
    assert main.TypeRequest(element_id=1, text=payload).text == payload


# ── Element ids ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("element_id", [1, 2, 500, 9_999, 10_000])
def test_accepted_element_ids(element_id: int) -> None:
    assert main.ClickRequest(element_id=element_id).element_id == element_id
    assert main.TypeRequest(element_id=element_id, text="x").element_id == element_id


@pytest.mark.parametrize("element_id", [0, -1, -10_000, 10_001, 1_000_000])
def test_rejected_element_ids(element_id: int) -> None:
    with pytest.raises(ValidationError):
        main.ClickRequest(element_id=element_id)
    with pytest.raises(ValidationError):
        main.TypeRequest(element_id=element_id, text="x")


@pytest.mark.parametrize("element_id", ["1; DROP TABLE", "abc", None, 1.5, [1]])
def test_non_integer_element_ids_are_rejected(element_id) -> None:
    with pytest.raises(ValidationError):
        main.ClickRequest(element_id=element_id)


# ── Scroll direction ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("direction", ["up", "down"])
def test_accepted_scroll_directions(direction: str) -> None:
    assert main.ScrollRequest(direction=direction).direction == direction


@pytest.mark.parametrize("direction", ["Up", "DOWN", "left", "right", "", "up ", None, 1])
def test_rejected_scroll_directions(direction) -> None:
    with pytest.raises(ValidationError):
        main.ScrollRequest(direction=direction)


# ── Wait bounds ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seconds", [0, 0.0, 0.5, 2, 4.999, 5.0])
def test_accepted_wait_durations(seconds) -> None:
    assert main.WaitRequest(seconds=seconds).seconds == float(seconds)


@pytest.mark.parametrize("seconds", [-0.1, -1, 5.001, 60, 3600, float("inf")])
def test_rejected_wait_durations(seconds) -> None:
    with pytest.raises(ValidationError):
        main.WaitRequest(seconds=seconds)


def test_wait_rejects_nan() -> None:
    with pytest.raises(ValidationError):
        main.WaitRequest(seconds=float("nan"))


# ── Missing fields ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "model",
    [main.NavigateRequest, main.ClickRequest, main.TypeRequest,
     main.ScrollRequest, main.PressKeyRequest, main.WaitRequest],
)
def test_required_fields_are_required(model) -> None:
    with pytest.raises(ValidationError):
        model()

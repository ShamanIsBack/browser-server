"""Pacing helpers: delays and scroll physics.

The scroll assertions matter functionally, not cosmetically — if the eased
steps do not sum to a full viewport step, scrolling silently drifts, and if
they are not delivered as several separate wheel events, scroll-driven lazy
loading never fires.
"""

from __future__ import annotations

import inspect

import pytest

from browser import pacing
from tests.conftest import FakePage


# ── Delays ────────────────────────────────────────────────────────────────────

async def test_pre_action_delay_sleeps_within_its_band(no_pacing) -> None:
    await pacing.pre_action_delay()
    assert len(no_pacing.sleeps) == 1
    assert 0.3 <= no_pacing.sleeps[0] <= 1.2


async def test_pre_action_delay_varies(no_pacing) -> None:
    for _ in range(50):
        await pacing.pre_action_delay()
    assert len(set(no_pacing.sleeps)) > 1, "a constant delay would be a bug"


def test_typing_delay_is_within_its_band() -> None:
    values = [pacing.typing_delay_ms() for _ in range(200)]
    assert all(50 <= v <= 150 for v in values)
    assert all(isinstance(v, int) for v in values)
    assert len(set(values)) > 1


# ── Eased scroll ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("direction,expected", [("down", 300), ("up", -300)])
async def test_scroll_deltas_sum_to_one_viewport_step(
    no_pacing, direction: str, expected: int
) -> None:
    page = FakePage()
    await pacing.eased_scroll(page, direction)
    assert round(sum(page.mouse.deltas), 6) == expected


async def test_scroll_is_delivered_as_several_events(no_pacing) -> None:
    """One big jump fires a single scroll event and skips lazy-load triggers."""
    page = FakePage()
    await pacing.eased_scroll(page, "down")
    assert len(page.mouse.deltas) == 5


async def test_scroll_never_reverses_direction(no_pacing) -> None:
    for direction, sign in (("down", 1), ("up", -1)):
        page = FakePage()
        await pacing.eased_scroll(page, direction)
        assert all(d * sign > 0 for d in page.mouse.deltas)


async def test_scroll_follows_a_smoothstep_ramp(no_pacing) -> None:
    """Slow at both ends, fastest in the middle."""
    page = FakePage()
    await pacing.eased_scroll(page, "down")
    deltas = page.mouse.deltas
    middle = deltas[len(deltas) // 2]
    assert middle > deltas[0]
    assert middle > deltas[-1]
    assert deltas[0] == pytest.approx(deltas[-1])  # symmetric curve


async def test_scroll_pauses_between_steps(no_pacing) -> None:
    page = FakePage()
    await pacing.eased_scroll(page, "down")
    assert len(no_pacing.sleeps) == 5
    assert all(0.05 <= s <= 0.15 for s in no_pacing.sleeps)


async def test_any_direction_other_than_down_scrolls_up(no_pacing) -> None:
    page = FakePage()
    await pacing.eased_scroll(page, "up")
    assert sum(page.mouse.deltas) < 0


# ── The module says what it is ────────────────────────────────────────────────

def test_module_documents_its_purpose_and_disclaims_evasion() -> None:
    doc = inspect.getdoc(pacing) or ""
    assert doc, "pacing must carry a module docstring"
    lowered = doc.lower()
    assert "lazy" in lowered
    assert "evasion" in lowered or "not mask" in lowered

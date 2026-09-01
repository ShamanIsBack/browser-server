"""Request pacing and scroll physics.

Three small helpers that put deliberate delays and eased motion into browser
actions. They exist for two practical reasons, neither of which is evasion:

1. **Pacing.** An agent loop issues actions as fast as the HTTP round-trip
   allows, which is far faster than any page is built to handle. A short delay
   before each action keeps request rates civil and gives the previous action's
   handlers time to run.

2. **Lazy loading.** Most infinite-scroll and image-lazy-load implementations
   are driven by scroll events and IntersectionObserver callbacks. A single
   large `mouse.wheel` jump fires one event and skips the intermediate
   positions, so content never loads. Stepping the scroll through a
   smoothstep-eased ramp fires the intervening events and lets the page
   populate before the next screenshot is taken.

These helpers do not mask the automation. Playwright remains detectable, the
user agent is a plain Chrome string, and nothing here alters the browser
fingerprint.
"""

import asyncio
import random

from playwright.async_api import Page


async def pre_action_delay() -> None:
    """Pause briefly before an action so request rates stay civil."""
    await asyncio.sleep(random.uniform(0.3, 1.2))


def typing_delay_ms() -> int:
    """Per-keystroke delay, in milliseconds.

    Typing a value instantly can outrun a page's own input handlers — debounced
    autocompletes and validation-on-keyup in particular.
    """
    return random.randint(50, 150)


async def eased_scroll(page: Page, direction: str) -> None:
    """Scroll one viewport step, ramped so scroll-driven loading fires.

    The step deltas follow the increments of a smoothstep curve, so they sum to
    exactly ``delta`` regardless of the step count.
    """
    delta = 300 if direction == "down" else -300
    steps = 5
    prev_eased = 0.0
    for i in range(steps):
        t = (i + 1) / steps
        eased = t * t * (3 - 2 * t)  # smoothstep cumulative position
        step_delta = delta * (eased - prev_eased)  # incremental delta, sums to delta
        prev_eased = eased
        await page.mouse.wheel(0, step_delta)
        await asyncio.sleep(random.uniform(0.05, 0.15))

from __future__ import annotations

MARKS_SCRIPT = """
() => {
    document.querySelectorAll('[data-mark-id]').forEach(el => el.removeAttribute('data-mark-id'));

    const selector = [
        'a[href]', 'button',
        'input:not([type="hidden"])', 'select', 'textarea',
        '[role="button"]', '[role="link"]', '[role="menuitem"]',
        '[role="tab"]', '[role="checkbox"]', '[role="combobox"]',
        '[contenteditable="true"]', '[tabindex="0"]'
    ].join(', ');

    const allVisible = Array.from(document.querySelectorAll(selector)).filter(el => {
        try {
            return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
        } catch (_) {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        }
    });

    const total = allVisible.length;
    const viewportH = window.innerHeight;
    const viewportW = window.innerWidth;
    const buffer = 50;

    const inViewport = allVisible.filter(el => {
        const r = el.getBoundingClientRect();
        return r.bottom >= -buffer
            && r.top <= viewportH + buffer
            && r.right >= 0
            && r.left <= viewportW;
    });

    const marks = inViewport.map((el, i) => {
        el.setAttribute('data-mark-id', String(i + 1));
        const tag = el.tagName.toLowerCase();
        const isSelect = el.tagName === 'SELECT';
        const isInput = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA';
        const raw = (isSelect
            ? ((el.options[el.selectedIndex] && el.options[el.selectedIndex].text) || el.value || '')
            : isInput
                ? (el.value || el.placeholder || el.getAttribute('aria-label') || el.getAttribute('title') || '')
                : (el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '')
        ).trim().substring(0, 80);
        const type = el.type || el.getAttribute('role') || null;
        return { id: i + 1, tag, text: raw, type };
    });

    return { marks, total };
}
"""


async def inject_marks(page) -> dict:
    try:
        result = await page.evaluate(MARKS_SCRIPT)
        if isinstance(result, dict) and "marks" in result:
            return result
        return {"marks": [], "total": 0}
    except Exception:
        return {"marks": [], "total": 0}

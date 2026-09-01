---
name: browser
description: Control a real Playwright-driven web browser via HTTP — navigate, click, type, scroll, and get screenshot + DOM feedback at every step
user-invocable: false
openclaw:
  requires:
    env: [BROWSER_SERVER_URL, BROWSER_API_KEY]
  primaryEnv: BROWSER_SERVER_URL
  emoji: "🌐"
  always: false
---

# Browser Skill

You can control a real web browser running on the browser server. Use it to complete web-based tasks step by step.

## Server

The browser server URL is available as `BROWSER_SERVER_URL`. All requests require the header `X-API-Key: <BROWSER_API_KEY>`.

> **Note on keys:** `BROWSER_API_KEY` is the value you pass in the header. The server must be started with the matching `API_KEY=<same value>` in its own environment. If the server's `API_KEY` is unset it will accept all requests (dev mode only — never deploy without it).

## Core Rules
- **Element IDs are ephemeral.** Valid ONLY for the most recent `GET /state` response. Never reuse an ID from a previous step.
- **Always inspect the screenshot** before acting — check for overlays, pop-ups, or cookie banners.
- Use `POST /press_key {"key":"Escape"}` to dismiss modals; follow with `GET /state` to confirm.
- Call `GET /state` after any unexpected result to get a fresh view.
- All responses are JSON. Errors come as `{"error": "<message>"}` — read and adjust.
- If the browser appears stuck (blank page, persistent error, unresponsive JS), call `POST /reset` to recover the session.
- When the task is complete, return a plain-text answer. Do not make further tool calls.

## Endpoints

### GET /health
Liveness check. No auth required, no lock taken, no browser interaction.
**Response:** `{"status": "ok"}`
Call before starting a long task to confirm the server is alive.

### GET /state
Snapshot the current browser — no action taken.
**Response:** `{"url": string, "dom_marks": [...], "total_page_marks": int, "screenshot_b64": string, "loading": bool}`

### POST /navigate
```json
{"url": "https://example.com"}
```
Loads the URL. Only `http` and `https` URLs are accepted. Best-effort cookie banner auto-dismiss on load only — banners that appear mid-session must be handled manually via `POST /click` or `POST /press_key {"key":"Escape"}`.
**Response:** current state (same as GET /state)

### POST /click
```json
{"element_id": 14}
```
Clicks an element by its mark ID. If the element is gone: `{"error": "element 14 no longer exists — call get_state to refresh"}`.
**Response:** current state

### POST /type
```json
{"element_id": 7, "text": "search query"}
```
Focuses an element and types character by character, with a small per-keystroke delay so debounced input handlers and autocompletes keep up.
**Response:** current state

### POST /scroll
```json
{"direction": "down"}
```
`direction` is `"up"` or `"down"`. The scroll is stepped through an eased ramp rather than jumped, so scroll-driven lazy loading and `IntersectionObserver` callbacks actually fire.
**Response:** current state

### POST /press_key
```json
{"key": "Escape"}
```
Sends a keyboard event (e.g. `"Escape"`, `"Enter"`, `"Tab"`).
**Response:** current state

### POST /wait
```json
{"seconds": 2.0}
```
Pauses up to 5 seconds. Use when a page is loading or an animation is running.
**Response:** current state

### POST /reset
```json
{"url": "https://example.com"}
```
Recovers a stuck session by recreating the browser context and page. The `url` field is optional — if provided, the server navigates there after reset; if omitted, the page stays blank.
**Cookies, session storage, and JS state are wiped.** The browser process itself is not restarted; only the context — so any login or form state on the page is lost and must be re-established.
**Response:** `{"reset": true, "url": "<current page url after reset>"}`
If no `url` was provided, call `GET /state` next to get fresh element IDs before any further action.

## State Response Format

`dom_marks` is a list of visible interactive elements **currently in the viewport** (with a 50px vertical buffer above and below), each with:
```json
{"id": 14, "tag": "button", "text": "Log In", "type": null}
```

`total_page_marks` is the count of all visible interactive elements on the page, regardless of viewport position. If `total_page_marks > len(dom_marks)`, there are interactive elements outside the viewport — call `POST /scroll` to reveal them.

`screenshot_b64` is a base64-encoded JPEG (max 1024px longest edge). Use it to detect overlays or hidden content before acting.

## Limitations
- **iframes and shadow DOM are not indexed** — elements inside iframes (e.g. embedded payments, reCAPTCHA) will not appear in `dom_marks`.
- **The browser is plainly automated and does not hide it.** There is no fingerprint masking or bot-detection evasion. Sites that block automated traffic will block this server, and that is the intended behaviour — if a site refuses access, respect it rather than looking for a way around it.
- **One session per server instance.** Requests are serialized — concurrent calls queue behind a lock. The server must be run with a single worker (`uvicorn main:app --workers 1`); multi-worker mode creates independent browser sessions that do not share state.
- **screenshot_b64** is a base64-encoded JPEG at quality 75, max 1024px on the longest edge (~80–150 KB per response, gzip-compressed in transit).

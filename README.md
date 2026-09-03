# Browser Server

**An AI agent can read a web page. It cannot use one.** This gives it hands.

---

## The problem

Ask an AI assistant to do something on the web — check an order status, pull a
figure out of a supplier portal, fill in a form on an internal tool — and it
stops at the first click. It can fetch a URL and read the HTML that comes back,
but a great deal of the modern web isn't in that HTML: it appears after a
login, behind a cookie banner, once a dropdown is opened, or when a list has
been scrolled far enough to load the next page. The assistant can describe the
page it was handed. It cannot get to the next one.

The usual workaround is to write a bespoke script for each site. That script
breaks the week the site changes its layout, and it only ever works for the one
task it was written for.

## The solution

A small server that runs one real Chrome browser and exposes it over HTTP.

The agent works the way a person does — it looks, then acts, then looks again.
Every action it takes comes back with a screenshot of what the page now looks
like and a numbered list of everything currently clickable on it:

```json
{
  "url": "https://example.com/orders",
  "dom_marks": [
    {"id": 1, "tag": "input",  "text": "Search orders", "type": "search"},
    {"id": 2, "tag": "button", "text": "Filter",        "type": null},
    {"id": 3, "tag": "a",      "text": "Order #4417",   "type": null}
  ],
  "total_page_marks": 28,
  "screenshot_b64": "...",
  "loading": false
}
```

So the agent never has to guess at a CSS selector or invent a snippet of
JavaScript. It says *click 3*, and gets back the page that click produced. When
`total_page_marks` is higher than the number of marks it can see, it knows to
scroll. Nothing is site-specific, so the same nine endpoints work on a site
nobody wrote code for.

The browser is deliberately, visibly automated. There is no fingerprint
masking and no bot-detection evasion — see [Security](#security) and
[ADR-005](docs/DECISIONS.md).

## Architecture

```
  ┌─────────────┐   HTTP + X-API-Key    ┌──────────────────────────────┐
  │  AI agent   │──────────────────────▶│  FastAPI  (main.py)          │
  │  (OpenClaw  │                       │   auth · validation · lock   │
  │   skill)    │◀──────────────────────│                              │
  └─────────────┘  screenshot + marks   └──────────────┬───────────────┘
                                                       │
                                        ┌──────────────▼───────────────┐
                                        │  BrowserSession              │
                                        │   one context, one page      │
                                        │   popups + downloads refused │
                                        └──────────────┬───────────────┘
                                                       │
                                        ┌──────────────▼───────────────┐
                                        │  Chromium (Playwright)       │
                                        └──────────────────────────────┘
```

Three decisions shape everything else. All are recorded with their trade-offs
in [`docs/DECISIONS.md`](docs/DECISIONS.md).

**One persistent session behind a lock, not a session pool.** A browsing task
is a chain — log in, search, filter, open the third result — and every step
depends on the state the last one left behind. Cookies, scroll position and
the contents of a half-filled form all live in the browser, not in the request.
Hand the second step to a different browser and the login is gone. So there is
exactly one browser context for the life of the process, and a single
`asyncio.Lock` serialises every request into it. Two actions can never
interleave on one page. The cost is honest and stated up front: this server
handles one conversation at a time, and it must run with `--workers 1`.

**An HTTP boundary rather than a library import.** Driving Playwright in-process
would be less machinery. But an agent runtime is not usually the place a
browser wants to live: Chromium needs its own memory headroom, a compatible
system image and periodic restarts, and it crashes in ways that should not take
the agent down with it. The HTTP seam means the browser can sit on another
machine, be restarted independently, and be spoken to by any client that can
send JSON. It also gives the security boundary somewhere to *be* — a
in-process library has no natural place to check a key or reject a URL.

**A marked DOM, not raw HTML.** Sending page source to a language model is
expensive and mostly noise. The injected script keeps only what is visible and
interactive, labels each one with an integer, and truncates the label text.
That turns an unbounded page into a short list — and makes the agent's
instruction unambiguous, because an integer cannot be a malformed selector.
Ids are regenerated on every look and are valid only for the most recent one.

## Why not a browser extension?

Claude in Chrome — and the equivalents now shipping from other vendors — drives
a real Chrome tab from inside the assistant, with no server to run and no key
to configure. For a person sitting at their own machine, it is simply better
than this, and this README will not pretend otherwise.

Three boundaries are why this still exists:

- **Nobody is at the keyboard.** An extension drives a *human's* live browser
  session, with that human signed in and present. This runs unattended — on a
  VM, from a cron entry, in a pipeline that fires at 03:00. Most automation an
  SME actually pays for is automation precisely because nobody is there for it.
- **The caller is not fixed to one vendor's model.** The HTTP boundary
  ([ADR-002](docs/DECISIONS.md)) means any client that speaks JSON works: GPT,
  a local model, a LangGraph node, a shell script with `curl`.
- **No vendor in a client's production path.** Automation built on one vendor's
  extension inherits that vendor's terms, availability and roadmap, and changes
  on their schedule rather than the client's.

The full argument — including the condition that would invalidate most of it,
which is a vendor shipping a headless or server-side version — is
[ADR-008](docs/DECISIONS.md).

## Security

This server drives a real browser on the machine it runs on, and the URLs it
visits are chosen by a language model — one that may be reading attacker-authored
text on the pages it is already visiting. It is built on that assumption.

**Binds to `127.0.0.1` by default.** Out of the box it is reachable only from
the machine it runs on. It terminates no TLS and does no rate limiting, so
exposing it on a network means putting it behind something that does.

**API-key authentication.** Every endpoint except `/health` requires an
`X-API-Key` header, compared with `hmac.compare_digest` so the check does not
leak the key through response timing. A key shorter than 16 characters is
treated as a misconfiguration, not a credential: the server refuses to
authenticate anyone and answers `503`, having said why at startup. If no key is
set at all the server runs open and logs a warning on every boot — that is a
development convenience and is documented as one.

**SSRF guard.** A prompt-injected agent's most valuable target is not the
public internet; it is whatever is reachable from the server's own network
position — the cloud metadata endpoint, an admin panel on the LAN, this
server's own port. Requested URLs must be `http` or `https`, and are rejected
when the hostname is an IP literal in a loopback, private, link-local,
multicast, reserved or unspecified range, in IPv4 or IPv6, including
IPv4-mapped IPv6 forms.

**What the SSRF guard does not do, stated plainly:** it never resolves DNS. It
inspects the hostname as written, so it stops `http://127.0.0.1/` and
`http://169.254.169.254/` but not `http://localhost/`, not an internal name
like `http://intranet.corp/`, and not an IP written in decimal
(`http://2130706433/`). Resolving names at validation time would close the
first case and still lose to DNS rebinding, because the browser resolves again
when it actually navigates. The guard is a cheap structural filter that removes
the easy cases; the real containment is the network the server is allowed to
sit on. These gaps are pinned by tests in
`tests/test_url_validation.py` so they stay visible, and the reasoning is in
[ADR-004](docs/DECISIONS.md).

**Other hardening.** Popups and new tabs are closed on arrival, so a page
cannot swap the agent's active page out from under it with `window.open`.
Downloads are refused at the context level. Typed text, element ids, key names
and wait durations are all bounded by the request models before anything
reaches the browser. Internal exception detail is logged, never returned — API
error responses are fixed strings.

**No bot-detection evasion.** An earlier revision integrated
`playwright-stealth` behind a flag. It was removed rather than left switched
off — the flag, the dependency, the endpoint and the config entry — and a test
fails the build if any of it returns. A site that blocks automated traffic is
making a decision this project respects. See [ADR-005](docs/DECISIONS.md).

## Stack

| | |
|---|---|
| **Runtime** | Python 3.11+ · asyncio (developed and tested on 3.14.5) |
| **Browser** | Playwright · Chromium |
| **API** | FastAPI · Pydantic v2 · Uvicorn (single worker) |
| **Imaging** | Pillow — screenshots downscaled to ≤1024px JPEG q75 |
| **Tests** | pytest · pytest-asyncio · pytest-cov · httpx ASGI transport |

**366 tests, 99% coverage.** The suite runs fully offline and never launches a
browser — Playwright is faked — so it needs no browser binary and reaches no
network.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"   # paste into API_KEY

python main.py
```

Then:

```bash
curl http://127.0.0.1:8765/health
curl -X POST http://127.0.0.1:8765/navigate \
     -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
     -d '{"url": "https://example.com"}'
```

Interactive API docs are at `http://127.0.0.1:8765/docs`.

### Tests

```bash
pip install -r requirements-dev.txt
pytest                                                    # 366 tests
pytest --cov=main --cov=config --cov=browser --cov-report=term-missing
```

No `playwright install` needed for the test suite.

## The agent side

[`skills/browser/SKILL.md`](skills/browser/SKILL.md) is the client contract —
the endpoint reference plus the operating rules an agent needs: element ids are
valid only for the most recent `/state`, look at the screenshot before acting,
`/reset` to recover a stuck session.

## Endpoints

| | |
|---|---|
| `GET /health` | Liveness. No auth, no lock, no browser. |
| `GET /state` | Screenshot + marks, no action taken. |
| `POST /navigate` | Load a URL. Best-effort cookie-banner dismissal. |
| `POST /click` | Click a marked element. |
| `POST /type` | Focus a marked element and type into it. |
| `POST /scroll` | One eased viewport step, up or down. |
| `POST /press_key` | Send a key or modifier combo. |
| `POST /wait` | Pause up to 5s, then re-observe. |
| `POST /reset` | Rebuild the context and page to recover a stuck session. |

Every action endpoint returns the same state shape as `GET /state`, so the
agent always ends a step looking at the result of it.

## Limitations

- **One session per process.** Requests queue. Not a multi-tenant service.
- **iframes and shadow DOM are not indexed.** Embedded payment forms and
  reCAPTCHA widgets will not appear in `dom_marks`.
- **Blocked is blocked.** No evasion; sites that refuse automation win.
- **No TLS, no rate limiting.** Loopback by default for that reason.

## Project status

**Feature-complete; not actively developed.** For interactive, at-the-keyboard
browsing the vendor extensions are better, and building a second and worse
version of something that already works is not a good use of the time. This
stays useful for the unattended case and for non-Claude callers — see
[ADR-008](docs/DECISIONS.md) — and it is left in a known state rather than
walked away from: the suite is green, the limitations above are the real ones,
and the two open items (the SSRF guard's DNS gap, and a Python floor claimed at
3.11 but only verified on 3.14.5) are written down rather than quietly dropped.

## License

MIT — see [`LICENSE`](LICENSE).

# Architecture decision records

Short records of the decisions that shaped this server, including the one that
was reversed. They are kept because the discarded option explains the shape of
what remains — and because a decision log with no reversals in it is usually a
decision log that was written afterwards.

---

## ADR-001 — One persistent session behind a lock, not a session pool

**Status:** accepted · **Date:** 2026-05-28

### Context

The obvious way to build a browser service is the way you build any other
service: a pool of workers, a browser context per request, horizontal scaling
behind a load balancer. That is how you would serve screenshots or run
scrapes.

It does not survive contact with the actual workload. An agent browsing session
is a *chain* of dependent steps:

```
navigate → dismiss cookie banner → type into search → click result 3 → scroll → read
```

Every step depends on state the previous step left in the browser, and almost
none of that state is in the request. Cookies and session storage hold the
login. The DOM holds a half-filled form. The scroll offset determines which
elements are even marked. The element id `3` is meaningless except against the
exact page the previous step produced.

Route step four to a different context in the pool and the login is gone, the
form is empty and id `3` points at nothing — or worse, at something else.

A pool with sticky sessions would work, but sticky sessions keyed to a
conversation, with one browser per key, is a session pool in name only. It has
all the cost and none of the benefit.

### Decision

One `BrowserSession` per process, holding one browser, one context and one page
for the process lifetime. A single module-level `asyncio.Lock` wraps every
handler, so requests queue rather than interleave.

The server must run with `workers = 1`. This is enforced at the entry point and
stated in the README, the skill file and the code comment at
`main.py`, because multi-worker mode fails in the most confusing possible way:
it starts cleanly, serves requests, and silently round-robins them across N
independent browsers that share nothing.

### Consequences

The server handles one conversation at a time. That is a real limit and it is
stated plainly rather than buried — it is not a multi-tenant service, and
scaling means running more instances, not more workers.

In exchange the concurrency model is trivially correct. There is no
interleaving of actions on a shared page, no lost-update race between a click
and the `/state` that reads its result, and no session-affinity infrastructure
to get subtly wrong. `tests/test_endpoints.py` asserts the serialisation
directly: ten concurrent requests, maximum observed concurrency of one.

---

## ADR-002 — An HTTP boundary, not an in-process library

**Status:** accepted · **Date:** 2026-05-28

### Context

The agent could `import playwright` and drive the browser directly. That is
less code, one less process, no serialisation, no auth to write.

Three things argued against it:

1. **Chromium is a bad tenant.** It wants its own memory headroom, a system
   image with the right shared libraries, and periodic restarts. Co-locating
   it with the agent runtime couples two very different operational profiles,
   and a browser crash becomes an agent crash.
2. **The browser often wants to be somewhere else.** Headful on a desktop for
   debugging, on a Linux box with the right fonts, on a machine with a
   particular network vantage point. An import pins it to wherever the agent
   happens to run.
3. **A library has nowhere to put a security boundary.** This is the argument
   that actually settled it. The URL that gets navigated to is chosen by a
   language model that is, by the nature of the job, reading untrusted text.
   Something has to check that URL, and an in-process helper function is a
   check the caller can simply not call. A network boundary is a place where
   authentication, validation and rate limiting have somewhere to live and
   cannot be bypassed by the caller changing its mind.

### Decision

HTTP, with an API key on every endpoint but `/health`, and Pydantic validation
on every request body.

### Consequences

More moving parts: a process to start, a key to configure, a port to keep
private. A round-trip and a base64-encoded screenshot per step, which is why
the response is gzipped and the screenshot is downscaled to a ≤1024px JPEG.

What it buys is that the security surface is a real surface. Every test in
`tests/test_url_validation.py` and `tests/test_auth.py` exists because there is
a boundary to test.

---

## ADR-003 — API-key auth, with a minimum length enforced at request time

**Status:** accepted · **Date:** 2026-05-28

### Context

The server needs to distinguish its agent from anything else that can reach the
port. The candidates were mutual TLS, an OAuth-style token flow, or a shared
secret in a header.

The deployment shape is one server, one client, usually on the same machine or
the same private network. mTLS means a certificate authority to run for a
two-party system. A token flow means an issuer. Both are real infrastructure
for a problem that a shared secret solves.

The failure mode of a shared secret is that it gets set to `changeme` and
stays there.

### Decision

A shared secret in `X-API-Key`, compared with `hmac.compare_digest`, with two
rules that make the weak cases loud:

- **No key set** → the server runs open and logs a warning at every startup.
  Dev mode is allowed, but never silent.
- **Key shorter than 16 characters** → the server refuses to authenticate
  anyone, answering `503 Server misconfigured` and logging the reason at
  startup. It does not accept the weak key, and it does not answer `401`,
  because the problem is the server's configuration and not the client's
  credential.

`.env.example` ships with `API_KEY=` empty rather than a placeholder value, and
includes the command to generate a real one.

### Consequences

A weak key cannot be used, which is stronger than warning about it. The
16-character floor is arbitrary but has to be somewhere, and the boundary is
tested in both directions.

`hmac.compare_digest` rather than `==` keeps the comparison constant-time;
`tests/test_auth.py` asserts that it is actually the function being called, so
a future refactor cannot quietly reintroduce `==`.

What this does not provide: no key rotation, no per-client identity, no
revocation, no rate limiting, and no TLS. The default `127.0.0.1` bind is doing
a lot of the work, and the README says so.

---

## ADR-004 — SSRF: deny known-bad IP ranges, without resolving DNS

**Status:** accepted, with a documented limitation · **Date:** 2026-05-28

### Context

The URLs this server visits are chosen by a language model reading untrusted
web content. Prompt injection therefore becomes SSRF: text on a page persuades
the agent to navigate somewhere, and the request goes out from *this server's*
network position — which may be a cloud instance with a metadata endpoint at
`169.254.169.254`, a LAN with an unauthenticated admin panel on it, or the
loopback interface where this very server is listening.

Three approaches were considered.

**An allowlist of permitted domains** is the only complete answer, and it
destroys the product. The entire value proposition is that the agent can visit
a site nobody wrote code for.

**Resolve the hostname, then check the resolved address.** This is the
textbook answer and it is genuinely stronger. It also does not actually close
the hole: the check resolves the name, and then the *browser* resolves it
again when it navigates. A DNS record with a one-second TTL can answer with a
public address for the first lookup and `127.0.0.1` for the second. Closing
that properly means pinning the resolved address and forcing the browser to
connect to it — which Playwright does not make available in any clean way —
and it adds a blocking DNS lookup, and a new failure mode, to every navigation.

**Reject IP literals in known-bad ranges.** Cheap, synchronous, no failure
modes, catches the direct attempts.

### Decision

The third. `_is_blocked_ip` rejects a hostname that parses as an IP address in
a loopback, private, link-local, multicast, reserved or unspecified range — in
IPv4 and IPv6, which covers IPv4-mapped IPv6 forms such as
`::ffff:127.0.0.1`. Non-`http(s)` schemes are rejected outright, which
disposes of `file://`, `data:` and `javascript:`. A hostname that is not an IP
literal is passed through unresolved.

### Consequences

**This is a partial defence and the documentation says so in those words.**
Everything below is allowed today:

| Input | Why it passes |
|---|---|
| `http://localhost/` | loopback reached by name |
| `http://intranet.corp/` | internal name on a split-horizon resolver |
| `http://metadata.google.internal/` | cloud metadata by name |
| `http://127.0.0.1.nip.io/` | wildcard DNS resolving to loopback |
| `http://2130706433/` | `127.0.0.1` as a decimal integer |
| `http://0177.0.0.1/` | octal first octet |
| `http://100.64.0.1/` | CGNAT space, which Python does not class as private |

These are asserted in `tests/test_url_validation.py`, in a section that says
explicitly that it pins current behaviour rather than desired behaviour. The
point is that the limitation cannot quietly stop being true, and that anyone
tightening the guard will have to rewrite those tests deliberately.

The honest framing, which the README uses: this guard removes the easy cases.
The containment that actually matters is the network the server is allowed to
sit on, and the `127.0.0.1` default bind.

**Revisit if** the server is ever deployed somewhere with a metadata endpoint,
or anywhere it can reach a network the operator cares about. At that point
resolve-and-pin, or an egress proxy with its own allowlist, stops being
over-engineering.

---

## ADR-005 — Remove the stealth integration rather than ship it switched off

**Status:** accepted — reverses an earlier decision · **Date:** 2026-09-01

### Context

An earlier revision integrated `playwright-stealth`: a `STEALTH` config flag, a
`_apply_stealth_to_context()` call on every context creation, a
`POST /stealth` endpoint to toggle it at runtime, and a section in the skill
file telling the agent when it was allowed to use it.

It was built carefully. It defaulted to off. The dependency was commented out
in `requirements.txt` with a note to install it "only where Terms of Service
permit". The skill file instructed the agent not to enable it unless the user
explicitly asked, and reminded it that stealth was "not a license to violate a
site's Terms of Service".

None of that changes what the library is for. `playwright-stealth` has exactly
one purpose: to make an automated browser register as a human one to
bot-detection systems. Its whole surface — patching `navigator.webdriver`,
masking the headless fingerprint, faking plugin and codec tables — is aimed at
defeating a site's decision about who may access it. A flag that defaults to
off is still a feature; the careful documentation around it was, on honest
reading, an argument that the feature should not exist.

The deciding argument was not the library, though. It was the shelf it sits on.
This repository will be linked publicly next to one whose compliance section
states that an earlier Terms-of-Service-violating scraper was **deleted rather
than shipped**. A portfolio that says that in one repository and ships a
bot-detection bypass in the next is not making a compliance claim. It is making
a claim about how deep the compliance goes, and answering it.

### Decision

Remove it completely, before the code's first commit: the flag, the import, the
per-context call, the `POST /stealth` endpoint, the request model, the config
entry, the commented-out dependency and the skill documentation. Not commented
out, not defaulted off, not behind an environment variable.

The removal was staged **ahead of the initial commit of the codebase** rather
than as a later revert, so `playwright-stealth` never enters the public git
history. A reviewer running `git log -p` finds no stealth integration to
discover, because as far as the published repository is concerned there was
never one.

`tests/test_no_stealth.py` scans every shipped source file, the requirements
files and the skill markdown for the string, and asserts the absence of the
endpoint, the request model, the config attribute and the session methods.
Reintroducing any of it fails the build.

### Consequences

Sites that block automated traffic will block this server. That is now a stated
property in the README and the skill file rather than a gap to be worked
around, and the skill instructs the agent to respect a refusal instead of
looking for a way past it.

What is kept is the part that was never evasion in the first place — see
ADR-006.

---

## ADR-006 — Keep the delays; rename `humanizer` to `pacing`

**Status:** accepted · **Date:** 2026-09-01

### Context

Removing stealth (ADR-005) raised the obvious follow-up: the module named
`humanizer.py`, which added a randomised delay before each action, a
per-keystroke typing delay, and a smoothstep-eased scroll. The name says
evasion. Does the code go too?

Reading it, no. What it actually does:

- **A pre-action delay** keeps request rates civil and lets the previous
  action's handlers finish.
- **A per-keystroke delay** is a correctness requirement. Setting an input's
  value instantly outruns debounced autocompletes and validation-on-keyup;
  the page's own JavaScript never sees the input.
- **An eased scroll** is the load-bearing one. Infinite scroll and image lazy
  loading are driven by scroll events and `IntersectionObserver` callbacks.
  A single large `mouse.wheel` jump fires one event and skips every
  intermediate position, so the content the agent came for never loads.
  Stepping through a smoothstep ramp fires the intervening events.

The randomisation is the only part that reads as evasion, and it is too weak to
be any: jitter in a 0.3–1.2s band does not defeat a detector that is reading
`navigator.webdriver`.

So the code stays and the *name* was the problem. `humanizer` claims an intent
the module does not implement, and after ADR-005 it would be the one artefact
in the repository still implying evasion.

### Decision

Rename to `browser/pacing.py`, with a module docstring that states the two real
reasons — request pacing and lazy-load triggering — and explicitly disclaims
the third: *"These helpers do not mask the automation. Playwright remains
detectable... nothing here alters the browser fingerprint."*

### Consequences

The behaviour is unchanged; only the name and the documentation are.
`tests/test_pacing.py` asserts the properties that matter functionally — that
the eased steps sum to exactly one viewport step, that they arrive as five
separate wheel events rather than one, and that the ramp is symmetric — and
also asserts that the module docstring still says what it is for, so the
explanation cannot rot away from the code.

---

## ADR-007 — Integer element handles, not selectors

**Status:** accepted · **Date:** 2026-05-28

### Context

The agent has to say which thing on the page to click. The options were: let it
write a CSS selector, let it write JavaScript, or give it a menu.

Selectors and JavaScript both mean the agent composes a string that is then
executed against the page. That is a large surface for the agent to get wrong
in ways that are hard to distinguish from the page having changed — and it puts
model-authored code into the page's execution context.

### Decision

A script injected on every observation finds visible interactive elements,
stamps each with `data-mark-id`, and returns a compact list of
`{id, tag, text, type}`. The agent picks an integer. The server turns it into
`[data-mark-id='N']` itself.

Ids are regenerated on every observation and are valid only for the most recent
one, which the skill file states as its first rule.

### Consequences

The agent cannot express an invalid selector, because it cannot express a
selector. A stale id produces a specific, actionable error —
`element 14 no longer exists — call get_state to refresh` — rather than a
silent mis-click.

The context sent to the model shrinks from a full DOM to a short list, with
label text truncated to 80 characters.

The cost is that anything the script does not index is invisible: **iframes and
shadow DOM are not traversed**, so embedded payment forms and reCAPTCHA widgets
do not appear. This is listed under Limitations in both the README and the
skill file. `total_page_marks` is returned alongside the viewport marks
specifically so the agent can tell "nothing here" from "nothing here *yet* —
scroll".

---

## ADR-008 — Why this exists alongside a vendor browser extension

**Status:** accepted, with a stated expiry condition · **Date:** 2026-09-03

### Context

Since this server was designed, vendor-supplied browser control has become
ordinary. Claude in Chrome, and the equivalents shipping from other vendors,
drive a real Chrome tab from inside the assistant itself: no server to run, no
API key to configure, no SSRF guard to write, and no `--workers 1` footgun to
document.

For a person sitting at their own machine asking an assistant to do something
on a page, that is straightforwardly better than this. It is already logged
into the sites they use, there is nothing to install and nothing to operate,
and it sees the page exactly as they do. Any reader who knows the extension
exists will ask why this repository does too, and the README should not dodge
the question.

Three boundaries answer it. Each is a place where the extension model does not
reach, not a place where this is merely different.

**1. Nobody is at the keyboard.** An extension drives a *human's* live browser
session — it needs that human signed in, with the browser open, on the machine
where the work happens. This server runs unattended: on a VM, from a cron
entry, inside a pipeline that fires at 03:00. That is not an edge case for the
work this is aimed at. Most automation an SME actually pays for is automation
precisely because nobody is present for it — the nightly supplier-portal check,
the form that gets filled when an order arrives, the weekly figure pulled from
a system with no API. An interactive tool cannot do the one thing that makes
those worth paying for.

**2. The caller is not fixed to one vendor's model.** ADR-002 chose an HTTP
boundary over an in-process library, and the consequence compounds here:
anything that can send JSON is a valid client. GPT, a local Llama or Qwen, a
LangGraph node, a Bash script with `curl`, a test harness with no model in it
at all. An extension is, by construction, one assistant's hands. This is a
capability any caller can rent.

**3. Vendor dependency in a client's production path.** A client's automation
built on one vendor's extension inherits that vendor's terms of service,
availability, pricing and roadmap. When the extension changes what it permits
or how it is billed, the client's process changes with it, on the vendor's
schedule rather than theirs. A ~600-line server they can read, host and pin is
a different kind of commitment to recommend — the dependency is Playwright and
Chromium, both of which they could already have been depending on.

### Decision

Keep this, and say plainly in the README what it is *not* for: it is not the
right tool for interactive, at-the-keyboard browsing, and the extension is the
better answer there. It is the right tool for unattended browser work, for
non-Claude callers, and for anywhere a vendor extension is not an acceptable
production dependency.

The README gets a short section pointing here, rather than leaving an obvious
question unanswered on the front page.

### Consequences

**The first boundary is the load-bearing one, and it is the one most likely to
stop being true.** If Anthropic — or any vendor — ships a server-side or
headless version of this capability, "nobody at the keyboard" largely
evaporates, and boundary 3 weakens with it because the vendor path would then
be a deployable one. Boundary 2 would survive, but it is the weakest of the
three on its own: "works with any model" is worth less than "works with no
human", and a single-vendor tool that runs unattended would cover most of what
this covers. **That is a real risk to the argument, stated here rather than
argued away.** Revisit this record if that ships; the honest outcome at that
point may be that this becomes a reference implementation rather than a
recommendation.

**Feature work on this project stops here.** For the author's own day-to-day
use the extension is simply better, and building a second, worse version of
something that already works is not a good use of the time. What this remains
is two things, both real: portfolio evidence — an HTTP security boundary, a
documented threat model, 366 offline tests and a decision log with a reversal
in it — and a deployable component for the unattended case, ready when a
project needs one.

That is a pause with a reason, not an abandonment. The suite is green, the
limitations are written down, and the SSRF and Python-floor items in the local
`TODO.md` are recorded rather than quietly dropped. Anyone picking it up,
including a later version of the author, starts from a known state.

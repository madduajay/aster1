# Aster

A local personal agent for macOS, built with FastAPI, DeepAgents/LangGraph, OpenAI, Browser Use, SQLite and a vanilla JavaScript chat UI.

This checkout includes application source, setup files, tests and validation scripts. Private runtime data, passwords, API keys, browser profiles, logs, archives and virtual environments are ignored by Git.

Story-writing requests include one polished illustration by default, including trip stories, unless the user explicitly requests text only or no pictures. The main agent calls `generate_illustration`, which creates or edits an image and checks it against the accompanying prose and user request. GPT-Image-2.6 Sunburst is the default generator (`PERSONAL_AGENT_IMAGE_MODEL` can override it); GPT-6.1 Terra performs the visual check. The existing API account needs access and credits for both models. Images are private to the owning user/chat. One failed check permits one correction attempt, with at most three scenes per request. A passed check improves alignment but cannot guarantee a perfect match. No dining/travel subagent or Company MCP participates in image creation.

Browser interaction defaults to the shared **PoC policy** in `personal_agent/browser_policy.py`: routine controls and ordinary form preparation proceed automatically, observed public provider links/redirects can extend the current task scope, and empty fields do not block a page-opening goal. Explicit URL navigation can be verified directly; other outcomes still use the independent verifier. An unchanged approval can be reused only before dispatch; changed state or a possibly executed submission cannot reuse it. The bounded profile allows 45 browser steps, 60 browser model calls, 12 recoveries (at most 3 for the same failure), and a ten-minute foreground task window. Final commitments and manual private/human-verification steps remain distinct from preparation. Set `PERSONAL_AGENT_BROWSER_POLICY=strict` before starting Aster to restore conservative actuation and provider approvals. Saved browser profiles and runtime data remain local and excluded from Git.

## Requirements

- macOS with Python 3.12 and its standard Keychain Access utility.
- An OpenAI API key with access to the models configured in `personal_agent/config.py`.
- A trusted TLS certificate valid for `aster.localhost`, with its matching private key. Provision these locally; neither belongs in Git.

## Install

From this checkout:

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
PLAYWRIGHT_BROWSERS_PATH="$PWD/.runtime/playwright" .venv/bin/python -m playwright install chromium
.venv/bin/python -m venv .runtime/browser-use-venv
.runtime/browser-use-venv/bin/pip install -r browser-use-requirements.lock
```

In macOS Keychain Access, create or update a generic password with service/name `local.aster.openai`, account `aster`, and your API key as its password. The launcher reads this item without putting the key in source, logs or command-line arguments.

Create `.runtime/tls/` and place your trusted certificate at `.runtime/tls/server.pem` and matching private key at `.runtime/tls/server.key`. Initialize the local certificate fingerprint manifest:

```sh
.venv/bin/python - <<'PYTLS'
import hashlib, json, os
from pathlib import Path
os.umask(0o077)
directory = Path('.runtime/tls')
for path in (Path('.runtime'), directory):
    path.chmod(0o700)
files = {suffix: directory / ('server.' + suffix) for suffix in ('pem', 'key')}
for path in files.values():
    path.chmod(0o600)
manifest = directory / 'fingerprints.json'
if manifest.exists():
    raise SystemExit('Manifest already exists; keep existing fingerprints.')
manifest.write_text(json.dumps({suffix: hashlib.sha256(path.read_bytes()).hexdigest()
                                for suffix, path in files.items()}))
PYTLS
```

The manifest lets the launcher use your provisioned TLS files without its legacy certificate-copy fallback. Keep the certificate trusted by macOS and your browser; the launcher verifies its fingerprint on later starts.

```sh
.venv/bin/python manage.py setup
.venv/bin/python manage.py start
.venv/bin/python manage.py status
```

Open **https://aster.localhost:55468**. Setup provisions ten local accounts, `user01` through `user10`; initial passwords are written to the owner-only `.runtime/initial-accounts.txt`. Each user must set their own password at first login. Stop with `.venv/bin/python manage.py stop`.

## Chat and images

Messages sent while a task is running in the same chat join that task. Aster combines the original request with additional instructions at its next planning step, keeping completed tool results rather than restarting the work. Later corrections override conflicting details. Files can be attached to these follow-ups. Stop/New Chat retain their cancellation behavior; browser approval and manual-input pauses still require their own controls. Requested itineraries proceed to the relevant subagents automatically without a separate trip-brief confirmation.

The UI supports formatted response cards, short answers by default, browser preview and manual takeover, and action approval cards. Recommendations automatically trigger web research, including follow-up requests. Click **+** in the composer to select files, or drag and drop them. Image thumbnails stay inside the message box while you type your question. Remove an attachment with its × button, or press Enter to send the images and question together as one message. Files are uploaded only when you send; a failed send preserves the question and attachments for retry. Sent image previews remain visible when reopening the chat. PNG, JPEG, WebP and GIF are recognized. General files have a 50 MB limit; images have a 20 MB limit, with up to four recent images supplied to the model within a combined 20 MB budget. Generic documents are available for approved website uploads; general document parsing is not included.

Sessions and persistent preferences are separate. Useful commands include `/sessions`, `/open SESSION_ID`, `/files`, `/memory`, `/remember TEXT`, `/forget ID`, `/forget all`, `/correct ID TEXT`, `/debug` and `/stop`. Private website credentials and verification codes belong in the interactive browser takeover, not chat. Consequential browser actions require their displayed approval.

Browser inspection reads public link, form, accessibility and test-ID attributes. Unsupported attributes and field values are omitted automatically instead of terminating a discovery query. Clear reservation-entry links and dialog openers proceed without an extra approval; final booking submissions, payments, account changes and private login fields retain their existing protections.

## Browser modes

Use the **Browser** selector in the chat header:

- **Headless** (default): bundled Chromium without a desktop window.
- **Visible**: installed Chrome starts with the tested minimal arguments; Playwright attaches afterward and Browser Use shares that session. It uses Chrome's normal keychain/cookie handling, natural window dimensions and native display pixel ratio. Preview and input coordinates follow the current CSS viewport without resizing the website to fit the sidebar. Aster does not tile, reposition, minimize, or hide the window.

Choose **Take control** to enter information in the wider right-side browser panel while keeping chat visible. **Release control**, at the top of the browser panel, continues a paused task with Browser Use once the website step is complete; after a completed task it simply ends manual control. **Return to chat** closes the panel without resuming a paused task. On narrow screens the panel uses the available screen width.

Cancel task stays beside Take control in the browser header. It stops pending work, closes the Aster-owned browser and its preview, and retains the saved profile. Returning to the website later starts a new browser request.

Both modes use native browser networking without Aster request interception. During manual website login or verification, the agent pauses without model calls or browser actions. Visible keeps Browser Use connected and idle, matching the isolated login test; Headless disconnects its Browser Use attachment. **Release control** refreshes page references before continuing, reconnecting Headless to the same tab when needed. These defaults persist across tasks and application restarts. Passive network diagnostics remain available.

Related preview questions first run a fresh read-only Browser Use inspection. These reads use page text first, with optional screenshots and direct reading rather than repeated extraction-model calls. A continuation reuses evidence from that request and checks only the specific missing facts. Web search is a fallback when the page cannot supply sufficient verified information. A topic change retires the preview and its browser working context; returning to that subject requires a new browser task. Chat history and explicit saved preferences remain available.

For a fresh Visible session, Aster opens a scoped public entry URL in native Chrome before attaching browser tools. Explicit user URLs take precedence; account lookups can reuse a previously observed login entry page for that user and domain. Session parameters are excluded. Existing open sessions are not navigated by this startup step.

Visible uses the normal Chrome service-worker policy, matching the isolated minimal-launch tests. Headless retains its existing persistent-context configuration. Chrome's control endpoint is loopback-only; Aster owns and closes only its dedicated Chrome process.


Finish or cancel active tasks before changing browser engine; switching closes browser tabs and may discard unsubmitted fields. No application restart is needed to switch modes. The choice is saved per user.

Headless uses `.runtime/browser-profiles/<user>/`; visible Chrome uses `.runtime/chrome-profiles/<user>/`. Saved logins are retained in these separate profiles. An imported copy of a personal Chrome profile stays local; Aster does not control the original personal profile. Browser Use and Playwright share the same Aster-owned browser.

## Dining, travel and background memory

Travel can start with loose guidelines. Aster researches three destination ideas and recommends one, or compares three flexible date windows, before requiring exact booking details. It discloses provisional defaults (economy, mid luxury (four-star) hotels, two travelers and one room; seven days when duration is unspecified) and keeps them separate from confirmed choices. Missing departure airport does not block destination or seasonal ideas. Comparisons use researched evidence, not a market-wide cheapest-fare engine. Exploratory checks never create watches or monitors. Requested itineraries stream in order: dates/defaults, flights, hotels, then verified days. Partial day plans remain provisional until every date is complete; reconnects replay saved sections without duplicating them.

When a trip request already specifies its destination and date range, Aster proceeds directly to the relevant Travel and Dining checks. Day-by-day plans add targeted sightseeing research after those delegations; they do not restart destination discovery or require a separate trip-brief confirmation.

Travel drafts survive restart within their owning chat and are invalidated by context clearing or excluded evidence. Preparation covers gear, transfers, connectivity, documents, forms and appointments using existing research and browser tools. Purchases, submissions and bookings retain inline approval; no preparation reminders are added. The opt-in `.venv/bin/python -m validation.travel_matrix --discovery` uses paid models/search against isolated local inventory and Temporal, with browser execution disabled, and writes `.runtime/travel-discovery-evaluation.json`.

Dining and travel now use independent DeepAgent services. Dining delegates to Company Dining MCP; Travel delegates to separate Hotels and Flights MCP endpoints. Opening events return through signed subagent webhooks and Temporal, then appear in the original chat and alerts. Explicit monitoring requests create native Temporal Schedules at the requested interval, with a five-minute default and one-minute minimum. See [the service architecture, sequence diagram and operating instructions](docs/availability-subagents.md).

A background worker starts with Aster. It reviews existing long-term facts against their original user evidence after five minutes of inactivity, checking hourly for changes. It stores a compact profile only after a second grounding check. Corrections and forgetting invalidate that derived profile; source facts are never automatically rewritten. This follows the evidence review and consolidation approach described in [Anthropic's memory article](https://www.anthropic.com/research/components-of-agent-memory). Use `/memory profile` and `/memory consolidate`.

The main harness calls `delegate_dining` or `delegate_travel`; each subagent's own harness calls the domain Company search tools. Available options return immediately without an event-watch workflow. Unavailable options receive main/subagent Temporal workflows before Company watch registration. Only Carbone New York, The Fifth Avenue Hotel New York, and Emirates first class to Dubai start unavailable; their openings arrive after 10, 20 and 30 seconds respectively. Missing details are requested in chat. This flow does not make bookings.

For open-ended requests such as Indian restaurants around Phoenix with a table for two, the agent can discover venues through web research, then checks their slots through Company MCP. Webpages and Resy search snippets are not accepted as table-availability confirmation. If no required Company check completes, Aster withholds the unsupported availability claim.

`manage.py start` starts Temporal, three Company endpoints and two subagents before Aster. Companions remain running when Aster stops and preserve pending events across worker downtime. The Mac must remain awake. Use `.venv/bin/python dining_stack.py status` or `stop` to manage the companion services. Temporal UI is at http://localhost:8233; service ports and independent startup details are documented in the architecture guide above. The legacy dining worker remains registered to drain previously started executions.

Alerts belong to the requesting account. Clicking the top Alerts button clears current notifications and hides the button when none remain; read status persists through logout/login. Notifications appear in the application, with no email or operating-system push. Only explicit availability requests create watches. Use `/watches` or `/watch cancel ID` to inspect or cancel them. The shared workers reload each user's persisted state rather than keeping a model session alive per user.

Opening events also post an assistant message to the chat that requested the watch. The message appears live when that chat is open, persists after alert dismissal and logout, and is delivered once even if Temporal retries. Other open chats and unsent drafts are preserved.

“Book now” and similar booking follow-ups use `browser_task` with the existing venue, date, time and party size. They do not recheck Company or create another availability watch. Company has no booking capability; the browser handles booking execution and final submission approval.

## Runtime configuration and storage

Model defaults, the local origin and timezone are in `personal_agent/config.py`. Browser model and default timezone can be overridden with `PERSONAL_AGENT_BROWSER_MODEL` and `PERSONAL_AGENT_TIMEZONE` before launch. The listener binds only to `127.0.0.1:55468`; no automatic startup service is installed.

All local state is under `.runtime/`, including SQLite data, user profiles, uploads, TLS files, and logs. Back it up separately while the app is stopped if you need to preserve accounts and conversations. The repository alone does not contain that state. The launcher expects to manage the process from this checkout; stop an instance in a different checkout before starting this one.

`tunnel.py` is included for optional ngrok access. To enable it, install the official ngrok executable locally at `.runtime/bin/ngrok`, place its authtoken in the owner-only `.runtime/ngrok-token.txt`, then run `.venv/bin/python tunnel.py start` and restart the app. Stop the tunnel with `.venv/bin/python tunnel.py stop`, then restart the app to remove its public origin. This is optional; the local URL works independently.

## Models and research

- GPT-5.4 mini: chat, image questions, memory, research routing and web search. Chat uses the Responses API with low reasoning effort.
- GPT-6.1 Terra: browser planning and independent browser outcome verification.
- Model requests retry transient connection failures, rate limits and server errors up to twice using SDK exponential backoff. Authentication and invalid requests fail immediately. Retries do not replay browser actions or restart a partially consumed stream. Per-request timeouts remain 45 seconds (40 for the browser planner), within existing task deadlines.
- Provider facts, prices, availability, policies and transaction details use official sources. Reputable independent reviews, reporting and comparisons are allowed as a separately labeled research mode, constrained to selected publisher domains. Unchecked links remain visible with an unverified label.

## Development and verification

```sh
.venv/bin/python -m pytest -q
RUN_BROWSER_TESTS=1 .venv/bin/python -m pytest -q
```

Browser tests use isolated accounts and synthetic services. Live acceptance scripts can call paid models; read their caps and scope before running them. The old pytest failure cache can contain names of tests that were renamed; current test output is authoritative.



Local Git tracks source and tests. `_to_delete/` is an ignored review area for old test artifacts, archives and snapshots; its cleanup manifest records their original paths and hashes. Nothing is permanently removed by the maintenance cleanup.

- [Architecture](docs/ARCHITECTURE.md) — main components and boundaries.
- [Changelog](docs/CHANGELOG.md) — recent changes.
- [Historical implementation notes](docs/HISTORY.md) — archived former README.
- [Historical verification report](docs/VERIFICATION.md) — retained prior validation evidence.

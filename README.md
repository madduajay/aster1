# Aster

A local personal agent for macOS, built with FastAPI, DeepAgents/LangGraph, OpenAI, Browser Use, SQLite and a vanilla JavaScript chat UI.

This checkout includes application source, setup files, tests and validation scripts. Private runtime data, passwords, API keys, browser profiles, logs, archives and virtual environments are ignored by Git.

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

The UI supports formatted response cards, short answers by default, browser preview and manual takeover, and action approval cards. Recommendations automatically trigger web research, including follow-up requests. Click **+** in the composer to select files, or drag and drop them. Image thumbnails stay inside the message box while you type your question. Remove an attachment with its × button, or press Enter to send the images and question together as one message. Files are uploaded only when you send; a failed send preserves the question and attachments for retry. Sent image previews remain visible when reopening the chat. PNG, JPEG, WebP and GIF are recognized. General files have a 50 MB limit; images have a 20 MB limit, with up to four recent images supplied to the model within a combined 20 MB budget. Generic documents are available for approved website uploads; general document parsing is not included.

Sessions and persistent preferences are separate. Useful commands include `/sessions`, `/open SESSION_ID`, `/files`, `/memory`, `/remember TEXT`, `/forget ID`, `/forget all`, `/correct ID TEXT`, `/debug` and `/stop`. Private website credentials and verification codes belong in the interactive browser takeover, not chat. Consequential browser actions require their displayed approval.

Browser inspection reads public link, form, accessibility and test-ID attributes. Unsupported attributes and field values are omitted automatically instead of terminating a discovery query. Clear reservation-entry links and dialog openers proceed without an extra approval; final booking submissions, payments, account changes and private login fields retain their existing protections.

## Dining, travel and background memory

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

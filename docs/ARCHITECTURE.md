# Architecture

The browser UI sends authenticated requests to FastAPI, which coordinates a per-user agent and SQLite storage.

| Component | Responsibility |
| --- | --- |
| `manage.py` | Manual HTTPS startup, shutdown, TLS fingerprint checks and Keychain loading |
| `personal_agent/app.py` | Authentication, API routes, attachment upload and live browser controls |
| `service.py` | User locks, task lifecycle, context, streaming, cancellation, and memory scheduling |
| `agent.py` | DeepAgents graph, research routing, web search and model/tool limits |
| `db.py` / `memory.py` | SQLite persistence, summaries, preferences, correction and forgetting |
| `attachments.py` | Owned file metadata and bounded image input for model calls |
| `browser_tasks.py` | Worker supervision, approval receipts, duplicate protection and independent verification |
| `browser_use_worker.py` | Isolated Browser Use perception/planning and guarded action execution |
| `browser_host.py` / `browser_control.py` | Per-user Chromium profiles and temporary manual takeover |
| `sources.py` | Source policy, domain constraints, provenance labels and safe link handling |
| `static/` | Chat rendering, composer attachments, streaming and takeover UI |
| `tunnel.py` | Optional ngrok lifecycle; public tunnel settings were not changed in maintenance |

## Boundaries

Research never authorizes a transaction. Independent-source mode is read-only context, not permission to change browser scope or act on an account. The browser worker's approval, ownership, and uncertain-outcome protections apply independently of model reasoning and retries.

SDK retries are confined to model inference requests. They do not rerun service tasks, tool handlers, browser clicks or submissions. A stream failure after output has started remains explicit.

All accounts, chats, files, Keychain credentials, certificates, browser profiles and logins remain local/private. `.runtime/` is not source code. Saved browser profiles and the optional public tunnel need separate operational access controls.

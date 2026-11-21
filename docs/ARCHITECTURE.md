# Architecture

The browser UI sends authenticated requests to FastAPI, which coordinates a per-user agent and SQLite storage.

Dining and travel use the independent services described in [Dining and Travel Subagents](availability-subagents.md), including the current sequence diagram, ports, webhook contracts, shared-state ownership and user-configured Temporal Schedules (five-minute default, one-minute minimum). The dining-only flow below describes retained compatibility workers for earlier executions; new requests use `delegate_dining`/`delegate_travel`, subagent DeepAgent MCP searches, and signed subagent webhooks.

| Component | Responsibility |
| --- | --- |
| `manage.py` | Manual HTTPS startup, shutdown, TLS fingerprint checks and Keychain loading |
| `personal_agent/app.py` | Authentication, API routes, attachment upload and live browser controls |
| `service.py` | User locks, task lifecycle, context, streaming, cancellation, and memory scheduling |
| `agent.py` | DeepAgents graph, research routing, web search and model/tool limits |
| `db.py` / `memory.py` | SQLite persistence, summaries, preferences, correction and forgetting |
| `background.py` | Idle memory worker, leased Temporal dispatch jobs and account-scoped alert status |
| `availability/` | Main delegation gateway, dining/travel DeepAgents, shared scoped state, signed webhook inboxes, Company inventories and Temporal schedules/workflows |
| `dining.py` / `dining_workflow.py` | Compatibility worker and activities for previously started dining workflows |
| `dining_company/` | Independent Company MCP server, its own inventory database and durable event outbox |
| `dining_stack.py` | Lifecycle for three Company services, two subagents and persistent local Temporal |
| `memory_profile.py` | Evidence-based idle consolidation, independent grounding check and version-checked derived profiles |
| `attachments.py` | Owned file metadata and bounded image input for model calls |
| `browser_tasks.py` | Worker supervision, approval receipts, duplicate protection and independent verification |
| `browser_use_worker.py` | Isolated Browser Use perception/planning and guarded action execution |
| `browser_host.py` / `browser_control.py` | Per-user Chromium profiles and temporary manual takeover |
| `sources.py` | Source policy, domain constraints, provenance labels and safe link handling |
| `static/` | Chat rendering, composer attachments, streaming and takeover UI |
| `tunnel.py` | Optional ngrok lifecycle; public tunnel settings were not changed in maintenance |

## Retained dining workflow compatibility

```mermaid
flowchart TD
  UI[Chat and Alerts UI] --> API[Authenticated FastAPI]
  API --> Service[Per-user foreground coordination]
  Service --> Agent[DeepAgent harness]
  Agent -->|Booking request: browser_task| Browser[Browser Use and final approval]
  Agent -->|Tool: MCP search_dining| Company[Company: independent process]
  Company --> Availability{Slots available?}
  Availability -->|Yes: return slots, no workflow| Agent
  Availability -->|No: queue follow-up watch| Jobs[(SQLite dispatch outbox)]
  Jobs --> Temporal[Temporal follow-up workflow]
  Temporal -->|Activity: MCP watch_dining| Company
  Company --> Inventory[(Company inventory and event outbox)]
  Inventory -->|Carbone: 10-second opening event| Signal[Temporal availability_opened signal]
  Signal -->|Resume waiting workflow| Temporal
  Temporal -->|Activity: deliver once| Notices[(Account-scoped notifications)]
  Notices --> UI
  Worker[Shared background worker] --> Jobs
  Worker -->|Idle memory job| Evidence[Active facts and original user evidence]
  Evidence --> Review[Compact summary and grounding model checks]
  Review --> Version{Memory version still current?}
  Version -->|Yes| Profile[(Disposable user profile)]
  Profile --> Agent
```

All ordinary chat requests enter DeepAgent. A model-based router distinguishes dining availability from general recommendations and web research. The initial Company lookup runs as an agent tool, not a Temporal activity. Missing details return to the agent for clarification. Company errors do not create watches or imply unavailability. Only an accepted unavailable result queues Temporal, using a durable SQLite outbox and stable workflow ID. Its registration activity starts Company's event subscription; the workflow then waits for a signal. Available results never start a workflow. Form submissions also enter the same agent path.

Booking execution is a separate intent, including short follow-ups after an opening. These runs use Browser Use; the Company availability tool is hidden and also rejects invocation if attempted. This prevents a booking follow-up from rechecking Company and generating a second watch. Browser execution retains its approval and outcome-verification boundaries.

Temporal records registration and delivery activity results and waits durably for a Company signal; it does not run Aster-side availability timers. Company owns inventory behavior and retains undelivered events in its separate database. Legacy signals can arrive while Aster's worker is stopped; replay resumes the workflow when it returns. New events use subagent webhooks and the `SubagentWatch` / `MainAvailability` workflows. Criteria correlation, watch ownership, status and delivery deduplication guard notification delivery. Cancelling a watch prevents a late notification. Legacy workflow/activity names remain registered to drain existing executions without breaking replay.

Company and Temporal have independent lifecycles and remain running when Aster stops. Memory maintenance stays inside Aster, yields to foreground activity, and discards results if source memories change during model calls. No per-user resident model session is required.

Research never authorizes a transaction. Independent-source mode is read-only context, not permission to change browser scope or act on an account. The browser worker's approval, ownership, and uncertain-outcome protections apply independently of model reasoning and retries.

SDK retries are confined to model inference requests. They do not rerun service tasks, tool handlers, browser clicks or submissions. A stream failure after output has started remains explicit.

All accounts, chats, files, Keychain credentials, certificates, browser profiles and logins remain local/private. `.runtime/` is not source code. Saved browser profiles and the optional public tunnel need separate operational access controls.

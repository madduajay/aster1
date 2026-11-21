# Company Dining

The current production interface uses `query_id` and a structured `criteria` object on `search_dining` / `watch_dining`, plus `cancel_dining`. Only Carbone **New York** initially lacks slots. Opening events go to the Dining subagent's signed webhook, rather than directly to Temporal. MCP requests require the internal bearer token. Hotels and flights have separate Company services. See [the current service architecture](../docs/availability-subagents.md) for ports, operation and event flow.

The older field-based interface and direct Temporal publisher described below remain available solely to drain existing dining executions.

Company is a separate process with its own SQLite database. It has no imports from Aster and no access to Aster's users, conversations, browser profiles or API keys. It exposes `search_dining` and `watch_dining` over MCP Streamable HTTP.

It can also use its own Python environment: install `dining_company/requirements.txt` and run the module against the configured Temporal server. Aster's environment and its OpenAI credentials are not needed by Company.

```sh
.venv/bin/python dining_stack.py start
# Or, against an already running Temporal server:
.venv/bin/python -m dining_company.app
```

The service listens on `127.0.0.1:55469`. Its MCP endpoint is `/mcp/`, and `/health` reports readiness. The request fields are `request_id` (an `aster-dining-` workflow ID), `venue`, `location`, ISO `date`, and `party_size` (1–20).

`search_dining` is the agent's availability lookup. Other venues return slots at 18:30, 19:00 and 19:30. Carbone always returns `available: false` and an empty initial slot list. A lookup alone does not schedule events or start a watch. Reusing an ID with different criteria is rejected.

Carbone matching includes names with descriptions or cities, such as “carbone indian restaurant” and “Carbone New York restaurant,” regardless of capitalization. Cached responses from the former exact-name rule are corrected when looked up again.

If unavailable, Aster starts a Temporal follow-up workflow. Its registration activity calls `watch_dining` with the same criteria and request ID. Company persists an opening event due ten seconds after the first registration. Repeated lookups or registrations do not reset that timer. Available venues do not register watches or start Temporal workflows.

The outbox publisher sends a Temporal `availability_opened` signal with a stable event ID, request criteria and a 19:00 slot. Failed delivery retries, and pending events survive Company restarts. Aster validates the correlated criteria and publishes one notification. No booking tools or real venue connections are included.

Configuration: `DINING_COMPANY_PORT`, `DINING_COMPANY_RUNTIME` (default `.runtime/company/`), `TEMPORAL_ADDRESS` (default `127.0.0.1:7233`), and `TEMPORAL_NAMESPACE` (default `default`). Aster separately accepts `DINING_COMPANY_URL` and `DINING_TASK_QUEUE`. The local launcher stores Temporal history in `.runtime/temporal.sqlite` and exposes its UI on loopback port 8233.

# Personal agent

A fresh, local application with ten separate user accounts. Python 3.12, FastAPI/Uvicorn, DeepAgents on LangGraph, `langchain-openai`, GPT‑4.1 mini for chat/memory, GPT‑5.4 mini for OpenAI Responses web search, Browser Use with GPT‑6.1 Terra, Playwright/Chromium hosting, SQLite/FTS5, and vanilla HTML/CSS/JavaScript.

## Open and log in

Open **https://aster.localhost:55468** on this Mac.

The ten usernames are `user01` through `user10`. Their unique temporary passwords are in `.runtime/initial-accounts.txt`, accessible only to your macOS account. Every user must choose a new password of at least 12 characters at first login. There is no public registration. Login sessions expire after 12 hours.

The chat keeps **New Chat** and **Login/Logout** in the header. A consequential pending browser action displays a plain-language approval card with **Approve** and **Cancel** buttons. You can also type **approve** or **cancel** while that card is visible. Internal approval IDs and JSON are not displayed. Enter sends; Shift+Enter adds a newline. Sending a new request or choosing **New Chat** automatically cancels the previous run and revokes its old controls before starting the next task. Explicit approval/Continue replies and short answers to the displayed information question still continue that task. During an input pause, a new instruction or question (for example, “Find other shoes” or “I want a different restaurant”) replaces the old task; short answers such as “7 PM” stay bound to the pending question. No `/stop` is needed to replace a task.

Results use descriptive clickable links wherever a verified official URL is available, including product names and result titles directly in the answer. Detail-page links are preferred when verified; the agent must not invent a link to make a result clickable. Harmless UTM tracking differences resolve to the exact verified URL. An unverified detail link can use a verified ancestor page labeled “official overview”; other unsupported citation lines are omitted without discarding valid results. Functional query parameters and distinct page paths remain significant.

| Chat command | Result |
|---|---|
| `/sessions` | List your previous chats and their IDs |
| `/open SESSION_ID` | Continue one of your chats |
| `/memory` | Inspect saved user facts and their IDs |
| `/debug` | Show recent captured browser exceptions in this chat |
| `/memory review` | Inspect quarantined memories from the earlier automatic policy |
| `/remember TEXT` | Explicitly save a lasting user fact or preference across chats |
| `/forget ID` | Remove a saved fact |
| `/forget all` | Clear your long-term facts |
| `/correct ID NEW TEXT` | Replace a fact with your correction |
| `/stop` | Stop your active run |
| `approve` / `cancel` | Answer the currently displayed approval card; buttons do the same |

## Start, stop, and status

Run from this project directory:

```sh
.venv/bin/python manage.py start
.venv/bin/python manage.py status
.venv/bin/python manage.py stop
```

Start loads the existing macOS Keychain item with service `local.aster.openai` and account `aster`. macOS may request native Keychain authorization. The key is passed through an anonymous pipe to the backend and never stored in source, frontend code, logs, or command-line arguments. Hosted LangSmith tracing is disabled.

The listener binds only to `127.0.0.1:55468`. The optional ngrok tunnel provides public access while it runs. No automatic startup service is installed. The existing `aster.localhost` TLS certificate and matching private key were copied byte-for-byte into the protected `.runtime/tls` directory; existing trust is retained. The certificate expires **January 26, 2027**. Renew it separately when needed; the application does not create or replace certificates.

## Sessions and memory

Raw user messages and delivered responses are retained under their session. Each completed exchange (including chat commands) updates a rolling summary with an exact message coverage cursor. Summary snapshots allow context to use a summary of **older** history alongside up to six recent exchanges verbatim (a 24,000-character target), uncovered messages, and the current request exactly once. Recent corrections take precedence over older summaries. If a summary fails, all uncovered messages remain available; no communication is silently dropped. An oversized backlog is summarized before the next model request. Context overflow stops explicitly instead of silently discarding messages.

Ordinary conversation, tasks, dates, bookings, and shopping filters stay in their session. A lasting fact is saved permanently only with explicit user remember intent, not an importance score alone. Natural English remember requests use structured extraction with user-message provenance, verbatim supporting evidence, and an independent server intent check. `/remember TEXT` is the deterministic option, including for phrasing/languages the intent check does not recognize. Secrets and temporary task details are excluded. Natural extraction remains model-based; inspect it with `/memory` and use `/correct ID TEXT` to replace a fact reliably.

Permanent facts live in their own table, separate from session messages and summaries. They have semantic keys, timestamps, provenance, active/superseded status, and correction links. Every turn consults this store: SQLite FTS5 retrieval combines keyword relevance, recency, and importance, with a bounded set of recent permanent defaults. Stopwords do not drive retrieval. No embeddings or vectors are used. Within a fact key, a newer explicit correction supersedes the old value; an unrelated new fact does not erase an older one.

Context precedence is current request → recent exact session conversation → older session summary → permanent defaults. A saved preference does not reactivate an unrelated task. Summaries distinguish user decisions from assistant suggestions; ambiguous short follow-ups require clarification before searching or acting.

Older summary snapshots also retain up to six original user statements as grounding notes, separately labeled from the generated synopsis. These original words establish user intent; the synopsis cannot turn assistant suggestions into user decisions. Private details are omitted from these notes.

Fact dependencies are tracked on delivered responses and summaries. Forgetting or correcting a fact invalidates dependent summaries and excludes its source/derived messages from future model context, while preserving unrelated communication and visible chat history. Old checkpoints with withdrawn facts cannot resume. Rebuilds use retained raw messages; forget/correction versions prevent a concurrent digest from restoring withdrawn memory. This is a context-forgetting operation, not deletion of visible history or owner backups.

The memory-v2 upgrade creates an owner-only `application.before-memory-v2.sqlite` backup, quarantines all earlier automatic promotions (excluded from permanent retrieval), and invalidates old summaries for rebuilding from original messages. `/memory review` lists quarantined facts; `/remember TEXT` or `/correct ID TEXT` explicitly saves one under the new policy. No past task is silently grandfathered into permanent memory.

At the owner's request, user01 and user02 subsequently had all permanent/quarantined facts and session context cleared. Their previous chat messages remain visible but are below each session's context floor and are not supplied to the model. Old graph checkpoints for those accounts were removed. New messages start fresh; backups are retained for owner recovery and are never used as agent memory.

## Web and browser actions

Web search uses OpenAI's Responses API `web_search` with GPT‑5.4 mini, native `filters.allowed_domains`, and low reasoning effort. The DeepAgents conversational model and session-summary/long-term-memory extraction use GPT‑4.1 mini through `langchain-openai`.

Research uses official, first-party websites only. The main GPT‑4.1 mini agent selects the relevant publisher/organization domains for each request; there is no brand-to-domain lookup table. The American Express example in the instructions illustrates the general rule. Those domains are passed to the native API filter on GPT‑5.4 mini search requests, and any search answer that includes an outside consulted source is discarded. Only official URL leads survive for direct browser reading; unvisited leads are not citations. Browser documents, redirects, and returned citations are checked against the selected scope. HTTP error pages are not accepted as evidence. Without accepted official evidence, the app reports the gap instead of filling it from model memory or using another website.

Chat/memory and search model names are configured separately in `personal_agent/config.py`. Official-domain identification is model-based; native filtering and URL checks enforce the selected scope but do not independently certify website ownership. Unknown ownership should lead to asking for the official website. Existing saved conversations are retained; old assistant answers and summaries are not treated as fresh web evidence. If filtered search fails, the app reports the gap rather than retrying without its domain filter.

Each user gets a separate saved Chromium profile. Chromium runs headlessly by default. Normal browser work shows a small read-only preview beside chat (above messages on smaller screens). Only when the agent requests information or a manual login/verification step does the panel expand into a live interactive view of the same Chromium tab. During entry, Chromium's viewport matches the panel's CSS dimensions: website text and fields render at normal size rather than shrinking a 1280px desktop page. Window resizing updates that viewport without reloading the page or clearing entered text; stale-size clicks are rejected. The agent remains paused. Click the website's own textbox and type or paste directly, then choose Continue. There is no separate visible Website entry field. Text, composed/IME input, paste, Tab, Shift+Tab, Enter, arrows, deletion, selection and horizontal/vertical scrolling are forwarded to the website. The browser stream uses an authenticated same-origin WebSocket and transient Chromium screencast frames, paced to at most about eight frames per second while retaining the latest frame. This is a remote Chromium tab, not an iframe of the third-party website. The original agent viewport is restored before planning resumes.

Accepted input is sent in order and drained before resuming. Private login fields/challenges and missing required information prevent premature Continue. Direct website entry records a completion marker rather than copying typed text into the conversation. No field values are returned with input acknowledgments, and typed secrets never enter model arguments, chat, or the action journal. Control binds to the authenticated user, run and pending input ID, with one live controller per user. Repeated command IDs are rejected. Ownership/login and the pending pause are rechecked while streaming and before each action; cancellation, resume and logout revoke control. The expanded view then returns to the small preview. Non-secret information may alternatively be supplied through chat.

Textbox focus, frame scope and form-readiness checks use trusted read-only DOM code in isolated Chromium worlds, so websites that override their main-world `eval` do not break direct typing. Website JavaScript and security settings are left unchanged. Continue validates required fields (native or `aria-required`) and invalid fields for both information and manual pauses. A failed prior keystroke does not poison the input queue or prevent a fresh readiness check. Continue hands control back to the agent; Cancel stops the task. These are application controls rather than website submit buttons.

Explicit requests to open/visit/show a homepage, main page, website or supplied URL require an actual `browser_task` call before the agent can answer. The routing guard uses the current user message, not session-summary text, and contains no website names or domain lookup table. Official-domain selection remains dynamic. Requests for a link, an explanation, or ordinary research retain normal routing. Unknown destinations can return a missing-domain validation result and ask for a URL instead of inventing one. The guard uses the API's [named tool choice](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create); after the first browser call, normal planning and clarification resume.

Manual takeover forwards separate mouse press and release events, preserving a user's held-button gesture instead of turning every interaction into a completed click. It retains viewport, ownership, login and pause checks; disconnect/cancel/resize releases an outstanding mouse button off the page. Human-verification pages remain manual steps, and Continue stays paused while their challenge is visible. The app does not automatically solve or bypass CAPTCHAs.

The preview refreshes about every 1.5 seconds without model calls. Completed runs show their last frame while it remains in backend memory; frames are not written to chat storage and are cleared on logout/restart. Old chats never show a newer run’s page. Optional `PERSONAL_AGENT_BROWSER_HEADLESS=0` still provides a local visible window. Users share no browser cookies or local storage. Logout closes that user's browser while retaining its saved profile. This application creates a dedicated browser profile rather than using your regular Chrome profile.

The agent supports page reading, navigation, tabs, form fields, selections, scrolling, screenshots, and downloads. Routine navigation, searches, field focus, non-sensitive form preparation, product options and adding a requested item to the cart proceed automatically. Viewing an application or checkout page does not submit it. Final submissions, purchases, bookings, messages, deletions, account changes, file sharing and new provider access require inline approval; ambiguous controls and non-search form submissions still pause conservatively. Approvals are tied to the proposed action and page snapshot. A page change invalidates the proposal and prompts a fresh observation and approval. Started action receipts prevent automatic replay after uncertain outcomes. Navigation is identified from observed link destinations, navigation landmarks, popup-menu attributes, controlled navigation panels, tabs, and native details disclosures—not website names. Opening those controls is classified as reading, so old misclassified menu receipts cannot block later browsing. During uncertain-submission recovery, these proven navigation actions remain available for inspection, while unsafe edits and submissions remain blocked. Genuine transaction receipts and their cross-task duplicate checks are retained.

Runs allow at most 24 conversational model calls and 3 web searches. Each delegated Browser Use task has a 30-step / 40-model-call cap and executes one browser action at a time. A four-minute active-execution timeout applies per execution/resume segment. Conversational/search counts and browser model usage persist across resumes and restarts. Ordinary interaction failures allow six recovery attempts, at most two identical failed approaches, within the active-execution bounds. User-input waiting time does not consume the recovery clock. Summaries use separate bounded calls. Website timeouts, blocked automation, closed browser windows, and failed downloads are reported; successful site access is not guaranteed. Local/private-network web destinations and arbitrary JavaScript, shell, and host filesystem tools are unavailable to the model.

## Installation and checks

Exact installed package versions are in `requirements.lock`. To recreate the environment:

```sh
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
PLAYWRIGHT_BROWSERS_PATH="$PWD/.runtime/playwright" .venv/bin/python -m playwright install chromium
.venv/bin/python -m venv .runtime/browser-use-venv
.runtime/browser-use-venv/bin/pip install -r browser-use-requirements.lock
.venv/bin/python manage.py setup
```

The initial setup expects the existing TLS files under `/private/tmp/aster-verification-20261025/.runtime/secrets`. After setup, normal launches use the protected local copy. The application contains no imported Aster code or migrated Aster history.

```sh
.venv/bin/python -m pytest -q
RUN_BROWSER_TESTS=1 PLAYWRIGHT_BROWSERS_PATH="$PWD/.runtime/playwright" .venv/bin/python -m pytest -q
```

The browser tests use a controlled local HTTP fixture and temporary databases/profiles. Only those fixtures disable the public-destination check. Normal application behavior retains it.

`validation/navigation_guard.py` exercises the actual Browser Use worker against legacy menu and unknown-booking receipts: menus can repeat, safe navigation remains available during inspection, a booking cannot repeat, unsafe edits are blocked, and no submission occurs. `tests/test_navigation_guard.py` covers navigation semantics across different controls, keyboard activation, history states and real DOM metadata.

`validation/automatic_actions.py` verifies the current policy through the actual Browser Use worker without paid calls: automatic size selection and exactly one cart addition, one approval for final purchase, rejected purchase and zero orders.

The older cart-approval acceptance scripts predate this policy and their historical reports describe that earlier behavior. `browser_use_acceptance.py` runs capped paid Browser Use/GPT calls against an isolated synthetic shop; it never changes a real cart or makes a purchase. It tests correct variant selection, an explicit cart approval, one submission, and independently grounded verification. `browser_use_ui_acceptance.py` checks the full trusted-HTTPS login → streamed chat → Browser Use → text approval → verified cart → summary → logout flow with isolated accounts and capped live models. `validation/browser_use_controls.py` and `validation/browser_use_boundary.py` exercise the actual library/worker without paid calls, including native dialogs, cancellation, and changed-price rejection. `ui_acceptance.py` is an initial-install test using pristine `user10`; it refuses an account with existing chats or a changed password. Historical acceptance reports remain under `.runtime`; the replaced executor's scripts are archived privately and are no longer active checks.

## Local data and recovery

All databases, browser profiles, initial passwords, logs, TLS keys, and downloaded artifacts live in `.runtime`, excluded from source control and restricted to the macOS owner. Back up that directory while the app is stopped to preserve accounts, chats, summaries, memory, and website logins. These files contain private data; the macOS account owner can access them.

LangGraph persists the interrupted conversation, approvals, and user-input questions. Missing non-secret information can be entered directly in the website panel or answered through the existing composer. Private login and verification use the browser panel with Continue/Cancel. Each reply binds to the exact pending input and authenticated run. Input pauses survive restart; a replacement planner receives the original goal, earlier answers and action journal, then inspects the current browser rather than replaying actions. Transient website state may expire across a restart, so a login step may need to be completed again.

LangGraph also persists approval history. On restart the Browser Use worker is gone, so pending browser approvals are invalidated and the task becomes interrupted. Previously dispatched actions with uncertain outcomes remain unknown and cannot be repeated automatically. Inspect the current page in a new inspection task before continuing; fresh consequential actions require fresh approval.

The previous Aster application was stopped by verified port ownership and SIGTERM. Its files and databases were preserved. To return to it, first stop this application, then use Aster's original launcher from its original project; never run both applications on port 55468. This application's start command can stop the verified old Aster runtime, but refuses an unrelated port owner.

## Browser execution and clock

DeepAgents/LangGraph delegates complete website goals to the local open-source Browser Use library, pinned to version 0.13.14 in a separate Python environment. GPT‑6.1 Terra handles browser perception/planning; GPT‑4.1 mini handles conversation, memory, and a separate outcome check. Browser Use attaches through loopback CDP to the application's per-user Chromium profiles. Its Actor API provides a selector fallback for visible labels omitted from its indexed DOM, plus hover and drag. The earlier custom DOM discovery, target-handle executor, expected-state schema, and model repair gates have been removed from active code.

`personal_agent/browser_host.py` owns profiles, network scope, downloads, and native dialogs. `browser_tasks.py` owns worker processes, per-user serialization, durable approvals/action receipts, cancellation, and independent outcome verification. `browser_use_worker.py` runs the library and intercepts every action. It exposes no arbitrary evaluation, shell, or host-file tools. Only a file owned by the authenticated user and referenced in the delegated task can be uploaded. Drop a file on the composer; `/files` lists artifact IDs.

Consequential actions, ambiguous controls, uploads, and native confirmations receive an inline approval card. Routine controls use their observed role, input type, form and destination to proceed without approval; website names never select policy. Reversible cart additions still receive dispatch receipts and uncertain-outcome/duplicate protection. Buttons and text shortcuts bind to that exact displayed action; stale or repeated responses cannot authorize a later action. The approval binds the operation, parameters, exact observed node, selected options, price, and page; the worker rechecks them after approval. A persisted dispatch receipt precedes execution. A click alone cannot establish success: a separate verifier requires actual quoted page evidence matching the task. Uncertain submissions cannot repeat automatically. Provider-domain approval expands browser interaction scope without expanding official research citations.

Cancellation terminates the isolated worker and closes only that user's browser; the saved profile survives. Restart never recreates a pending submission. Human verification, private login fields, closed shadow DOM, and websites that reject automation can still require manual interaction. No claim of universal website compatibility is made.

Each model call receives a fresh system clock in `America/Phoenix`; `current_time` supports other IANA timezones. Set `PERSONAL_AGENT_TIMEZONE` before starting to change the default. Historical session dates cannot override the runtime date.

## Public ngrok access

The optional tunnel forwards public HTTPS to the existing loopback HTTPS service. It preserves login/password-change requirements, secure cookies, exact host and same-origin mutation checks. No public signup or startup-at-login is enabled. Chromium runs on this Mac; its authenticated live preview also works through the public tunnel. Manual website login can use the temporary interactive browser takeover through the authenticated app.

The official ngrok executable is installed privately at `.runtime/bin/ngrok`. Save the ngrok account authtoken in `.runtime/ngrok-token.txt` (never in source or chat), then run:

```sh
.venv/bin/python tunnel.py start
.venv/bin/python manage.py stop
.venv/bin/python manage.py start
.venv/bin/python tunnel.py status
```

The launcher consumes the token into owner-only `.runtime/ngrok.yml`, disables local HTTP introspection, verifies the upstream TLS certificate, and saves the assigned exact public origin. App restart enables that host. Passwords and chats pass through ngrok's HTTPS service; ngrok terminates the public TLS connection. The Mac and both processes must stay running and awake.

To remove public access:

```sh
.venv/bin/python tunnel.py stop
.venv/bin/python manage.py stop
.venv/bin/python manage.py start
```

The local `https://aster.localhost:55468` URL remains available independently of the tunnel.


Search fields are identified by actual HTML search/searchbox and GET-form semantics, not brand mappings. Keyboard actions bind to the current focused control and field value. Ordinary catalog searches do not receive transaction approvals or duplicate-submission blocks. Historical uncertain transaction receipts remain protected, including legacy keyboard submissions. A terminal policy block reports its actual cause instead of substituting a generic cart-verification failure.

`validation/search_boundary.py` reproduces two searches with an old coarse Enter receipt using the actual guarded Browser Use worker and deterministic planning (zero paid calls). `validation/nike_search_acceptance.py` is a capped live public Nike catalog-search check in a fresh headless profile; all consequential proposals are rejected and no real cart or purchase is attempted.


## Browser recovery and input handoff

Browser recovery uses observed page state, independent of brands and website mappings. It reads visible embedded frames (including cross-origin widgets), prioritizes the active form/dialog, and excludes background-page text from final verification. A login window is a waiting state rather than proof that a reservation or cart operation failed.

Stale selectors, hidden/disabled targets and changed pages return actionable feedback to the planner. The planner can wait, discover another real control, switch observation methods and replan within the original constraints. A changed approval requires a new approval; it never authorizes a different price or target. `browser_connection.py` serializes application and library CDP reconnects, restores the previously observed tab, checks its attachment, and clears old DOM references. Recovery makes at most two connection attempts within a 35-second reconnect/inspection bound. A failed pre-dispatch click returns to planning with fresh page state; old targets and approvals are not replayed. A closed original tab does not silently substitute another tab. If a dispatched consequential action becomes uncertain, the worker is restricted to inspection; resulting confirmation may resolve the uncertainty, but the action is never blindly repeated. Approval replies include the action category so the worker can enforce this restriction immediately. A failed final inspection produces an unverified result with diagnostics instead of crashing the worker or using stale evidence. Attempts and exhausted recovery limits are reported in chat progress/results.

`validation/connection_recovery.py` closes actual worker WebSockets against an isolated synthetic shop. It covers simultaneous reconnect requests, disconnect before dispatch, lost focus, disconnect after approval with fresh approval, uncertain submission without replay, rejection, and failed final inspection. It uses no paid model calls or real website transactions; its report is `.runtime/connection-recovery.json`.

Website takeover is available only for the authenticated user, the exact run, and its pending information/manual-input ID. It exposes scoped clicks, text entry, a small key whitelist and scrolling; it has no arbitrary JavaScript, URL or host-file API. Native website dialogs can be accepted with Enter or dismissed with Escape while in takeover. Continue refuses a still-visible private verification field or missing required information. Cancellation/logout revoke control, terminate work and preserve the dedicated user profile. Phone numbers supplied through chat remain session-scoped and are not automatically promoted into permanent memory.

New APIs: `POST /api/runs/{rid}/input` accepts a response bound to the pending input ID, with `completed_in_browser` distinguishing direct website completion; `POST /api/runs/{rid}/takeover` controls the browser only during an owned information/manual pause. Both use server-derived identity, the existing secure login cookie and same-origin mutation protection. Validation errors do not echo submitted private values.

Reproducible checks: `validation/recovery_controls.py` verifies actual Browser Use cross-origin observations and direct private entry without paid calls; `validation/recovery_boundary.py` checks stale-control recovery, changed-price reapproval, uncertain-submission inspection without duplication, and bounded recovery through the actual worker. `tests/test_recovery_ui.py` covers the complete HTTPS chat-to-phone-to-private-login-to-approval-to-confirmation flow with deterministic models. `validation/recovery_acceptance.py` performs a capped live-model version on synthetic pages only (18 browser calls maximum plus independent verification).


## Browser exception details

Browser worker exceptions now retain the original Python type, sanitized message, preparation/execution stage, source filename/line/function and a short stack trace. Failed attempts retain the requested element index, coordinates or selector even if no action row could be created. Page reading, frame reading, target resolution, metadata inspection, validation and execution are distinguished. A wait failure is no longer automatically described as a stale target.

Captured diagnostics appear in streamed status and are appended to unverified browser responses. `/debug` retrieves the recent diagnostics for the current user's current chat. `GET /api/runs/{rid}/diagnostics` exposes structured details only to the run owner. The worker captures errors before dispatch, tool-returned errors and fatal exits. No raw traceback locals, typed values or full page content are collected in diagnostics; credential/header assignments, tokens, email addresses, numeric codes, URL credentials/query/fragment and known typed values are redacted. Diagnostics are audit data, not permanent user facts. Previously discarded exceptions cannot be reconstructed retroactively.


## Current browser model and live input checks

The default browser perception/planning model is `gpt-6.1-terra`; chat, memory and independent outcome verification remain `gpt-4.1-mini`, and official-domain web search remains `gpt-5.4-mini`. `PERSONAL_AGENT_BROWSER_MODEL` remains an explicit override. The pinned Browser Use planner returns structured JSON actions rather than OpenAI function calls. Its GPT-6 options preserve low reasoning effort and omit unsupported sampling parameters; no native computer-use or unrestricted tools are added. `validation/terra_probe.py` verifies one structured-output call, and `validation/terra_acceptance.py` caps an isolated synthetic shop at ten browser calls plus independent verification.

`GET /api/runs/{rid}/browser` and its JPEG frame endpoint provide the small read-only preview. `WSS /api/runs/{rid}/browser/control?input_id=...` is available only during a matching user-input pause. It requires the existing HTTPS login cookie and exact allowed Origin/Host; the raw CDP endpoint is never exposed. Frame and input data stay transient. The browser agent continues using Browser Use and the existing action/approval boundary; the WebSocket is exclusively direct user control.

# Changelog

## 2026-11-20

- Dining follow-ups that reference a prior stay now check every date in that scoped range and only the requested meals, including lunch/dinner-only requests without a full itinerary.
- Replaced fixed three-option hotel/flight discovery with user-requested counts or per-request random defaults of four to seven, preserving counts across retries and reporting verified shortfalls.
- Added compact, lazy-loaded venue/item thumbnails and brief descriptions with clickable names. Photos are cached from matching public source pages behind an authenticated local endpoint; missing photos use a neutral placeholder. Shopping recommendations stay in the main agent.

- Fixed combined flight/hotel prompts binding “flights” as a hotel name. Applied user-authorized defaults: two people across dining/hotels/flights, one hotel room, dining today, and a one-night hotel stay starting tomorrow when dates are omitted. Explicit and relevant trip details take precedence.

- Monitoring honors the user's requested interval instead of always scheduling five minutes, with a one-minute minimum. Existing schedules recover cadence from their originating request and update in place; chat acknowledgements and the background panel show the actual cadence.

- Replaced attraction extraction from headings/URL slugs with quoted, cited attraction records. Composition fills daily activity periods from these records and derives travel days from checked flights instead of repeating model repair loops.
- Rejected research publishers as airlines, normalized city/IATA connection aliases, and prioritized verified nonstop routes in discovery.
- Added timezone-aware, route-dependent Company schedule configuration and airport-transfer buffers; trip hotel checks start on the checked arrival date, with a narrowly validated correlation adjustment. Standalone hotel dates remain explicit.
- Preserved currency-prefixed budgets and converted provider prices using dated ECB reference rates; fallback trip tables omit meals outside the travel window.
- Added browser-driven worldwide itinerary evaluation, saved responses/screenshots, and regressions for these audit findings.

- Removed redundant category titles above availability tables; column headers identify the results.

- Bound named hotel requests to literal user wording in the trusted delegation envelope; missing or rewritten model arguments can no longer turn OYO Times Square into general New York discovery.
- Added a subagent check against requested property names, backward-compatible request metadata, and live tests for spelling variants and property-switch follow-ups.

- Preserved explicitly requested hotel names when users include generic words such as “hotel” or city aliases, preventing Hyatt Grand Central from being replaced by generic New York discovery.

- Fixed Fifth Avenue Hotel aliases with New York embedded inside the property name; Company now applies the twenty-second opening rule consistently and repairs incorrectly available initial inventory without resetting genuine openings.

- Normalized common dining time formats and date timestamps; structured validation produces plain-language field questions rather than parser errors.
- Removed automatic technical diagnostics from chat, run status and public event streams, while retaining internal records and explicit debugging access.
- Added safe API/failure wording and a display projection for known historical system-error messages without rewriting saved history.

- Trip dining inherits dates, destination and traveler count; Dining researches breakfast/lunch/dinner separately and Company returns meal-specific slots for each date.
- Day-by-day plans automatically use checked meals, respect flight arrival/departure boundaries, and explicitly label sourced entertainment.
- Dining venue selection prefers unique venues across meal types; the composer requires replacement when a venue appears more than twice.

- Kept route connections owned by Travel research, preventing malformed main-model route fields from becoming user questions.
- Consolidated venue results into category tables, merged repeated dining dates, and suppressed superseded errors and duplicate source footers.
- Added safe Markdown table rendering and verified persisted grouped results in Chromium.

- Joined research discovery to exact Company candidate checks and replaced disconnected availability/research lists with consolidated sourced options.
- Added validated day-by-day composition with date-specific dining, cited sightseeing, rating/budget constraints and travel-day checks; expanded live evaluation to the full ten-day Dubai request.

- Fixed short travel follow-ups and corrections using scoped pending/recent criteria and server-side reply binding; added actual multi-turn model evaluation rather than relying on one fully specified smoke prompt.
- Added read-only web search to both subagent harnesses, returning sourced research separately from Company availability and preserving independent checks if research fails.

- Added independent Dining and Travel DeepAgent services, with automatic general-purpose subagents explicitly disabled in every harness.
- Routed new availability requests through subagent APIs and three Company MCP endpoints; retained old dining workers for pending execution replay.
- Added persistent inventory/opening outboxes, signed subagent webhooks, durable inbox dispatch, Temporal forwarding and atomic main chat/alert delivery.
- Added explicit five-minute native Temporal monitoring with quiet unchanged checks, failure/recovery updates, per-category expiry and background-panel cancellation controls.
- Preserved opened inventory on repeat checks and aliases, scoped requests/preferences/checkpoints by user and chat, and kept booking execution with the browser.
- Verified isolated service/worker restarts, 10/20/30-second openings, delivery retries, duplicates, cancellation, native schedules and browser alert/draft persistence. The real-model combined availability smoke check passed in 10.5 seconds.

## 2026-11-19

- Separated booking follow-ups from availability checks: “book now” uses the browser without rechecking Company or creating another watch.

- Relaxed browser discovery to read more public metadata and omit unsupported attributes automatically instead of stopping the task.
- Allowed observed reservation-entry links/dialogs without an extra approval, while retaining final submission, credential and payment protections.

- Opening notifications now also post once to their original chat and appear live without replacing unsent drafts. Dismissing an alert preserves its chat message.

- Fixed Company's Carbone matching to include venue descriptions and locations; corrected older cached alias responses on lookup.

- Required Company MCP checks after open-ended dining discovery; web search and Resy snippets cannot substitute for table availability.
- Withheld web-only dining availability claims when no Company check completed.

- Moved initial dining availability lookup into DeepAgent's Company MCP tool; removed the direct dining shortcut around the agent.
- Split Company lookup from watch registration. Only unavailable results start Temporal follow-up workflows; available slots return directly.
- Kept replay compatibility for earlier workflows and routed missing-detail forms through the agent harness.

- Replaced Aster's Carbone timer and event generator with a separate Company dining MCP service.
- Added a Temporal workflow that waits for Company's availability signal, resumes after worker restarts, and delivers one owned alert.
- Company returns configured slots for other venues and emits Carbone openings ten seconds after its initial unavailable response.
- Added independent service lifecycle management and persistent Temporal history, Company inventory and event outbox.

- Added durable background memory consolidation with source evidence, grounding checks and correction/forget invalidation.
- Added an opt-in Carbone availability demo: collect location, date and party size, then emit a clearly simulated in-app opening after ten seconds, with no restaurant or MCP calls.
- Added account-scoped Alerts, missing-detail forms, cancellation and persistent read status.

- Initialized local Git from the existing Codex runtime branch and preserved a pre-maintenance baseline.
- Moved GPT-5.4 mini chat and memory to Responses with low reasoning effort.
- Enabled two bounded SDK retries for transient model failures, without replaying browser actions.
- Added independently labeled review/news/comparison research while retaining official sources for provider facts.
- Re-ran current tests rather than relying on obsolete pytest failure-cache entries.
- Split setup and architecture from archived implementation and verification history.
- Quarantined confirmed old test artifacts and snapshots for review; no permanent deletion.
- Public tunnel and authentication deliberately unchanged at the user's request.

## Earlier changes

See [historical implementation notes](HISTORY.md) for chat styling, image attachments, browser recovery, memory and research-routing work.

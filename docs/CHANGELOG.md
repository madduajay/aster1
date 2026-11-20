# Changelog

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

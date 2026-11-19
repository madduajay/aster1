# Changelog

## 2026-11-19

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

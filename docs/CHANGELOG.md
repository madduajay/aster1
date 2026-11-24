# Changelog

## 2026-11-23

- Undated trip checks now research a suitable seasonal window and adopt it provisionally without asking for dates or confirmation. Flights and hotels inherit the same window before provider validation; the response discloses the exact proposed dates. Working dates persist in the chat draft and are cleared by explicit destination/date/duration corrections rather than becoming permanent preferences. Known dates remain authoritative.

- Aster now introduces itself as "Aster - your Personal Agent", including in chats whose older replies used a different assistant name. Questions about its underlying technology remain separate from its product identity.

- Reduced unnecessary routine-browser approvals for POST-backed searches, calendar cells, cookie choices, popup dismissal, checkout navigation and read-only account links. Private input, final commitments and account-changing actions retain their existing boundaries.
- New Chat and new prompts clear the browser panel before submission, and stale asynchronous preview responses cannot restore it. Unrelated browser tasks, including topic changes within the same chat after the old view was retired, start in a fresh tab explicitly bound to the Browser Use worker. Their preview stays hidden until current-task navigation or completion; related follow-ups retain their linked page. Unbound runs no longer capture an arbitrary tab from a previous task.

- Fixed a blank embedded browser after a preview load error: live frames now restore image visibility and retain the preview load handler across Take control / Release control. Real image-decode fixtures cover both display modes; the Amex login form was visibly restored in the live Aster panel without restarting Chrome.

- Visible Chrome now uses an explicitly allocated loopback debugging port rather than Chrome's special port-zero launch mode. Startup discovers and validates the new browser endpoint directly, then publishes the endpoint file for existing workers. Existing profiles, browser security and networking settings are retained. An isolated native Chrome fixture confirms page load before attachment and `navigator.webdriver=false` after attachment. Live acceptance: the user confirmed successful manual Amex sign-in in Aster after this change; ordinary Chrome had worked while the prior Aster launch failed. The isolated fixture verifies the launch behavior, while the Amex result is user-confirmed. The precise server-side rejection mechanism is not independently established.

## 2026-11-22

- Specific itinerary requests with a known destination and date range now override a contradictory exploration classification. The gate recovers explicit ISO or month-name ranges, retains occupancy/cabin/rating/budget requirements, and invokes the required Travel/Dining services before main-agent sightseeing research. Flexible-date and open-ended destination requests remain exploratory. Regression coverage includes a deliberately misclassified, fully specified Dubai itinerary.

- Flight checks now inherit missing departure/return dates from the matching active trip or one unambiguous matching hotel range. Explicit flight dates and one-way requests take precedence; context clearing/new-trip boundaries prevent stale inheritance. Restaurant follow-ups reuse the active trip range and honor requested meal types, while standalone dated/local dining checks stay separate. Added date/range inheritance regressions.

- Fixed itinerary gap repair forcing a Dining call after routing had filtered that tool out, which caused a provider 400 error. Repair now enables the required availability tool and its delegation guard before selecting it. Merged day-by-day requests retain dining requirements from the original prompt. A final tool-choice consistency guard prevents absent-tool requests; a strict provider-like regression reproduces the original failure.

- Running/resuming tasks now accept additional same-chat prompts and draft attachments on the same run. Durable follow-ups are combined at model boundaries (including late arrivals during summarization), preserving checkpointed tool results and rerouting outstanding work. Updated response streams clear superseded partial text. Explicit stop, other chats and paused browser control semantics remain separate; queued updates cannot approve a stale consequential action. Itineraries do not require an extra trip-brief confirmation.

- Added travel exploration, flexible-date comparison and preparation intents. Main-agent research can recommend destinations/windows without forcing missing dates, origin or cabin into inventory validation. Typed chat drafts persist confirmed inputs separately from assumptions, candidates and preparation checklists; they respect account ownership, context clearing and source exclusions.
- Exploration/comparison checks enforce `observe_only` across gateway, workflow/schedule and subagent watch boundaries. Concrete flight checks disclose an economy default when no cabin is supplied. Existing shopping/browser preparation keeps its approval controls. Added deterministic travel-planning tests and an opt-in isolated `validation.travel_matrix --discovery` scenario; no live-pricing provider or preparation scheduler was added.

- Chosen-retailer shopping now inspects matching product detail pages through Browser Use before web search. Verified product links satisfy shopping evidence requirements; search is reserved for missing facts after bounded browser inspection. Shopping scope survives a manual pause or graph reconstruction.
- PoC browsing proceeds through ordinary menus, color/size/date filters, public search fields and non-sensitive form preparation without separate approvals per click. Read-only research explicitly permits these viewing controls. Final submissions/commitments still require approval; passwords and CAPTCHA remain manual. Browser launch, profiles, networking and display settings are unchanged.
- Validation: 248 regression checks passed (8 skipped), followed by 149 routing/policy checks after the last routing update. A real-model controlled retailer test applied filters and read two product pages with zero approval prompts and zero web searches; a separate real-model form test required exactly one approval at final submission. These fixtures do not establish external-site login or retailer compatibility.

- Added a permanent Cancel task button beside Take control in the browser header, including running, completed-preview and manual-control states. Explicit cancellation stops pending work, closes the owned browser while retaining its profile, retires preview context, and blocks saved previews from reopening after reload. Completed website outcome records remain intact. Return to chat and Release control retain their separate behavior.

- Delayed Company openings and monitor availability changes now render available results with the same compact tables, linked venue names, photos and descriptions as interactive checks. Alert bodies remain concise. Named unavailable venues prepare photo metadata before watch registration; opening publication restores scoped research sources and reuses that cache. Company inventory, cancellation checks, atomic chat/alert delivery and semantic deduplication are unchanged. Verified with cache-backed photo/table UI checks and the isolated MCP/webhook/Temporal restart and cancellation integration.

- Fixed long-site research losing early evidence: preserve a representative per visited page before retaining selected-control variants, and share current-request read-only evidence across a bounded continuation. Verification uses the original user goal; navigation hints no longer become extra completion requirements. The verifier identifies specific missing facts and selects only directly relevant observed links for follow-up.
- Read-only Browser Use now uses text-first inspection with optional screenshots, direct page reads without an extra extraction-model call, current-tab reuse guidance, and bounded initial/continuation calls. Booking and manual-login launch/session settings are unchanged. Captured large-site evidence replay retained the five read cards and narrowed the outstanding gap to one card. A real-model six-page catalog check completed in 37.5 seconds with eight browser-model calls, no extraction-model calls and no web search; this does not establish timing on external sites.

- Generic website reading now follows relevant on-site detail links, tabs and section controls before declaring a hub insufficient. A bounded second browser task is required when relevant observed detail links remain, before search fallback. Verification combines only pages and distinct UI states actually observed during the current read-only task; transaction checks still require the resulting current page.
- Removed brand-specific examples from runtime source/relevance prompts. Real main-agent plus Browser Use acceptance passed on a benefits hub and an independent API documentation site, reading three linked detail pages in each case with zero web searches. Re-run with `python -m validation.browser_research --scenario cards` or `--scenario docs`.

- Relevant preview follow-ups now force a fresh, scoped, read-only Browser Use task before an answer or search. Independently sufficient page evidence skips search; insufficient reads can fall back to sourced research without replacing that answer with a browser completion error. Existing booking/availability routing remains available after inspection.
- Preview relevance uses a bounded main-model context check with conservative local fallback. Topic changes retire the linked preview chain and image cache, exclude its scratch exchanges from active prompts, and invalidate affected session summaries. Raw chat and explicit long-term preferences remain; retired previews cannot be restored automatically by later similar questions. Browser launch, profiles, networking and manual login behavior are unchanged.

- Related chat follow-ups retain the prior completed browser preview across answers and reloads without resuming its agent. The new task remains independently controlled; unrelated topics, cancelled tasks, new chats and explicit Return to chat keep the old preview closed. Browser launch, networking and login settings are unchanged.

- Corrected read-and-summarize verification to check retrieved page content before the final chat response, without requiring that response to have already been delivered as a browser action. Final summaries use observed content rather than treating worker draft claims as evidence. Replayed the reported public benefits-page evidence through the configured verifier.

- Fixed cross-task tab switching checking the previous website instead of the requested destination tab. Switches bind the observed destination ID/URL, revalidate before dispatch, and retain public/domain checks. Page-opening requests that also ask for reading, summarization or a balance check continue through that work instead of being classified as navigation only.

- Fresh Visible sessions now open their scoped entry page natively before Playwright and Browser Use attach, matching the tested startup order. Explicit URLs take precedence, observed login entries are user/domain scoped, and session parameters never enter launch arguments. Existing sessions and Headless are unchanged. Fixture checks validate startup order; real-site sign-in remains a manual acceptance check.

- Visible manual input now retains an idle Browser Use connection while the agent waits for the user, matching the successful isolated comparison. Release invalidates cached page references without reconnecting or replaying actions. Headless keeps its existing detach/reconnect behavior. New browser workers load this change without restarting Aster.
- Visible now uses the tested minimal native Chrome startup and attaches Playwright afterward. Chrome retains normal storage/keychain handling and natural display dimensions; the existing preview and Browser Use handoff are retained. Headless is unchanged. A disposable-cookie probe found that Playwright's default storage flags could not read a cookie created with native storage settings; this is a reproduced compatibility issue, not proof of the cause of every Amex rejection.
- Visible mode now uses Chrome's natural window dimensions and display pixel ratio. Preview/manual-control coordinates read native CSS layout metrics without website JavaScript evaluation or viewport writes. Native window changes update control geometry and scaled frame mapping; stale-coordinate inputs are rejected. Headless retains its existing viewport. Network and manual-login settings are unchanged.
- Rolled back the later live-page follow-up routing and tab-attachment changes after a reported login regression. Persistent native networking, both-mode manual Browser Use detachment, the retained profile and the flicker fix are preserved.

- Retained native networking without request interception as the permanent default in both Headless and Visible modes, replacing the one-run Amex comparison. Passive network diagnostics and agent action checks remain. Existing profiles, browser launch settings and the flicker fix are preserved.
- Manual-input pauses in both modes disconnect Browser Use before publishing the handoff and reconnect only after the user releases control, retaining the application-owned browser session. Reconnection validates the same tab and invalidates stale agent targets. Real browser tests cover both modes; the user confirmed three consecutive Visible Amex sign-ins with CAPTCHA.
- Visible browser control now scales the existing page into the sidebar without resizing or foregrounding Chrome during takeover or release. Visible previews capture only the existing viewport without Playwright screenshot styling; Headless retains its existing resizing and capture behavior.
- Added internal per-run Visible profile generation records so a real-site login test can be tied to the exact profile used by Aster. Isolated browser fixture tests do not establish that real Amex authentication succeeds.

## 2026-11-21

- Reverted the Visible display-only scaling change at user request, restoring the previous viewport resizing and restoration behavior.


- Added a neutral Release control button at the top of the browser panel only, to return a paused website task to Browser Use after manual sign-in/input, using the existing scoped continuation API and private-step readiness check. Return to chat continues to close the panel without resuming; completed tasks are not rerun when releasing a later manual takeover.


- Restored an explicit Take control action and a wider right-side desktop panel with chat visible; removed the full-screen expansion controls. Native Chrome's viewport stays unchanged. Extended browser routing to named-site sign-in requests and semantic website intent for providers supplied without URLs. Read-only account lookups are explicitly supported after manual authentication instead of returning a generic inability response.

- Restored the saved pre-rebuild Playwright-managed Visible configuration: installed Chrome, the original 1280×850 initial viewport and shared screencast/manual-control path. Removed the later direct-process launcher, native-viewport/download helpers and transport observer. Saved profiles, Headless configuration, website-opening fixes and the wider right-side Take control UI are retained.

- Removed the remaining minimized-mode screenshot polling, custom viewport emulation, and native-window helper cache. Both Visible and Headless use Chromium screencasts for direct control.

- Restored ordinary Visible Chrome with its normal desktop window and Aster live panel. Removed the Tile windows UI/API, native window-arrangement script, and stream-hiding behavior. Headless behavior and saved Chrome profiles are retained.

- Added opt-in HTTP/1.1 diagnostics (`PERSONAL_AGENT_BROWSER_DISABLE_HTTP2=1`), preserving TLS validation and Chromium sandboxing. Retained digits in network error codes such as ERR_HTTP2_PROTOCOL_ERROR and separated Chromium navigation-error pages from domain-permission denials.
- Added a per-user Headless / Visible Chrome selector. Headless remains the default; idle switching closes browser tabs and retains independent saved profiles for each engine. Bound screencast dimensions to CSS viewport size on Retina displays, and merged cross-process frame trees by frame/parent identity for manual input and Browser Use observations.
- In PoC mode, authorized pages may load public embedded document dependencies, including POST-backed frames. Public-destination checks still run first, top-level navigation rules remain, and embedded hosts gain no agent DOM/action scope.
- Retained the last 100 redacted browser request-status records per run and allowed owned diagnostic history after completion/cancellation and restart. Reading live form flags still requires the current browser-input pause. Added a concise network-status summary without recording credentials, headers, bodies or URL paths/queries.
- Corrected saved browser restoration to select the matching page or create a separate tab, preserving unrelated pages. Restoring a login screen permits its safe GET URL while retaining mutation-URL checks. Shopping guidance now establishes the delivery country, verifies storefront region/currency and avoids blindly using the first regional search result.
- Added authenticated, pause-bound login diagnostics for field validity, disabled login buttons and a bounded transient network-status history. Diagnostics omit credential values, request/response bodies, headers and URL paths/queries; they do not submit forms.
- Fixed page-level typing during browser takeover when a website autofocuses its field but the local page has no keyboard target. Typing/paste route to the owned remote field; chat and other local inputs retain their own keyboard handling.
- Removed the Resume agent chat button and its UI guidance. Paused tasks expose Open browser and Cancel task, while new chat requests can move on from the old task.
- Stopped projecting an old browser task into chat after a newer request, so a greeting or unrelated answer no longer inherits an Open browser card. Greetings start a fresh request rather than being treated as website input.
- Replaced browser Done/Continue with Return to chat. Returning closes the panel without executing the agent or cancelling a paused task; chat exposes separate Open browser, Resume agent and Cancel task actions. The closed state persists across reloads and private-step checks apply only to explicit agent resumption.
- Stabilized manual browser control with a fixed narrow-screen panel, panel-local Done/Continue controls, isolated website scrolling, and touch swipe/tap support. Adjacent wheel events coalesce behind slow acknowledgements; nested scrollers receive the pointer position. Mobile keyboard height changes retain the website layout, and real viewport resizes restart Chromium capture to prevent stale frame geometry.
- Added explicit Take control / Done controls for completed browser tasks. Manual interaction reuses the authenticated, pause-bound browser channel; Done releases control without model execution, required-field checks, or logging typed values. Saved completed pages can be reopened after a server restart using only their previously observed safe URL.
- Corrected guidance that described a completed read-only browser preview as already editable. Active input cards now preserve their actual server-provided handoff prompt.

- Enabled a shared PoC browser policy by default (`PERSONAL_AGENT_BROWSER_POLICY=strict` retains the previous conservative profile). Routine preparation uses target/form semantics, unrelated private fields no longer block ordinary controls, and observed public provider links/top-level redirects can proceed without scope approval.
- Added direct proof for explicit page-opening goals, reuse of an unchanged undispatched approval, optional selector pre-inspection in PoC mode, and bounded recovery of up to twelve failures/three identical attempts. Task budgets, worker instructions and actuation use the same policy profile.
- Fixed navigation-only requests being interrupted by an empty phone field on completion. Phone prompts no longer invent SMS/country-code requirements. Known pending navigation pauses are repaired without advancing the website or collecting information.
- Added synthetic real-model acceptance for opening forms, ordinary controls with unrelated password fields, cross-provider navigation and one approved/once-only submission, plus policy/ownership/redirect/approval regressions. Final commitments, private controls, human verification, private-network exclusion and duplicate-submission protections remain enforced.

- Added main-agent text-to-image generation and reference-image editing, using GPT-Image-2.6 Sunburst by default and GPT-6.1 Terra for independent visual checks against scene prose and the user's request.
- A failed visual check permits one correction attempt; unverified images are not published. The final chat uses the checked scene prose verbatim, with private image attachments that survive reloads and support later edits.
- Added three-scene request bounds, idempotent tool calls, cancellation handling, scoped reference access, and browser/model acceptance coverage. Retained meaningful prose-polish instructions and replaced the temporary no-image-capability instruction with actual tool guidance.

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

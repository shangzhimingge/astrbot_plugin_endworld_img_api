# WebUI Configuration Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a native AstrBot Plugin Page that edits and persists every plugin setting, reports runtime state, imports/exports configuration, and tests each source URL inline.

**Architecture:** Keep validation and change summaries in a framework-independent Python module. Register thin AstrBot Web API handlers on the existing plugin instance, preserving the injected configuration object's identity and reusing the existing network safety checks. Build the Page with dependency-free HTML, CSS, and JavaScript using only `window.AstrBotPluginPage`.

**Tech Stack:** Python 3, AstrBot Plugin Pages/Web API, aiohttp, HTML5, CSS, ES modules, Python `unittest`.

## Global Constraints

- Keep existing message commands and image-fetch behavior compatible.
- Use `pages/settings/index.html` and the official Page bridge.
- Display API testing beside each current URL input and test unsaved values.
- Treat the backend as the final validation boundary; rejected input must not alter runtime or persisted configuration.
- Support light/dark themes, keyboard focus, desktop, and narrow screens.

---

### Task 1: Schema validation and change summaries

**Files:**
- Create: `webui_config.py`
- Modify: `_conf_schema.json`
- Create: `tests/test_webui_config.py`

**Interfaces:**
- Produces: `load_schema()`, `validate_and_normalize(candidate, schema)`, `summarize_changes(before, after)`, and `ConfigValidationError.errors`.

- [ ] Write tests for a complete round-trip, input immutability, whitespace normalization, source order, unknown/missing keys, bool-versus-int typing, ranges, enums, invalid URLs, and empty source lists.
- [ ] Run `python -m unittest tests.test_webui_config -v`; expect failures because `webui_config.py` is absent.
- [ ] Implement schema loading relative to the module and recursive validation for `string`, `int`, `bool`, `list`, and `template_list`; return field-path errors such as `sources[0].apis[1]`.
- [ ] Require a nonempty source name, at least one keyword, and at least one HTTP/HTTPS URL with a hostname; trim string-list entries and force `__template_key` to `default_source`.
- [ ] Add `compress_quality` bounds `1..100` and `cooldown` minimum `0` to `_conf_schema.json`.
- [ ] Implement change summaries for scalar changes plus added, removed, and reordered sources.
- [ ] Re-run the focused tests; expect all to pass.
- [ ] Commit with `feat: validate WebUI configuration payloads`.

### Task 2: Page API handlers and atomic runtime updates

**Files:**
- Modify: `main.py`
- Create: `tests/test_webui_handlers.py`

**Interfaces:**
- Consumes: Task 1 validation helpers.
- Produces: handlers for `config`, `config/save`, `api/test`, `status`, `config/export`, and `config/import` under `/astrbot_plugin_endworld_img_api/`.

- [ ] Add fake AstrBot modules, context, request objects, upload objects, and a dict-like configuration with `save_config()` to the handler tests.
- [ ] Test exact route/method registration; deep-copy reads; save identity preservation; single persistence call; rollback on persistence failure; import preview versus confirmation; export round-trip; and status fields.
- [ ] Run `python -m unittest tests.test_webui_handlers -v`; expect handler-related failures.
- [ ] Register the six routes during initialization with `context.register_web_api()` and use `astrbot.api.web` response/request helpers.
- [ ] Add `_config_lock`, `_session_lock`, `_last_saved_at`, `_close_session()`, and `_apply_config()`. Save by validating, snapshotting, `clear()`/`update()`, and `save_config()`; roll back on exceptions.
- [ ] Close and detach the aiohttp session only when `verify_ssl` changes so the next request recreates it.
- [ ] Implement import upload preview with a 256 KiB limit and a safely named temporary file under plugin data, always deleting it; confirm through JSON `{config, confirm: true}`.
- [ ] Implement JSON export with `file_response` and filename `endworld-img-config.json`.
- [ ] Re-run focused tests; expect all to pass.
- [ ] Commit with `feat: add Plugin Page configuration APIs`.

### Task 3: Reusable safe URL diagnostics

**Files:**
- Modify: `main.py`
- Modify: `tests/test_webui_handlers.py`

**Interfaces:**
- Produces: `FetchResult`, `_fetch_url(...) -> FetchResult`; preserves `_safe_fetch(...) -> tuple[bytes, str, str]`.

- [ ] Add tests for unsupported schemes, private destinations, redirect revalidation, response status/content type/final URL, timeout errors, and the existing tuple contract.
- [ ] Run the focused tests and confirm the new diagnostic tests fail.
- [ ] Refactor current fetching into `_fetch_url`, validating HTTP/HTTPS and every redirect with existing SSRF checks; keep `_safe_fetch` as a compatibility wrapper.
- [ ] Implement `page_test_api` using `_fetch_url` and monotonic elapsed time without persisting the supplied URL.
- [ ] Re-run focused tests; expect all to pass and existing callers to keep their tuple behavior.
- [ ] Commit with `feat: add inline source API diagnostics`.

### Task 4: Responsive settings Page

**Files:**
- Create: `pages/settings/index.html`
- Create: `pages/settings/app.js`
- Create: `pages/settings/style.css`

**Interfaces:**
- Consumes: Task 2 bridge endpoints and response contracts.

- [ ] Create a semantic Page shell with status, source management, sending, image processing, text, import/export, and save regions; load relative CSS and an external module script.
- [ ] In `app.js`, await `bridge.ready()`, load config/status, maintain a deep-cloned draft and dirty state, and render all schema fields.
- [ ] Render collapsible source cards with add/delete/reorder controls and row editors for keywords, APIs, and groups.
- [ ] Put Test and Delete controls beside every API input. Call `apiPost("api/test", {url})` with the current unsaved input, disable only that Test control, and write the result to its own `aria-live` region.
- [ ] Map backend field-path errors to controls; preserve draft data after errors; warn on `beforeunload` while dirty.
- [ ] Add export through `bridge.download`, import preview through `bridge.upload`, a visible change summary, and confirmed application through `apiPost`.
- [ ] Add light/dark CSS variables, visible `:focus-visible`, touch-sized controls, two-column desktop groups, and a single-column narrow layout.
- [ ] Run `node --check pages/settings/app.js`; expect exit code `0`.
- [ ] Commit with `feat: add responsive plugin settings Page`.

### Task 5: Documentation, versioning, and full verification

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `metadata.yaml`
- Modify: `main.py`

- [ ] Document where to open the Page, every configuration group, inline API testing, import preview/confirmation, export, and unsaved-change warnings.
- [ ] Bump plugin version consistently to `6.5.0` and record the Plugin Pages-compatible AstrBot version floor in metadata.
- [ ] Run `python -m unittest discover -s tests -v`; expect all tests to pass.
- [ ] Run `python -m compileall -q main.py webui_config.py tests`; expect exit code `0`.
- [ ] Run `python -m json.tool _conf_schema.json`; expect valid JSON.
- [ ] Run `node --check pages/settings/app.js`; expect exit code `0`.
- [ ] Run `git diff --check`; expect no whitespace errors.
- [ ] Inspect the complete diff against the approved design and verify every acceptance criterion has evidence.
- [ ] Commit with `docs: document WebUI configuration center`.

## Acceptance Mapping

| Criterion | Tasks |
|---|---|
| Native Page discovery and bridge communication | 2, 4, 5 |
| All settings load, edit, persist, and update runtime state | 1, 2, 4 |
| SSL changes rebuild the session | 2 |
| Per-URL inline tests use current unsaved values | 3, 4 |
| Import/export round-trip and rejected imports preserve state | 1, 2, 4 |
| Theme, narrow-screen, keyboard, and error usability | 4 |
| Python, JSON, JavaScript, and regression checks pass | 1-5 |

# 🤝 Session Handoff — LMArena-fork (English translation & modernization)

*Written 2026-10-06 (UTC) by the assistant sessions that performed this work. Read this before continuing development.*

## Current state

| | |
|---|---|
| Repo | `freeforall1932-design/LMArena-fork` (fork of `Shuairen51/LMArena`, upstream author `Lianues`) |
| Working branch | `chrome-extension` (based on `main` @ `368c292`) |
| PRs | **#1** — translation/modernization/arena.ai wave: **MERGED** into main on 2026-10-06 (`368c292`) · **#2** — Chrome extension: open (branch `chrome-extension`, commit `7732989`) |
| Local clone | `~/LMArena-fork` (sandbox workspace; branch checked out, tree clean) |
| Version | `2.8.0` in `config.jsonc`, userscript `@version`, and extension `manifest.json`/`constants.js` — a consistency test in `tests/extension_units.mjs` enforces this; keep them in sync! |
| Tests | `python tests/integration_smoke.py` → **35/35 passing** · `node tests/extension_units.mjs` → **10/10 passing** (2026-10-06), incl. live arena.ai catalog fetch |

### Commit history on the branch
1. `2272093` — *Translate project to English, modernize code, and fix bugs* (9 files, +1337/−1003): full EN translation of README/config/comments/logs, JSONC parser rewrite, WS replacement race fix, aiohttp async updater, TextDecoder `{stream:true}` UTF-8 fix, `sys.exit`, `allow_reuse_address`, no-file-serving id_updater handler, dynamic update-folder detection, regex-replacement injection fixes, `server_host`/`server_port` config, OpenAI role-delta chunk, dead-code removal, requirements floors.
2. `50b4b81` — Arena.ai rebrand compatibility + docs: userscript `@match arena.ai` & `/nextjs-api/stream/...` path with legacy fallback & reCAPTCHA error surfacing; `model_updater.py` rewritten around the **public** `GET https://arena.ai/nextjs-api/model-catalog` endpoint (direct fetch, `--via-browser` fallback, `--direct-only`); hot-reload of config/models/endpoint-map (mtime-based, `refresh_dynamic_files()`); README reworked into a step-by-step Quick Start with checkpoints + Troubleshooting table; **Mermaid diagram fixed** (`Trying to inactivate an inactive participant (IU)` — activation balance corrected and validated with mermaid v11 parser); `tests/integration_smoke.py` added; `PROJECT_SUMMARY.md` + this file added; version → 2.7.0. **Data refresh:** `available_models.json` regenerated from the live catalog (251 unique models, now with per-model `arenas` tags); `models.json` stale entries repaired against the live catalog (`gemini-2.5-pro` ID changed on the site; `gpt-5` renamed `gpt-5-high`; `nano-banana` re-pointed to `gemini-2.5-flash-image-preview` ID; +6 current models added). `model_endpoint_map.json` intentionally left untouched (contains example/user session IDs — stale `o3-xxx`/`gemini-xxx` keys are harmless no-ops).
3. `7732989` (branch `chrome-extension`, PR #2) — **Chrome extension (MV3, load-unpacked)** in `chrome-extension/`: content-script WebSocket bridge (same protocol as the userscript → **zero server changes**; content-script fetch keeps the page origin so SameSite=Lax session cookies flow), service-worker ID capture via `chrome.webRequest` observation (**no `window.fetch` monkey-patching**), in-flight counter messaging so the bridge's own retries are never self-captured, badge status (`ON`/`CAP`/`ERR`) + popup with persisted port config (`chrome.storage.local`, live reconnect on change), one-shot capture armed via `chrome.storage.session` (survives SW restarts). `tests/extension_units.mjs` (10/10): capture-regex parity with the userscript, endpoint path builders, manifest integrity, cross-file version consistency. Version → **2.8.0** everywhere. README Step 2 now offers extension vs userscript (**never both** — they fight over the single server-side WS). **Caveat: not yet smoke-tested in a real browser** (no Chrome in the sandbox) — use the first-load checklist below.

### First-load checklist for the extension (needs a real browser — do this once)
1. `chrome://extensions` → Developer mode → **Load unpacked** → select `chrome-extension/`. No errors on the card.
2. Start `python api_server.py`; open an arena.ai tab → badge **ON**, title ✅, server log shows "Tampermonkey script connected via WebSocket" (log wording is legacy; it's the extension).
3. Popup shows "Connected", correct ports; change port → Save → tab reconnects (server log shows replacement) → change back.
4. `python id_updater.py` → badge **CAP**, title 🎯 → click Retry on an assistant message → id_updater terminal prints the captured IDs; config.jsonc updated.
5. End-to-end: `curl` a chat completion via `http://127.0.0.1:5102/v1/chat/completions` (this is the step that also verifies leftover-work #1: whether the injected `{messages, modelId}` body is accepted by the current endpoint / captcha enforcement).
6. Two tabs open → only the last is authoritative (expected; single-WS design).
7. SW edge: `chrome://extensions` → service worker "Inspect" console → confirm `[API Bridge BG] installed` and no CSP/importScripts errors.

## Environment / credential notes

- **PAT**: the fine-grained token used for pushing **lacks the `Administration` scope**, so `PATCH /repos/...` (repo description) returns `403 Resource not accessible by personal access token`. The GitHub **repo "About" description is still Chinese** — fix it either in the repo UI (⚙️ gear on the repo home) or with a token that has *Administration: Read & Write*. Suggested English text is in the PR conversation.
- The token seen in this session **expires 2026-10-06 17:00 UTC** and was shared in plaintext — it should be considered compromised; rotate it.
- **Git history stays Chinese** (99 upstream commits like `添加更新模型功能`). Rewriting history on a fork was judged destructive and unnecessary; only the working tree was translated.
- Mermaid validation harness: `npm i mermaid jsdom` in a scratch dir + `mermaid.parse()` under jsdom globals (script pattern preserved in the PR conversation; `/tmp` sandboxes are ephemeral).
- Sandbox had **no `curl`** — use Python `requests`/`urllib` for HTTP probing.

## Verified facts about arena.ai (2026-10-06) — don't re-discover these

- `lmarena.ai` 301-redirects to `arena.ai`; live sections: `/`, `/chat`, `/agent`, `/text-to-image`, `/work`.
- Chat retry endpoint (from the site's own bundle): `PUT /nextjs-api/stream/retry-evaluation-session-message/{sessionId}/messages/{messageId}`; the site now sends `{recaptchaV3Token}` (and `recaptchaV2Token` on challenge) — obtained via an internal `getRecaptchaV3Token("chat_retry")` helper inside the bundle.
- Legacy `/api/*` prefix: `403 {"error":"Route not allowed"}` for probed routes.
- Other stream endpoints in the bundle: `api/stream/create-evaluation`, `api/stream/post-to-evaluation/{id}`, `api/stream/resume-webdev/{id}`, `api/stream/resume-video-workflow/{id}`; agent mode: `/api/coding-agent/sessions` (+ GitHub OAuth connector endpoints).
- **Public** model catalog: `GET https://arena.ai/nextjs-api/model-catalog` — anonymous 200 with a browser UA; list of `{arena, complete, models[]}`; `models[]` entries keep the old schema (`id`, `publicName`, `name`, `displayName`, `organization`, `provider`, `capabilities`, `userSelectable`) plus `rank`. 6 arenas / 459 entries / 251 unique by publicName.
- Initial anonymous HTML of `/` and `/chat` contains **no** embedded model JSON — the old HTML-scraping updater is effectively dead on the new site (kept only as fallback).

## Leftover work (prioritized)

1. **Verify the injected-payload contract with a logged-in session** (needs a human account): does `PUT /nextjs-api/stream/retry-...` still accept `{messages:[...], modelId}` bodies (the bridge's injection), or only `{recaptchaV3Token}`? Test via the bridge end-to-end on `/chat` and in battle mode — with the userscript **and** the extension (first-load checklist step 5). Everything else depends on this answer.
2. **reCAPTCHA v3 support** if #1 shows rejection: options — (a) userscript hooks the site's `getRecaptchaV3Token` from the bundle (find webpack chunk exporting it, call with action `"chat_retry"`), (b) trigger a real invisible-recaptcha execution with the site key, (c) fall back to prompting the user to click Retry manually.
3. **Autonomous sessions** via `create-evaluation` + `post-to-evaluation/{id}`: would remove the manual `id_updater.py` Retry-click step and auto-heal dead sessions. Reverse-engineer request/response shapes from the bundles (`1m3sfs-riwu6o.js`, `2rs5h181e-5e9.js` contained them).
4. **CI**: GitHub Actions running `pip install -r requirements.txt && python tests/integration_smoke.py --skip-live && node tests/extension_units.mjs` on PRs (offline modes exist for exactly this).
5. **Repo description** → English (needs Administration scope or UI edit — see above). Also consider Topics: `openai-api`, `lmarena`, `arena-ai`, `proxy`, `fastapi`.
6. Multi-tab WebSocket pooling (server dict of connections + health-aware selection) for real concurrency. The extension's worker-as-hub design is the natural client-side half; the server still supports only one `browser_ws`.
7. New arenas support (Agent/WebDev/Video) — large effort, different protocols; only after #1–#3.
8. Nice-to-haves: `/healthz` endpoint; rate limiting; `n>1` fan-out for images (old versions had parallel image tasks — current unified pipeline ignores `n`); real token usage from stream metadata if exposed; config schema validation at startup; Windows `os.execv` restart quirk check (idle-restart path).
9. PR #1 is merged (main is at version `2.7.0`). After merging PR #2, remote `main` reaches `2.8.0`; existing installs then auto-update cleanly (updater compares `packaging.version`). Optionally delete the stale merged branch `english-translation-and-modernization` on the remote.
10. **Extension follow-ups**: real-browser smoke test (checklist above — sandbox had no Chrome); decide load-unpacked-only vs Chrome Web Store publication (store review may scrutinize third-party-site automation); optional `chrome.notifications` on session death / Cloudflare challenge; Firefox port guards (`world: MAIN` not needed anymore, but `storage.session` landed in FF 127+).

## Gotchas for future editors
- **Version sync is now 4 places**: `config.jsonc` `"version"`, userscript `@version`, extension `manifest.json` `"version"`, extension `common/constants.js` `VERSION`. `tests/extension_units.mjs` enforces consistency — bump all together. (The userscript's banner log string carries a version too; cosmetic only.)
- **Extension data path must stay in the content script.** Moving the Arena fetch into the service worker would drop SameSite=Lax cookies (extension-origin requests are cross-site) and break auth. The SW is for capture/badge/status only.
- **Never run the userscript and the extension together** — both open a WS per Arena tab and the server keeps only the last connection; they flap. Documented in README + extension README.
- `chrome.storage.session` is not readable from content scripts by default (trusted-contexts only) — capture state lives there deliberately; content scripts get results via `tabs.sendMessage({type:'capture_result'})`.

- Keep `strip_json_comments` (string-aware!) — the old naive regex corrupts any config value containing `//`. Three copies exist (api_server, id_updater, update_script); if you touch one, touch all (or factor into a shared module + fix `sys.path` handling in `modules/update_script.py`).
- The 413 detection in `non_stream_response` compares against the `ATTACHMENT_TOO_LARGE_MESSAGE` constant — keep them in sync if you reword it.
- Userscript title prefixes: `✅ ` (connected) and `🎯 ` (capture mode) are UI contracts documented in the README checkpoints; use the `addTitlePrefix`/`removeTitlePrefix` helpers (surrogate-pair safe).
- `model_updater.py` direct fetch sends a browser User-Agent — a bare `python-requests` UA may get blocked; keep the header.
- The smoke test patches `config.jsonc` in a **temp copy**, never in the repo; keep it that way.
- Version bumps: update `config.jsonc` `"version"` **and** userscript `@version` together.

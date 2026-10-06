# 📊 Project Summary — LMArena Bridge

*Last verified live against arena.ai on **2026-10-06** (UTC). All claims below marked ✅ were verified by direct HTTP probing of the production site or by the automated test suite (`python tests/integration_smoke.py`, 35/35 passing).*

## What the project is

**LMArena Bridge** turns [Arena.ai](https://arena.ai/) (formerly [LMArena.ai](https://lmarena.ai/), née "Chatbot Arena") into an **OpenAI-compatible API endpoint** running on your own machine. It lets any OpenAI-API client — chat apps (e.g. SillyTavern, ChatBox, LobeChat), coding plugins (Continue, Cline), curl, SDKs — talk to the frontier models hosted on Arena (Gemini, GPT, Claude, DeepSeek, Grok, image models, ...) **using your own logged-in browser session**, for free.

It is a *reverse proxy / bridge*, not an official API client: a local FastAPI server relays OpenAI-format requests to a Tampermonkey userscript running on an open Arena tab, which replays them against the site's internal streaming API with your cookies attached.

## Architecture

```
OpenAI client ──HTTP──▶ api_server.py (FastAPI, :5102)
                             ▲  │
                     WebSocket  │  (request_id-tagged frames)
                             │  ▼
                LMArenaApiBridge.js (Tampermonkey, on arena.ai tab)
                             │  ▲
                    fetch (PUT, cookies) │ SSE-style stream
                             ▼  │
                          arena.ai internal API
```

Two sidecar scripts: `id_updater.py` (one-click capture of a session/message ID pair into `config.jsonc`) and `model_updater.py` (refreshes `available_models.json` from Arena's **public** model-catalog API — no browser needed).

## What it can do (feature checklist)

| Capability | Status |
|---|---|
| `POST /v1/chat/completions` — streaming (SSE, OpenAI chunk format incl. `role` delta + `[DONE]`) | ✅ tested |
| `POST /v1/chat/completions` — non-streaming (full JSON, `usage` estimates) | ✅ tested |
| `GET /v1/models` — OpenAI model list from `models.json` | ✅ tested |
| Text-to-image models unified into chat (returns Markdown `![Image](url)`), streaming too | ✅ tested |
| Multi-file Base64 attachments (images/audio/PDF/code) via `image_url` data-URIs; original filename via the `detail` field | ✅ tested (payload build) |
| Full conversation-history injection (multi-turn contexts) | ✅ |
| Tavern Mode (merges all `system` messages — for SillyTavern-style clients) | ✅ tested |
| Bypass Mode (extra empty user message to slip past moderation) | ✅ |
| Battle mode + Direct-chat mode, per-message `participantPosition` handling | ✅ tested |
| Per-model session pools (`model_endpoint_map.json`) with random selection + mode binding | ✅ tested |
| Optional `api_key` (Bearer auth) on chat endpoint | ✅ tested |
| **Hot reload** of `config.jsonc` / `models.json` / `model_endpoint_map.json` — no restart | ✅ tested |
| One-command model list refresh, direct from `arena.ai/nextjs-api/model-catalog` (251 unique models, 6 arenas, incl. per-model `arenas` tags) | ✅ tested live |
| One-click session-ID capture (`id_updater.py` + Retry-button interception) | ✅ flow tested |
| **Chrome extension (MV3, load-unpacked)** as a Tampermonkey alternative: badge status, popup port config, `webRequest`-based ID capture (no page patching), isolated-world execution | ✅ unit-tested; protocol identical to the userscript (browser smoke pending — no Chrome in CI sandbox) |
| Auto program update from this GitHub repo (version compare → zip → merge config → restart) | ✅ logic tested |
| Idle auto-restart, Cloudflare-challenge detection + auto page refresh, friendly errors for 413/captcha | ✅ |
| Works on **arena.ai** *and* legacy **lmarena.ai** (userscript matches both; new `/nextjs-api/stream/...` path with `/api/stream/...` fallback) | ✅ code-level; see caveats below |

## Compatibility with arena.ai — verified state (2026-10-06)

The site rebranded from LMArena to **Arena.ai** and changed internals. Findings from probing the live site and its JS bundles:

| Item | Finding | Impact |
|---|---|---|
| Domain | ✅ `lmarena.ai` → **301** → `arena.ai`; pages `/`, `/chat`, `/agent`, `/text-to-image`, `/work` all live | Userscript now `@match`es both domains (before this update it **would not even load** on the redirected site) |
| Chat retry endpoint | ✅ Still exists: `PUT /nextjs-api/stream/retry-evaluation-session-message/{sessionId}/messages/{messageId}` (found in the site's own JS) — path prefix changed from `/api/stream/` | Userscript now tries `/nextjs-api/` first, falls back to `/api/` on 404 |
| Old `/api/*` prefix | ⚠️ `GET /api/model-catalog` → `403 {"error":"Route not allowed"}` — the legacy prefix appears locked down | Fallback kept only for safety |
| **reCAPTCHA** | ⚠️ The site's own retry call now sends `recaptchaV3Token` (and v2 on demand) in the PUT body. The bridge does **not** generate tokens | **Main open risk.** The bridge's PUT may be rejected when captcha enforcement triggers. The userscript now surfaces a clear, actionable error when this happens. See "What it lacks" #1 |
| Model catalog | ✅ Public JSON at `GET /nextjs-api/model-catalog` (anonymous, browser UA): 6 arenas — text (123), code (83), text-to-image (38), search (17), text-to-video (75), document (123); 251 unique models; same schema as before plus `rank`/`userSelectable` | `model_updater.py` rewritten to use it directly — browser flow no longer required |
| Old HTML scraping | ❌ Anonymous page HTML no longer embeds model JSON (`publicName` absent from initial HTML) | Legacy browser-extraction path kept as fallback only; it may return 0 models on some pages |
| Message injection contract | ❓ Unverified: the site's own retry sends *only* captcha tokens now; whether the endpoint still accepts a full `{messages, modelId}` body (what the bridge injects) can only be confirmed with a logged-in session | Highest-priority leftover verification — see SESSION_HANDOFF.md |
| Agent / WebDev / Video / Work arenas | ❌ Different APIs entirely (`/api/coding-agent/sessions`, `api/stream/resume-webdev/{id}`, `api/stream/resume-video-workflow/{id}`, `create-evaluation`, `post-to-evaluation/{id}`) | Not supported by the bridge (chat + text-to-image only) |

## What it lacks / known limitations

1. **No reCAPTCHA handling** (biggest gap). Arena now obtains a reCAPTCHA-v3 token for `chat_retry`. If enforcement kicks in for the bridge's requests, they fail with a surfaced error. Possible fixes: hook the page's own token generator from the userscript (it lives inside the site bundle, not on `window` — needs reverse-engineering), or piggyback on a real user Retry click.
2. **No automatic session refresh.** A captured `session_id`/`message_id` pair dies when the underlying conversation expires; today a human must re-run `id_updater.py` and click Retry. The site's own `create-evaluation` + `post-to-evaluation/{id}` endpoints (seen in its JS) could let the bridge **create and maintain its own sessions autonomously** — the most impactful future upgrade.
3. **No automatic model-list refresh.** `models.json` curation is manual by design (you choose which of the 251 models to expose); `model_updater.py` is one command but not scheduled, and it only writes the *reference* file.
4. **Single browser tab.** The server keeps exactly one WebSocket; opening a second Arena tab silently replaces the first (handled gracefully, but no multi-tab pooling/load-balancing). The Chrome extension's worker-hub design is a stepping stone toward pooling, but the server still needs a multi-connection upgrade.
5. **Chat + text-to-image only.** No Agent mode, WebDev arena, video generation, search-arena or "Work" support — those use different endpoints and flows.
6. **Approximate OpenAI-spec coverage.** `usage` numbers are character-count estimates; `n > 1`, `tools`/function-calling, `response_format` (JSON mode), `temperature`/`top_p` etc. are ignored — Arena's UI flow doesn't accept them. Errors are returned in-stream as `[LMArena Bridge Error]` text plus proper HTTP codes for non-stream.
7. **Login is manual.** The bridge rides on browser cookies; if the Arena session logs out or Cloudflare challenges appear, a human must fix the tab (the server detects both and tells you).
8. **Auto-update is branch-based** (`main.zip`), with config merging but no checksums/signatures and no rollback.
9. **Fair-use / ToS risk.** This proxies a free evaluation website in a way its operators may not intend; heavy automated use can get sessions rate-limited or banned. The ID-pool feature exists to spread load, but there is no built-in rate limiting.

## Quick pointers

- Run it: see **README.md → Quick Start (Step by Step)**.
- Re-verify everything: `python tests/integration_smoke.py` (add `--skip-live` for offline).
- Session state & leftover work: **SESSION_HANDOFF.md**.

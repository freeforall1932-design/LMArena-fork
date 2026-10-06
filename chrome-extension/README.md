# 🧩 LMArena API Bridge — Chrome Extension (load unpacked)

A native Chrome (Manifest V3) alternative to the Tampermonkey userscript. It speaks the **exact same WebSocket protocol** as `api_server.py`, so the Python side is unchanged.

## Why use the extension instead of Tampermonkey?

| | Tampermonkey script | Chrome extension |
|---|---|---|
| Dependency | Tampermonkey + manual paste | None — load once in `chrome://extensions` |
| ID capture | Monkey-patches the page's `window.fetch` | Cleaner: `chrome.webRequest` observation in the service worker |
| Status UI | Page-title prefixes (✅/🎯) | Toolbar **badge** (`ON` / `CAP` / `ERR`) + popup, title prefixes kept too |
| Ports | Hard-coded constants | Configurable in the popup (persisted) |
| Page interference | Runs in page context (page could see/patch it) | Runs in an isolated world (page cannot tamper) |

> ⚠️ **Run only ONE bridge at a time.** If both the userscript and the extension are active, each Arena tab opens its own WebSocket and the server keeps only the last connection — they will fight. Disable one.

## Install (load unpacked)

1. Clone/download this repository.
2. Open `chrome://extensions` in Chrome (or any Chromium browser: Edge, Brave, Vivaldi…).
3. Enable **Developer mode** (toggle, top-right).
4. Click **Load unpacked** and select the **`chrome-extension/`** folder (the one containing `manifest.json`).
5. Pin the 🌉 icon to the toolbar for easy status checks.

## Use

1. Start the local server from the project root: `python api_server.py`
2. Open (or refresh) any **arena.ai** tab while logged in.
   - Badge turns **ON** (green) and the page title gets the ✅ prefix.
3. One-time session capture (or whenever a session dies):
   - Run `python id_updater.py`, pick a mode.
   - Badge turns **CAP** (orange), title shows 🎯.
   - On the Arena page, click **Retry** on an assistant message from your target model.
   - The extension observes that request (no page patching), posts the IDs to `id_updater.py`, and the badge returns to **ON**.
4. Point your OpenAI-compatible app (Hermes, ChatBox, SillyTavern, LobeChat, Continue, …) at:
   - **Base URL**: `http://127.0.0.1:5102/v1`
   - **API Key**: anything (or the `api_key` you set in `config.jsonc`)

## Configure ports

Click the toolbar icon → set **API server port** / **ID updater port** → **Save ports**. Open Arena tabs reconnect automatically. (If you change the server port, remember `config.jsonc`'s `server_port` must match.)

## Architecture

```
arena.ai tab
 ├─ content/bridge.js (isolated world)
 │    • WebSocket ↔ ws://localhost:5102/ws   (holds the data path; page-origin
 │    • fetch(PUT /nextjs-api/stream/…)         fetch carries session cookies)
 │    • streams response chunks back to the server
 ├─ background.js (service worker)
 │    • chrome.webRequest observer → ID capture (one-shot, ignores the
 │      bridge's own in-flight requests)
 │    • POSTs captured IDs → http://127.0.0.1:5103/update (id_updater.py)
 │    • badge + popup status hub
 └─ popup/ (status, port settings)
```

Data path never touches the service worker, so MV3 worker lifetime limits cannot interrupt a streaming response.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Badge stays `–` | No Arena tab open, or the tab was opened before the extension was loaded → refresh the tab |
| Badge `ERR` / red, console shows `WebSocket error` | Nothing is listening on the port: start `python api_server.py` (wait for `Uvicorn running on http://127.0.0.1:5102`) or fix the port in the popup. The bridge alternates `127.0.0.1`/`localhost` between retries to survive IPv6 (`::1`) resolution mismatches |
| Console shows `Refused to connect to 'ws://…' … Content Security Policy` | The site's CSP is blocking page-context WebSockets — report the exact message; the fix is moving the socket into the service worker (tracked in SESSION_HANDOFF.md) |
| Badge `ON` but requests 503 | Another tab/bridge (userscript?) replaced this connection — keep one Arena tab, disable the Tampermonkey version |
| `CAP` never resolves after Retry click | `id_updater.py` not running (its listener must be up first), or wrong ID-updater port in the popup |
| Response mentions reCAPTCHA | Arena is challenging automated retries — interact with the page manually once, then retry |
| Extension stopped working after edits | `chrome://extensions` → click ⟳ on the card, then refresh the Arena tab |

## Files

```
chrome-extension/
├── manifest.json          # MV3 manifest (load-unpacked ready)
├── background.js          # Service worker: webRequest capture, badge, hub
├── common/constants.js    # Shared constants (ports, URL regex, endpoint paths)
├── content/bridge.js      # WebSocket + streaming fetch relay (page origin)
├── popup/popup.html       # Toolbar popup UI
├── popup/popup.js         # Popup logic (status, port settings)
└── README.md              # This file
```

// common/constants.js
// Shared constants for the LMArena API Bridge Chrome extension.
// Loaded into the content-script context (via manifest "js" list) and into the
// service worker (via importScripts). Attaches everything to globalThis.LMAB.

(function () {
    'use strict';

    const LMAB = {
        VERSION: "2.8.0",

        DEFAULT_SERVER_PORT: 5102,        // api_server.py WebSocket + HTTP
        DEFAULT_ID_UPDATER_PORT: 5103,    // id_updater.py one-shot listener

        // Matches both the current /nextjs-api/stream/... path and the legacy
        // /api/stream/... path used before the Arena.ai rebrand.
        CAPTURE_URL_RE: /\/(?:nextjs-)?api\/stream\/retry-evaluation-session-message\/([a-f0-9-]+)\/messages\/([a-f0-9-]+)/,

        // Endpoint candidates for the bridge's own retry calls, in order of
        // preference. A 404 on one path falls through to the next.
        STREAM_PATH_BUILDERS: [
            (sessionId, messageId) => `/nextjs-api/stream/retry-evaluation-session-message/${sessionId}/messages/${messageId}`,
            (sessionId, messageId) => `/api/stream/retry-evaluation-session-message/${sessionId}/messages/${messageId}`,
        ],

        // Hosts the extension operates on (used for tab queries + webRequest filters).
        ARENA_URL_PATTERNS: [
            "*://arena.ai/*",
            "*://*.arena.ai/*",
            "*://lmarena.ai/*",
            "*://*.lmarena.ai/*",
        ],

        TITLE_OK_PREFIX: "✅ ",
        TITLE_CAPTURE_PREFIX: "🎯 ",
    };

    globalThis.LMAB = LMAB;
})();

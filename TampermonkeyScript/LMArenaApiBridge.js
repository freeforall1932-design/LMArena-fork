// ==UserScript==
// @name         LMArena API Bridge
// @namespace    http://tampermonkey.net/
// @version      2.8.0
// @description  Bridges Arena.ai (formerly LMArena) to a local API server via WebSocket for streamlined automation.
// @author       Lianues
// @match        https://lmarena.ai/*
// @match        https://*.lmarena.ai/*
// @match        https://arena.ai/*
// @match        https://*.arena.ai/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=arena.ai
// @grant        none
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';

    // --- Configuration ---
    const SERVER_URL = "ws://localhost:5102/ws"; // Must match the port in api_server.py / config.jsonc
    const ID_UPDATER_URL = "http://127.0.0.1:5103/update"; // One-shot listener run by id_updater.py
    // Arena.ai (post-rebrand) serves the streaming API under /nextjs-api/...
    // The legacy /api/... prefix is tried as a fallback for older deployments.
    const STREAM_PATH_BUILDERS = [
        (sessionId, messageId) => `/nextjs-api/stream/retry-evaluation-session-message/${sessionId}/messages/${messageId}`,
        (sessionId, messageId) => `/api/stream/retry-evaluation-session-message/${sessionId}/messages/${messageId}`,
    ];
    let socket;
    let reconnectTimer = null;
    let isCaptureModeActive = false; // Toggle for ID-capture mode

    const TITLE_OK_PREFIX = "✅ ";
    const TITLE_CAPTURE_PREFIX = "🎯 ";

    function addTitlePrefix(prefix) {
        if (!document.title.startsWith(prefix)) {
            document.title = prefix + document.title;
        }
    }

    function removeTitlePrefix(prefix) {
        if (document.title.startsWith(prefix)) {
            document.title = document.title.slice(prefix.length);
        }
    }

    // --- Core logic ---
    function connect() {
        // Guard against overlapping reconnect attempts.
        if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
            return;
        }

        console.log(`[API Bridge] Connecting to the local server: ${SERVER_URL}...`);
        socket = new WebSocket(SERVER_URL);

        socket.onopen = () => {
            console.log("[API Bridge] ✅ WebSocket connection to the local server established.");
            addTitlePrefix(TITLE_OK_PREFIX);
        };

        socket.onmessage = async (event) => {
            try {
                const message = JSON.parse(event.data);

                // Check whether this is a command rather than a standard chat request.
                if (message.command) {
                    console.log(`[API Bridge] ⬇️ Command received: ${message.command}`);
                    if (message.command === 'refresh' || message.command === 'reconnect') {
                        console.log(`[API Bridge] Received the '${message.command}' command; reloading the page...`);
                        location.reload();
                    } else if (message.command === 'activate_id_capture') {
                        console.log("[API Bridge] ✅ ID-capture mode activated. Please trigger a 'Retry' action on the page.");
                        isCaptureModeActive = true;
                        // Give the user a visual hint.
                        addTitlePrefix(TITLE_CAPTURE_PREFIX);
                    } else if (message.command === 'send_page_source') {
                        console.log("[API Bridge] Received the send-page-source command; sending...");
                        sendPageSource();
                    }
                    return;
                }

                const { request_id, payload } = message;

                if (!request_id || !payload) {
                    console.error("[API Bridge] Received an invalid message from the server:", message);
                    return;
                }

                console.log(`[API Bridge] ⬇️ Chat request ${request_id.substring(0, 8)} received. Preparing to fetch.`);
                await executeFetchAndStreamBack(request_id, payload);

            } catch (error) {
                console.error("[API Bridge] Error while processing a server message:", error);
            }
        };

        socket.onclose = () => {
            console.warn("[API Bridge] 🔌 Connection to the local server closed. Retrying in 5 seconds...");
            removeTitlePrefix(TITLE_OK_PREFIX);
            if (reconnectTimer === null) {
                reconnectTimer = setTimeout(() => {
                    reconnectTimer = null;
                    connect();
                }, 5000);
            }
        };

        socket.onerror = (error) => {
            console.error("[API Bridge] ❌ WebSocket error:", error);
            socket.close(); // Triggers the reconnect logic in onclose.
        };
    }

    async function executeFetchAndStreamBack(requestId, payload) {
        console.log(`[API Bridge] Current origin: ${window.location.hostname}`);
        const { message_templates, target_model_id, session_id, message_id } = payload;

        // --- Use the session information passed down from the backend config ---
        if (!session_id || !message_id) {
            const errorMsg = "The session information received from the backend (session_id or message_id) is empty. Please run `id_updater.py` first to set it up.";
            console.error(`[API Bridge] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, "[DONE]");
            return;
        }

        // The URL is the same for both chat and text-to-image requests.
        // Try the current /nextjs-api/ path first, then the legacy /api/ one.
        const httpMethod = 'PUT';

        console.log("[API Bridge] Preparing request for session:", session_id);

        const newMessages = [];
        let lastMsgIdInChain = null;

        if (!message_templates || message_templates.length === 0) {
            const errorMsg = "The message list received from the backend is empty.";
            console.error(`[API Bridge] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, "[DONE]");
            return;
        }

        // This loop is shared by chat and text-to-image flows because the
        // backend has already prepared the correct message_templates.
        for (let i = 0; i < message_templates.length; i++) {
            const template = message_templates[i];
            const currentMsgId = crypto.randomUUID();
            const parentIds = lastMsgIdInChain ? [lastMsgIdInChain] : [];

            // Only the last message in the chain is 'pending'.
            const status = (i === message_templates.length - 1) ? 'pending' : 'success';

            newMessages.push({
                role: template.role,
                content: template.content,
                id: currentMsgId,
                evaluationId: null,
                evaluationSessionId: session_id,
                parentMessageIds: parentIds,
                experimental_attachments: template.attachments || [],
                failureReason: null,
                metadata: null,
                participantPosition: template.participantPosition || "a",
                createdAt: new Date().toISOString(),
                updatedAt: new Date().toISOString(),
                status: status,
            });
            lastMsgIdInChain = currentMsgId;
        }

        const body = {
            messages: newMessages,
            modelId: target_model_id,
        };

        console.log("[API Bridge] Final payload to send to the LMArena API:", JSON.stringify(body, null, 2));

        try {
            // Use the original (unwrapped) fetch so the bridge's own requests
            // never pass through the capture interceptor below.
            let response = null;
            let apiUrl = null;
            for (let p = 0; p < STREAM_PATH_BUILDERS.length; p++) {
                apiUrl = STREAM_PATH_BUILDERS[p](session_id, message_id);
                console.log(`[API Bridge] Using API endpoint: ${apiUrl}`);
                response = await originalFetch(apiUrl, {
                    method: httpMethod,
                    headers: {
                        'Content-Type': 'text/plain;charset=UTF-8', // Arena uses text/plain
                        'Accept': '*/*',
                    },
                    body: JSON.stringify(body),
                    credentials: 'include' // Cookies are required.
                });
                // A 404 means this path no longer exists; try the next candidate.
                if (response.status !== 404 || p === STREAM_PATH_BUILDERS.length - 1) {
                    break;
                }
                console.warn(`[API Bridge] Endpoint ${apiUrl} returned 404; trying the fallback path...`);
            }

            if (!response.ok || !response.body) {
                const errorBody = await response.text();
                if (/recaptcha/i.test(errorBody)) {
                    throw new Error(
                        "Arena.ai rejected the request because a reCAPTCHA token is required for this retry call. " +
                        "Interact with the Arena page in your browser (complete any visible captcha), then retry. " +
                        `Raw response: ${errorBody.slice(0, 200)}`
                    );
                }
                throw new Error(`Abnormal network response. Status: ${response.status}. Body: ${errorBody}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();

            while (true) {
                const { value, done } = await reader.read();
                if (done) {
                    // Flush any bytes buffered by the streaming decoder.
                    const tail = decoder.decode();
                    if (tail) {
                        sendToServer(requestId, tail);
                    }
                    console.log(`[API Bridge] ✅ Stream for request ${requestId.substring(0, 8)} ended.`);
                    sendToServer(requestId, "[DONE]");
                    break;
                }
                // { stream: true } keeps multi-byte UTF-8 characters that span
                // chunk boundaries intact instead of replacing them with U+FFFD.
                const chunk = decoder.decode(value, { stream: true });
                // Forward the raw chunk straight back to the backend.
                if (chunk) {
                    sendToServer(requestId, chunk);
                }
            }

        } catch (error) {
            console.error(`[API Bridge] ❌ Error while fetching for request ${requestId.substring(0, 8)}:`, error);
            sendToServer(requestId, { error: error.message });
            sendToServer(requestId, "[DONE]");
        }
    }

    function sendToServer(requestId, data) {
        if (socket && socket.readyState === WebSocket.OPEN) {
            const message = {
                request_id: requestId,
                data: data
            };
            socket.send(JSON.stringify(message));
        } else {
            console.error("[API Bridge] Cannot send data; the WebSocket connection is not open.");
        }
    }

    // --- Network request interception ---
    const originalFetch = window.fetch;
    window.fetch = function (...args) {
        const urlArg = args[0];
        let urlString = '';

        // Make sure we always work with a string URL.
        if (urlArg instanceof Request) {
            urlString = urlArg.url;
        } else if (urlArg instanceof URL) {
            urlString = urlArg.href;
        } else if (typeof urlArg === 'string') {
            urlString = urlArg;
        }

        // Only attempt matching when the URL is a valid string.
        // Matches both the current /nextjs-api/stream/... and the legacy /api/stream/... paths.
        if (urlString) {
            const match = urlString.match(/\/(?:nextjs-)?api\/stream\/retry-evaluation-session-message\/([a-f0-9-]+)\/messages\/([a-f0-9-]+)/);

            // Only capture IDs for requests that were NOT issued by the bridge
            // itself (the bridge uses originalFetch and never reaches this
            // wrapper), and only while capture mode is active.
            if (match && isCaptureModeActive) {
                const sessionId = match[1];
                const messageId = match[2];
                console.log("[API Bridge Interceptor] 🎯 IDs captured while capture mode was active! Sending...");

                // Turn capture mode off to guarantee a single submission.
                isCaptureModeActive = false;
                removeTitlePrefix(TITLE_CAPTURE_PREFIX);

                // Asynchronously send the captured IDs to the local id_updater.py listener.
                originalFetch(ID_UPDATER_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ sessionId, messageId })
                })
                    .then(response => {
                        if (!response.ok) throw new Error(`Server responded with status: ${response.status}`);
                        console.log("[API Bridge] ✅ ID update sent successfully. Capture mode has been disabled automatically.");
                    })
                    .catch(err => {
                        console.error('[API Bridge] Error while sending the ID update:', err.message);
                        // Even if sending fails, capture mode stays off; no retry.
                    });
            }
        }

        // Call the original fetch so normal page behaviour is unaffected.
        return originalFetch.apply(this, args);
    };

    // --- Page source sender ---
    async function sendPageSource() {
        try {
            const htmlContent = document.documentElement.outerHTML;
            await originalFetch('http://localhost:5102/internal/update_available_models', {
                method: 'POST',
                headers: {
                    'Content-Type': 'text/html; charset=utf-8'
                },
                body: htmlContent
            });
            console.log("[API Bridge] Page source sent successfully.");
        } catch (e) {
            console.error("[API Bridge] Failed to send the page source:", e);
        }
    }

    // --- Start the connection ---
    console.log("========================================");
    console.log("  LMArena API Bridge v2.8.0 is running.");
    console.log("  - Works on arena.ai (and legacy lmarena.ai)");
    console.log("  - Chat features connect to ws://localhost:5102");
    console.log("  - The ID capturer posts to http://localhost:5103");
    console.log("========================================");

    connect(); // Establish the WebSocket connection.

})();

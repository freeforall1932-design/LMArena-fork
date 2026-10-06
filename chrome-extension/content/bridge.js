// content/bridge.js
// Content script for the LMArena API Bridge extension (ISOLATED world).
//
// This is a port of TampermonkeyScript/LMArenaApiBridge.js with the same
// WebSocket protocol, so the local Python server is unchanged. Differences:
//   - No window.fetch monkey-patching: ID capture is done by the service
//     worker via chrome.webRequest (see background.js).
//   - Ports are configurable via the extension popup (chrome.storage.local).
//   - Connection status is reported to the worker for the toolbar badge.
//   - Content-script fetch runs with the page origin, so session cookies
//     (including SameSite=Lax) are attached automatically, and the page
//     cannot tamper with our fetch (isolated world).

(function () {
    'use strict';

    if (globalThis.__lmabBridgeLoaded) return; // one bridge per document
    globalThis.__lmabBridgeLoaded = true;

    let socket = null;
    let reconnectTimer = null;
    let serverPort = LMAB.DEFAULT_SERVER_PORT;

    // --- Title indicators (parity with the userscript UX) --------------------

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

    // --- Status reporting (badge in the toolbar) ------------------------------

    function reportStatus(state, detail) {
        try {
            chrome.runtime.sendMessage({ type: 'status', state, detail: detail || '' }).catch(() => {});
        } catch (e) {
            // Extension context invalidated (e.g. reloaded unpacked) - ignore.
        }
    }

    function setInFlight(delta) {
        try {
            chrome.runtime.sendMessage({ type: 'bridge_inflight', delta }).catch(() => {});
        } catch (e) { /* ignore */ }
    }

    // --- Core WebSocket logic --------------------------------------------------

    function connect() {
        if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
            return;
        }
        const url = `ws://localhost:${serverPort}/ws`;
        console.log(`[API Bridge] Connecting to the local server: ${url}...`);
        socket = new WebSocket(url);

        socket.onopen = () => {
            console.log('[API Bridge] ✅ WebSocket connection to the local server established.');
            addTitlePrefix(LMAB.TITLE_OK_PREFIX);
            reportStatus('connected');
        };

        socket.onmessage = async (event) => {
            try {
                const message = JSON.parse(event.data);

                // Commands from the server.
                if (message.command) {
                    console.log(`[API Bridge] ⬇️ Command received: ${message.command}`);
                    if (message.command === 'refresh' || message.command === 'reconnect') {
                        console.log(`[API Bridge] Received the '${message.command}' command; reloading the page...`);
                        location.reload();
                    } else if (message.command === 'activate_id_capture') {
                        console.log("[API Bridge] ✅ ID-capture mode activated. Click 'Retry' on an assistant message in this site.");
                        chrome.runtime.sendMessage({ type: 'arm_capture' }).catch(() => {});
                        addTitlePrefix(LMAB.TITLE_CAPTURE_PREFIX);
                    } else if (message.command === 'send_page_source') {
                        console.log('[API Bridge] Received the send-page-source command; sending...');
                        sendPageSource();
                    }
                    return;
                }

                // Chat/image request payloads.
                const { request_id, payload } = message;
                if (!request_id || !payload) {
                    console.error('[API Bridge] Received an invalid message from the server:', message);
                    return;
                }
                console.log(`[API Bridge] ⬇️ Chat request ${request_id.substring(0, 8)} received. Preparing to fetch.`);
                await executeFetchAndStreamBack(request_id, payload);
            } catch (error) {
                console.error('[API Bridge] Error while processing a server message:', error);
            }
        };

        socket.onclose = () => {
            console.warn('[API Bridge] 🔌 Connection to the local server closed. Retrying in 5 seconds...');
            removeTitlePrefix(LMAB.TITLE_OK_PREFIX);
            reportStatus('disconnected');
            if (reconnectTimer === null) {
                reconnectTimer = setTimeout(() => {
                    reconnectTimer = null;
                    connect();
                }, 5000);
            }
        };

        socket.onerror = (error) => {
            console.error('[API Bridge] ❌ WebSocket error:', error);
            reportStatus('error', 'WebSocket error; is api_server.py running?');
            socket.close(); // triggers the reconnect logic in onclose
        };
    }

    // --- Request execution ------------------------------------------------------

    async function executeFetchAndStreamBack(requestId, payload) {
        const { message_templates, target_model_id, session_id, message_id } = payload;

        if (!session_id || !message_id) {
            const errorMsg = 'The session information received from the backend (session_id or message_id) is empty. Please run `id_updater.py` first to set it up.';
            console.error(`[API Bridge] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, '[DONE]');
            return;
        }

        const httpMethod = 'PUT';
        const body = buildRequestBody(message_templates, target_model_id, session_id);
        if (body === null) {
            const errorMsg = 'The message list received from the backend is empty.';
            console.error(`[API Bridge] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, '[DONE]');
            return;
        }

        console.log('[API Bridge] Final payload to send to the Arena API:', JSON.stringify(body, null, 2));

        // Tell the worker a bridge request is starting, so a concurrent ID
        // capture ignores our own retry call.
        setInFlight(+1);
        try {
            // Try the current /nextjs-api/ path first, then the legacy /api/ one.
            let response = null;
            let apiUrl = null;
            for (let p = 0; p < LMAB.STREAM_PATH_BUILDERS.length; p++) {
                apiUrl = LMAB.STREAM_PATH_BUILDERS[p](session_id, message_id);
                console.log(`[API Bridge] Using API endpoint: ${apiUrl}`);
                response = await fetch(apiUrl, {
                    method: httpMethod,
                    headers: {
                        'Content-Type': 'text/plain;charset=UTF-8', // Arena uses text/plain
                        'Accept': '*/*',
                    },
                    body: JSON.stringify(body),
                    credentials: 'include', // Session cookies are required.
                });
                if (response.status !== 404 || p === LMAB.STREAM_PATH_BUILDERS.length - 1) break;
                console.warn(`[API Bridge] Endpoint ${apiUrl} returned 404; trying the fallback path...`);
            }

            if (!response.ok || !response.body) {
                const errorBody = await response.text();
                if (/recaptcha/i.test(errorBody)) {
                    throw new Error(
                        'Arena.ai rejected the request because a reCAPTCHA token is required for this retry call. ' +
                        'Interact with the Arena page in your browser (complete any visible captcha), then retry. ' +
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
                    if (tail) sendToServer(requestId, tail);
                    console.log(`[API Bridge] ✅ Stream for request ${requestId.substring(0, 8)} ended.`);
                    sendToServer(requestId, '[DONE]');
                    break;
                }
                // { stream: true } keeps multi-byte UTF-8 characters that span
                // chunk boundaries intact instead of replacing them with U+FFFD.
                const chunk = decoder.decode(value, { stream: true });
                if (chunk) sendToServer(requestId, chunk);
            }
        } catch (error) {
            console.error(`[API Bridge] ❌ Error while fetching for request ${requestId.substring(0, 8)}:`, error);
            sendToServer(requestId, { error: error.message });
            sendToServer(requestId, '[DONE]');
        } finally {
            setInFlight(-1);
        }
    }

    function buildRequestBody(messageTemplates, targetModelId, sessionId) {
        if (!messageTemplates || messageTemplates.length === 0) return null;

        const newMessages = [];
        let lastMsgIdInChain = null;

        for (let i = 0; i < messageTemplates.length; i++) {
            const template = messageTemplates[i];
            const currentMsgId = crypto.randomUUID();
            const parentIds = lastMsgIdInChain ? [lastMsgIdInChain] : [];
            // Only the last message in the chain is 'pending'.
            const status = (i === messageTemplates.length - 1) ? 'pending' : 'success';

            newMessages.push({
                role: template.role,
                content: template.content,
                id: currentMsgId,
                evaluationId: null,
                evaluationSessionId: sessionId,
                parentMessageIds: parentIds,
                experimental_attachments: template.attachments || [],
                failureReason: null,
                metadata: null,
                participantPosition: template.participantPosition || 'a',
                createdAt: new Date().toISOString(),
                updatedAt: new Date().toISOString(),
                status: status,
            });
            lastMsgIdInChain = currentMsgId;
        }

        return { messages: newMessages, modelId: targetModelId };
    }

    function sendToServer(requestId, data) {
        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ request_id: requestId, data }));
        } else {
            console.error('[API Bridge] Cannot send data; the WebSocket connection is not open.');
        }
    }

    // --- Legacy page-source sender (browser fallback for model_updater.py) -----

    async function sendPageSource() {
        try {
            const htmlContent = document.documentElement.outerHTML;
            await fetch(`http://localhost:${serverPort}/internal/update_available_models`, {
                method: 'POST',
                headers: { 'Content-Type': 'text/html; charset=utf-8' },
                body: htmlContent,
            });
            console.log('[API Bridge] Page source sent successfully.');
        } catch (e) {
            console.error('[API Bridge] Failed to send the page source:', e);
        }
    }

    // --- Capture-result feedback from the worker --------------------------------

    chrome.runtime.onMessage.addListener((msg) => {
        if (msg && msg.type === 'capture_result') {
            removeTitlePrefix(LMAB.TITLE_CAPTURE_PREFIX);
            if (msg.ok) {
                console.log(`[API Bridge] ✅ ID capture delivered to id_updater.py (session ...${String(msg.sessionId).slice(-6)}).`);
            } else {
                console.error(`[API Bridge] ❌ ID capture failed: ${msg.error}. Is id_updater.py running?`);
            }
        }
    });

    // --- Configuration + startup --------------------------------------------------

    chrome.storage.local.get(['serverPort'], (stored) => {
        serverPort = Number(stored.serverPort) || LMAB.DEFAULT_SERVER_PORT;

        console.log('========================================');
        console.log(`  LMArena API Bridge extension v${LMAB.VERSION} is running.`);
        console.log(`  - Chat features connect to ws://localhost:${serverPort}`);
        console.log('  - ID capture is handled by the extension worker (webRequest).');
        console.log('========================================');

        connect();
    });

    // Reconnect immediately when the port is changed in the popup.
    chrome.storage.onChanged.addListener((changes, area) => {
        if (area === 'local' && changes.serverPort) {
            const newPort = Number(changes.serverPort.newValue) || LMAB.DEFAULT_SERVER_PORT;
            if (newPort !== serverPort) {
                console.log(`[API Bridge] Server port changed to ${newPort}; reconnecting...`);
                serverPort = newPort;
                if (socket) {
                    try { socket.onclose = null; socket.close(); } catch (e) { /* ignore */ }
                    socket = null;
                }
                if (reconnectTimer !== null) { clearTimeout(reconnectTimer); reconnectTimer = null; }
                connect();
            }
        }
    });
})();

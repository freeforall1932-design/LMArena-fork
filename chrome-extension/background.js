// background.js
// MV3 service worker for the LMArena API Bridge extension.
//
// Responsibilities:
//   - Capture session/message IDs by *observing* the page's own retry requests
//     via chrome.webRequest (no window.fetch monkey-patching needed).
//   - Deliver captured IDs to the local id_updater.py listener.
//   - Maintain the toolbar badge (ON / CAP / ERR) and shared status for the popup.
//   - Track how many bridge requests are in flight so the bridge's own calls
//     are never mistaken for a user-initiated Retry during capture mode.
//
// The data path (WebSocket to the local server + streaming fetches) lives in
// content/bridge.js, because content-script fetches run with the page origin
// and therefore carry the session cookies (including SameSite=Lax ones).

importScripts('common/constants.js');

const BADGE = {
    idle: { text: '–', color: '#6b7280' },
    on: { text: 'ON', color: '#16a34a' },
    cap: { text: 'CAP', color: '#d97706' },
    err: { text: 'ERR', color: '#dc2626' },
};

// In-memory counter of bridge-issued requests (reset on SW restart, which is
// harmless: a restart mid-request would already have broken that request's WS
// path, and capture mode is a deliberate, short-lived user action).
let bridgeInFlight = 0;

// --- Storage helpers -------------------------------------------------------

async function getSessionState() {
    return chrome.storage.session.get(['captureArmed', 'bridgeStatus', 'bridgeTabId', 'bridgeDetail']);
}

async function getPorts() {
    const stored = await chrome.storage.local.get(['serverPort', 'idUpdaterPort']);
    return {
        serverPort: Number(stored.serverPort) || LMAB.DEFAULT_SERVER_PORT,
        idUpdaterPort: Number(stored.idUpdaterPort) || LMAB.DEFAULT_ID_UPDATER_PORT,
    };
}

async function applyBadge() {
    const { captureArmed, bridgeStatus } = await getSessionState();
    let badge = BADGE.idle;
    if (captureArmed) badge = BADGE.cap;
    else if (bridgeStatus === 'connected') badge = BADGE.on;
    else if (bridgeStatus === 'error') badge = BADGE.err;
    try {
        await chrome.action.setBadgeText({ text: badge.text });
        await chrome.action.setBadgeBackgroundColor({ color: badge.color });
    } catch (e) {
        console.warn('[API Bridge BG] badge update failed:', e);
    }
}

// --- ID capture via webRequest ---------------------------------------------

chrome.webRequest.onBeforeRequest.addListener(
    (details) => { void handlePossibleCapture(details); },
    { urls: LMAB.ARENA_URL_PATTERNS, types: ['xmlhttprequest'] }
);

async function handlePossibleCapture(details) {
    const { captureArmed } = await getSessionState();
    if (!captureArmed) return;          // capture mode not active
    if (bridgeInFlight > 0) return;     // ignore the bridge's own retry calls

    const match = LMAB.CAPTURE_URL_RE.exec(details.url || '');
    if (!match) return;

    const [, sessionId, messageId] = match;
    // One-shot: disarm immediately so exactly one capture is delivered.
    await chrome.storage.session.set({ captureArmed: false });
    await applyBadge();
    await deliverCapturedIds(sessionId, messageId);
}

async function deliverCapturedIds(sessionId, messageId) {
    const { idUpdaterPort } = await getPorts();
    const url = `http://127.0.0.1:${idUpdaterPort}/update`;
    let ok = false;
    let error = null;
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sessionId, messageId }),
        });
        ok = response.ok;
        if (!ok) error = `id_updater responded with status ${response.status}`;
    } catch (e) {
        error = String(e && e.message ? e.message : e);
    }

    console.log(ok
        ? '[API Bridge BG] ✅ Captured IDs delivered to id_updater.py.'
        : `[API Bridge BG] ❌ Failed to deliver captured IDs: ${error}`);

    // Let every Arena tab's content script log the outcome and clean up its
    // title indicator.
    notifyArenaTabs({ type: 'capture_result', ok, sessionId, messageId, error });
}

async function notifyArenaTabs(message) {
    try {
        const tabs = await chrome.tabs.query({ url: LMAB.ARENA_URL_PATTERNS });
        for (const tab of tabs) {
            if (tab.id != null) {
                chrome.tabs.sendMessage(tab.id, message).catch(() => { /* tab may not have a listener */ });
            }
        }
    } catch (e) {
        console.warn('[API Bridge BG] tab notify failed:', e);
    }
}

// --- Messaging hub ----------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    (async () => {
        try {
            switch (msg && msg.type) {
                case 'status': {
                    await chrome.storage.session.set({
                        bridgeStatus: msg.state,               // 'connected' | 'disconnected' | 'error'
                        bridgeTabId: sender.tab ? sender.tab.id : null,
                        bridgeTabUrl: sender.tab ? sender.tab.url : null,
                        bridgeDetail: msg.detail || '',
                    });
                    await applyBadge();
                    sendResponse({ ok: true });
                    break;
                }
                case 'arm_capture': {
                    await chrome.storage.session.set({ captureArmed: true });
                    await applyBadge();
                    sendResponse({ ok: true });
                    break;
                }
                case 'bridge_inflight': {
                    bridgeInFlight = Math.max(0, bridgeInFlight + (msg.delta || 0));
                    sendResponse({ ok: true, bridgeInFlight });
                    break;
                }
                case 'get_status': {
                    const session = await getSessionState();
                    const ports = await getPorts();
                    sendResponse({
                        version: LMAB.VERSION,
                        bridgeStatus: session.bridgeStatus || 'disconnected',
                        bridgeTabId: session.bridgeTabId ?? null,
                        bridgeTabUrl: session.bridgeTabUrl || null,
                        bridgeDetail: session.bridgeDetail || '',
                        captureArmed: !!session.captureArmed,
                        bridgeInFlight,
                        serverPort: ports.serverPort,
                        idUpdaterPort: ports.idUpdaterPort,
                    });
                    break;
                }
                default:
                    sendResponse({ ok: false, error: 'unknown message type' });
            }
        } catch (e) {
            console.error('[API Bridge BG] message handling error:', e);
            sendResponse({ ok: false, error: String(e) });
        }
    })();
    return true; // respond asynchronously
});

// --- Lifecycle ---------------------------------------------------------------

chrome.runtime.onInstalled.addListener(async () => {
    const stored = await chrome.storage.local.get(['serverPort', 'idUpdaterPort']);
    const defaults = {};
    if (!stored.serverPort) defaults.serverPort = LMAB.DEFAULT_SERVER_PORT;
    if (!stored.idUpdaterPort) defaults.idUpdaterPort = LMAB.DEFAULT_ID_UPDATER_PORT;
    if (Object.keys(defaults).length) await chrome.storage.local.set(defaults);
    await applyBadge();
    console.log(`[API Bridge BG] installed (v${LMAB.VERSION}).`);
});

chrome.runtime.onStartup.addListener(async () => {
    await applyBadge();
});

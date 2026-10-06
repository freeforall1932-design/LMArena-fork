// popup/popup.js
// Toolbar popup: shows bridge status and lets the user configure local ports.

const $ = (id) => document.getElementById(id);

const STATUS_LABELS = {
    connected: 'Connected to local server',
    disconnected: 'Not connected (is api_server.py running? Open an arena.ai tab)',
    error: 'Connection error — check api_server.py',
};

async function refreshStatus() {
    try {
        const st = await chrome.runtime.sendMessage({ type: 'get_status' });
        if (!st) return;

        const dot = $('statusDot');
        dot.className = 'dot';
        if (st.captureArmed) {
            dot.classList.add('capture');
            $('statusText').textContent = 'ID-capture armed 🎯 — click Retry on an assistant message';
        } else {
            dot.classList.add(st.bridgeStatus === 'connected' ? 'connected' : (st.bridgeStatus === 'error' ? 'error' : ''));
            $('statusText').textContent = STATUS_LABELS[st.bridgeStatus] || st.bridgeStatus;
        }

        const details = [];
        if (st.bridgeTabUrl) details.push(`Active tab: ${new URL(st.bridgeTabUrl).host}`);
        if (st.bridgeDetail) details.push(st.bridgeDetail);
        details.push(`Server :${st.serverPort} · ID updater :${st.idUpdaterPort}`);
        if (st.bridgeInFlight > 0) details.push(`${st.bridgeInFlight} request(s) in flight…`);
        $('statusDetail').textContent = details.join(' · ');

        $('hintPort').textContent = st.serverPort;
        $('version').textContent = `v${st.version}`;

        // Fill inputs only when the user is not editing them.
        if (document.activeElement !== $('serverPort')) $('serverPort').value = st.serverPort;
        if (document.activeElement !== $('idUpdaterPort')) $('idUpdaterPort').value = st.idUpdaterPort;
    } catch (e) {
        $('statusText').textContent = 'Status unavailable';
        $('statusDetail').textContent = String(e && e.message ? e.message : e);
    }
}

$('saveBtn').addEventListener('click', async () => {
    const serverPort = Number($('serverPort').value) || 5102;
    const idUpdaterPort = Number($('idUpdaterPort').value) || 5103;
    await chrome.storage.local.set({ serverPort, idUpdaterPort });
    $('saved').textContent = 'Saved — content scripts will reconnect.';
    setTimeout(() => { $('saved').textContent = ''; }, 2500);
    refreshStatus();
});

refreshStatus();
setInterval(refreshStatus, 1000); // keep the popup live while open

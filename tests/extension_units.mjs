// tests/extension_units.mjs
// Offline unit tests for the Chrome extension's shared logic + consistency
// checks across the repo's version numbers. Run: node tests/extension_units.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
let passed = 0;
let failed = 0;

function check(name, fn) {
    try {
        fn();
        passed++;
        console.log(`[PASS] ${name}`);
    } catch (e) {
        failed++;
        console.log(`[FAIL] ${name}: ${e.message}`);
    }
}

// --- Load shared constants (plain script that sets globalThis.LMAB) ---
await import(path.join(ROOT, 'chrome-extension', 'common', 'constants.js'));
const LMAB = globalThis.LMAB;

check('constants: LMAB attached to globalThis', () => assert.ok(LMAB && LMAB.CAPTURE_URL_RE));

// --- Capture URL regex: both path prefixes, correct groups ---
const newUrl = 'https://arena.ai/nextjs-api/stream/retry-evaluation-session-message/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/messages/11111111-2222-3333-4444-555555555555?x=1';
const oldUrl = 'https://lmarena.ai/api/stream/retry-evaluation-session-message/0f0f0f0f-1e1e-2d2d-3c3c-4b4b4b4b4b4b/messages/9a9a9a9a-8b8b-7c7c-6d6d-5e5e5e5e5e5e';

check('regex: matches current /nextjs-api/ path with correct groups', () => {
    const m = LMAB.CAPTURE_URL_RE.exec(newUrl);
    assert.ok(m);
    assert.equal(m[1], 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee');
    assert.equal(m[2], '11111111-2222-3333-4444-555555555555');
});

check('regex: matches legacy /api/ path', () => {
    const m = LMAB.CAPTURE_URL_RE.exec(oldUrl);
    assert.ok(m);
    assert.equal(m[1], '0f0f0f0f-1e1e-2d2d-3c3c-4b4b4b4b4b4b');
});

check('regex: does not match unrelated endpoints', () => {
    assert.equal(LMAB.CAPTURE_URL_RE.test('https://arena.ai/nextjs-api/model-catalog'), false);
    assert.equal(LMAB.CAPTURE_URL_RE.test('https://arena.ai/api/stream/post-to-evaluation/abc'), false);
    // Note: the regex is deliberately path-only (host-agnostic). Host scoping is
    // enforced by the webRequest URL filter (LMAB.ARENA_URL_PATTERNS) and the
    // manifest host_permissions, so only Arena-origin requests are observed.
});

// --- Endpoint path builders ---
check('paths: primary is /nextjs-api/, fallback is /api/', () => {
    assert.equal(LMAB.STREAM_PATH_BUILDERS[0]('S1', 'M1'),
        '/nextjs-api/stream/retry-evaluation-session-message/S1/messages/M1');
    assert.equal(LMAB.STREAM_PATH_BUILDERS[1]('S1', 'M1'),
        '/api/stream/retry-evaluation-session-message/S1/messages/M1');
    assert.equal(LMAB.STREAM_PATH_BUILDERS.length, 2);
});

// --- WebSocket URL builder (IPv6/localhost hardening) ---
check('wsUrl: alternates 127.0.0.1/localhost across reconnect attempts', () => {
    assert.equal(LMAB.wsUrl(5102, 0), 'ws://127.0.0.1:5102/ws');
    assert.equal(LMAB.wsUrl(5102, 1), 'ws://localhost:5102/ws');
    assert.equal(LMAB.wsUrl(5102, 2), 'ws://127.0.0.1:5102/ws');
    assert.equal(LMAB.wsUrl(6000, -1), 'ws://localhost:6000/ws'); // negative-safe
});

// --- Manifest validity ---
const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, 'chrome-extension', 'manifest.json'), 'utf-8'));

check('manifest: MV3 with service worker + content scripts', () => {
    assert.equal(manifest.manifest_version, 3);
    assert.ok(manifest.background.service_worker);
    assert.ok(manifest.content_scripts[0].matches.length >= 4);
    assert.ok(manifest.content_scripts[0].js.includes('common/constants.js'));
});

check('manifest: required permissions present', () => {
    for (const p of ['webRequest', 'storage', 'tabs']) assert.ok(manifest.permissions.includes(p), p);
    for (const h of ['https://arena.ai/*', 'http://127.0.0.1/*']) assert.ok(manifest.host_permissions.includes(h), h);
});

check('manifest: all referenced files exist', () => {
    const files = [
        manifest.background.service_worker,
        ...manifest.content_scripts[0].js,
        manifest.action.default_popup,
    ];
    for (const f of files) {
        assert.ok(fs.existsSync(path.join(ROOT, 'chrome-extension', f)), f);
    }
});

// --- Version consistency across the whole project ---
check('versions: manifest == constants == config.jsonc == userscript', () => {
    const cfg = fs.readFileSync(path.join(ROOT, 'config.jsonc'), 'utf-8');
    const cfgVersion = cfg.match(/"version"\s*:\s*"([^"]+)"/)[1];
    const userscript = fs.readFileSync(path.join(ROOT, 'TampermonkeyScript', 'LMArenaApiBridge.js'), 'utf-8');
    const tmVersion = userscript.match(/@version\s+([\d.]+)/)[1];
    assert.equal(manifest.version, LMAB.VERSION, 'manifest vs constants');
    assert.equal(manifest.version, cfgVersion, 'manifest vs config.jsonc');
    assert.equal(manifest.version, tmVersion, 'manifest vs userscript @version');
});

// --- Userscript and extension share the same capture semantics ---
check('userscript: interceptor regex is identical to the extension capture regex', () => {
    const userscript = fs.readFileSync(path.join(ROOT, 'TampermonkeyScript', 'LMArenaApiBridge.js'), 'utf-8');
    const tmRegex = userscript.match(/urlString\.match\((\/.+?\/)\)/)[1];
    // Identical source => identical matching behaviour (already covered above).
    assert.equal(tmRegex, String(LMAB.CAPTURE_URL_RE));
});

console.log(`\n==== ${passed}/${passed + failed} extension unit checks passed ====`);
process.exit(failed ? 1 : 0);

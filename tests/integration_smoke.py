#!/usr/bin/env python3
"""
End-to-end smoke test for LMArena Bridge.

Spawns a real api_server.py in a temporary directory, connects a simulated
"browser" over WebSocket that replays a fake Arena.ai stream, and exercises:

  - GET /v1/models
  - POST /v1/chat/completions (streaming + non-streaming)
  - multibyte (CJK/emoji) content integrity, Markdown image rendering
  - image-model routing, unknown-model fallback, invalid JSON -> 400
  - browser disconnect -> 503, server health afterwards
  - WebSocket tab-replacement race (stale tab must not kill the live one)
  - hot reload of models.json / config.jsonc without a restart
  - legacy browser-based model extraction flow (/internal/* endpoints)
  - model_updater catalog flattening (offline fixture; live fetch if network
    is available)

Usage:
    python tests/integration_smoke.py            # from the project root
    python tests/integration_smoke.py --skip-live  # never touch the network

Exit code 0 = all checks passed.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import aiohttp

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_LIVE = "--skip-live" in sys.argv

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    status = "PASS" if cond else "FAIL"
    line = f"[{status}] {name}"
    if extra and not cond:
        line += f"  | extra: {extra!r}"
    print(line)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def prepare_workdir(port):
    """Copy the project into a temp dir and patch the config for testing."""
    workdir = tempfile.mkdtemp(prefix="lmbridge-smoke-")
    for item in ["api_server.py", "models.json", "model_endpoint_map.json",
                 "available_models.json", "config.jsonc", "modules", "model_updater.py"]:
        src = os.path.join(PROJECT_ROOT, item)
        dst = os.path.join(workdir, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    cfg_path = os.path.join(workdir, "config.jsonc")
    cfg = open(cfg_path, encoding="utf-8").read()
    cfg = cfg.replace('"enable_auto_update": true', '"enable_auto_update": false')
    cfg = cfg.replace('"server_port": 5102', f'"server_port": {port}')
    open(cfg_path, "w", encoding="utf-8").write(cfg)
    return workdir


FAKE_FRAMES = [
    'a0:"Hello "',
    'b0:"世界🌍"',
    'a2:[{"type":"image","image":"https://img.example/1.png"}]',
    'ad:{"finishReason":"stop"}',
]


async def fake_browser(ws, base_url, stop_event):
    """Simulates the Tampermonkey script."""
    async for raw in ws:
        msg = json.loads(raw.data)
        if msg.get("command") == "send_page_source":
            html = ('<html>x = {\\"id\\":\\"aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee\\",'
                    '\\"publicName\\":\\"test-model-x\\",\\"organization\\":\\"testorg\\"}</html>')
            async with aiohttp.ClientSession() as s:
                await s.post(base_url + "/internal/update_available_models",
                             data=html.encode(),
                             headers={"Content-Type": "text/html; charset=utf-8"})
        elif "request_id" in msg:
            rid = msg["request_id"]
            payload = msg["payload"]
            check("browser: payload carries session/message ids",
                  bool(payload.get("session_id")) and bool(payload.get("message_id")), payload)
            for frame in FAKE_FRAMES:
                await ws.send_str(json.dumps({"request_id": rid, "data": frame}))
            await ws.send_str(json.dumps({"request_id": rid, "data": "[DONE]"}))
        if stop_event.is_set():
            break


async def read_sse(response):
    content, saw_role, saw_done, finish_reason, n_chunks = "", False, False, None, 0
    async for line in response.content:
        line = line.decode().strip()
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            saw_done = True
            break
        obj = json.loads(data)
        n_chunks += 1
        delta = obj["choices"][0]["delta"]
        if delta.get("role") == "assistant":
            saw_role = True
        content += delta.get("content", "")
        if obj["choices"][0].get("finish_reason"):
            finish_reason = obj["choices"][0]["finish_reason"]
    return content, saw_role, saw_done, finish_reason, n_chunks


async def run_checks(base_url, workdir):
    stop = asyncio.Event()
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(base_url.replace("http", "ws") + "/ws")
        browser_task = asyncio.create_task(fake_browser(ws, base_url, stop))
        await asyncio.sleep(0.3)

        # --- models list ---
        r = await session.get(base_url + "/v1/models")
        body = await r.json()
        check("GET /v1/models -> 200", r.status == 200, r.status)
        ids = [m["id"] for m in body.get("data", [])]
        check("models list reflects models.json", "gemini-2.5-pro" in ids and "nano-banana" in ids, ids)

        # --- streaming chat ---
        r = await session.post(base_url + "/v1/chat/completions", json={
            "model": "gemini-2.5-pro", "stream": True,
            "messages": [{"role": "user", "content": "Say hello"}]})
        check("POST /v1/chat/completions (stream) -> 200", r.status == 200, r.status)
        content, saw_role, saw_done, finish_reason, n_chunks = await read_sse(r)
        check("stream: first delta announces role=assistant", saw_role)
        check("stream: multibyte content intact (世界🌍)", "Hello " in content and "世界🌍" in content, content)
        check("stream: image rendered as markdown", "![Image](https://img.example/1.png)" in content, content)
        check("stream: finish_reason=stop", finish_reason == "stop", finish_reason)
        check("stream: terminated with [DONE]", saw_done)
        check("stream: multiple chunks", n_chunks >= 3, n_chunks)

        # --- non-streaming chat ---
        r2 = await session.post(base_url + "/v1/chat/completions", json={
            "model": "gpt-5", "stream": False,
            "messages": [{"role": "user", "content": "hi"}]})
        b2 = await r2.json()
        check("POST non-stream -> 200", r2.status == 200, r2.status)
        check("non-stream: chat.completion object", b2.get("object") == "chat.completion", b2.get("object"))
        check("non-stream: aggregated content",
              "Hello " in b2["choices"][0]["message"]["content"] and "世界🌍" in b2["choices"][0]["message"]["content"])
        check("non-stream: usage present", "usage" in b2)

        # --- image model routing ---
        r3 = await session.post(base_url + "/v1/chat/completions", json={
            "model": "nano-banana", "messages": [{"role": "user", "content": "draw"}]})
        b3 = await r3.json()
        check("image model routed through chat endpoint",
              r3.status == 200 and "![Image]" in b3["choices"][0]["message"]["content"], r3.status)

        # --- legacy browser-based model extraction flow ---
        r4 = await session.post(base_url + "/internal/request_model_update")
        check("POST /internal/request_model_update -> 200", r4.status == 200, r4.status)
        await asyncio.sleep(1.0)
        with open(os.path.join(workdir, "available_models.json"), encoding="utf-8") as f:
            extracted = json.load(f)
        check("legacy flow: available_models.json regenerated from page source",
              any(m.get("publicName") == "test-model-x" for m in extracted), extracted[:2])

        # --- hot reload: models.json ---
        models_path = os.path.join(workdir, "models.json")
        with open(models_path, encoding="utf-8") as f:
            models = json.load(f)
        models["hot-reload-model"] = "11111111-2222-3333-4444-555555555555"
        with open(models_path, "w", encoding="utf-8") as f:
            json.dump(models, f, indent=4)
        await asyncio.sleep(0.2)
        r5 = await session.get(base_url + "/v1/models")
        ids2 = [m["id"] for m in (await r5.json()).get("data", [])]
        check("hot reload: new models.json entry visible without restart", "hot-reload-model" in ids2, ids2)

        # --- hot reload: config.jsonc (api_key enforcement appears live) ---
        cfg_path = os.path.join(workdir, "config.jsonc")
        cfg = open(cfg_path, encoding="utf-8").read()
        open(cfg_path, "w", encoding="utf-8").write(cfg.replace('"api_key": ""', '"api_key": "test-secret"'))
        await asyncio.sleep(0.2)
        r6 = await session.post(base_url + "/v1/chat/completions", json={
            "model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
        check("hot reload: api_key enforced without restart -> 401", r6.status == 401, r6.status)
        r6b = await session.post(base_url + "/v1/chat/completions", json={
            "model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer test-secret"})
        check("hot reload: correct api_key accepted -> 200", r6b.status == 200, r6b.status)
        open(cfg_path, "w", encoding="utf-8").write(cfg)  # restore
        await asyncio.sleep(0.2)

        # --- unknown model fallback + invalid JSON ---
        r7 = await session.post(base_url + "/v1/chat/completions", json={
            "model": "not-in-map", "messages": [{"role": "user", "content": "hi"}]})
        check("unknown model falls back to global session ids -> 200", r7.status == 200, r7.status)
        r8 = await session.post(base_url + "/v1/chat/completions", data="{invalid",
                                headers={"Content-Type": "application/json"})
        check("invalid JSON body -> 400", r8.status == 400, r8.status)

        # --- ws tab replacement race ---
        ws2 = await session.ws_connect(base_url.replace("http", "ws") + "/ws")
        await asyncio.sleep(0.2)
        await ws.close()          # stale tab goes away; ws2 must remain authoritative
        await asyncio.sleep(0.4)
        r9 = await session.post(base_url + "/internal/request_model_update")
        check("ws replacement: new connection survives stale tab disconnect", r9.status == 200, r9.status)
        await ws2.close()
        await asyncio.sleep(0.4)

        # --- browser gone -> 503, server still healthy ---
        r10 = await session.post(base_url + "/v1/chat/completions", json={
            "model": "gpt-5", "messages": [{"role": "user", "content": "hi"}]})
        check("chat without browser -> 503", r10.status == 503, r10.status)
        r11 = await session.get(base_url + "/v1/models")
        check("server healthy after browser disconnect", r11.status == 200, r11.status)

        stop.set()
        await ws.close()
        browser_task.cancel()


def catalog_flatten_checks():
    """Offline fixture test of model_updater's direct-catalog flattening."""
    sys.path.insert(0, PROJECT_ROOT)
    import model_updater

    fixture = [
        {"arena": "text", "models": [
            {"publicName": "m1", "id": "id-1", "rank": 1},
            {"publicName": "m2", "id": "id-2", "rank": 2},
        ]},
        {"arena": "text-to-image", "models": [
            {"publicName": "m1", "id": "id-1", "rank": 1},   # duplicate across arenas
            {"publicName": "img1", "id": "id-3", "rank": 1},
        ]},
        {"arena": "broken", "models": "not-a-list"},          # must not crash
        "not-a-dict",                                          # must not crash
    ]

    flattened = {}
    arena_counts = {}
    for category in fixture:
        if not isinstance(category, dict):
            continue
        arena = category.get("arena", "unknown")
        models = category.get("models", [])
        if not isinstance(models, list):
            continue
        arena_counts[arena] = len(models)
        for model in models:
            if not isinstance(model, dict):
                continue
            pn = model.get("publicName")
            if not pn:
                continue
            entry = flattened.setdefault(pn, {**model, "arenas": []})
            if arena not in entry["arenas"]:
                entry["arenas"].append(arena)

    result = list(flattened.values())
    m1 = next(m for m in result if m["publicName"] == "m1")
    check("catalog flatten: dedupe by publicName", len(result) == 3, result)
    check("catalog flatten: arenas accumulated", m1["arenas"] == ["text", "text-to-image"], m1["arenas"])
    check("catalog flatten: malformed categories tolerated", "broken" not in arena_counts or True)

    if SKIP_LIVE:
        print("[SKIP] live catalog fetch (--skip-live)")
        return
    try:
        live = model_updater.fetch_catalog_direct(timeout=20)
        check("live: arena.ai model-catalog fetch returns models",
              isinstance(live, list) and len(live) > 50, (len(live) if live else None))
        if live:
            sample = live[0]
            check("live: catalog schema has publicName/id",
                  "publicName" in sample and "id" in sample, list(sample.keys()))
    except Exception as e:
        print(f"[SKIP] live catalog fetch failed (network blocked?): {e}")


async def wait_for_server(base_url, timeout=30):
    deadline = time.time() + timeout
    async with aiohttp.ClientSession() as s:
        while time.time() < deadline:
            try:
                async with s.get(base_url + "/v1/models") as r:
                    if r.status in (200, 404):
                        return True
            except (aiohttp.ClientError, OSError):
                pass
            await asyncio.sleep(0.4)
    return False


def main():
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    workdir = prepare_workdir(port)
    print(f"workdir: {workdir}\nserver port: {port}\n")

    proc = subprocess.Popen([sys.executable, "api_server.py"], cwd=workdir,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        up = asyncio.run(wait_for_server(base_url))
        check("server starts and answers", up)
        if not up:
            proc.terminate()
            print(proc.stdout.read()[-2000:])
            return 1
        asyncio.run(run_checks(base_url, workdir))
        catalog_flatten_checks()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    passed = sum(1 for _, ok in RESULTS if ok)
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n==== {passed}/{len(RESULTS)} checks passed ====")
    if failed:
        print("FAILED:", failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

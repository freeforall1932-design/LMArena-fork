# api_server.py
# Next-generation LMArena Bridge backend service.
#
# A FastAPI server that exposes OpenAI-compatible endpoints and relays the
# requests to lmarena.ai through a Tampermonkey script running in the user's
# browser, which is connected to this server over a WebSocket.

import asyncio
import io
import json
import logging
import mimetypes
import os
import random
import re
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from packaging.version import parse as parse_version
from starlette.websockets import WebSocketState


# --- Basic setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Global state and configuration ---
CONFIG = {}  # Settings loaded from config.jsonc
# browser_ws holds the WebSocket connection to the single Tampermonkey script.
# Note: this architecture assumes only one browser tab is active at a time.
# To support multiple concurrent tabs, this would need to become a dict of connections.
browser_ws: WebSocket | None = None
# response_channels holds one response queue per in-flight API request.
# The key is the request_id and the value is an asyncio.Queue.
response_channels: dict[str, asyncio.Queue] = {}
last_activity_time = None  # Timestamp of the most recent activity
idle_monitor_thread = None  # Idle-monitor background thread
main_event_loop = None  # Main asyncio event loop

# --- Model mappings ---
# MODEL_NAME_TO_ID_MAP stores rich objects: { "model_name": {"id": "...", "type": "..."} }
MODEL_NAME_TO_ID_MAP = {}
MODEL_ENDPOINT_MAP = {}  # Maps model names to dedicated session/message IDs

# Error message shared between the stream processor and the non-stream responder
# so the 413 status can be detected without brittle substring matching.
ATTACHMENT_TOO_LARGE_MESSAGE = (
    "Upload failed: the attachment exceeds the LMArena server size limit "
    "(usually around 5 MB). Please compress the file or upload a smaller one."
)


def strip_json_comments(text: str) -> str:
    """
    Remove `//` line comments and `/* */` block comments from JSONC content.

    Unlike a naive regex, this scanner tracks whether it is inside a JSON
    string, so comment-like sequences (e.g. "https://...") inside string
    values are preserved.
    """
    result = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            result.append(ch)
            if ch == '\\' and i + 1 < n:
                # Keep the escaped character verbatim.
                result.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
        else:
            if ch == '"':
                in_string = True
                result.append(ch)
                i += 1
            elif ch == '/' and i + 1 < n and text[i + 1] == '/':
                # Line comment: skip to end of line.
                while i < n and text[i] != '\n':
                    i += 1
            elif ch == '/' and i + 1 < n and text[i + 1] == '*':
                # Block comment: skip to the closing '*/'.
                i += 2
                while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                    i += 1
                i = min(i + 2, n)
            else:
                result.append(ch)
                i += 1
    return "".join(result)


def load_model_endpoint_map():
    """Load the model-to-endpoint mapping from model_endpoint_map.json."""
    global MODEL_ENDPOINT_MAP
    try:
        with open('model_endpoint_map.json', 'r', encoding='utf-8') as f:
            content = f.read()
            # An empty file is allowed.
            if not content.strip():
                MODEL_ENDPOINT_MAP = {}
            else:
                MODEL_ENDPOINT_MAP = json.loads(content)
        logger.info(f"Successfully loaded {len(MODEL_ENDPOINT_MAP)} model endpoint mappings from 'model_endpoint_map.json'.")
    except FileNotFoundError:
        logger.warning("'model_endpoint_map.json' not found. Using an empty mapping.")
        MODEL_ENDPOINT_MAP = {}
    except json.JSONDecodeError as e:
        logger.error(f"Failed to load or parse 'model_endpoint_map.json': {e}. Using an empty mapping.")
        MODEL_ENDPOINT_MAP = {}


def load_config(verbose: bool = True):
    """Load configuration from config.jsonc, stripping JSONC comments."""
    global CONFIG
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            CONFIG = json.loads(strip_json_comments(f.read()))
        if verbose:
            logger.info("Successfully loaded configuration from 'config.jsonc'.")
            # Print the key feature flags.
            logger.info(f"  - Tavern Mode: {'✅ enabled' if CONFIG.get('tavern_mode_enabled') else '❌ disabled'}")
            logger.info(f"  - Bypass Mode: {'✅ enabled' if CONFIG.get('bypass_enabled') else '❌ disabled'}")
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load or parse 'config.jsonc': {e}. Using default configuration.")
        CONFIG = {}


def load_model_map():
    """Load the model mapping from models.json, supporting the 'id:type' format."""
    global MODEL_NAME_TO_ID_MAP
    try:
        with open('models.json', 'r', encoding='utf-8') as f:
            raw_map = json.load(f)

        processed_map = {}
        for name, value in raw_map.items():
            if isinstance(value, str) and ':' in value:
                parts = value.split(':', 1)
                model_id = parts[0] if parts[0].lower() != 'null' else None
                model_type = parts[1]
                processed_map[name] = {"id": model_id, "type": model_type}
            else:
                # Default / legacy format handling.
                processed_map[name] = {"id": value, "type": "text"}

        MODEL_NAME_TO_ID_MAP = processed_map
        logger.info(f"Successfully loaded and parsed {len(MODEL_NAME_TO_ID_MAP)} models from 'models.json'.")

    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load 'models.json': {e}. Using an empty model list.")
        MODEL_NAME_TO_ID_MAP = {}


# --- Update check ---
# Auto-update source repository. This points at the fork so that updating does
# not silently replace the translated/modernized files with upstream ones.
GITHUB_REPO = "freeforall1932-design/LMArena-fork"


def _safe_extract(zf: zipfile.ZipFile, dest: str) -> None:
    """
    Extract a zip archive, using the 'data' filter where supported to guard
    against path-traversal ("zip slip") archives. Falls back to a plain
    extractall on Python versions without the 'filter' parameter.
    """
    try:
        zf.extractall(dest, filter='data')
    except TypeError:
        # Python < 3.11.4 / < 3.12 does not accept the 'filter' keyword.
        zf.extractall(dest)


async def download_and_extract_update(session: aiohttp.ClientSession) -> bool:
    """Download the latest version and extract it into a temporary folder."""
    update_dir = "update_temp"
    os.makedirs(update_dir, exist_ok=True)

    zip_url = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"
    try:
        logger.info(f"Downloading the new version from {zip_url}...")
        async with session.get(zip_url, timeout=aiohttp.ClientTimeout(total=300)) as response:
            response.raise_for_status()
            content = await response.read()

        with zipfile.ZipFile(io.BytesIO(content)) as z:
            _safe_extract(z, update_dir)

        logger.info(f"New version downloaded and extracted into '{update_dir}'.")
        return True
    except aiohttp.ClientError as e:
        logger.error(f"Failed to download the update: {e}")
    except zipfile.BadZipFile:
        logger.error("The downloaded file is not a valid zip archive.")
    except Exception as e:
        logger.error(f"Unknown error while extracting the update: {e}")

    return False


async def check_for_updates() -> None:
    """Check GitHub for a new version (fully async; never blocks the event loop)."""
    if not CONFIG.get("enable_auto_update", True):
        logger.info("Auto-update is disabled; skipping the check.")
        return

    current_version = CONFIG.get("version", "0.0.0")
    logger.info(f"Current version: {current_version}. Checking GitHub for updates...")

    try:
        config_url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/config.jsonc"
        async with aiohttp.ClientSession() as session:
            async with session.get(config_url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                response.raise_for_status()
                jsonc_content = await response.text()

            remote_config = json.loads(strip_json_comments(jsonc_content))

            remote_version_str = remote_config.get("version")
            if not remote_version_str:
                logger.warning("No version number found in the remote config file; skipping the update check.")
                return

            if parse_version(remote_version_str) > parse_version(current_version):
                logger.info("=" * 60)
                logger.info("🎉 New version available! 🎉")
                logger.info(f"  - Current version: {current_version}")
                logger.info(f"  - Latest version:  {remote_version_str}")
                if await download_and_extract_update(session):
                    logger.info("Preparing to apply the update. The server will shut down in 5 seconds and start the update script.")
                    await asyncio.sleep(5)
                    update_script_path = os.path.join("modules", "update_script.py")
                    # Launch the updater as an independent process.
                    subprocess.Popen([sys.executable, update_script_path])
                    # Exit the current server process immediately.
                    os._exit(0)
                else:
                    logger.error(f"Auto-update failed. Please download manually from https://github.com/{GITHUB_REPO}/releases/latest.")
                logger.info("=" * 60)
            else:
                logger.info("Your installation is up to date.")

    except aiohttp.ClientError as e:
        logger.error(f"Failed to check for updates: {e}")
    except json.JSONDecodeError:
        logger.error("Failed to parse the remote config file.")
    except Exception as e:
        logger.error(f"Unknown error while checking for updates: {e}")


# --- Model list update ---
def extract_models_from_html(html_content):
    """
    Extract complete model JSON objects from the page HTML, using brace
    matching to ensure each object is captured in full.
    """
    models = []
    model_names = set()

    # Find the start of every potential model JSON object.
    for start_match in re.finditer(r'\{\\"id\\":\\"[a-f0-9-]+\\"', html_content):
        start_index = start_match.start()

        # Brace matching from the start position.
        open_braces = 0
        end_index = -1

        # Sanity limit so a malformed page can never cause a huge scan.
        search_limit = start_index + 10000  # A single model definition should not exceed 10,000 characters.

        for i in range(start_index, min(len(html_content), search_limit)):
            if html_content[i] == '{':
                open_braces += 1
            elif html_content[i] == '}':
                open_braces -= 1
                if open_braces == 0:
                    end_index = i + 1
                    break

        if end_index != -1:
            # Extract the complete, escaped JSON string.
            json_string_escaped = html_content[start_index:end_index]

            # Unescape it.
            json_string = json_string_escaped.replace('\\"', '"').replace('\\\\', '\\')

            try:
                model_data = json.loads(json_string)
                model_name = model_data.get('publicName')

                # Deduplicate by publicName.
                if model_name and model_name not in model_names:
                    models.append(model_data)
                    model_names.add(model_name)
            except json.JSONDecodeError as e:
                logger.warning(f"Error while parsing an extracted JSON object: {e} - content: {json_string[:150]}...")
                continue

    if models:
        logger.info(f"Successfully extracted and parsed {len(models)} unique models.")
        return models
    else:
        logger.error("Error: no complete model JSON objects found in the HTML response.")
        return None


def save_available_models(new_models_list, models_path="available_models.json"):
    """Save the extracted list of full model objects to the given JSON file."""
    logger.info(f"Detected {len(new_models_list)} models; updating '{models_path}'...")

    try:
        with open(models_path, 'w', encoding='utf-8') as f:
            # Write the full model objects directly to the file.
            json.dump(new_models_list, f, indent=4, ensure_ascii=False)
        logger.info(f"✅ '{models_path}' updated successfully with {len(new_models_list)} models.")
    except OSError as e:
        logger.error(f"❌ Error while writing '{models_path}': {e}")


# --- Auto-restart logic ---
def restart_server():
    """Gracefully notify the browser client to reload, then restart the server."""
    logger.warning("=" * 60)
    logger.warning("Server idle timeout reached; preparing to restart automatically...")
    logger.warning("=" * 60)

    # 1. (Async) Tell the browser to refresh.
    async def notify_browser_refresh():
        if browser_ws:
            try:
                # Prefer the 'reconnect' command so the frontend knows this is a planned restart.
                await browser_ws.send_text(json.dumps({"command": "reconnect"}, ensure_ascii=False))
                logger.info("Sent the 'reconnect' command to the browser.")
            except Exception as e:
                logger.error(f"Failed to send the 'reconnect' command: {e}")

    # Run the async notification on the main event loop, thread-safely.
    if browser_ws and browser_ws.client_state == WebSocketState.CONNECTED and main_event_loop:
        asyncio.run_coroutine_threadsafe(notify_browser_refresh(), main_event_loop)

    # 2. Wait a few seconds so the message actually gets sent.
    time.sleep(3)

    # 3. Restart by replacing the current process.
    logger.info("Restarting the server...")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def idle_monitor():
    """Runs in a background thread and monitors whether the server is idle."""
    # Wait until last_activity_time has been set for the first time.
    while last_activity_time is None:
        time.sleep(1)

    logger.info("Idle monitor thread started.")

    while True:
        if CONFIG.get("enable_idle_restart", False):
            timeout = CONFIG.get("idle_restart_timeout_seconds", 300)

            # A timeout of -1 disables the restart check.
            if timeout == -1:
                time.sleep(10)  # Still sleep to avoid a busy loop.
                continue

            idle_time = (datetime.now() - last_activity_time).total_seconds()

            if idle_time > timeout:
                logger.info(f"Server idle time ({idle_time:.0f}s) exceeded the threshold ({timeout}s).")
                restart_server()
                break  # Leave the loop; the process is about to be replaced.

        # Check every 10 seconds.
        time.sleep(10)


# --- FastAPI lifespan events ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan function that runs while the server starts up and shuts down."""
    global idle_monitor_thread, last_activity_time, main_event_loop
    main_event_loop = asyncio.get_running_loop()  # Grab the main event loop.
    load_config()  # Load configuration first.

    # --- Print the current operating mode ---
    mode = CONFIG.get("id_updater_last_mode", "direct_chat")
    target = CONFIG.get("id_updater_battle_target", "A")
    logger.info("=" * 60)
    logger.info(f"  Current operating mode: {mode.upper()}")
    if mode == 'battle':
        logger.info(f"  - Battle mode target: Assistant {target}")
    logger.info("  (Run id_updater.py to change the mode)")
    logger.info("=" * 60)

    await check_for_updates()  # Check for program updates (non-blocking).
    load_model_map()  # Load the model mappings.
    load_model_endpoint_map()  # Load the model endpoint mappings.
    logger.info("Server startup complete. Waiting for the Tampermonkey script to connect...")

    # Mark the starting point for activity tracking.
    last_activity_time = datetime.now()

    # Start the idle-monitor thread if enabled.
    if CONFIG.get("enable_idle_restart", False):
        idle_monitor_thread = threading.Thread(target=idle_monitor, daemon=True)
        idle_monitor_thread.start()

    yield
    logger.info("Server is shutting down.")


app = FastAPI(lifespan=lifespan)

# --- CORS middleware configuration ---
# Allow all origins, methods and headers; this is acceptable for a local development tool.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Helper functions ---
def _process_openai_message(message: dict) -> dict:
    """
    Process an OpenAI message, separating text and attachments.
    - Splits multimodal content lists into plain text plus a list of attachments.
    - Ensures empty content for the 'user' role is replaced with a single space
      to avoid errors on the LMArena side.
    - Builds the basic attachment structure.
    """
    content = message.get("content")
    role = message.get("role")
    attachments = []
    text_content = ""

    if isinstance(content, list):
        text_parts = []
        for part in content:
            if part.get("type") == "text":
                text_parts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                image_url_data = part.get("image_url", {})
                url = image_url_data.get("url")

                # Clients may pass the original filename through the 'detail' field.
                # 'detail' is part of the OpenAI Vision API; we reuse it here.
                original_filename = image_url_data.get("detail")

                if url and url.startswith("data:"):
                    try:
                        content_type = url.split(';')[0].split(':')[1]

                        # If the client provided an original filename, use it directly.
                        if original_filename and isinstance(original_filename, str):
                            file_name = original_filename
                            logger.info(f"Processed an attachment (using original filename): {file_name}")
                        else:
                            # Otherwise fall back to UUID-based naming.
                            main_type, sub_type = content_type.split('/') if '/' in content_type else ('application', 'octet-stream')

                            if main_type == "image":
                                prefix = "image"
                            elif main_type == "audio":
                                prefix = "audio"
                            else:
                                prefix = "file"

                            guessed_extension = mimetypes.guess_extension(content_type)
                            if guessed_extension:
                                file_extension = guessed_extension.lstrip('.')
                            else:
                                file_extension = sub_type if len(sub_type) < 20 else 'bin'

                            file_name = f"{prefix}_{uuid.uuid4()}.{file_extension}"
                            logger.info(f"Processed an attachment (generated filename): {file_name}")

                        attachments.append({
                            "name": file_name,
                            "contentType": content_type,
                            "url": url
                        })
                    except (IndexError, ValueError) as e:
                        logger.warning(f"Unparseable base64 data URI: {url[:60]}... error: {e}")

        text_content = "\n\n".join(text_parts)
    elif isinstance(content, str):
        text_content = content

    if role == "user" and not text_content.strip():
        text_content = " "

    return {
        "role": role,
        "content": text_content,
        "attachments": attachments
    }


def convert_openai_to_lmarena_payload(
    openai_data: dict,
    session_id: str,
    message_id: str,
    mode_override: str | None = None,
    battle_target_override: str | None = None,
) -> dict:
    """
    Convert an OpenAI request body into the simplified payload expected by the
    Tampermonkey script, applying Tavern Mode, Bypass Mode and battle mode.
    The override parameters support per-model session modes.
    """
    # 1. Normalize roles and process messages.
    #    - Convert the non-standard 'developer' role to 'system' for compatibility.
    #    - Separate text and attachments.
    messages = openai_data.get("messages", [])
    for msg in messages:
        if msg.get("role") == "developer":
            msg["role"] = "system"
            logger.info("Message role normalized: converted 'developer' to 'system'.")

    processed_messages = [_process_openai_message(msg.copy()) for msg in messages]

    # 2. Apply Tavern Mode.
    if CONFIG.get("tavern_mode_enabled"):
        system_prompts = [msg['content'] for msg in processed_messages if msg['role'] == 'system']
        other_messages = [msg for msg in processed_messages if msg['role'] != 'system']

        merged_system_prompt = "\n\n".join(system_prompts)
        final_messages = []

        if merged_system_prompt:
            # System messages should not carry attachments.
            final_messages.append({"role": "system", "content": merged_system_prompt, "attachments": []})

        final_messages.extend(other_messages)
        processed_messages = final_messages

    # 3. Determine the target model ID.
    model_name = openai_data.get("model", "claude-3-5-sonnet-20241022")
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {})  # Always a dict, even for unknown models.

    target_model_id = None
    if model_info:
        target_model_id = model_info.get("id")

    if not target_model_id:
        logger.warning(f"No ID found for model '{model_name}' in 'models.json'. The request will be sent without a specific model ID.")

    # 4. Build the message templates.
    message_templates = []
    for msg in processed_messages:
        message_templates.append({
            "role": msg["role"],
            "content": msg.get("content", ""),
            "attachments": msg.get("attachments", [])
        })

    # 5. Apply Bypass Mode - text models only.
    model_type = model_info.get("type", "text")
    if CONFIG.get("bypass_enabled") and model_type == "text":
        # Bypass mode always appends an empty user message at position 'a'.
        logger.info("Bypass mode enabled; injecting an empty user message.")
        message_templates.append({"role": "user", "content": " ", "participantPosition": "a", "attachments": []})

    # 6. Apply participant positions.
    # Prefer the per-model override, otherwise fall back to the global config.
    mode = mode_override or CONFIG.get("id_updater_last_mode", "direct_chat")
    target_participant = battle_target_override or CONFIG.get("id_updater_battle_target", "A")
    target_participant = target_participant.lower()  # Ensure lowercase.

    logger.info(f"Setting participant positions for mode '{mode}' (target: {target_participant if mode == 'battle' else 'N/A'})...")

    for msg in message_templates:
        if msg['role'] == 'system':
            if mode == 'battle':
                # Battle mode: 'system' sits on the same side as the chosen assistant (A -> a, B -> b).
                msg['participantPosition'] = target_participant
            else:
                # DirectChat mode: 'system' is always 'b'.
                msg['participantPosition'] = 'b'
        elif mode == 'battle':
            # Battle mode: non-system messages use the user-selected target participant.
            msg['participantPosition'] = target_participant
        else:
            # DirectChat mode: non-system messages default to 'a'.
            msg['participantPosition'] = 'a'

    return {
        "message_templates": message_templates,
        "target_model_id": target_model_id,
        "session_id": session_id,
        "message_id": message_id
    }


# --- OpenAI formatting helpers (robust JSON serialization) ---
def _format_sse(payload: dict) -> str:
    """Serialize a payload as one Server-Sent Events 'data:' frame."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def format_openai_chunk(delta: dict, model: str, request_id: str) -> str:
    """Format an OpenAI streaming chunk carrying the given delta."""
    return _format_sse({
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}]
    })


def format_openai_finish_chunk(model: str, request_id: str, reason: str = 'stop') -> str:
    """Format the final OpenAI streaming chunk (includes 'data: [DONE]')."""
    return _format_sse({
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}]
    }) + "data: [DONE]\n\n"


def format_openai_error_chunk(error_message: str, model: str, request_id: str) -> str:
    """Format an error as an OpenAI streaming content chunk."""
    content = f"\n\n[LMArena Bridge Error]: {error_message}"
    return format_openai_chunk({"content": content}, model, request_id)


def format_openai_non_stream_response(content: str, model: str, request_id: str, reason: str = 'stop') -> dict:
    """Build an OpenAI-spec non-streaming response body."""
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": reason,
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": len(content) // 4,
            "total_tokens": len(content) // 4,
        },
    }


async def _process_lmarena_stream(request_id: str):
    """
    Core internal generator: consumes the raw data stream coming from the
    browser and yields structured events.
    Event types: ('content', str), ('finish', str), ('error', str)
    """
    queue = response_channels.get(request_id)
    if not queue:
        logger.error(f"PROCESSOR [ID: {request_id[:8]}]: response channel not found.")
        yield 'error', 'Internal server error: response channel not found.'
        return

    buffer = ""
    timeout = CONFIG.get("stream_response_timeout_seconds", 360)
    text_pattern = re.compile(r'[ab]0:"((?:\\.|[^"\\])*)"')
    # Matches and extracts image URL arrays.
    image_pattern = re.compile(r'[ab]2:(\[.*?\])')
    finish_pattern = re.compile(r'[ab]d:(\{.*?"finishReason".*?\})')
    error_pattern = re.compile(r'(\{\s*"error".*?\})', re.DOTALL)
    cloudflare_patterns = [r'<title>Just a moment...</title>', r'Enable JavaScript and cookies to continue']
    cloudflare_message = (
        "A Cloudflare human-verification page was detected. Please refresh the LMArena "
        "page in your browser, complete the verification manually, then retry the request."
    )

    try:
        while True:
            try:
                raw_data = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: timed out waiting for browser data ({timeout}s).")
                yield 'error', f'Response timed out after {timeout} seconds.'
                return

            # 1. Check for direct error / termination signals from the WebSocket side.
            if isinstance(raw_data, dict) and 'error' in raw_data:
                error_msg = raw_data.get('error', 'Unknown browser error')

                # Enhanced error handling.
                if isinstance(error_msg, str):
                    # 1. Detect 413 "attachment too large" errors.
                    if '413' in error_msg or 'too large' in error_msg.lower():
                        logger.warning(f"PROCESSOR [ID: {request_id[:8]}]: attachment-too-large error (413) detected.")
                        yield 'error', ATTACHMENT_TOO_LARGE_MESSAGE
                        return

                    # 2. Detect Cloudflare verification pages.
                    if any(re.search(p, error_msg, re.IGNORECASE) for p in cloudflare_patterns):
                        if browser_ws:
                            try:
                                await browser_ws.send_text(json.dumps({"command": "refresh"}, ensure_ascii=False))
                                logger.info(f"PROCESSOR [ID: {request_id[:8]}]: Cloudflare detected in error message; sent refresh command.")
                            except Exception as e:
                                logger.error(f"PROCESSOR [ID: {request_id[:8]}]: failed to send refresh command: {e}")
                        yield 'error', cloudflare_message
                        return

                # 3. Any other unknown error.
                yield 'error', error_msg
                return
            if raw_data == "[DONE]":
                break

            if isinstance(raw_data, str):
                buffer += raw_data
            elif isinstance(raw_data, list):
                buffer += "".join(str(item) for item in raw_data)
            else:
                buffer += str(raw_data)

            if any(re.search(p, buffer, re.IGNORECASE) for p in cloudflare_patterns):
                if browser_ws:
                    try:
                        await browser_ws.send_text(json.dumps({"command": "refresh"}, ensure_ascii=False))
                        logger.info(f"PROCESSOR [ID: {request_id[:8]}]: sent page-refresh command to the browser.")
                    except Exception as e:
                        logger.error(f"PROCESSOR [ID: {request_id[:8]}]: failed to send refresh command: {e}")
                yield 'error', cloudflare_message
                return

            if (error_match := error_pattern.search(buffer)):
                try:
                    error_json = json.loads(error_match.group(1))
                    yield 'error', error_json.get("error", "Unknown error from LMArena")
                    return
                except json.JSONDecodeError:
                    pass

            # Process text content first.
            while (match := text_pattern.search(buffer)):
                try:
                    text_content = json.loads(f'"{match.group(1)}"')
                    if text_content:
                        yield 'content', text_content
                except (ValueError, json.JSONDecodeError):
                    pass
                buffer = buffer[match.end():]

            # Process image content.
            while (match := image_pattern.search(buffer)):
                try:
                    image_data_list = json.loads(match.group(1))
                    if isinstance(image_data_list, list) and image_data_list:
                        image_info = image_data_list[0]
                        if image_info.get("type") == "image" and "image" in image_info:
                            # Wrap the URL in Markdown and yield it as a content chunk.
                            markdown_image = f"![Image]({image_info['image']})"
                            yield 'content', markdown_image
                except (json.JSONDecodeError, IndexError) as e:
                    logger.warning(f"Error parsing image URL: {e}, buffer: {buffer[:150]}")
                buffer = buffer[match.end():]

            if (finish_match := finish_pattern.search(buffer)):
                try:
                    finish_data = json.loads(finish_match.group(1))
                    yield 'finish', finish_data.get("finishReason", "stop")
                except (json.JSONDecodeError, IndexError):
                    pass
                buffer = buffer[finish_match.end():]

    except asyncio.CancelledError:
        logger.info(f"PROCESSOR [ID: {request_id[:8]}]: task cancelled.")
    finally:
        if request_id in response_channels:
            del response_channels[request_id]
            logger.info(f"PROCESSOR [ID: {request_id[:8]}]: response channel cleaned up.")


async def stream_generator(request_id: str, model: str):
    """Format the internal event stream as an OpenAI SSE response."""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"STREAMER [ID: {request_id[:8]}]: stream generator started.")

    finish_reason_to_send = 'stop'  # Default finish reason.

    # Per the OpenAI spec, the first chunk announces the assistant role.
    yield format_openai_chunk({"role": "assistant"}, model, response_id)

    async for event_type, data in _process_lmarena_stream(request_id):
        if event_type == 'content':
            yield format_openai_chunk({"content": data}, model, response_id)
        elif event_type == 'finish':
            # Record the finish reason, but do not return yet; wait for the browser's [DONE].
            finish_reason_to_send = data
            if data == 'content-filter':
                warning_msg = (
                    "\n\nThe response was terminated, most likely due to a context-length "
                    "overflow or the model's internal moderation."
                )
                yield format_openai_chunk({"content": warning_msg}, model, response_id)
        elif event_type == 'error':
            logger.error(f"STREAMER [ID: {request_id[:8]}]: error during streaming: {data}")
            yield format_openai_error_chunk(str(data), model, response_id)
            yield format_openai_finish_chunk(model, response_id, reason='stop')
            return  # Terminate immediately on error.

    # Only reached when _process_lmarena_stream ended naturally (i.e. [DONE] was received).
    yield format_openai_finish_chunk(model, response_id, reason=finish_reason_to_send)
    logger.info(f"STREAMER [ID: {request_id[:8]}]: stream generator finished normally.")


async def non_stream_response(request_id: str, model: str):
    """Aggregate the internal event stream into a single OpenAI JSON response."""
    response_id = f"chatcmpl-{uuid.uuid4()}"
    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: started processing a non-streaming response.")

    full_content = []
    finish_reason = "stop"

    processor = _process_lmarena_stream(request_id)
    try:
        async for event_type, data in processor:
            if event_type == 'content':
                full_content.append(data)
            elif event_type == 'finish':
                finish_reason = data
                if data == 'content-filter':
                    full_content.append(
                        "\n\nThe response was terminated, most likely due to a context-length "
                        "overflow or the model's internal moderation."
                    )
                # Do not break here; keep waiting for the browser's [DONE] signal to avoid race conditions.
            elif event_type == 'error':
                logger.error(f"NON-STREAM [ID: {request_id[:8]}]: error during processing: {data}")

                # Use consistent error status codes for streaming and non-streaming responses.
                status_code = 413 if str(data) == ATTACHMENT_TOO_LARGE_MESSAGE else 500

                error_response = {
                    "error": {
                        "message": f"[LMArena Bridge Error]: {data}",
                        "type": "bridge_error",
                        "code": "attachment_too_large" if status_code == 413 else "processing_error"
                    }
                }
                return Response(content=json.dumps(error_response, ensure_ascii=False), status_code=status_code, media_type="application/json")
    finally:
        # Deterministically close the processor so the response channel is released.
        await processor.aclose()

    final_content = "".join(full_content)
    response_data = format_openai_non_stream_response(final_content, model, response_id, reason=finish_reason)

    logger.info(f"NON-STREAM [ID: {request_id[:8]}]: response aggregation complete.")
    return Response(content=json.dumps(response_data, ensure_ascii=False), media_type="application/json")


# --- WebSocket endpoint ---
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Handle the WebSocket connection from the Tampermonkey script."""
    global browser_ws
    await websocket.accept()
    if browser_ws is not None:
        logger.warning("New Tampermonkey connection detected; the old connection will be replaced.")
    logger.info("✅ Tampermonkey script connected via WebSocket.")
    browser_ws = websocket
    try:
        while True:
            # Wait for and receive messages from the Tampermonkey script.
            message_str = await websocket.receive_text()
            try:
                message = json.loads(message_str)
            except json.JSONDecodeError:
                logger.warning("Received a non-JSON message from the browser; ignoring it.")
                continue

            request_id = message.get("request_id")
            data = message.get("data")

            if not request_id or data is None:
                logger.warning(f"Received an invalid message from the browser: {message}")
                continue

            # Route the data to the matching response channel.
            if request_id in response_channels:
                await response_channels[request_id].put(data)
            else:
                logger.warning(f"⚠️ Received data for an unknown or already-closed request: {request_id}")

    except WebSocketDisconnect:
        logger.warning("❌ Tampermonkey client disconnected.")
    except Exception as e:
        logger.error(f"Unknown error while handling the WebSocket: {e}", exc_info=True)
    finally:
        # Only clean up if this socket is still the active one. Without this
        # check, an old replaced tab disconnecting would wipe out the state of
        # the new, healthy connection.
        if browser_ws is websocket:
            browser_ws = None
            # Wake up any pending requests so they do not hang forever.
            for queue in response_channels.values():
                await queue.put({"error": "Browser disconnected during operation"})
            response_channels.clear()
            logger.info("WebSocket connection cleaned up.")


# --- OpenAI-compatible API endpoints ---
@app.get("/v1/models")
async def get_models():
    """Serve an OpenAI-compatible model list."""
    if not MODEL_NAME_TO_ID_MAP:
        return JSONResponse(
            status_code=404,
            content={"error": "The model list is empty or 'models.json' was not found."}
        )

    return {
        "object": "list",
        "data": [
            {
                "id": model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "LMArenaBridge"
            }
            for model_name in MODEL_NAME_TO_ID_MAP.keys()
        ],
    }


@app.post("/internal/request_model_update")
async def request_model_update():
    """
    Receive a request from model_updater.py and instruct the Tampermonkey
    script (via WebSocket) to send the page source.
    """
    if not browser_ws:
        logger.warning("MODEL UPDATE: update request received, but no browser is connected.")
        raise HTTPException(status_code=503, detail="Browser client not connected.")

    try:
        logger.info("MODEL UPDATE: request received; sending command via WebSocket...")
        await browser_ws.send_text(json.dumps({"command": "send_page_source"}))
        logger.info("MODEL UPDATE: 'send_page_source' command sent successfully.")
        return JSONResponse({"status": "success", "message": "Request to send page source sent."})
    except Exception as e:
        logger.error(f"MODEL UPDATE: error while sending the command: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send command via WebSocket.")


@app.post("/internal/update_available_models")
async def update_available_models_endpoint(request: Request):
    """
    Receive the page HTML from the Tampermonkey script, extract the models
    and update available_models.json.
    """
    html_content = await request.body()
    if not html_content:
        logger.warning("Model update request contained no HTML.")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "No HTML content received."}
        )

    logger.info("Received page content from the Tampermonkey script; extracting available models...")
    new_models_list = extract_models_from_html(html_content.decode('utf-8', errors='replace'))

    if new_models_list:
        save_available_models(new_models_list)
        return JSONResponse({"status": "success", "message": "Available models file updated."})
    else:
        logger.error("Could not extract model data from the HTML provided by the Tampermonkey script.")
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Could not extract model data from HTML."}
        )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    Handle chat completion requests.
    Receives an OpenAI-format request, converts it to the LMArena format,
    sends it to the Tampermonkey script over WebSocket, then streams the
    result back.
    """
    global last_activity_time
    last_activity_time = datetime.now()  # Update the activity timestamp.

    try:
        openai_req = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON request body.")

    model_name = openai_req.get("model")
    logger.info(f"API request received for model '{model_name}'.")
    model_info = MODEL_NAME_TO_ID_MAP.get(model_name, {})  # Empty dict for unknown models instead of None.
    model_type = model_info.get("type", "text")  # Defaults to text.

    # --- Route by model type ---
    if model_type == 'image':
        logger.info(f"Model '{model_name}' is of type 'image'; it will be handled through the main chat pipeline.")
        # Image models no longer need a separate handler: _process_lmarena_stream
        # already understands image payloads, so image generation natively
        # supports both streaming and non-streaming responses.

    # Reload the latest configuration so session IDs etc. are always current.
    load_config(verbose=False)

    # --- API key validation ---
    api_key = CONFIG.get("api_key")
    if api_key:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            raise HTTPException(
                status_code=401,
                detail="No API key provided. Please send it in the Authorization header as 'Bearer YOUR_KEY'."
            )

        provided_key = auth_header.split(' ')[1]
        if provided_key != api_key:
            raise HTTPException(
                status_code=401,
                detail="The provided API key is incorrect."
            )

    if not browser_ws:
        raise HTTPException(
            status_code=503,
            detail="The Tampermonkey client is not connected. Make sure an LMArena page is open and the script is active."
        )

    # --- Model-to-session-ID mapping logic ---
    session_id, message_id = None, None
    mode_override, battle_target_override = None, None

    if model_name and model_name in MODEL_ENDPOINT_MAP:
        mapping_entry = MODEL_ENDPOINT_MAP[model_name]
        selected_mapping = None

        if isinstance(mapping_entry, list) and mapping_entry:
            selected_mapping = random.choice(mapping_entry)
            logger.info(f"Randomly selected one mapping from the ID pool for model '{model_name}'.")
        elif isinstance(mapping_entry, dict):
            selected_mapping = mapping_entry
            logger.info(f"Found a single endpoint mapping (legacy format) for model '{model_name}'.")

        if selected_mapping:
            session_id = selected_mapping.get("session_id")
            message_id = selected_mapping.get("message_id")
            # Importantly, also pick up the mode information.
            mode_override = selected_mapping.get("mode")  # May be None.
            battle_target_override = selected_mapping.get("battle_target")  # May be None.
            log_msg = f"Using Session ID: ...{session_id[-6:] if session_id else 'N/A'}"
            if mode_override:
                log_msg += f" (mode: {mode_override}"
                if mode_override == 'battle':
                    log_msg += f", target: {battle_target_override or 'A'}"
                log_msg += ")"
            logger.info(log_msg)

    # If session_id is still None, fall back to the global IDs.
    if not session_id:
        if CONFIG.get("use_default_ids_if_mapping_not_found", True):
            session_id = CONFIG.get("session_id")
            message_id = CONFIG.get("message_id")
            # When using the global IDs, do not override the mode; use the global setting.
            mode_override, battle_target_override = None, None
            logger.info(f"No valid mapping found for model '{model_name}'; using the global default Session ID per config: ...{session_id[-6:] if session_id else 'N/A'}")
        else:
            logger.error(f"No valid mapping found for model '{model_name}' in 'model_endpoint_map.json', and the fallback to default IDs is disabled.")
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Model '{model_name}' has no dedicated session ID configured. Add a valid mapping in "
                    f"'model_endpoint_map.json' or enable 'use_default_ids_if_mapping_not_found' in 'config.jsonc'."
                )
            )

    # --- Validate the final session information ---
    if not session_id or not message_id or "YOUR_" in session_id or "YOUR_" in message_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "The resolved session ID or message ID is invalid. Check the configuration in "
                "'model_endpoint_map.json' and 'config.jsonc', or run `id_updater.py` to refresh the defaults."
            )
        )

    if not model_name or model_name not in MODEL_NAME_TO_ID_MAP:
        logger.warning(f"Requested model '{model_name}' is not in models.json; the request will be sent without a specific model ID.")

    request_id = str(uuid.uuid4())
    response_channels[request_id] = asyncio.Queue()
    logger.info(f"API CALL [ID: {request_id[:8]}]: response channel created.")

    try:
        # 1. Convert the request, passing along any mode overrides.
        lmarena_payload = convert_openai_to_lmarena_payload(
            openai_req,
            session_id,
            message_id,
            mode_override=mode_override,
            battle_target_override=battle_target_override
        )

        # 2. Wrap it into the message sent to the browser.
        message_to_browser = {
            "request_id": request_id,
            "payload": lmarena_payload
        }

        # 3. Send it over the WebSocket.
        logger.info(f"API CALL [ID: {request_id[:8]}]: sending payload to the Tampermonkey script via WebSocket.")
        await browser_ws.send_text(json.dumps(message_to_browser))

        # 4. Decide the response type based on the 'stream' parameter.
        is_stream = openai_req.get("stream", False)

        if is_stream:
            # Return a streaming response.
            return StreamingResponse(
                stream_generator(request_id, model_name or "default_model"),
                media_type="text/event-stream"
            )
        else:
            # Return a non-streaming response.
            return await non_stream_response(request_id, model_name or "default_model")
    except Exception as e:
        # If something failed during setup, clean up the channel.
        if request_id in response_channels:
            del response_channels[request_id]
        logger.error(f"API CALL [ID: {request_id[:8]}]: fatal error while processing the request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# --- Internal communication endpoints ---
@app.post("/internal/start_id_capture")
async def start_id_capture():
    """
    Receive a notification from id_updater.py and instruct the Tampermonkey
    script (via WebSocket) to activate ID-capture mode.
    """
    if not browser_ws:
        logger.warning("ID CAPTURE: activation request received, but no browser is connected.")
        raise HTTPException(status_code=503, detail="Browser client not connected.")

    try:
        logger.info("ID CAPTURE: activation request received; sending command via WebSocket...")
        await browser_ws.send_text(json.dumps({"command": "activate_id_capture"}))
        logger.info("ID CAPTURE: activation command sent successfully.")
        return JSONResponse({"status": "success", "message": "Activation command sent."})
    except Exception as e:
        logger.error(f"ID CAPTURE: error while sending the activation command: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to send command via WebSocket.")


# --- Main entry point ---
if __name__ == "__main__":
    # Host and port are read from config.jsonc (with sensible defaults).
    load_config(verbose=False)
    api_host = str(CONFIG.get("server_host", "127.0.0.1"))
    api_port = int(CONFIG.get("server_port", 5102))

    logger.info("🚀 Starting the LMArena Bridge API server...")
    logger.info(f"   - Listen address: http://{api_host}:{api_port}")
    logger.info(f"   - WebSocket endpoint: ws://{api_host}:{api_port}/ws")

    uvicorn.run(app, host=api_host, port=api_port)

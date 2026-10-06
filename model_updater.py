# model_updater.py
#
# Refreshes available_models.json - the reference list of every model currently
# offered on Arena.ai (formerly LMArena).
#
# Primary path (recommended): fetch the public model catalog API directly:
#     GET https://arena.ai/nextjs-api/model-catalog
# This needs NO browser, NO Tampermonkey script and NO running api_server.
#
# Fallback path (legacy): ask the running api_server to instruct the browser's
# Tampermonkey script to send the page HTML, from which models are extracted.
# Use `--via-browser` to force this path.
#
# Usage:
#   python model_updater.py                # direct catalog fetch, browser fallback
#   python model_updater.py --via-browser  # force the legacy browser flow
#   python model_updater.py --direct-only  # never fall back to the browser flow

import argparse
import json
import logging
import os
import re
import sys
import time

import requests

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CATALOG_URL = "https://arena.ai/nextjs-api/model-catalog"
AVAILABLE_MODELS_PATH = "available_models.json"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def get_api_server_url() -> str:
    """Build the API server base URL, honouring 'server_port' from config.jsonc."""
    port = 5102
    try:
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
        # Strip JSONC comments before parsing.
        content = re.sub(r'//.*', '', content)
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        port = int(json.loads(content).get("server_port", port))
    except (OSError, ValueError, KeyError):
        pass  # Fall back to the default port.
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# Primary path: direct catalog fetch
# ---------------------------------------------------------------------------
def fetch_catalog_direct(timeout: int = 30):
    """
    Fetch and flatten the public Arena.ai model catalog.

    Returns a flat list of model objects (deduplicated by publicName, with an
    added "arenas" list showing every arena the model appears in), or None on
    failure.
    """
    try:
        logging.info(f"Fetching the model catalog directly from {CATALOG_URL} ...")
        response = requests.get(
            CATALOG_URL,
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        categories = response.json()
    except requests.RequestException as e:
        logging.error(f"❌ Direct catalog fetch failed: {e}")
        return None
    except ValueError as e:
        logging.error(f"❌ Direct catalog fetch returned invalid JSON: {e}")
        return None

    if not isinstance(categories, list):
        logging.error("❌ Unexpected catalog format (top level is not a list).")
        return None

    flattened: dict[str, dict] = {}
    arena_counts = {}
    for category in categories:
        if not isinstance(category, dict):
            continue
        arena = category.get("arena", "unknown")
        models = category.get("models", [])
        arena_counts[arena] = len(models)
        for model in models:
            if not isinstance(model, dict):
                continue
            public_name = model.get("publicName")
            if not public_name:
                continue
            entry = flattened.setdefault(public_name, {**model, "arenas": []})
            if arena not in entry["arenas"]:
                entry["arenas"].append(arena)

    if not flattened:
        logging.error("❌ Catalog contained no usable models.")
        return None

    for arena, count in arena_counts.items():
        logging.info(f"   - arena '{arena}': {count} models")
    return list(flattened.values())


def save_models(models_list) -> bool:
    """Write the model reference list to available_models.json."""
    try:
        with open(AVAILABLE_MODELS_PATH, 'w', encoding='utf-8') as f:
            json.dump(models_list, f, indent=4, ensure_ascii=False)
        logging.info(f"✅ '{AVAILABLE_MODELS_PATH}' updated with {len(models_list)} unique models.")
        return True
    except OSError as e:
        logging.error(f"❌ Error while writing '{AVAILABLE_MODELS_PATH}': {e}")
        return False


# ---------------------------------------------------------------------------
# Fallback path: legacy browser-based flow (through api_server + userscript)
# ---------------------------------------------------------------------------
def trigger_browser_update() -> bool:
    """Ask the main server to start the browser-based model-list update flow."""
    api_server_url = get_api_server_url()
    try:
        logging.info("Sending the model-list update request to the main server...")
        response = requests.post(f"{api_server_url}/internal/request_model_update", timeout=10)
        response.raise_for_status()

        if response.json().get("status") == "success":
            logging.info("✅ Successfully asked the server to update the model list.")
            logging.info("Make sure an Arena/LMArena page is open; the script will extract the model list from it automatically.")
            logging.info(f"The server will save the result to `{AVAILABLE_MODELS_PATH}`.")
            return True
        else:
            logging.error(f"❌ The server returned an error: {response.json().get('message')}")
            return False

    except requests.exceptions.RequestException:
        logging.error(f"❌ Could not connect to the main server ({api_server_url}).")
        logging.error("Make sure `api_server.py` is running, or use the direct catalog fetch (default).")
        return False
    except Exception as e:
        logging.error(f"Unknown error: {e}")
        return False


def wait_for_browser_result(timeout_seconds: int = 60) -> bool:
    """Poll available_models.json until it is (re)written by the server."""
    mtime_before = os.path.getmtime(AVAILABLE_MODELS_PATH) if os.path.exists(AVAILABLE_MODELS_PATH) else None
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if os.path.exists(AVAILABLE_MODELS_PATH):
            if mtime_before is None or os.path.getmtime(AVAILABLE_MODELS_PATH) > mtime_before:
                logging.info(f"✅ `{AVAILABLE_MODELS_PATH}` has been updated. You can now copy models into `models.json`.")
                return True
        time.sleep(1)
    logging.warning(f"⚠️ `{AVAILABLE_MODELS_PATH}` was not updated within {timeout_seconds}s.")
    logging.warning("   Check that an Arena page is open and the Tampermonkey script is connected (page title starts with ✅).")
    logging.warning("   Note: the site no longer embeds model data in its HTML on every page — open a chat/arena page,")
    logging.warning("   or simply use the default direct catalog fetch instead of --via-browser.")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh available_models.json from Arena.ai.")
    parser.add_argument("--via-browser", action="store_true",
                        help="force the legacy browser-based flow (requires api_server.py + an open Arena tab)")
    parser.add_argument("--direct-only", action="store_true",
                        help="never fall back to the browser flow if the direct fetch fails")
    args = parser.parse_args()

    if args.via_browser:
        ok = trigger_browser_update() and wait_for_browser_result()
        return 0 if ok else 1

    models = fetch_catalog_direct()
    if models and save_models(models):
        logging.info("Done. Copy the models you want into `models.json` as \"publicName\": \"id\" pairs")
        logging.info("   (append \":image\" to the id for text-to-image models).")
        return 0

    if args.direct_only:
        return 1

    logging.warning("Direct fetch unavailable; falling back to the browser-based flow...")
    ok = trigger_browser_update() and wait_for_browser_result()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

# model_updater.py
#
# Triggers the model-list update flow on the main API server. The server asks
# the Tampermonkey script for the current LMArena page source, extracts every
# available model from it and saves the result to available_models.json.

import logging
import os
import time

import requests

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

AVAILABLE_MODELS_PATH = "available_models.json"


def get_api_server_url() -> str:
    """Build the API server base URL, honouring 'server_port' from config.jsonc."""
    port = 5102
    try:
        import json
        import re
        with open('config.jsonc', 'r', encoding='utf-8') as f:
            content = f.read()
        # Strip JSONC comments before parsing.
        content = re.sub(r'//.*', '', content)
        content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
        port = int(json.loads(content).get("server_port", port))
    except (OSError, ValueError, KeyError):
        pass  # Fall back to the default port.
    return f"http://127.0.0.1:{port}"


def trigger_model_update() -> bool:
    """Ask the main server to start the model-list update flow."""
    api_server_url = get_api_server_url()
    try:
        logging.info("Sending the model-list update request to the main server...")
        response = requests.post(f"{api_server_url}/internal/request_model_update", timeout=10)
        response.raise_for_status()

        if response.json().get("status") == "success":
            logging.info("✅ Successfully asked the server to update the model list.")
            logging.info("Make sure an LMArena page is open; the script will extract the latest model list from it automatically.")
            logging.info(f"The server will save the result to `{AVAILABLE_MODELS_PATH}`.")
            return True
        else:
            logging.error(f"❌ The server returned an error: {response.json().get('message')}")
            return False

    except requests.exceptions.RequestException:
        logging.error(f"❌ Could not connect to the main server ({api_server_url}).")
        logging.error("Make sure `api_server.py` is running.")
        return False
    except Exception as e:
        logging.error(f"Unknown error: {e}")
        return False


def wait_for_result(timeout_seconds: int = 60) -> None:
    """Poll available_models.json until it is (re)written, so the user gets feedback."""
    mtime_before = os.path.getmtime(AVAILABLE_MODELS_PATH) if os.path.exists(AVAILABLE_MODELS_PATH) else None
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if os.path.exists(AVAILABLE_MODELS_PATH):
            if mtime_before is None or os.path.getmtime(AVAILABLE_MODELS_PATH) > mtime_before:
                logging.info(f"✅ `{AVAILABLE_MODELS_PATH}` has been updated. You can now copy models into `models.json`.")
                return
        time.sleep(1)
    logging.warning(f"⚠️ `{AVAILABLE_MODELS_PATH}` was not updated within {timeout_seconds}s.")
    logging.warning("   Check that an LMArena page is open and the Tampermonkey script is connected (page title starts with ✅).")


if __name__ == "__main__":
    if trigger_model_update():
        wait_for_result()
    # Exit automatically once the script is done.

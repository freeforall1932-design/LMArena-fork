# id_updater.py
#
# A one-shot HTTP listener that receives the session information captured by
# the Tampermonkey script - according to the mode chosen by the user
# (DirectChat or Battle) - and writes it into config.jsonc.

import http.server
import json
import os
import re
import socketserver
import sys
import threading

import requests

# --- Configuration ---
HOST = "127.0.0.1"
PORT = 5103
CONFIG_PATH = 'config.jsonc'


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
                while i < n and text[i] != '\n':
                    i += 1
            elif ch == '/' and i + 1 < n and text[i + 1] == '*':
                i += 2
                while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                    i += 1
                i = min(i + 2, n)
            else:
                result.append(ch)
                i += 1
    return "".join(result)


def read_config():
    """Read and parse config.jsonc, stripping comments before parsing."""
    if not os.path.exists(CONFIG_PATH):
        print(f"❌ Error: the configuration file '{CONFIG_PATH}' does not exist.")
        return None
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.loads(strip_json_comments(f.read()))
    except (OSError, json.JSONDecodeError) as e:
        print(f"❌ Error while reading or parsing '{CONFIG_PATH}': {e}")
        return None


def save_config_value(key, value):
    """
    Safely update a single key/value pair in config.jsonc while preserving the
    original formatting and comments. Only works for string values.
    """
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            content = f.read()

        # Replace the value of "key": "..." using a regex.
        # - json.dumps()[1:-1] JSON-escapes the value (quotes, backslashes...).
        # - A lambda replacement prevents regex group references (\g<1>, \1)
        #   inside the value from being interpreted by re.subn.
        escaped_value = json.dumps(str(value))[1:-1]
        pattern = re.compile(rf'("{key}"\s*:\s*")[^"]*(")')
        new_content, count = pattern.subn(lambda m: m.group(1) + escaped_value + m.group(2), content, count=1)

        if count == 0:
            print(f"🤔 Warning: could not find the key '{key}' in '{CONFIG_PATH}'.")
            return False

        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return True
    except (OSError, re.error) as e:
        print(f"❌ Error while updating '{CONFIG_PATH}': {e}")
        return False


def save_session_ids(session_id, message_id):
    """Write the new session IDs into config.jsonc."""
    print(f"\n📝 Attempting to write the IDs into '{CONFIG_PATH}'...")
    res1 = save_config_value("session_id", session_id)
    res2 = save_config_value("message_id", message_id)
    if res1 and res2:
        print("✅ IDs updated successfully.")
        print(f"   - session_id: {session_id}")
        print(f"   - message_id: {message_id}")
    else:
        print("❌ Failed to update the IDs. Please check the error messages above.")


class RequestHandler(http.server.BaseHTTPRequestHandler):
    """
    Minimal request handler for the capture listener.

    It deliberately derives from BaseHTTPRequestHandler (not
    SimpleHTTPRequestHandler) so it never serves files from disk.
    """

    def _send_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _send_json(self, status_code: int, body: bytes):
        self.send_response(status_code)
        self._send_cors_headers()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_POST(self):
        if self.path == '/update':
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                post_data = self.rfile.read(content_length)
                data = json.loads(post_data)

                session_id = data.get('sessionId')
                message_id = data.get('messageId')

                if session_id and message_id:
                    print("\n" + "=" * 50)
                    print("🎉 Successfully captured the IDs from the browser!")
                    print(f"  - Session ID: {session_id}")
                    print(f"  - Message ID: {message_id}")
                    print("=" * 50)

                    save_session_ids(session_id, message_id)

                    self._send_json(200, b'{"status": "success"}')

                    print("\nTask complete; the server will shut down automatically in 1 second.")
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    self._send_json(400, b'{"error": "Missing sessionId or messageId"}')
            except (ValueError, json.JSONDecodeError) as e:
                self._send_json(500, json.dumps({"error": f"Internal server error: {e}"}).encode('utf-8'))
        else:
            self._send_json(404, b'{"error": "Not Found"}')

    def do_GET(self):
        self._send_json(404, b'{"error": "Not Found"}')

    def log_message(self, format, *args):
        # Silence the default per-request access log.
        return


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    """TCPServer that allows immediate rebinding and handles requests in threads."""
    allow_reuse_address = True
    daemon_threads = True


def run_server():
    with ReusableTCPServer((HOST, PORT), RequestHandler) as httpd:
        print("\n" + "=" * 50)
        print("  🚀 Session-ID update listener started")
        print(f"  - Listening on: http://{HOST}:{PORT}")
        print("  - Interact with the LMArena page in your browser to trigger the ID capture.")
        print("  - This script shuts down automatically after a successful capture.")
        print("=" * 50)
        httpd.serve_forever()


def notify_api_server(api_port: int = 5102):
    """Notify the main API server that the ID-update flow has started."""
    api_server_url = f"http://127.0.0.1:{api_port}/internal/start_id_capture"
    try:
        response = requests.post(api_server_url, timeout=5)
        if response.status_code == 200:
            print("✅ Successfully notified the main server to activate ID-capture mode.")
            return True
        else:
            print(f"⚠️ Failed to notify the main server; status code: {response.status_code}.")
            print(f"   - Error message: {response.text}")
            return False
    except requests.ConnectionError:
        print("❌ Could not connect to the main API server. Make sure api_server.py is running.")
        return False
    except requests.RequestException as e:
        print(f"❌ Unknown error while notifying the main server: {e}")
        return False


if __name__ == "__main__":
    config = read_config()
    if not config:
        sys.exit(1)

    # --- Get the user's choice ---
    last_mode = config.get("id_updater_last_mode", "direct_chat")
    mode_map = {"a": "direct_chat", "b": "battle"}

    prompt = f"Select a mode [a: DirectChat, b: Battle] (default: last used = {last_mode}): "
    choice = input(prompt).lower().strip()

    if not choice:
        mode = last_mode
    else:
        mode = mode_map.get(choice)
        if not mode:
            print(f"Invalid input; using the default: {last_mode}")
            mode = last_mode

    save_config_value("id_updater_last_mode", mode)
    print(f"Current mode: {mode.upper()}")

    if mode == 'battle':
        last_target = config.get("id_updater_battle_target", "A")
        target_prompt = f"Select the message to update [A (required for search models) or B] (default: last used = {last_target}): "
        target_choice = input(target_prompt).upper().strip()

        if not target_choice:
            target = last_target
        elif target_choice in ("A", "B"):
            target = target_choice
        else:
            print(f"Invalid input; using the default: {last_target}")
            target = last_target

        save_config_value("id_updater_battle_target", target)
        print(f"Battle target: Assistant {target}")
        print("Note: whether you choose A or B, the captured IDs are written to the main session_id and message_id.")

    # Notify the main server before starting the listener.
    api_port = int(config.get("server_port", 5102))
    if notify_api_server(api_port):
        run_server()
        print("Server closed.")
    else:
        print("\nID-update flow aborted because the main server could not be notified.")
        sys.exit(1)

# update_script.py
#
# Applies a downloaded update: copies the new program files over the current
# installation, intelligently merges config.jsonc (preserving comments and
# user values), cleans up the temporary download folder and restarts the
# main program.

import json
import os
import re
import shutil
import subprocess
import sys
import time

# Project root, derived from this file's location (<root>/modules/update_script.py)
# so the script works no matter which working directory it was launched from.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def load_jsonc_values(path):
    """Load data from a .jsonc file, ignoring comments, and return the key/value pairs."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.loads(strip_json_comments(f.read()))
    except (OSError, json.JSONDecodeError) as e:
        print(f"Error while loading or parsing values from {path}: {e}")
        return None


def find_source_dir(update_dir: str):
    """
    Locate the extracted repository folder inside the update directory.
    GitHub archives unpack into a single '<repo>-<branch>' folder; detect it
    dynamically instead of hard-coding the repository name.
    """
    if not os.path.isdir(update_dir):
        return None
    entries = [
        os.path.join(update_dir, name)
        for name in os.listdir(update_dir)
        if os.path.isdir(os.path.join(update_dir, name))
    ]
    if len(entries) == 1:
        return entries[0]
    # Fall back to the classic GitHub archive naming convention.
    for entry in entries:
        if os.path.basename(entry).endswith('-main'):
            return entry
    return entries[0] if entries else None


def main():
    print("--- Update script started ---")

    # 1. Wait for the main program to exit.
    print("Waiting for the main program to shut down (3 seconds)...")
    time.sleep(3)

    # 2. Define paths.
    destination_dir = PROJECT_ROOT
    update_dir = os.path.join(PROJECT_ROOT, "update_temp")
    source_dir_inner = find_source_dir(update_dir)
    config_filename = 'config.jsonc'
    models_filename = 'models.json'
    model_endpoint_map_filename = 'model_endpoint_map.json'

    if not source_dir_inner:
        print(f"Error: could not find the extracted source directory inside {update_dir}. Update failed.")
        return

    print(f"Source directory:      {os.path.abspath(source_dir_inner)}")
    print(f"Destination directory: {os.path.abspath(destination_dir)}")

    # 3. Back up the current configuration values.
    print("Backing up the current configuration values...")
    old_config_path = os.path.join(destination_dir, config_filename)
    old_config_values = load_jsonc_values(old_config_path)

    # 4. Copy the new files (except user-managed configuration files).
    # Note: file deletion is intentionally disabled to protect user data;
    # this script only copies files and merges the configuration.
    print("\n--- File change policy ---")
    print("[*] File deletion is disabled to protect user data. Only file copying and config merging are performed.")

    print("\n[+] Copying new files...")
    new_config_template_path = os.path.join(source_dir_inner, config_filename)
    try:
        for item in os.listdir(source_dir_inner):
            s = os.path.join(source_dir_inner, item)
            d = os.path.join(destination_dir, item)

            # Skip VCS metadata directories.
            if item in {".git", ".github"}:
                continue

            if os.path.basename(s) == config_filename:
                continue  # Skip the main config file; it is merged later.

            if os.path.basename(s) == model_endpoint_map_filename:
                continue  # Skip the model endpoint map; keep the user's local version.

            if os.path.basename(s) == models_filename:
                continue  # Skip models.json; keep the user's local version.

            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        print("Files copied successfully.")

    except OSError as e:
        print(f"Error while copying files: {e}")
        return

    # 5. Intelligently merge the configuration.
    if old_config_values and os.path.exists(new_config_template_path):
        print("\n[*] Merging configuration intelligently (preserving comments)...")
        try:
            with open(new_config_template_path, 'r', encoding='utf-8') as f:
                new_config_content = f.read()

            new_version_values = load_jsonc_values(new_config_template_path) or {}
            new_version = new_version_values.get("version", "unknown")
            old_config_values["version"] = new_version

            for key, value in old_config_values.items():
                if isinstance(value, str):
                    # json.dumps escapes quotes/backslashes and adds the surrounding quotes.
                    replacement_value = json.dumps(value)
                elif isinstance(value, bool):
                    replacement_value = str(value).lower()
                else:
                    replacement_value = str(value)

                pattern = re.compile(f'("{key}"\\s*:\\s*)(?:".*?"|true|false|[\\d\\.]+)')
                if pattern.search(new_config_content):
                    # Use a lambda so backslashes / group references inside the
                    # value cannot corrupt the replacement.
                    new_config_content = pattern.sub(lambda m: m.group(1) + replacement_value, new_config_content)

            with open(old_config_path, 'w', encoding='utf-8') as f:
                f.write(new_config_content)
            print("Configuration merged successfully.")

        except (OSError, re.error) as e:
            print(f"Critical error while merging the configuration: {e}")
    else:
        print("Intelligent merge not possible; using the new config file directly.")
        if os.path.exists(new_config_template_path):
            shutil.copy2(new_config_template_path, old_config_path)

    # 6. Clean up the temporary folder.
    print("\n[*] Cleaning up temporary files...")
    try:
        shutil.rmtree(update_dir, ignore_errors=True)
        print("Cleanup complete.")
    except OSError as e:
        print(f"Error while cleaning up temporary files: {e}")

    # 7. Restart the main program.
    print("\n[*] Restarting the main program...")
    main_script_path = os.path.join(destination_dir, "api_server.py")
    try:
        if not os.path.exists(main_script_path):
            print(f"Error: the main program script {main_script_path} was not found.")
            return

        subprocess.Popen([sys.executable, main_script_path], cwd=destination_dir)
        print("The main program has been restarted in the background.")
    except OSError as e:
        print(f"Failed to restart the main program: {e}")
        print(f"Please run it manually: {main_script_path}")

    print("--- Update complete ---")


if __name__ == "__main__":
    main()

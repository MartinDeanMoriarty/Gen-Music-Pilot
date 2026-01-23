import os
import json
import time
import requests 

from utils.filesystem import load_config

# Load configs
config = load_config("comfy")
COMFY_URL = config["comfy_url"]
OUTPUT_DIR = config["output_dir"]

# Help methods
# ------------------------------------------------------

def load_workflow(workflow_path):
    with open(workflow_path, "r", encoding="utf-8") as f:
        return json.load(f)

def wait_for_file_ready(path, timeout=220):
    for i in range(timeout):
        exists = os.path.exists(path)
        size = os.path.getsize(path) if exists else 0
        print(f"⏱️ File checking {i+1}/{timeout}: exists={exists}, size={size}, path={path}")
        if exists and size > 0:
            return True
        time.sleep(1)
    return False

def send_prompt(prompt):
    response = requests.post(f"{COMFY_URL}/prompt", json={"prompt": prompt})
    response.raise_for_status()
    prompt_id = response.json()["prompt_id"]

    print(f"Prompt send with ID: {prompt_id}")

    # Wait to finish
    while True:
        time.sleep(1)
        hist = requests.get(f"{COMFY_URL}/history/{prompt_id}").json()
        data = hist.get(prompt_id)
        if not data:
            print(f"⚠️ Still no data for Prompt-ID: {prompt_id}")
            continue

        status_obj = data.get("status", {})
        status_str = status_obj.get("status_str", "")

        print(f"⏳ State: {status_str}")

        if status_str.lower() in ("completed", "success"):
            print(f"🎯 Prompt success with state: {status_str}")
            break
        elif status_str == "error":
            raise RuntimeError(f"❌ Prompt error: {status_obj}")
        else:
            continue

    # Try, to add audio file
    output_files = []
    try:
        for node_id, node_data in data.get("outputs", {}).items():            
            print("📦 Outputs:", json.dumps(data.get("outputs", {}), indent=2))            
            # Check directly
            if "audio" in node_data:
                for entry in node_data["audio"]:
                    if isinstance(entry, dict) and "filename" in entry and "subfolder" in entry:
                        subfolder = entry.get("subfolder", "")
                        full_path = os.path.join(OUTPUT_DIR, subfolder, entry["filename"])
                        output_files.append(full_path)
    except Exception:
        pass

    # Outside try/except:
    if output_files:        
        audio_path = output_files[0]
        if wait_for_file_ready(audio_path):
            print("🎵 Audio-File ready:", audio_path)
            return audio_path
        else:
            print("⚠️ Audio-file did not finish!")
            raise RuntimeError("Audio-file did not finish.")    

# Workflow-Checking & Input-Replacement
# ------------------------------------------------------

def run_workflow(workflow_path, inputs):
    raw = load_workflow(workflow_path)

    # Check if it is an API-Export
    if isinstance(raw.get("nodes"), list):
        raise ValueError(
            f"'{workflow_path}' might be a GUI-Workflow.\n"
            "Please use in ComfyUI: File → Export → API."
        )

    # API-compatible prompt
    prompt = raw.copy()

    # Replace (Based on Node-Titles)
    for node_id, node in prompt.items():
        # Especially for SaveAudioMP3
        if node.get("class_type") == "SaveAudioMP3" and "filename_prefix" in inputs:
            node["inputs"]["filename_prefix"] = inputs["filename_prefix"]
            print(f"🎯 filename_prefix set: {node['inputs']['filename_prefix']}")

        # Replace other entries
        meta = node.get("_meta", {})
        title = meta.get("title", "").strip()
        if title in inputs:
            new_val = inputs[title]
            if "inputs" in node and "value" in node["inputs"]:
                node["inputs"]["value"] = new_val
                print(f"Replaced entry: {title} → {new_val}")
            elif "inputs" in node:
                for k, v in node["inputs"].items():
                    if isinstance(v, str) or v == "":
                        node["inputs"][k] = new_val
                        print(f"Replaced entry: {title}.{k} → {new_val}")
                        break

    # Prompt debug
    with open("logs/debug_prompt.json", "w", encoding="utf-8") as f:
        json.dump(prompt, f, indent=2, ensure_ascii=False)

    return send_prompt(prompt)

# Convenience-Functions
# ------------------------------------------------------

def generate_music(song_name, genre_desc, length, filename_prefix="audio/music"):
    return run_workflow("workflows/Radio_Music+Lyrics.json", {
        "Song Name": song_name,
        "Song Genre & Description": genre_desc,
        "Song Length - Seconds": length,
        "filename_prefix": filename_prefix
    })

def generate_moderator(song_name, artist, genre_desc, length, interaction, filename_prefix="audio/moderator"):
    return run_workflow("workflows/Radio_TTS.json", {
        "Song Name": song_name,
        "Song Interpret": artist,
        "Song Genre & Description": genre_desc,
        "Song Length - Seconds": length,
        "Caller Interaction": interaction,
        "filename_prefix": filename_prefix
    })

def generate_story(song_name, artist, genre_desc, length, interaction, weather, filename_prefix="audio/story"):
    return run_workflow("workflows/Radio_Story.json", {
        "Song Name": song_name,
        "Song Interpret": artist,
        "Song Genre & Description": genre_desc,
        "Song Length - Seconds": length,
        "Caller Interaction": interaction,
        "Weather": weather,
        "filename_prefix": filename_prefix
    })

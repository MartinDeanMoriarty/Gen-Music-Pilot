import os 
import sys
import json
import random

def get_resource_path():
    #Works with AppImage and python venv as well
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return base_path

def load_json_file(filename):
    path = os.path.join(get_resource_path(), f"../assets/{filename}")    
    if not os.path.exists(path):
        print(f"❌ No Song data files found.")
        raise FileNotFoundError(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        print(f"☝️ Asset loaded: {filename}")
        return json.load(f)

def load_song_data():
    names = load_json_file("names.json")
    artists = load_json_file("artists.json")
    genres = load_json_file("genres.json")
    return names, artists, genres

def random_mix(names, artists, genres):
    return {
        "song_name": random.choice(names),
        "artist": random.choice(artists),
        "genre_desc": random.choice(genres)
    }

def load_config(module):
    #Load and validate config.json     
    config_path = os.path.join(get_resource_path(), "../config/config.json")

    if not os.path.exists(config_path):
        print(f"❌ Config file not found.")
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path, "r", encoding="utf-8") as file:        
        config = json.load(file)
        print(f"☝️ Config loaded for: {module}")
    
    required_keys = ["comfy_url", "output_dir"]
    for key in required_keys:
        if key not in config:
            print(f"❌ Config file found.")
            raise KeyError(f"Missing config value: {key}")

    return config

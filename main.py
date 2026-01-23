import os
import sys
import time
import random
import datetime
import threading
import gradio as gr
import requests 
from version import __version__ 
print(f"📻 Starting Gen-Music-Pilot v{__version__} ❤️")
from stream_server import start_stream_server, stop_stream_server
from utils.player_instance import player
from utils.filesystem import random_mix, load_config, load_song_data
from comfy_api import generate_music, generate_moderator, generate_story
from packaging import version

# Load configs
config = load_config("main")
FALLBACK_DIR = config["fallback_dir"]
WEATHER_API_KEY = config["openweathermap_apikey"]
WEATHER_LOCATION = config["location"]
NAMES, ARTISTS, GENRES = load_song_data()
STREAMING = config["streaming"] # True or False
AUDIO_PLAYER = config["audio_player"] # True or False
print(f"ℹ️  Audio Player: {AUDIO_PLAYER}")
print(f"ℹ️  Streaming: {STREAMING}")
missing = [
    name for name, val in {
        "WEATHER_LOCATION": WEATHER_LOCATION,
        "FALLBACK_DIR": FALLBACK_DIR,
        "STREAMING": STREAMING,
        "AUDIO_PLAYER": AUDIO_PLAYER,
        "NAMES": NAMES,
        "ARTISTS": ARTISTS,
        "GENRES": GENRES
    }.items()
    if val is None or val == "" or val == []
]
if missing:      
    print(f"❌ Error: The Following is missing: {', '.join(missing)}")
    print(f"⬆️")
else:
    if version.parse(gr.__version__) < version.parse("4.0.0"):         
        print(f"⚠️ Gradio version to low, please update at least to 4.0.0")
        print(f"ℹ️  Gradio version: {gr.__version__} ")        
    else:
        if (STREAMING == False) and (AUDIO_PLAYER == False):
            print(f"ℹ️ ❌ 🔇 No Steaming and no audio output! Change at least one of those settings! ") 
        else:            
            print(f"☝️ 🤓 Seems legit...")
            print(f"ℹ️  Make sure ComfyUI is running! ")
            print(f"⬇️  Open your browser and visit the gui.")

# Variable for weather cache
cached_weather = None
last_query_time = 0  # Unix-Timestamp
cache_valid_duration = 3600  # 1 Hour    

user_interaction = "None"
progress_state = "Not started yet."

# Background-Thread for Story-Generation
def story_scheduler():
    while player.is_playing():
        now = datetime.datetime.now()
        print(f"🕵️ No story time at: {now.hour}:{now.minute}")
        if now.minute in [0, 30]:
            player.set_story_time(True)
            print(f"🕵️ Story time detected at: {now.hour}:{now.minute}")
        time.sleep(60)

# Background-Thread for managing a clips queue
def queue_manager(min_clips=4, check_interval=5):
    global progress_state
    while player.is_playing():
        time.sleep(check_interval)
        current = player.queue_length()
        print(f"🎚️ Queue-Check: {current} Clips")        
        if current < min_clips:
            print("🔁 Generate new batch...")
            # Generate batch
            # High chance to switch to mode 1 if in mode 2
            mode = player.get_mode() 
            if (mode == 2) and (get_random(0, 100) < 90):
                mode = 1    
            # Slim chance to switch to mode 2 but not if a story is in queue
            if (player.get_story_time() == False) and (get_random(0, 100) < 10):
                mode = 2
            else:
                mode = 1
            player.set_mode(mode) 
            mode = player.get_mode()           
            # Get random song data
            song_data = random_mix(NAMES, ARTISTS, GENRES)            
            # Set Song Length random between 2 and 5 Minutes
            length_value = get_random(120, 300)
            # Get weather data            
            weather_value = get_weather(WEATHER_API_KEY, WEATHER_LOCATION)
            # Get user interaction
            actual_interaction = get_user_interaction()
            # Reset user interaction
            reset_user_interaction() 
            print(f"ℹ️ Mode: {mode}, Random mix:{song_data}, Length: {length_value}, Weather: {weather_value}, Injection: {actual_interaction}")
            generate_batch(
                song_data["song_name"],
                song_data["artist"],
                song_data["genre_desc"],
                length_value,
                weather_value,
                mode,
                actual_interaction
            )
        else:
            progress_state = "Resting"    

# Generates a batch of two, moderator and music 
def generate_batch(song_name, artist, genre_desc, length, weather, mode, interaction):
    global progress_state
    mod_path = None
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    batch = []
    
    try:       
        # Moderator (if Mode 1)
        if mode == 1:
            # No double moderation
            if player.get_story_time() == True:  
                progress_state = "Generating Story..."                
                story_path = generate_story(song_name, artist, genre_desc, length, interaction, weather, filename_prefix=f"audio/story_{timestamp}")
                batch.append(story_path)
                player.add_to_info("Story")                 
                print("🧾 Story-Path:", story_path)
                player.set_story_time(False)
            else:           
                progress_state = "Generating Moderation..."
                mod_path = generate_moderator(song_name, artist, genre_desc, length, interaction, filename_prefix=f"audio/moderator_{timestamp}")
                batch.append(mod_path)
                player.add_to_info("Moderation")                
                print("🧾 Moderator-Path:", mod_path)

        time.sleep(1)  # Slow down a bit ;)

        # Music
        progress_state = "Generating Music..."
        music_path = generate_music(song_name, genre_desc, length, filename_prefix=f"audio/music_{timestamp}")
        batch.append(music_path)
        player.add_to_info(f"{song_name} by {artist}")
        print("🧾 Music-Path:", music_path)

        if not mod_path:
            print("❌ Moderator-file not found!")
        if not music_path:
            print("❌ Music-file not found!")

        for path in batch:
            if path and os.path.exists(path):
                progress_state = "Processing..."
                player.add_to_queue(path)                
            else:
                music_fallback()
                progress_state = "Fallback!"
                print(f"⚠️ Fallback! Path error: {path}") 

    except Exception as e:                
        music_fallback()

def music_fallback():
    prefix = "music"
    try:        
        files = [f for f in os.listdir(FALLBACK_DIR) if f.endswith(".mp3") and prefix in f.lower()]
        if not files:
            raise FileNotFoundError("⚠️ No audio file found.")
        # Sort by date (oldest first)
        files.sort(key=lambda f: os.path.getmtime(os.path.join(FALLBACK_DIR, f)), reverse=False)
        fallback_path = os.path.join(FALLBACK_DIR, files[0])        
        player.add_to_queue(fallback_path)
        print("🔄 Added fallback to queue:", fallback_path)
    except Exception as e:
        print(f"⚠️ Error: {e}")                         

def get_weather(api_key, location):
    global cached_weather, last_query_time
    
    if api_key == "":
        print(f"Error: No Api-Key!")
        return f"Weather service not available."
    
    current_time = time.time()  # Actual time in second (Unix-timestamp)
    
    # check if last request in over a hour ago 
    if cached_weather is not None and (current_time - last_query_time) < cache_valid_duration:
        print(f"Using cached data.")
        return cached_weather  # Return cached weather

    # Request new weather
    url = f"http://api.openweathermap.org/data/2.5/weather?q={location}&appid={api_key}&units=metric&lang=de"

    response = requests.get(url)
    data = response.json()

    if response.status_code == 200:
        # Cache weather for this request
        message = f"Wetter in {location}: "
        message += f"Temperatur: {data['main']['temp']}°C "
        message += f"Feuchtigkeit: {data['main']['humidity']}% "
        message += f"Windgeschwindigkeit: {data['wind']['speed']} m/s "
        
        # Save cache and time
        cached_weather = message
        last_query_time = current_time
        # Return weather
        return message
    else:
        print(f"Error: {data['message']}")
        return f"Weather service not available."
    
def get_random(start, end):
    return random.randint(start, end)

stream_thread = threading.Thread(target=start_stream_server)
def start_radio():    
    # Start queue and player output 
    player.start()    
    # Start queue manager 
    threading.Thread(target=queue_manager, daemon=True).start()  
    # Start story scheduler
    threading.Thread(target=story_scheduler, daemon=True).start()   
    # Set up gui output and start stream
    message = "🟢 Started!" 
    message += " 🔸 " 
    if AUDIO_PLAYER == True:
        message += "🔊 Audio Output"
    else:
        message += "🔇 No Audio" 
    message += " 🔸 "  
    if STREAMING == True:
        stream_thread.start()         
        message += "🛜 Stream On-Air ⟿ Connect to http://localhost:8080/live"
    else: 
        message += "❌ Stream Off-Air"           
    return message

def stop_radio():
    global progress_state
    progress_state = "Stopped!"
    print (f"🛑 Stopping everything! Comfy will still finish a batch it has started.")
    player.stop()
    stop_stream_server()     
    #stream_thread.join()    
    return "🛑 Stopping everything! Comfy will still finish a batch it has started."

# Helper function to refresh the actual state
def get_state():   
    global progress_state
    try:        
        clip_info = player.get_current_clip_info() or "Nothing"
        queue_len = player.queue_length() or "?"
        mode = player.get_mode() or "?"
        story_time = player.get_story_time() or "?"  
        complete_state_message = f"{progress_state} | ▶️ Now playing: {clip_info} | Mode: {mode} | Queue: {queue_len} | Story: {story_time}" 
        return complete_state_message
    except Exception as e:
        return progress_state

def set_user_interaction(user_injection):
    global user_interaction
    user_interaction = user_injection
    print(f"💉 Injecting: " + user_interaction)
    return f"💉 Injecting the following in next possible moderation: {user_interaction}"

def get_user_interaction():
    global user_interaction
    if user_interaction == "":
        user_interaction = "None"        
    return user_interaction

def reset_user_interaction():
    global user_interaction
    user_interaction = "None"
    print(f"💉 Injection rest: " + user_interaction)

# Auto-Refresh every 5 seconds
refresh_js = """
    <script>
    setInterval(() => {
    var refresh_btn = document.getElementById("refresh_btn")      
        refresh_btn.click();            
    }, 5000);  // every 5000ms = 5 seconds
    </script>
    """
    
# Gradio UI
with gr.Blocks(head=refresh_js, title=f"📻 Gen-Music-Pilot v{__version__}") as demo:
         
    # Title
    refresh_btn = gr.Button(f"📻 Welcome to Gen-Music-Pilot", elem_id="refresh_btn")

    with gr.Row():
        player_queue = gr.Textbox(label="🎛️ State", lines=2, interactive=False) 

    info = gr.Textbox(label="ℹ️ Info - It takes ~2 minutes on startup. Depends on the hardware!", placeholder=f"Press ▶️ Start to go On-Air!", interactive=False)
       
    start_btn = gr.Button("▶️ Start")
    stop_btn = gr.Button("⏹️ Stop")
    
    user_injection = gr.Textbox(label="📞 Caller Injection", scale=2, placeholder="Hello, my name is ***. (please greet my mother|it's my birthday|play the next song for me please)", lines=2, interactive=True)      
    injection_btn = gr.Button("💉 Inject", scale=1)    
    injection_info = gr.Textbox(label="ℹ️ Injection Info", scale=1, placeholder=f"Use 📞 Caller Injection!", interactive=False)
       
    start_btn.click(start_radio, outputs=info)
    stop_btn.click(stop_radio, outputs=info)
    injection_btn.click(set_user_interaction, inputs=user_injection, outputs=injection_info)
    injection_btn.click(lambda: None, None, user_injection, queue=False)
    refresh_btn.click(get_state, outputs=player_queue)

if __name__ == "__main__":
    demo.launch()
    if not sys.argv[1]:
        print("🪢 Argument used " + sys.argv[1])

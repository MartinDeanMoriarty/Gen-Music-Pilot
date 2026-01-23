import os 
import time
import threading
import traceback
import aiohttp

from pydub import AudioSegment
from multiprocessing import Process
from pydub.playback import _play_with_simpleaudio

def play_audio_process(path, play_audio=False):
    try:
        audio = AudioSegment.from_mp3(path)
        if play_audio == True:
            playback = _play_with_simpleaudio(audio)
            playback.wait_done()
        else:
            # Simulate the audio length
            duration = len(audio) / 1000.0  # pydub returns length in ms
            print(f"🔇 Simulate audio playback ({duration:.1f}s) von {os.path.basename(path)} ...")
            time.sleep(duration)
    except Exception as e:
        print(f"❌ Audio-Prozess error: {e}")
        traceback.print_exc()

class RadioPlayer:
    def __init__(self, audio_enabled=False):
        self.queue = []
        self.info = []
        self.lock = threading.Lock()
        self.playing = False
        self.stop_flag = False
        self.story_time = False
        self.mode = 1
        self.current_player_process = None
        self.current_clip = None
        self.current_clip_info = None
        self.audio_enabled = audio_enabled

    def add_to_queue(self, filepath):
        with self.lock:
            self.queue.append(filepath)

    def add_to_info(self, info):
        with self.lock:
            self.info.append(info)            

    def clear_queue(self):
        with self.lock:
            self.queue = [] 
            self.info = [] 

    def set_mode(self, mode_int):
        self.mode = mode_int

    def get_mode(self):
        return self.mode
    
    def get_queue(self):
        return self.queue

    def set_story_time(self, interval_bool):
        self.story_time = interval_bool

    def get_story_time(self):
        return self.story_time
    
    def get_current_clip(self):
        return self.current_clip
    
    def get_current_clip_info(self):
        return self.current_clip_info
    
    def is_playing(self):
        return self.playing

    def queue_length(self):
        with self.lock:
            return len(self.queue)

    def play_loop(self):
        self.playing = True
        while not self.stop_flag:
            next_clip = None
            next_info = None
            with self.lock:
                if self.queue:
                    next_clip = self.queue.pop(0)
                    next_info = self.info.pop(0)

            if next_clip and os.path.exists(next_clip):
                print(f"▶️ Now playing: {next_clip}")
                self.current_clip = next_clip
                self.current_clip_info = next_info
                try:
                    self.current_player_process = Process(
                        target=play_audio_process,
                        args=(next_clip, self.audio_enabled)
                    )
                    self.current_player_process.start()
                    self.current_player_process.join()
                    self.current_player_process = None
                except Exception as e:
                    print(f"❌ Audio-Process starting error: {e}")
                    traceback.print_exc()
                    continue
            else:
                time.sleep(1)
        self.playing = False

    def start(self):
        if not self.playing:
            self.stop_flag = False
            threading.Thread(target=self.play_loop, daemon=True).start()

    def stop(self):
        self.stop_flag = True
        self.clear_queue()
        if self.current_player_process and self.current_player_process.is_alive():
            print("⏹️ Audio-Process Stopped ...")
            self.current_player_process.terminate()
            self.current_player_process.join()
            self.current_player_process = None 

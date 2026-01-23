# player instance
from player import RadioPlayer
from utils.filesystem import load_config
config = load_config("player")
AUDIO_PLAYER = config["audio_player"] # True or False
player = RadioPlayer(audio_enabled=AUDIO_PLAYER)

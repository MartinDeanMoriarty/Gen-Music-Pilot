# 📻 Gen-Music-Pilot

> A fun frankenstein project.
> 
> To make it clear, this is really just a fun project and not a solution!

## Table of Contents
> [Features](#features)
>
> [Dependencies](#dependencies) 
>
> [Warning](#warning)
>
> [Installation](#installation) 
>
> [Contributing](#contributing)
>
> [License](#license)

---

## Features

><u>Run an autonomous generative local radio station.</u>
>
><u>Decide to listen directly in a browser or over network.</u>
>
>This project is possible with the power of (too) many dependencies! 
>
>~10 seconds moderation + 120 seconds music takes ~ 90 seconds on a 4070Ti 12Gb vram + 64Gb ram (depends also on settings in workflows and used ai models as well)
>
><u>ComfyUI workflows:</u><br>
>
>First workflow: A LLM (Ollama) gets random song data and generates text. Time and Date is also injected.
<br>TTS (TTS Audio Suite) generates audio of this text with a cloned (optional) voice.
>
>Second workflow: A LLM (Ollama) gets the same song data and generates lyrics. Ace-Step will then handle the lyrics and song data to generate music.
>
>A third workflow triggers around 0 and 30 Minutes hourly, for example: ~2:00 , ~2:30. You can use an other voice for this. In this workflow, weather data is injected as well. 
>
>All generated output will be saved to mp3 files and added to a queue to be played one by one.
<br>The queue will always contain 4 clips, so endless listening should be assured.   
>
><u>A so called "Caller Interaction" lets you "phone" the radio station</u><br><br>
---

## Dependencies
><b>[ComfyUi](https://www.comfy.org/)</b><br>[manual-install-windows-linux](https://github.com/comfyanonymous/ComfyUI#manual-install-windows-linux)
>
><b>[ACE-Step](https://github.com/ace-step/ACE-Step/)</b> (Models needed!)<br>
>
><b>[Ollama](https://ollama.com/)</b><br>`curl -fsSL https://ollama.com/install.sh | sh`
>
><b>[Open Weather Map](https://home.openweathermap.org/)</b><br>
[API-Key](https://home.openweathermap.org/api_keys)
>
><b>[Python3](https://www.python.org/)</b><br>`sudo apt install python3`
>
><b>[Gradio](https://www.gradio.app/)</b><br>`pip install gradio`
>
><b>[Requests](https://requests.readthedocs.io/)</b><br>`pip install requests`<br>Requests officially supports Python 3.9+
>
><b>[pydub](https://www.pydub.com/)</b><br>`pip install pydub`
>
><b>[simpleaudio](https://docs.aiohttp.org/)</b><br>`pip install simpleaudio`
>
><b>[aiohttp](https://github.com/hamiltron/py-simple-audio)</b><br>`pip install aiohttp`
>
>Custom nodes used in workflows:
>
>[ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager)
>
>[ComfyUI-Easy-Use](https://github.com/yolain/ComfyUI-Easy-Use)
>
>[ComfyUI-KJNodes](https://github.com/kijai/ComfyUI-KJNodes)
>
>[comfyui-ollama](https://github.com/stavsap/comfyui-ollama)
>
>[ComfyUI-JakeUpgrade](https://github.com/jakechai/ComfyUI-JakeUpgrade)
>
>[ComfyUI-Unload-Model](https://github.com/SeanScripts/ComfyUI-Unload-Model)
>
>[TTS Audio Suite](https://github.com/diodiogod/TTS-Audio-Suite)
>
>[ComfyUI-CitronNodes](https://github.com/citronlegacy/ComfyUI-CitronNodes)
>
>[ComfyUI-NumberToText](https://github.com/MartinDeanMoriarty/ComfyUI-NumberToText)
>
>**Thanks to all of them. Without them this app would not work!**

---
## Warning
> [!WARNING]
> Downloading an using this project means you are old enough to know the risks of the internet!
>
> **This project is NOT easy to set up and get running!
> So make sure you got some time on your hand for trouble shooting.**
---

## Installation
>The app is tested against the newest version of ComfyUI (manual installation) but TTS Audio Suite has problems with newest numpy and there might be other conflicts I am not aware of.
>
>Run the workflows found in 'workflows/origin_workflows/' with ComfyUI to make sure they are running correctly and do suit your needs, for example, you need to set the llm model for Ollama, a clone-voice, or just translate some text. 
>
>The origin_workflows files must be exported from within ComfyUi as Export-API after any change! 
>
>Any errors in workflows will break the app without a plausible error message.   
>  
>It is important to install all the missing nodes inside the workflows. <br>This can be done easily with [ComfyUI-Manager](https://github.com/Comfy-Org/ComfyUI-Manager). 
>
>Make sure to download all needed ai models.
>
>    
>Have a look at ./config/config.json and adjust it.
>
>Make sure Ollama is installed and running.
>
>It is recommended to use a virtual environment. 
>
>
><b>Using Gen-Music-Pilot:</b>
>
><b>start.sh:</b>
>
>The script handles a virtual environment to install and start the app to keep your system clean(er).
>1. Download or clone this repo:<br>`git clone [Link_To_Repo]`
>2. Edit config file!<br>`config/config.json`
>3. File permission:<br>`chmod +x start.sh`
>4. Run start.sh:<br>`./start.sh`
>
><b>Old school:</b>
>1. Download or clone this repo:<br>`git clone [Link_To_Repo]`
>2. Edit config file!<br>`config/config.json`
>3. change dir:<br>`cd Gen-Music-Pilot`
>4. Make virtual environment:<br>`python3 -m venv venv`
>5. Activate virtual environment:<br>`source venv/bin/activate`
>6. Install requirements:<br>`pip install -r requirements.txt`
>7. Start app:<br>`python3 main.py`
>
>Go to http://127.0.0.1:7860 in your browser.
>
><b>For development:</b>
>1. Download or clone this repo:<br>`git clone [Link_To_Repo]`
>2. Edit config file!<br>`config/config.json`
>3. Open project in your ide as python project
>4. Set up virtual environment and requirements.
>5. Run it in the ide.
>
>Go to http://127.0.0.1:7860 in your browser.
>

---

## Contributing
>There is not much to do since this is just for fun.
>
>But there are some improvements I will make in the future.

---

## License
>Creative Commons
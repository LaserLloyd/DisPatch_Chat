"""Drive mode: hands-free voice for DisPatch threads (unlocked tier only).

Speech runs on this machine's CPU:

* Silero VAD and Smart Turn v3.2 handle turn taking, with turn merge and a
  patience setting.
* Parakeet TDT 0.6B turns speech into text.
* A swappable TTS engine speaks the reply (Kokoro by default; see tts.py).

The transcript goes through the app's NORMAL send path, so a spoken message
lands in the thread like a typed one and any bot type can answer.

This is optional. It needs the ``voice`` dependency group and the model files
(``python -m app.voice.download``). With ``DISPATCH_VOICE=auto`` the feature
turns on only when both are present. Voice profiles, cloning and a Voices GUI
plug in through ``registry.py``. See docs/voice-drive-mode.md.
"""

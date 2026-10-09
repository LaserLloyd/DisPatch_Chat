"""Drive mode: hands-free voice for DisPatch threads (unlocked tier only).

Speech runs on this machine's CPU:

* Silero VAD and Smart Turn v3.2 handle turn taking, with turn merge and a
  patience setting.
* Parakeet TDT 0.6B turns speech into text.
* A swappable TTS engine speaks the reply (dispatch-voice's Chatterbox by
  default, with optional plugins such as dispatch-voice-kokoro; see tts.py).

The transcript goes through the app's NORMAL send path, so a spoken message
lands in the thread like a typed one and any bot type can answer.

This is optional, twice over. It needs the ``voice`` dependency group and
the model files (``python -m app.voice.download``), and it is OFF until the
owner turns it on in Settings › Voice (``enabled`` in ``<data>/voice.yaml``;
``DISPATCH_VOICE=1`` / ``0`` overrides the switch). Voice profiles, cloning
and a Voices GUI plug in through ``registry.py``; the advanced pieces (Voice
cloning, Built-in voices, Voice lab) are opt-in tools in tools.yaml.
See docs/voice-drive-mode.md.
"""

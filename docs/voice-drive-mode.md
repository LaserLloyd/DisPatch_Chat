# Voice chat (formerly "Drive mode")

Voice chat — talking to a thread hands-free, with replies read back as they
stream — is no longer part of DisPatch. Since 2.2.0 it is the separate
**DisPatch Voice** add-on (MIT): https://github.com/LaserLloyd/DisPatch_Voice

- **Add it:** [extensions.md → Voice](extensions.md#voice) — install the
  latest 1.x release into DisPatch's environment, fetch the speech models
  once, restart. Settings → Voice shows the same commands, ready to copy.
- **How it works, its settings, protocol and models:** the add-on's own
  documentation (`docs/voice-chat/` in its repository).
- **Upgrading a 2.1 install that used Drive mode:**
  [CHANGELOG.md → 2.2.0](../CHANGELOG.md).

Your `voice.yaml`, recorded voices (`<data>/voices/`), the speech-model
directories and each bot's voice keep their names and paths; the add-on reads
them as they are.

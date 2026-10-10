# Third-party notices

DisPatch Chat is MIT-licensed (see `LICENSE`).

The vendored frontend libraries are inventoried, with their licence texts, in
[frontend/static/vendor/README.md](frontend/static/vendor/README.md). The
Python packages DisPatch installs keep their own licences (see
`backend/uv.lock` for the exact set).

**Voice chat is not part of DisPatch.** Since 2.2.0 it is the separate
DisPatch Voice add-on (MIT, https://github.com/LaserLloyd/DisPatch_Voice),
installed beside DisPatch when wanted. The speech models and libraries it
installs or downloads (Parakeet TDT, Silero VAD, Smart Turn, Chatterbox-Turbo,
onnx-asr, soundfile and the rest) are credited in that package's own
`THIRD_PARTY_NOTICES.md`, and its optional Kokoro built-in voices are a
further plugin licensed GPL-3.0-or-later. DisPatch never downloads, imports
or redistributes any of them.

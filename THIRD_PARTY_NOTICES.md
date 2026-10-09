# Third-party notices

DisPatch Chat is MIT-licensed (see `LICENSE`). This file credits the
third-party models and libraries that its optional **Drive mode** (hands-free
voice) installs or downloads. None of them is redistributed in this
repository: the Python packages come from the package index when you install
the `voice` dependency group, and the model weights are downloaded by the user
at install time (`python -m app.voice.download`, `dispatch-voice
fetch-models`). If you build an image or bundle that **includes** these
weights or packages, the notices below become a condition of redistributing
them. Keep them with your distribution.

The vendored frontend libraries are inventoried separately in
[frontend/static/vendor/README.md](frontend/static/vendor/README.md).

## Speech models (downloaded at install time, not redistributed)

- **Parakeet TDT 0.6B v2** © NVIDIA Corporation, licensed under CC BY 4.0
  (https://creativecommons.org/licenses/by/4.0/). Source:
  https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2. DisPatch uses the ONNX
  conversion with int8 quantization by istupakov
  (https://huggingface.co/istupakov/parakeet-tdt-0.6b-v2-onnx), which the user
  downloads at install time. It is not redistributed with DisPatch.
- **Silero VAD** © 2020-present Silero Team, MIT.
  https://github.com/snakers4/silero-vad
- **Smart Turn v3.2** by Pipecat (pipecat-ai), BSD-2-Clause.
  https://huggingface.co/pipecat-ai/smart-turn-v3
- **Chatterbox-Turbo** © Resemble AI, MIT.
  https://huggingface.co/ResembleAI/chatterbox-turbo-ONNX (via the separate
  dispatch-voice package). dispatch-voice's stock voice is built from a short
  clip of synthetic speech generated with Kokoro-82M (hexgrad, Apache-2.0);
  it is not a recording of a real person.

## Runtime libraries (installed by the `voice` dependency group)

These keep their own licences:

| Package | Licence |
|---|---|
| onnx-asr | MIT |
| ONNX Runtime (already a base dependency) | MIT (its wheel ships `ThirdPartyNotices.txt`) |
| numpy (already a base dependency) | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| soundfile | BSD-3-Clause; its wheel bundles libsndfile under **LGPL-2.1**, loaded dynamically and replaceable |
| pyloudnorm → scipy | MIT → BSD-3-Clause (scipy's wheel bundles the GCC runtime under GPL-3.0 **with the GCC Runtime Library Exception**, which does not extend to programs that use it) |
| tokenizers | Apache-2.0 |
| huggingface_hub (+ hf-xet, httpx2, httpcore2, truststore, filelock, fsspec, click, packaging, pyyaml, tqdm) | Apache-2.0; hf-xet Apache-2.0; httpx2/httpcore2/fsspec/click BSD-3-Clause; truststore/filelock/pyyaml MIT; packaging Apache-2.0 OR BSD-2-Clause; tqdm MPL-2.0 AND MIT (unmodified) |

The `voice` group contains no GPL or AGPL package (checked with `uv tree
--only-group voice` against each package's metadata, 2026-10-10).

## Not part of DisPatch: the optional Kokoro plugin

The Kokoro-82M built-in voices live in a separate, optional package,
**dispatch-voice-kokoro**, licensed **GPL-3.0-or-later**. Its runtime
(`kokoro-onnx`) loads phonemizer and espeak-ng (both GPL-3.0-or-later) in
process. DisPatch does not depend on it, does not import it by name, and does
not download its models. Installing it makes the running DisPatch process a
combined work with GPL-3 code. That is fine for private use. Redistributing
DisPatch together with the plugin must follow GPL-3 terms. The Kokoro-82M
weights themselves are Apache-2.0 (https://huggingface.co/hexgrad/Kokoro-82M).

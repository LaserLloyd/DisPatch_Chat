"""Fetch the Drive-mode speech models into a local directory.

    uv run --group voice python -m app.voice.download [--dest DIR] [--force]

DIR defaults to the same place the server looks (``DISPATCH_SPEECH_MODELS``,
else ``<data dir>/speech-models``). Standard library only, so it runs before
any voice dependency is installed. Every file comes from a public, ungated
release; nothing here needs an account or a token.

Total on disk: ~810 MB. That is Parakeet int8 (~670 MB), Kokoro int8 (~90 MB),
the Kokoro voices (~28 MB), Smart Turn (~8 MB) and Silero (~2 MB).

Licences: Silero VAD (MIT), Smart Turn v3 (BSD-2), Parakeet TDT 0.6B v2
(CC-BY-4.0), Kokoro-82M (Apache-2.0). Kokoro's runtime phonemizer chain
(phonemizer + espeak-ng) is GPL-3.0 and is installed by the ``voice``
dependency group. It is NOT vendored into this repository; see
docs/voice-drive-mode.md.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

_HF = "https://huggingface.co"
_GH = "https://github.com"

# Pinned revisions: a moving branch (`main`, `master`) could hand a fresh
# install different bytes from the ones this release was tested with. Every
# file is ALSO checked against its sha256 below, so a re-tagged release asset
# or a tampered mirror fails loudly instead of being loaded.
_SILERO_REV = "1e261b036686cd0017d500ee96acd1c4ba572a9d"
_SMART_TURN_REV = "f766f81d3cfdf7737ac64aad813d91bbfd56bf93"
_PARAKEET_REV = "0bbb45a3365852604aef28b538a8f066f4ccaa85"
_KOKORO_TAG = "model-files-v1.0"

#: (relative path under DEST, URL, sha256). Relative paths are what config.py expects.
FILES: list[tuple[str, str, str]] = [
    ("silero_vad.onnx",
     f"{_GH}/snakers4/silero-vad/raw/{_SILERO_REV}/src/silero_vad/data/silero_vad.onnx",
     "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"),
    ("smart-turn-v3.2-cpu.onnx",
     f"{_HF}/pipecat-ai/smart-turn-v3/resolve/{_SMART_TURN_REV}/smart-turn-v3.2-cpu.onnx",
     "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f"),
    ("kokoro/kokoro-v1.0.int8.onnx",
     f"{_GH}/thewh1teagle/kokoro-onnx/releases/download/{_KOKORO_TAG}/kokoro-v1.0.int8.onnx",
     "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb"),
    ("kokoro/voices-v1.0.bin",
     f"{_GH}/thewh1teagle/kokoro-onnx/releases/download/{_KOKORO_TAG}/voices-v1.0.bin",
     "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"),
] + [
    (f"parakeet-tdt-0.6b-v2/{name}",
     f"{_HF}/istupakov/parakeet-tdt-0.6b-v2-onnx/resolve/{_PARAKEET_REV}/{name}", sha)
    for name, sha in (
        ("config.json", "666903c76b9798caf2c210afd4f6cd60b08a8dbf9800ec8d7a3bc0d2148ac466"),
        ("vocab.txt", "ec182b70dd42113aff6c5372c75cac58c952443eb22322f57bbd7f53977d497d"),
        ("nemo128.onnx", "a9fde1486ebfcc08f328d75ad4610c67835fea58c73ba57e3209a6f6cf019e9f"),
        ("encoder-model.int8.onnx",
         "3e0581fda6ab843888b51e56d7ee78b6d5bc3237ec113af1f732d1d5286aa155"),
        ("decoder_joint-model.int8.onnx",
         "a449f49acd68979d418651dd2dcb737cc0f1bf0225e009e29ee326354edbf7d3"),
    )
]


class ChecksumError(RuntimeError):
    pass


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url: str, dest: Path, sha256: str) -> None:
    """Download to a .part file, verify, then move into place. A file that
    fails its checksum never reaches `dest`."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "dispatch-voice-download"})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, tmp.open("wb") as f:
        while chunk := r.read(1 << 20):
            h.update(chunk)
            f.write(chunk)
    if h.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise ChecksumError(f"sha256 mismatch (got {h.hexdigest()}, want {sha256})")
    tmp.replace(dest)


def main(argv: list[str] | None = None) -> int:
    from .config import load_settings
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, default=None)
    ap.add_argument("--force", action="store_true", help="re-download existing files")
    ap.add_argument("--verify", action="store_true",
                    help="only check the files already on disk against their sha256")
    args = ap.parse_args(argv)
    dest = args.dest or load_settings().models_dir
    print(f"voice models -> {dest}")
    bad = 0
    for rel, url, sha in FILES:
        target = dest / rel
        have = target.exists() and target.stat().st_size > 0
        if args.verify or (have and not args.force):
            if not have:
                print(f"  MISSING {rel}", file=sys.stderr)
                bad += 1
            elif sha256_of(target) != sha:
                print(f"  BAD    {rel}: sha256 mismatch (re-run with --force)", file=sys.stderr)
                bad += 1
            else:
                print(f"  have  {rel}")
            continue
        print(f"  fetch {rel}")
        try:
            _fetch(url, target, sha)
        except Exception as e:
            print(f"  FAILED {rel}: {e}", file=sys.stderr)
            return 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

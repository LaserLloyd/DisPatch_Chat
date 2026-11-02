# Default avatars

`atlas-face.png` (512x512), `atlas-full.png` (1024x1024) and `atlas-thumb.png`
(128x128) are the shipped avatar for the default bot, Atlas. On start-up,
`app/firstrun.py` copies any of them that are missing into the data
directory's `avatars/` folder. An existing file is never overwritten.

Provenance (November 2026 picture, the pixel-art sloth astronaut): supplied by
the project maintainer; the face crop and the thumbnail were cut from it with
the author's tooling. Distributed under the repo's MIT-0 licence.

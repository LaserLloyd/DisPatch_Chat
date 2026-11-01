"""A chat's title comes from its first message without picture markdown or inline markers."""
from __future__ import annotations

from app import main


def test_the_title_leaves_out_pictures_and_markers():
    assert main._title_from("hello from me ![pic.png](/media/2e7a1b.png)") == "hello from me"
    assert main._title_from("[[doc:abc|notes.pdf]] summarise this please") == "summarise this please"
    assert main._title_from("![x](/media/a.png)") == "![x](/media/a.png)"        # nothing else: unchanged
    assert len(main._title_from("word " * 40)) <= 50

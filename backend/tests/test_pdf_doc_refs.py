"""PDF [[doc:…]] resolution for agent-facing text.

Regression: a PDF upload reached the receiving bot as a bare path, so the bot
hallucinated its contents instead of reading them. Text-layer PDFs are now read through poppler's `pdftotext`;
image-only PDFs (scans, app screenshots) fall back to OCR via
rapidocr-onnxruntime when installed (pages rendered with `pdftoppm`); anything
unreadable becomes an honest attachment marker — never binary mojibake.

Run: cd backend && uv run pytest tests/test_pdf_doc_refs.py
"""
from __future__ import annotations

import asyncio
import io
import shutil
from pathlib import Path
from typing import ClassVar

import pytest
from PIL import Image, ImageDraw, ImageFont

from app import config, main
from app.database import Database

HAVE_PDFTOTEXT = shutil.which("pdftotext") is not None
HAVE_PDFTOPPM = shutil.which("pdftoppm") is not None
try:
    from rapidocr_onnxruntime import RapidOCR
    HAVE_RAPIDOCR = True
except ImportError:
    HAVE_RAPIDOCR = False


# --------------------------------------------------------------------------- #
# Fixtures / builders
# --------------------------------------------------------------------------- #

@pytest.fixture
async def pdf_env(tmp_path, monkeypatch):
    """Isolated data dir + FILES_DIR + DB; nothing touches the live data dir.

    Mirrors drop_env: config dirs are monkeypatched so TestClient's lifespan
    (which connects main.db and runs the one-shot migrations) stays hermetic.
    """
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    (tmp_path / "config.yaml").write_text(
        "bots:\n"
        "- id: nova\n  name: Nova\n  emoji: 🌙\n  order: 0\n  visible: true\n  safe: true\n"
        "- id: atlas\n  name: Atlas\n  emoji: 🔥\n  order: 1\n  visible: true\n  safe: false\n"
    )
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(config, "FILES_DIR", files_dir)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(main, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(main, "FILES_DIR", files_dir)
    config._invalidate_bots_cache()
    main._ACK_SEEN.clear()
    main._delivered.clear()
    main._thread_bot.clear()
    db = Database(tmp_path / "chats.db")
    monkeypatch.setattr(main, "db", db)
    await db.connect()
    yield db, files_dir
    await db.close()


def _xref(objects: list[bytes]) -> bytes:
    """Serialize PDF objects with a correct xref table."""
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i)
        out.write(obj)
        out.write(b"\nendobj\n")
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objects) + 1, xref_pos))
    return out.getvalue()


def text_pdf(text: str) -> bytes:
    """Single-page PDF with a real text layer (what pdftotext reads)."""
    stream = f"BT /F1 20 Tf 50 700 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        b" /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    return _xref(objects)


def _font(size: int) -> ImageFont.FreeTypeFont | None:
    for p in ("/usr/share/fonts/liberation-sans-fonts/LiberationSans-Bold.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return None


def image_pdf(lines: list[str]) -> bytes:
    """Single-page PDF whose only content is a raster (scanned-app style)."""
    font = _font(48)
    img = Image.new("RGB", (1000, 420), "white")
    d = ImageDraw.Draw(img)
    if font:
        y = 60
        for ln in lines:
            d.text((50, y), ln, fill="black", font=font)
            y += 90
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    jpeg = buf.getvalue()
    stream = b"q 1000 0 0 420 0 0 cm /Im1 Do Q"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1000 420]"
        b" /Contents 4 0 R /Resources << /XObject << /Im1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 1000 /Height 420"
        b" /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode"
        b" /Length %d >>\nstream\n" % len(jpeg) + jpeg + b"\nendstream",
    ]
    return _xref(objects)


async def _add_blob(db: Database, files_dir: Path, name: str, data: bytes,
                    mime: str) -> str:
    """Insert a file row + blob, return its id."""
    stored = f"f{name}"  # keeps the real extension (e.g. .pdf) for ext checks
    rec = await db.add_file(name, stored, len(data), mime)
    (files_dir / stored).write_bytes(data)
    return rec["id"]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

async def test_pdf_text_layer_inlines(pdf_env):
    """A text-layer PDF reaches the agent as inlined text, not a path."""
    db, files_dir = pdf_env
    fid = await _add_blob(db, files_dir, "scale.pdf",
                          text_pdf("ARBOLEAF SCALE REPORT Weight: 207.7 lb"),
                          "application/pdf")
    resolved = await main._resolve_doc_refs(f"[[doc:{fid}|scale.pdf]]")
    assert "--- BEGIN DOCUMENT: scale.pdf ---" in resolved
    assert "ARBOLEAF SCALE REPORT" in resolved
    assert "207.7" in resolved
    assert "Attached file" not in resolved


async def test_pdf_image_only_ocrs_when_available(pdf_env):
    """An image-only PDF is OCR'd so the agent gets the real numbers."""
    if not (HAVE_PDFTOPPM and HAVE_RAPIDOCR):
        pytest.skip("needs pdftoppm + rapidocr-onnxruntime")
    db, files_dir = pdf_env
    fid = await _add_blob(db, files_dir, "scale.pdf",
                          image_pdf(["ARBOLEAF SCALE REPORT",
                                     "Weight: 207.7 lb",
                                     "Body Fat Mass: 52 lb"]),
                          "application/pdf")
    resolved = await main._resolve_doc_refs(f"[[doc:{fid}|scale.pdf]]")
    assert "--- BEGIN DOCUMENT: scale.pdf ---" in resolved
    assert "207.7" in resolved          # OCR'd from the raster
    assert "Body Fat Mass" in resolved
    assert "Attached file" not in resolved


async def test_pdf_unreadable_falls_back_to_marker(pdf_env, monkeypatch):
    """Unreadable PDF (no text layer, no OCR) → honest path marker, no mojibake."""
    db, files_dir = pdf_env
    monkeypatch.setattr(main, "_pdf_ocr_head_text",
                        lambda path, max_chars: ("", False))
    fid = await _add_blob(db, files_dir, "scale.pdf",
                          image_pdf(["ARBOLEAF SCALE REPORT"]),
                          "application/pdf")
    resolved = await main._resolve_doc_refs(f"[[doc:{fid}|scale.pdf]]")
    assert "Attached file" in resolved
    assert str(files_dir) in resolved
    assert "BEGIN DOCUMENT" not in resolved
    assert "[[doc:" not in resolved


async def test_pdf_head_text_never_returns_binary_garbage(pdf_env, tmp_path):
    """Image PDF → pdftotext yields nothing; OCR or empty, never mojibake.

    Before the fix, .pdf blobs went through _read_head as UTF-8 text, so the
    agent saw replacement chars / binary noise instead of content.
    """
    blob_path = tmp_path / "scale.pdf"
    blob_path.write_bytes(image_pdf(["ARBOLEAF SCALE REPORT",
                                     "Weight: 207.7 lb"]))
    text, truncated = await asyncio.to_thread(
        main._pdf_head_text, blob_path, 100_000)
    assert "\ufffd" not in text          # no replacement-char mojibake
    assert "ARBOLEAF" in text or text == ""
    assert truncated is False or text != ""


async def test_markdown_doc_still_inlines(pdf_env):
    """Non-PDF text docs keep the existing inline behaviour (regression)."""
    db, files_dir = pdf_env
    fid = await _add_blob(db, files_dir, "notes.md",
                          b"# Notes\n- one\n- two\n", "text/markdown")
    resolved = await main._resolve_doc_refs(f"[[doc:{fid}|notes.md]]")
    assert "BEGIN DOCUMENT" in resolved
    assert "- one" in resolved
    assert "Attached file" not in resolved


async def test_unknown_file_keeps_original_ref(pdf_env):
    """A ref to a missing file row is left untouched (regression)."""
    resolved = await main._resolve_doc_refs("[[doc:does-not-exist|x.pdf]]")
    assert resolved == "[[doc:does-not-exist|x.pdf]]"


async def test_pdf_reaches_agent_gateway_boundary(pdf_env, monkeypatch):
    """End-to-end at the gateway boundary: upload → [[doc:…]] → resolved text.

    run_agent_turn is the real send path. `_send_with_gateway_retry` is stubbed
    at the exact point the prompt is handed to the model, so the recorded
    `message` IS the text the bot would receive. No LLM call is made.
    """
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    db, files_dir = pdf_env
    pdf_bytes = text_pdf("ARBOLEAF SCALE REPORT Weight: 207.7 lb")

    sent: list[tuple[str, str, str]] = []

    class FakeReply:
        metadata: ClassVar[dict] = {"model": "fake", "provider": "fake",
                                    "session_id": "fake-sess-1"}
        payloads: ClassVar[list] = [SimpleNamespace(text="On it!", sub=False)]

    async def fake_send(bot_id, session_key, message, thread_id):
        sent.append((bot_id, session_key, message))
        return FakeReply()

    async def fake_watch(thread_id, bot_id, session_key, handoff):
        handoff["texts"] = []

    async def fake_second_look(thread_id, bot_id, session_key, persisted):
        pass

    monkeypatch.setattr(main, "_send_with_gateway_retry", fake_send)
    monkeypatch.setattr(main, "_watch_progress", fake_watch)
    monkeypatch.setattr(main, "_media_second_look", fake_second_look)

    tid = (await db.create_thread("nova", title="pdf test")).id
    with TestClient(main.app) as client:
        up = client.post("/api/upload",
                         files={"file": ("scale.pdf", pdf_bytes,
                                          "application/pdf")})
        assert up.status_code == 200, up.text
        uploaded_id = up.json()["id"]

        # Send path — real code, with only the LLM call stubbed at the boundary.
        await main.run_agent_turn(tid, "nova", f"[[doc:{uploaded_id}|scale.pdf]]")

    assert len(sent) == 1
    _, _, agent_text = sent[0]
    assert "--- BEGIN DOCUMENT: scale.pdf ---" in agent_text
    assert "ARBOLEAF SCALE REPORT" in agent_text
    assert "207.7" in agent_text
    assert "Attached file" not in agent_text

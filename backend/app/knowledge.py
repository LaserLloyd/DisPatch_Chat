"""A searchable knowledge corpus for advisor bots.

The corpus is a directory of Markdown files — written by people, by agents
reporting back, and by the nightly conversation digest. THE FILES ARE THE
TRUTH; this module keeps a cache of them in SQLite so a turn can find the few
paragraphs that matter in milliseconds:

* every file is split into chunks of roughly ``CHUNK_CHARS``;
* chunks go into an FTS5 table (keyword search, always available);
* when an embeddings endpoint is configured, chunks also get a vector, filled
  in the background — a corpus is searchable the moment it is scanned, and
  gets better as vectors arrive;
* a query ranks by keyword AND, when the query can be embedded inside its time
  budget, by cosine similarity; the two lists are merged with reciprocal-rank
  fusion. A slow or absent embeddings server degrades the search to keywords,
  never to an error — the chat must not stop because a GPU box is asleep.

Nothing here talks to the chat. `advisor.py` owns the turn; this module only
answers "what in the corpus is about this?" and writes report files.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("dispatch.knowledge")

CHUNK_CHARS = 1200          # target chunk size
CHUNK_OVERLAP = 150         # carried into the next chunk so a sentence split
                            # across a boundary is still findable whole
MAX_FILE_BYTES = 2 * 1024 * 1024   # skip anything bigger: not notes
EMBED_BATCH = 16
RRF_K = 60                  # standard reciprocal-rank-fusion constant
PROFILE_NAME = "_profile.md"

_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
_WORD_RE = re.compile(r"[\w][\w'-]{1,}", re.U)
_DATE_IN_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# Words that carry no search signal. Kept short on purpose: FTS5's bm25 already
# discounts common terms; this only stops a query like "what do you think
# about it" from OR-ing together a hundred meaningless hits.
_STOP = frozenset("""
a about above after again all am an and any are as at be because been before
being below between both but by can could did do does doing down during each
few for from further had has have having he her here hers him his how i if in
into is it its itself just let me more most my no nor not now of off on once
only or other our out over own same she should so some such than that the
their them then there these they this those through to too under until up very
was we were what when where which while who whom why will with would you your
yours think tell know like get got want really maybe also still
""".split())


@dataclass
class Hit:
    """One retrieved chunk, ready to be shown to a model."""

    path: str            # relative to the corpus root
    title: str
    date: str            # best-known date of the file ("" when unknown)
    text: str
    score: float = 0.0


@dataclass
class EmbedConfig:
    """Where to get vectors. Any OpenAI-compatible `/embeddings` endpoint."""

    base_url: str
    model: str
    api_key: str = ""
    timeout_s: float = 2.0          # budget for a QUERY embedding
    batch_timeout_s: float = 120.0  # budget for a background batch
    extra_body: dict = field(default_factory=dict)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split simple ``key: value`` YAML front matter from the body.

    Deliberately not a YAML parser: report files are written by models, and a
    model that emits a stray colon must not make a whole file unreadable. Only
    flat scalar keys are read; anything else is left in the body untouched.
    """
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.startswith((" ", "\t", "-")):
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta, text[m.end():]


def chunk_text(body: str, size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split on paragraph boundaries into chunks of about `size` characters.

    Paragraphs are kept whole when they fit; an oversized paragraph is cut on
    the nearest sentence end (or hard-cut as a last resort). Each chunk after
    the first starts with the tail of the previous one.
    """
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    pieces: list[str] = []
    for p in paras:
        while len(p) > size:
            cut = max(p.rfind(". ", 0, size), p.rfind("\n", 0, size))
            cut = cut + 1 if cut > size // 3 else size
            pieces.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            pieces.append(p)
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if cur and len(cur) + len(piece) + 2 > size:
            chunks.append(cur)
            tail = cur[-overlap:]
            sp = tail.find(" ")
            cur = (tail[sp + 1:] if 0 <= sp < len(tail) - 1 else tail) + "\n\n" + piece
        else:
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


def fts_query(text: str, max_terms: int = 12) -> str:
    """Turn free text into a safe FTS5 OR-query of its meaningful words."""
    seen: list[str] = []
    for w in _WORD_RE.findall(text.lower()):
        w = w.strip("'-")
        if len(w) < 3 or w in _STOP or w in seen:
            continue
        seen.append(w)
        if len(seen) >= max_terms:
            break
    # Each term quoted: FTS5 syntax characters in user text are then literal.
    return " OR ".join(f'"{w}"' for w in seen)


def slugify(text: str) -> str:
    """The file-name form of a note slug (lowercase ASCII words, dashes)."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:70] or "note"


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _normalise(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _title_for(rel: str, meta: dict[str, str], body: str) -> str:
    if meta.get("title"):
        return meta["title"]
    m = _HEADING_RE.search(body)
    if m:
        return m.group(1)[:200]
    return Path(rel).stem.replace("-", " ").replace("_", " ")


def _date_for(path: Path, rel: str, meta: dict[str, str]) -> str:
    for key in ("updated", "created", "date"):
        if meta.get(key):
            return meta[key][:10]
    m = _DATE_IN_NAME_RE.search(rel)
    if m:
        return m.group(1)
    with contextlib.suppress(OSError):
        return time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
    return ""


class KnowledgeIndex:
    """The cache over one corpus directory. Thread-safe; async-friendly.

    All SQLite work runs in a worker thread (``asyncio.to_thread``) behind one
    lock — the corpus is small (thousands of chunks, not millions), so a single
    connection is simpler than a pool and never contends with the chat DB.
    """

    def __init__(self, root: Path, db_path: Path,
                 embed: EmbedConfig | None = None) -> None:
        # Resolved once: every path the index hands out (write_note, scans) is
        # relative to THIS, so a symlinked home or corpus dir cannot make
        # relative_to() fail, and containment checks compare like with like.
        self.root = Path(root).expanduser().resolve()
        self.db_path = Path(db_path)
        self.embed = embed
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._vectors: list[tuple[int, list[float]]] | None = None  # cache
        self.last_scan: float = 0.0
        self.last_error: str = ""

    # -- storage ----------------------------------------------------------- #

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS files(
                    path TEXT PRIMARY KEY, sha TEXT NOT NULL, title TEXT,
                    date TEXT, scanned_at REAL);
                CREATE TABLE IF NOT EXISTS chunks(
                    id INTEGER PRIMARY KEY, path TEXT NOT NULL, ord INTEGER,
                    text TEXT NOT NULL, vec BLOB, vec_model TEXT);
                CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text, content='chunks', content_rowid='id');
                CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text)
                    VALUES ('delete', old.id, old.text);
                END;
            """)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- scanning ---------------------------------------------------------- #

    def _iter_files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            # Never follow a symlink (directory or file): a link to a secrets
            # file dropped into the corpus would otherwise be indexed and then
            # sent to the cloud provider as "retrieved notes".
            dirnames[:] = [d for d in dirnames if not d.startswith(".")
                           and not (Path(dirpath) / d).is_symlink()]
            for fn in filenames:
                if fn.endswith(".md") and not fn.startswith("."):
                    path = Path(dirpath) / fn
                    if self._inside(path):
                        out.append(path)
        return sorted(out)

    def _inside(self, path: Path) -> bool:
        """A regular, non-symlink file whose real path is under the root."""
        try:
            if path.is_symlink() or not path.is_file():
                return False
            real = path.resolve()
        except OSError:
            return False
        return self.root in real.parents

    @staticmethod
    def _read_nofollow(path: Path) -> bytes:
        """Read a file, refusing a symlink swapped in after the check."""
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as f:
            return f.read()

    def scan_sync(self) -> dict[str, int]:
        """Bring the index in line with the directory. Returns change counts."""
        stats = {"added": 0, "changed": 0, "removed": 0, "files": 0}
        with self._lock:
            db = self._db()
            known = dict(db.execute("SELECT path, sha FROM files"))
            present: set[str] = set()
            for path in self._iter_files():
                rel = path.relative_to(self.root).as_posix()
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    raw = self._read_nofollow(path)
                except OSError:
                    continue
                present.add(rel)
                sha = hashlib.sha1(raw).hexdigest()
                if known.get(rel) == sha:
                    continue
                text = raw.decode("utf-8", errors="replace")
                meta, body = parse_front_matter(text)
                title = _title_for(rel, meta, body)
                date = _date_for(path, rel, meta)
                db.execute("DELETE FROM chunks WHERE path=?", (rel,))
                for i, chunk in enumerate(chunk_text(body)):
                    db.execute("INSERT INTO chunks(path, ord, text) VALUES (?,?,?)",
                               (rel, i, chunk))
                db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
                           (rel, sha, title, date, time.time()))
                stats["changed" if rel in known else "added"] += 1
            for rel in set(known) - present:
                db.execute("DELETE FROM chunks WHERE path=?", (rel,))
                db.execute("DELETE FROM files WHERE path=?", (rel,))
                stats["removed"] += 1
            db.commit()
            stats["files"] = len(present)
            if stats["added"] or stats["changed"] or stats["removed"]:
                self._vectors = None
        self.last_scan = time.time()
        return stats

    async def scan(self) -> dict[str, int]:
        return await asyncio.to_thread(self.scan_sync)

    # -- embeddings -------------------------------------------------------- #

    async def _embed_texts(self, texts: list[str], timeout: float) -> list[list[float]]:
        cfg = self.embed
        assert cfg is not None
        headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
        url = cfg.base_url.rstrip("/") + "/embeddings"
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json={
                "model": cfg.model, "input": texts, **cfg.extra_body})
        if r.status_code >= 400:
            raise RuntimeError(f"embeddings HTTP {r.status_code}: {r.text[:200]}")
        data = r.json().get("data") or []
        vecs = [d.get("embedding") for d in sorted(data, key=lambda d: d.get("index", 0))]
        if len(vecs) != len(texts) or not all(isinstance(v, list) and v for v in vecs):
            raise RuntimeError("embeddings response did not match the request")
        return [_normalise([float(x) for x in v]) for v in vecs]

    async def embed_pending(self, limit: int = 256) -> int:
        """Fill in vectors for chunks that lack one (or have another model's)."""
        if self.embed is None:
            return 0
        model = self.embed.model

        def pending() -> list[tuple[int, str]]:
            with self._lock:
                return list(self._db().execute(
                    "SELECT id, text FROM chunks WHERE vec IS NULL OR vec_model IS NOT ? "
                    "ORDER BY id LIMIT ?", (model, limit)))

        rows = await asyncio.to_thread(pending)
        done = 0
        for i in range(0, len(rows), EMBED_BATCH):
            batch = rows[i:i + EMBED_BATCH]
            vecs = await self._embed_texts([t for _, t in batch],
                                           self.embed.batch_timeout_s)

            def store(batch=batch, vecs=vecs) -> None:
                with self._lock:
                    db = self._db()
                    for (cid, _), v in zip(batch, vecs, strict=True):
                        db.execute("UPDATE chunks SET vec=?, vec_model=? WHERE id=?",
                                   (_pack(v), model, cid))
                    db.commit()
                    self._vectors = None

            await asyncio.to_thread(store)
            done += len(batch)
        return done

    def _load_vectors(self) -> list[tuple[int, list[float]]]:
        if self._vectors is None:
            model = self.embed.model if self.embed else None
            self._vectors = [(cid, _unpack(blob)) for cid, blob in self._db().execute(
                "SELECT id, vec FROM chunks WHERE vec IS NOT NULL AND vec_model IS ?",
                (model,))]
        return self._vectors

    # -- search ------------------------------------------------------------ #

    def _keyword_ids(self, query: str, k: int) -> list[int]:
        q = fts_query(query)
        if not q:
            return []
        try:
            return [r[0] for r in self._db().execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
                "ORDER BY bm25(chunks_fts) LIMIT ?", (q, k))]
        except sqlite3.OperationalError:
            return []

    def _vector_ids(self, qvec: list[float], k: int) -> list[int]:
        scored = []
        for cid, vec in self._load_vectors():
            if len(vec) != len(qvec):
                continue
            scored.append((sum(a * b for a, b in zip(qvec, vec, strict=True)), cid))
        scored.sort(reverse=True)
        return [cid for _, cid in scored[:k]]

    async def search(self, query: str, *, k: int = 6, budget_chars: int = 6000,
                     exclude: tuple[str, ...] = (PROFILE_NAME,)) -> list[Hit]:
        """Best chunks for `query`, within `budget_chars` of text in total."""
        query = (query or "").strip()
        if not query:
            return []
        qvec: list[float] | None = None
        if self.embed is not None:
            try:
                qvec = (await asyncio.wait_for(
                    self._embed_texts([query[:2000]], self.embed.timeout_s),
                    self.embed.timeout_s + 0.5))[0]
            except Exception as e:      # slow/asleep/misconfigured: keywords only
                self.last_error = f"query embedding skipped: {str(e)[:160]}"
                log.debug(self.last_error)

        def run() -> list[Hit]:
            with self._lock:
                kw = self._keyword_ids(query, k * 4)
                vc = self._vector_ids(qvec, k * 4) if qvec else []
                scores: dict[int, float] = {}
                for ranked in (kw, vc):
                    for rank, cid in enumerate(ranked):
                        scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
                if not scores:
                    return []
                ids = sorted(scores, key=scores.get, reverse=True)
                rows = {r[0]: r[1:] for r in self._db().execute(
                    f"SELECT c.id, c.path, c.text, f.title, f.date FROM chunks c "
                    f"JOIN files f ON f.path=c.path WHERE c.id IN "
                    f"({','.join('?' * len(ids))})", ids)}
                hits: list[Hit] = []
                used = 0
                for cid in ids:
                    if cid not in rows:
                        continue
                    path, text, title, date = rows[cid]
                    if path in exclude:
                        continue
                    if used + len(text) > budget_chars and hits:
                        break
                    hits.append(Hit(path, title or path, date or "", text, scores[cid]))
                    used += len(text)
                    if len(hits) >= k:
                        break
                return hits

        return await asyncio.to_thread(run)

    def stats_sync(self) -> dict[str, Any]:
        with self._lock:
            db = self._db()
            files = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            vecs = db.execute("SELECT COUNT(*) FROM chunks WHERE vec IS NOT NULL").fetchone()[0]
        return {"root": str(self.root), "files": files, "chunks": chunks,
                "embedded": vecs, "embeddings": bool(self.embed),
                "last_scan": self.last_scan, "last_error": self.last_error}

    # -- files ------------------------------------------------------------- #

    def read_profile(self, max_chars: int = 8000) -> str:
        """The always-in-prompt profile, or "" when the corpus has none."""
        p = self.root / PROFILE_NAME
        if not self._inside(p):          # absent, or a symlink out of the corpus
            return ""
        try:
            text = self._read_nofollow(p).decode("utf-8", errors="replace")
        except OSError:
            return ""
        return parse_front_matter(text)[1].strip()[:max_chars]

    def note_path(self, subdir: str, slug: str) -> Path:
        """Where :meth:`write_note` puts `slug` in `subdir` (no suffixing).

        Raises ValueError when `subdir` would leave the corpus root. Callers
        that need to know whether a note already exists (the digest's
        idempotency check) must ask HERE rather than rebuild the name by hand:
        a hand-built name that slugified differently never matched, so the
        check never fired.
        """
        target_dir = (self.root / subdir).resolve()
        if self.root not in (target_dir, *target_dir.parents):
            raise ValueError("note directory escapes the corpus root")
        return target_dir / f"{slugify(slug)}.md"

    def write_note(self, subdir: str, slug: str, front: dict[str, str],
                   body: str, *, overwrite: bool = False) -> Path:
        """Atomically write a Markdown note with front matter; return its path.

        By default never overwrites: a name collision gets a numeric suffix,
        because a report is a record of what was found at a moment, not a slot.
        ``overwrite=True`` is for notes that ARE a slot (one digest per day):
        the same path is replaced atomically, never unlinked first.
        """
        path = self.note_path(subdir, slug)
        target_dir = path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        safe = path.stem
        n = 2
        while path.exists() and not overwrite:
            path = target_dir / f"{safe}-{n}.md"
            n += 1
        lines = ["---"]
        for k, v in front.items():
            v = str(v).replace("\n", " ").strip()
            lines.append(f"{k}: {v}")
        lines += ["---", "", body.strip(), ""]
        fd, tmp = tempfile.mkstemp(dir=target_dir, prefix=".tmp-", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return path

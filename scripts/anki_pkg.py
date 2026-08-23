"""Read an Anki .apkg archive into plain Python data.

Handles both package layouts that AnkiWeb serves:

* legacy  -- ``collection.anki2`` / ``collection.anki21`` plus a JSON ``media``
             map and raw numbered blobs.
* v3      -- ``collection.anki21b`` (zstd), a zstd protobuf ``media`` map and
             individually zstd-compressed numbered blobs.

Nothing here depends on the Anki desktop libraries, so it runs anywhere a
plain CPython does.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
FIELD_SEP = "\x1f"

# Collection candidates in the order Anki itself prefers them.
COLLECTION_NAMES = ("collection.anki21b", "collection.anki21", "collection.anki2")


def _maybe_unzstd(blob: bytes) -> bytes:
    """Return ``blob`` decompressed when it carries the zstd frame magic."""
    if not blob.startswith(ZSTD_MAGIC):
        return blob
    import zstandard

    return zstandard.ZstdDecompressor().decompress(blob, max_output_size=1 << 31)


# --------------------------------------------------------------------------
# minimal protobuf reader -- only enough for the v3 media map
# --------------------------------------------------------------------------

def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def _iter_protobuf(buf: bytes):
    """Yield ``(field_number, wire_type, value)`` triples from a message."""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field_no, wire = key >> 3, key & 0x07
        if wire == 0:
            value, pos = _read_varint(buf, pos)
        elif wire == 2:
            length, pos = _read_varint(buf, pos)
            value, pos = buf[pos : pos + length], pos + length
        elif wire == 5:
            value, pos = buf[pos : pos + 4], pos + 4
        elif wire == 1:
            value, pos = buf[pos : pos + 8], pos + 8
        else:  # pragma: no cover - groups do not appear in Anki's schema
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield field_no, wire, value


def _parse_media_protobuf(blob: bytes) -> dict[str, str]:
    """``MediaEntries { repeated MediaEntry entries = 1 }`` -> index -> name."""
    names: dict[str, str] = {}
    index = 0
    for field_no, wire, value in _iter_protobuf(blob):
        if field_no != 1 or wire != 2:
            continue
        for sub_no, sub_wire, sub_value in _iter_protobuf(value):
            if sub_no == 1 and sub_wire == 2:
                names[str(index)] = sub_value.decode("utf-8")
                break
        index += 1
    return names


# --------------------------------------------------------------------------
# public data model
# --------------------------------------------------------------------------

@dataclass
class NoteType:
    id: int
    name: str
    fields: list[str] = field(default_factory=list)


@dataclass
class Note:
    id: int
    guid: str
    notetype_id: int
    tags: list[str]
    fields: list[str]

    def as_dict(self, notetype: NoteType) -> dict[str, str]:
        return dict(zip(notetype.fields, self.fields))


@dataclass
class Collection:
    notetypes: dict[int, NoteType]
    notes: list[Note]
    media: dict[str, bytes]  # real filename -> file contents
    deck_names: list[str]


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _extract_collection_db(zf: zipfile.ZipFile, workdir: Path) -> Path:
    members = set(zf.namelist())
    for name in COLLECTION_NAMES:
        if name in members:
            db_path = workdir / "collection.sqlite"
            db_path.write_bytes(_maybe_unzstd(zf.read(name)))
            return db_path
    raise ValueError(f"no Anki collection found in package; members: {sorted(members)[:20]}")


def _read_media(zf: zipfile.ZipFile) -> dict[str, bytes]:
    members = set(zf.namelist())
    if "media" not in members:
        return {}

    raw = _maybe_unzstd(zf.read("media"))
    try:
        index_to_name = {str(k): v for k, v in json.loads(raw.decode("utf-8")).items()}
    except (UnicodeDecodeError, json.JSONDecodeError):
        index_to_name = _parse_media_protobuf(raw)

    media: dict[str, bytes] = {}
    for index, filename in index_to_name.items():
        if index not in members:
            continue
        media[filename] = _maybe_unzstd(zf.read(index))
    return media


def _read_notetypes(conn: sqlite3.Connection) -> dict[int, NoteType]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    # Schema 18+ keeps note types in dedicated tables.
    if {"notetypes", "fields"} <= tables:
        notetypes: dict[int, NoteType] = {}
        for ntid, name in conn.execute("SELECT id, name FROM notetypes"):
            notetypes[ntid] = NoteType(id=ntid, name=name)
        for ntid, name, ord_ in conn.execute("SELECT ntid, name, ord FROM fields ORDER BY ntid, ord"):
            if ntid in notetypes:
                notetypes[ntid].fields.append(name)
        if any(nt.fields for nt in notetypes.values()):
            return notetypes

    # Schema 11 stores everything as JSON on the single `col` row.
    models_json = conn.execute("SELECT models FROM col").fetchone()[0]
    notetypes = {}
    for ntid, model in json.loads(models_json).items():
        notetypes[int(ntid)] = NoteType(
            id=int(ntid),
            name=model["name"],
            fields=[f["name"] for f in sorted(model["flds"], key=lambda f: f["ord"])],
        )
    return notetypes


def _read_deck_names(conn: sqlite3.Connection) -> list[str]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "decks" in tables:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(decks)")}
        if "name" in cols:
            return [row[0].replace("\x1f", "::") for row in conn.execute("SELECT name FROM decks")]
    decks_json = conn.execute("SELECT decks FROM col").fetchone()[0]
    if decks_json:
        return [d["name"] for d in json.loads(decks_json).values()]
    return []


def read_package(path: str | Path) -> Collection:
    """Load an ``.apkg`` file into a :class:`Collection`."""
    path = Path(path)
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(path) as zf:
        db_path = _extract_collection_db(zf, Path(tmp))
        media = _read_media(zf)
        conn = sqlite3.connect(db_path)
        try:
            notetypes = _read_notetypes(conn)
            deck_names = _read_deck_names(conn)
            notes = [
                Note(
                    id=nid,
                    guid=guid,
                    notetype_id=mid,
                    tags=tags.split(),
                    fields=flds.split(FIELD_SEP),
                )
                for nid, guid, mid, tags, flds in conn.execute(
                    "SELECT id, guid, mid, tags, flds FROM notes ORDER BY id"
                )
            ]
        finally:
            conn.close()

    return Collection(notetypes=notetypes, notes=notes, media=media, deck_names=deck_names)

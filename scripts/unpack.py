"""Unpack the source .apkg into reviewable JSON plus a media folder.

The source deck's field names are not known ahead of time, so the layout is
detected from the note contents and can be pinned in ``config.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from anki_pkg import Collection, NoteType, read_package

SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
IMG_RE = re.compile(r"<img[^>]+src=[\"']?([^\"'>\s]+)", re.I)
TAG_RE = re.compile(r"<[^>]+>")

# Characters that only show up on the Spanish side of the deck.
SPANISH_MARKERS = set("ñáéíóúüÁÉÍÓÚÜÑ¿¡")

SPANISH_NAME_HINTS = ("spanish", "espanol", "español", "target", "front", "word", "phrase")
ENGLISH_NAME_HINTS = ("english", "translation", "meaning", "back", "definition", "ingles")
AUDIO_NAME_HINTS = ("audio", "sound", "tts", "pronunciation", "voice")


def strip_html(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = SOUND_RE.sub(" ", text)
    text = IMG_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(text.split()).strip()


def _spanish_score(values: list[str]) -> float:
    hits = sum(1 for v in values if SPANISH_MARKERS & set(v))
    return hits / max(len(values), 1)


def _ascii_ratio(values: list[str]) -> float:
    joined = "".join(values)
    if not joined:
        return 0.0
    ascii_chars = sum(1 for ch in joined if unicodedata.normalize("NFKD", ch).isascii())
    return ascii_chars / len(joined)


def detect_layout(collection: Collection, notetype: NoteType, notes: list) -> dict[str, str | None]:
    """Return a mapping of role -> field name for the busiest note type."""
    columns: dict[str, list[str]] = {name: [] for name in notetype.fields}
    for note in notes:
        for name, value in zip(notetype.fields, note.fields):
            columns[name].append(value)

    audio_field = None
    for name, values in columns.items():
        if any(SOUND_RE.search(v) for v in values):
            audio_field = name
            break
    if audio_field is None:
        for name in notetype.fields:
            if any(h in name.lower() for h in AUDIO_NAME_HINTS):
                audio_field = name
                break

    image_field = None
    for name, values in columns.items():
        if name != audio_field and any(IMG_RE.search(v) for v in values):
            image_field = name
            break

    text_fields = [
        name
        for name in notetype.fields
        if name not in (audio_field, image_field)
        and any(strip_html(v) for v in columns[name])
    ]

    def by_name(hints: tuple[str, ...]) -> str | None:
        for name in text_fields:
            if any(h in name.lower() for h in hints):
                return name
        return None

    spanish = by_name(SPANISH_NAME_HINTS)
    english = by_name(ENGLISH_NAME_HINTS)

    # Names are often generic ("Front"/"Back"); fall back to the text itself.
    if spanish is None or english is None or spanish == english:
        scored = sorted(
            text_fields,
            key=lambda n: (_spanish_score([strip_html(v) for v in columns[n]]), -_ascii_ratio([strip_html(v) for v in columns[n]])),
            reverse=True,
        )
        if scored:
            spanish = spanish or scored[0]
            english = next((n for n in scored if n != spanish), None) or english

    return {"spanish": spanish, "english": english, "audio": audio_field, "image": image_field}


def pick_main_notetype(collection: Collection) -> tuple[NoteType, list]:
    counts = Counter(n.notetype_id for n in collection.notes)
    if not counts:
        raise SystemExit("source deck contains no notes")
    ntid, _ = counts.most_common(1)[0]
    return collection.notetypes[ntid], [n for n in collection.notes if n.notetype_id == ntid]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apkg", type=Path, default=Path("source/source_deck.apkg"))
    ap.add_argument("--out-json", type=Path, default=Path("data/notes.json"))
    ap.add_argument("--media-dir", type=Path, default=Path("build/source_media"))
    ap.add_argument("--config", type=Path, default=Path("config.json"))
    args = ap.parse_args()

    collection = read_package(args.apkg)
    notetype, notes = pick_main_notetype(collection)

    layout = detect_layout(collection, notetype, notes)
    if args.config.exists():
        override = json.loads(args.config.read_text()).get("fields", {})
        layout.update({k: v for k, v in override.items() if v})

    print(f"source decks     : {collection.deck_names}")
    print(f"note types       : {[nt.name for nt in collection.notetypes.values()]}")
    print(f"main note type   : {notetype.name} -> {notetype.fields}")
    print(f"detected layout  : {layout}")
    print(f"notes            : {len(notes)}")
    print(f"media files      : {len(collection.media)}")

    if not layout["spanish"] or not layout["english"]:
        raise SystemExit(
            "could not identify the Spanish/English fields; pin them in config.json "
            'as {"fields": {"spanish": "...", "english": "...", "audio": "..."}}'
        )

    args.media_dir.mkdir(parents=True, exist_ok=True)
    for filename, blob in collection.media.items():
        (args.media_dir / filename).write_bytes(blob)

    records = []
    for note in notes:
        values = note.as_dict(notetype)
        audio_raw = values.get(layout["audio"] or "", "")
        records.append(
            {
                "guid": note.guid,
                "spanish": strip_html(values.get(layout["spanish"], "")),
                "english": strip_html(values.get(layout["english"], "")),
                "audio": [m.group(1) for m in SOUND_RE.finditer(audio_raw)],
                "tags": note.tags,
            }
        )

    records = [r for r in records if r["spanish"] and r["english"]]

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(
            {
                "source_deck_names": collection.deck_names,
                "source_notetype": notetype.name,
                "layout": layout,
                "notes": records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    with_audio = sum(1 for r in records if r["audio"])
    print(f"wrote {len(records)} notes ({with_audio} with audio) -> {args.out_json}")


if __name__ == "__main__":
    main()

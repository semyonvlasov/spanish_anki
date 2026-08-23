"""Build the two study decks from the unpacked notes plus the image cache.

deck 1 -- Spanish -> English, the context image hidden behind a hint button.
deck 2 -- English -> Spanish, the image shown up front, Spanish audio hidden
          behind a hint button.

On the reverse deck the hinted audio is a plain ``<audio controls>`` element
rather than Anki's ``[sound:]`` tag: Anki auto-plays every ``[sound:]`` it
finds on the question side, which would read the answer out loud before the
learner has guessed.  The native tag is kept for the answer side, where the
replay is wanted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import genanki
import requests

# Stable ids -- changing these makes Anki treat a rebuild as a brand new deck
# and lose the learner's scheduling, so they are pinned deliberately.
MODEL_ID_FORWARD = 1_612_004_101
MODEL_ID_REVERSE = 1_612_004_102
DECK_ID_FORWARD = 2_071_004_101
DECK_ID_REVERSE = 2_071_004_102

CSS = """
.card {
  font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  font-size: 22px;
  text-align: center;
  color: #1f2328;
  background-color: #fbfaf7;
  padding: 18px 14px;
}
.nightMode.card, .night_mode .card { color: #e6e6e6; background-color: #202124; }

.phrase { font-size: 30px; line-height: 1.35; margin: 14px 0; }
.phrase.es { font-weight: 600; }
.phrase.en { color: #2f6f4f; }
.nightMode .phrase.en, .night_mode .phrase.en { color: #7fd1a6; }

.img img, .img > img { max-width: 100%; max-height: 340px; border-radius: 12px; }
.img { margin: 14px 0; }

.audio { margin: 10px 0; }
.audio audio { width: min(320px, 90%); }

hr#answer { border: none; border-top: 1px solid #d9d5cc; margin: 18px 0; }
.nightMode hr#answer, .night_mode hr#answer { border-top-color: #45484d; }

/* Hint rendered as a button; <details> keeps it working without JavaScript. */
details.hint { margin: 16px auto; max-width: 420px; }
details.hint > summary {
  cursor: pointer;
  list-style: none;
  display: inline-block;
  padding: 9px 20px;
  font-size: 17px;
  border-radius: 999px;
  border: 1px solid #cfcabd;
  background: #f1ede4;
  color: #4a4438;
  user-select: none;
}
details.hint > summary::-webkit-details-marker { display: none; }
details.hint > summary:active { transform: translateY(1px); }
details.hint[open] > summary { opacity: 0.55; font-size: 15px; padding: 6px 14px; }
.nightMode details.hint > summary, .night_mode details.hint > summary {
  background: #2f3134; border-color: #4a4d52; color: #d7d3c8;
}
.tags { margin-top: 14px; font-size: 13px; opacity: 0.5; }
"""

FORWARD_QFMT = """
<div class="phrase es">{{Spanish}}</div>
<div class="audio">{{Audio}}</div>
{{#Image}}
<details class="hint"><summary>&#128444;&#65039;&nbsp; Показать картинку</summary>
  <div class="img">{{Image}}</div>
</details>
{{/Image}}
"""

FORWARD_AFMT = """
<div class="phrase es">{{Spanish}}</div>
<div class="audio">{{Audio}}</div>
<hr id=answer>
<div class="phrase en">{{English}}</div>
{{#Image}}<div class="img">{{Image}}</div>{{/Image}}
"""

REVERSE_QFMT = """
<div class="phrase en">{{English}}</div>
{{#Image}}<div class="img">{{Image}}</div>{{/Image}}
{{#AudioPlayer}}
<details class="hint"><summary>&#128266;&nbsp; Прослушать по-испански</summary>
  <div class="audio">{{AudioPlayer}}</div>
</details>
{{/AudioPlayer}}
"""

REVERSE_AFMT = """
<div class="phrase en">{{English}}</div>
{{#Image}}<div class="img">{{Image}}</div>{{/Image}}
<hr id=answer>
<div class="phrase es">{{Spanish}}</div>
<div class="audio">{{Audio}}</div>
"""

FIELDS = [{"name": n} for n in ("Spanish", "English", "Audio", "AudioPlayer", "Image", "SourceGuid")]


def make_model(model_id: int, name: str, qfmt: str, afmt: str) -> genanki.Model:
    return genanki.Model(
        model_id,
        name,
        fields=FIELDS,
        templates=[{"name": "Card 1", "qfmt": qfmt, "afmt": afmt}],
        css=CSS,
    )


class StableNote(genanki.Note):
    """Anki matches notes across imports by guid, so derive it deterministically."""

    def __init__(self, *args, stable_guid: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._stable_guid = stable_guid

    @property
    def guid(self):
        return self._stable_guid

    @guid.setter
    def guid(self, _value):  # genanki's __init__ assigns to this; ignore it
        pass


def ensure_image(entry: dict, images_dir: Path) -> Path | None:
    """Return the local image path, pulling it from Drive if only a link is stored."""
    if not entry:
        return None
    local = images_dir / entry["file"]
    if local.exists():
        return local
    url = entry.get("download_url") or entry.get("drive_download_url")
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  could not fetch {entry['file']} from Drive: {exc}")
        return None
    images_dir.mkdir(parents=True, exist_ok=True)
    local.write_bytes(resp.content)
    return local


def build(
    notes: list[dict],
    images: dict[str, dict],
    tags: dict[str, dict],
    source_media: Path,
    images_dir: Path,
    staging: Path,
    variant: str,
    deck_name: str,
    out_path: Path,
) -> None:
    reverse = variant == "reverse"
    model = make_model(
        MODEL_ID_REVERSE if reverse else MODEL_ID_FORWARD,
        "Spanish EN->ES (image + hinted audio)" if reverse else "Spanish ES->EN (hinted image)",
        REVERSE_QFMT if reverse else FORWARD_QFMT,
        REVERSE_AFMT if reverse else FORWARD_AFMT,
    )
    deck = genanki.Deck(DECK_ID_REVERSE if reverse else DECK_ID_FORWARD, deck_name)

    staging.mkdir(parents=True, exist_ok=True)
    media_files: set[str] = set()
    used_images = 0
    used_audio = 0

    for record in notes:
        audio_tag = ""
        audio_player = ""
        for audio_name in record.get("audio", []):
            src = source_media / audio_name
            if not src.exists():
                continue
            dest = staging / audio_name
            if not dest.exists():
                shutil.copyfile(src, dest)
            media_files.add(str(dest))
            audio_tag += f"[sound:{audio_name}]"
            audio_player += f'<audio controls preload="none" src="{audio_name}"></audio>'
        if audio_tag:
            used_audio += 1

        image_html = ""
        # Notes that reduced to the same concept share one image.
        concept_key = (tags.get(record["guid"]) or {}).get("concept_key", "")
        entry = images.get(concept_key) if concept_key else None
        local = ensure_image(entry, images_dir)
        if local and local.exists():
            dest = staging / local.name
            if not dest.exists():
                shutil.copyfile(local, dest)
            media_files.add(str(dest))
            image_html = f'<img src="{local.name}">'
            used_images += 1

        note = StableNote(
            model=model,
            fields=[
                record["spanish"],
                record["english"],
                audio_tag,
                audio_player if reverse else "",
                image_html,
                record["guid"],
            ],
            tags=[t for t in record.get("tags", []) if t],
            stable_guid=hashlib.sha1(f"{variant}:{record['guid']}".encode()).hexdigest()[:22],
        )
        deck.add_note(note)

    package = genanki.Package(deck)
    package.media_files = sorted(media_files)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    package.write_to_file(str(out_path))

    size_mb = out_path.stat().st_size / 1e6
    print(
        f"{out_path.name}: {len(deck.notes)} notes, {used_images} with image, "
        f"{used_audio} with audio, {len(media_files)} media files, {size_mb:.1f} MB"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notes", type=Path, default=Path("data/notes.json"))
    ap.add_argument("--manifest", type=Path, default=Path("data/images.json"))
    ap.add_argument("--tags", type=Path, default=Path("data/tags.json"))
    ap.add_argument("--images-dir", type=Path, default=Path("build/images"))
    ap.add_argument("--source-media", type=Path, default=Path("build/source_media"))
    ap.add_argument("--out-dir", type=Path, default=Path("dist"))
    ap.add_argument("--forward-name", default="Español::Фразы (ES→EN, картинка в хинте)")
    ap.add_argument("--reverse-name", default="Español::Фразы (EN→ES, аудио в хинте)")
    args = ap.parse_args()

    notes = json.loads(args.notes.read_text(encoding="utf-8"))["notes"]
    images = {}
    if args.manifest.exists():
        images = json.loads(args.manifest.read_text(encoding="utf-8")).get("images", {})
    tags = {}
    if args.tags.exists():
        tags = json.loads(args.tags.read_text(encoding="utf-8")).get("tags", {})
    reachable = sum(
        1 for n in notes if (tags.get(n["guid"]) or {}).get("concept_key") in images
    )
    print(
        f"{len(notes):,} notes, {len(images):,} images in manifest, "
        f"{reachable:,} notes resolve to an image"
    )

    for variant, deck_name, filename in (
        ("forward", args.forward_name, "spanish_es-en_hinted-image.apkg"),
        ("reverse", args.reverse_name, "spanish_en-es_hinted-audio.apkg"),
    ):
        build(
            notes=notes,
            images=images,
            tags=tags,
            source_media=args.source_media,
            images_dir=args.images_dir,
            staging=Path("build/media_staging") / variant,
            variant=variant,
            deck_name=deck_name,
            out_path=args.out_dir / filename,
        )


if __name__ == "__main__":
    main()

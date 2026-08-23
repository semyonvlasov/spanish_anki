"""Check a built .apkg for the ways these decks can quietly be broken.

The dangerous failure is silent: a note referencing media that never made it
into the package shows the learner a broken image or plays nothing, and the
deck still imports cleanly. So every reference is resolved against the
package's own media table.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from anki_pkg import read_package

IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"')
SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
AUDIO_SRC_RE = re.compile(r'<audio[^>]+src="([^"]+)"')


def verify(path: Path, expect_variant: str) -> list[str]:
    problems: list[str] = []
    collection = read_package(path)
    notetype = next(iter(collection.notetypes.values()))
    fields = notetype.fields
    media = set(collection.media)

    required = {"Spanish", "English", "Audio", "AudioPlayer", "Image"}
    missing_fields = required - set(fields)
    if missing_fields:
        problems.append(f"note type is missing fields: {sorted(missing_fields)}")
        return problems

    dangling_img = dangling_sound = dangling_audio = 0
    with_image = with_audio = with_player = 0
    empty_both_sides = 0

    for note in collection.notes:
        values = note.as_dict(notetype)

        for name in IMG_SRC_RE.findall(values["Image"]):
            with_image += 1
            if name not in media:
                dangling_img += 1
        for name in SOUND_RE.findall(values["Audio"]):
            with_audio += 1
            if name not in media:
                dangling_sound += 1
        for name in AUDIO_SRC_RE.findall(values["AudioPlayer"]):
            with_player += 1
            if name not in media:
                dangling_audio += 1

        if not values["Spanish"].strip() or not values["English"].strip():
            empty_both_sides += 1

    if dangling_img:
        problems.append(f"{dangling_img} <img> references point at media not in the package")
    if dangling_sound:
        problems.append(f"{dangling_sound} [sound:] references point at media not in the package")
    if dangling_audio:
        problems.append(f"{dangling_audio} <audio> references point at media not in the package")
    if empty_both_sides:
        problems.append(f"{empty_both_sides} notes have an empty Spanish or English side")

    # The reverse deck must not carry a playable [sound:] on the question side;
    # that is exactly what the hinted <audio> element exists to avoid.
    if expect_variant == "reverse" and with_player == 0 and with_audio:
        problems.append("reverse deck has no AudioPlayer content -- the hint would be empty")
    if expect_variant == "forward" and with_player:
        problems.append("forward deck should not populate AudioPlayer")

    referenced = {
        name
        for note in collection.notes
        for name in IMG_SRC_RE.findall(note.as_dict(notetype)["Image"])
        + SOUND_RE.findall(note.as_dict(notetype)["Audio"])
        + AUDIO_SRC_RE.findall(note.as_dict(notetype)["AudioPlayer"])
    }
    orphan_media = len(media - referenced)

    print(f"{path.name}")
    print(f"  notes                 : {len(collection.notes):,}")
    print(f"  media files in package: {len(media):,}")
    print(f"  notes with an image   : {with_image:,}")
    print(f"  notes with audio      : {with_audio:,}")
    print(f"  hinted audio players  : {with_player:,}")
    print(f"  unreferenced media    : {orphan_media:,}")
    print(f"  size                  : {path.stat().st_size / 1e6:,.0f} MB")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dist", type=Path, default=Path("dist"))
    args = ap.parse_args()

    checks = [
        (args.dist / "spanish_es-en_hinted-image.apkg", "forward"),
        (args.dist / "spanish_en-es_hinted-audio.apkg", "reverse"),
    ]

    failed = False
    for path, variant in checks:
        if not path.exists():
            print(f"MISSING {path}")
            failed = True
            continue
        problems = verify(path, variant)
        if problems:
            failed = True
            for problem in problems:
                print(f"  PROBLEM: {problem}")
        else:
            print("  OK")
        print()

    if failed:
        sys.exit(1)
    print("both decks verified")


if __name__ == "__main__":
    main()

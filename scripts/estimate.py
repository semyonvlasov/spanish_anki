"""Print how big and how slow a full image pass would be, before committing to it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

AVG_JPEG_BYTES = {400: 26_000, 600: 45_000, 800: 72_000}
IMAGES_PER_SEC = 2.2  # measured against the free providers with 4 workers


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notes", type=Path, default=Path("data/notes.json"))
    ap.add_argument("--source-media", type=Path, default=Path("build/source_media"))
    args = ap.parse_args()

    doc = json.loads(args.notes.read_text(encoding="utf-8"))
    notes = doc["notes"]
    total = len(notes)
    with_audio = sum(1 for n in notes if n["audio"])

    audio_bytes = sum(f.stat().st_size for f in args.source_media.glob("*") if f.is_file())

    print("=" * 64)
    print(f"notes                 : {total:,}")
    print(f"  with audio          : {with_audio:,}")
    print(f"source media on disk  : {audio_bytes / 1e6:,.0f} MB")
    print(f"detected layout       : {doc['layout']}")
    print("-" * 64)
    print("sample notes:")
    for note in notes[:5]:
        print(f"  ES: {note['spanish'][:70]}")
        print(f"  EN: {note['english'][:70]}")
        print(f"  audio: {note['audio']}")
        print()
    print("-" * 64)
    print(f"{'images':>10} | {'edge':>5} | {'image MB':>9} | {'deck MB':>9} | fetch time")
    for count in sorted({500, 1000, 2000, 5000, total}):
        if count > total:
            continue
        for edge, avg in AVG_JPEG_BYTES.items():
            mb = count * avg / 1e6
            hours = count / IMAGES_PER_SEC / 3600
            print(
                f"{count:>10,} | {edge:>5} | {mb:>9,.0f} | "
                f"{mb + audio_bytes / 1e6:>9,.0f} | {hours:>5.1f} h"
            )
        print()
    print("=" * 64)


if __name__ == "__main__":
    main()

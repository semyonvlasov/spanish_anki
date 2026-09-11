"""Build a self-contained page showing what each card would actually look up.

Judging the image search from filenames is hopeless; this puts the Spanish
sentence, its English gloss, the query that was derived from it and the
picture that came back side by side, with the images inlined so the page is
one file that opens anywhere.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import random
from pathlib import Path

from fetch_images import entry_images


def data_uri(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notes", type=Path, default=Path("data/notes.json"))
    ap.add_argument("--tags", type=Path, default=Path("data/tags.json"))
    ap.add_argument("--manifest", type=Path, default=Path("data/images.json"))
    ap.add_argument("--images-dir", type=Path, default=Path("build/images"))
    ap.add_argument("--out", type=Path, default=Path("build/contact-sheet.html"))
    ap.add_argument("--max", type=int, default=120)
    args = ap.parse_args()

    notes = json.loads(args.notes.read_text(encoding="utf-8"))["notes"]
    tags = json.loads(args.tags.read_text(encoding="utf-8"))["tags"]
    images = json.loads(args.manifest.read_text(encoding="utf-8"))["images"]

    # One example sentence per concept that actually has a picture.
    by_concept: dict[str, dict] = {}
    for note in notes:
        key = (tags.get(note["guid"]) or {}).get("concept_key")
        if key and key in images and key not in by_concept:
            by_concept[key] = note

    rows = []
    for key, note in list(by_concept.items())[: args.max]:
        entry = images[key]
        tag = tags[note["guid"]]
        cells = []
        for image in entry_images(entry):
            path = args.images_dir / image["file"]
            if not path.exists():
                continue
            cells.append(
                f'<figure><img src="{data_uri(path)}" loading="lazy">'
                f'<figcaption>{html.escape(image.get("term") or "")} '
                f'<span>{html.escape(image.get("provider") or "")}</span></figcaption></figure>'
            )
        if not cells:
            continue
        terms = " + ".join(t["query"] for t in tag.get("terms", [])) or "&mdash;"
        rows.append(
            f'<article><div class="text">'
            f'<p class="es">{html.escape(note["spanish"])}</p>'
            f'<p class="en">{html.escape(note["english"])}</p>'
            f'<p class="meta">query <code>{html.escape(tag.get("query", ""))}</code></p>'
            f'<p class="meta">parts {terms} &middot; {entry["notes"]} cards share this</p>'
            f'</div><div class="shots">{"".join(cells)}</div></article>'
        )

    providers: dict[str, int] = {}
    for entry in images.values():
        for image in entry_images(entry):
            providers[image["provider"]] = providers.get(image["provider"], 0) + 1
    paired = sum(1 for e in images.values() if len(entry_images(e)) > 1)

    page = f"""<!doctype html>
<meta charset="utf-8">
<title>Image search contact sheet</title>
<style>
body {{ margin:0; padding:26px; background:#eceae5; font:15px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif; color:#241f1a; }}
h1 {{ font-size:20px; margin:0 0 4px; }}
.summary {{ margin:0 0 22px; color:#5d564c; font-size:14px; }}
article {{ display:grid; grid-template-columns:minmax(240px,1fr) auto; gap:18px; align-items:center;
  background:#fff; border-radius:12px; padding:14px 16px; margin-bottom:12px; box-shadow:0 1px 3px rgba(0,0,0,.09); }}
.es {{ font-weight:600; margin:0 0 3px; }}
.en {{ margin:0 0 8px; color:#2f6f4f; }}
.meta {{ margin:2px 0; font-size:12.5px; color:#6f675c; }}
code {{ background:#f2efe9; padding:1px 6px; border-radius:5px; }}
.shots {{ display:flex; gap:10px; }}
figure {{ margin:0; text-align:center; }}
figure img {{ height:150px; width:auto; max-width:230px; object-fit:cover; border-radius:9px; display:block; }}
figcaption {{ font-size:11.5px; color:#7b7367; margin-top:4px; }}
figcaption span {{ opacity:.65; }}
@media (max-width:760px) {{ article {{ grid-template-columns:1fr; }} }}
</style>
<h1>Image search contact sheet</h1>
<p class="summary">{len(rows)} concepts shown &middot; {paired:,} of {len(images):,} concepts
came back as a pair &middot; providers: {html.escape(str(providers))}</p>
{"".join(rows)}
"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB, {len(rows)} rows)")


if __name__ == "__main__":
    main()

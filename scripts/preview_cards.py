"""Render the card templates to a standalone HTML page for eyeballing.

The templates and CSS are imported from build_decks, not copied, so the
preview cannot drift from what actually ships in the decks.
"""

from __future__ import annotations

import argparse
import base64
import io
import re
from pathlib import Path

from build_decks import (
    CSS,
    FORWARD_AFMT,
    FORWARD_QFMT,
    REVERSE_AFMT,
    REVERSE_QFMT,
)

SAMPLE = {
    "Spanish": "El anciano montaba en bicicleta.",
    "English": "The old man was riding a bicycle.",
}


def sample_image(kind: str = "bicycle") -> str:
    """A stand-in photo, drawn locally so the preview needs no network."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (600, 360), (208, 220, 228))
    draw = ImageDraw.Draw(image)
    if kind == "person":
        draw.ellipse((265, 90, 335, 160), outline=(70, 80, 90), width=9)
        draw.line([(300, 160), (300, 265)], fill=(70, 80, 90), width=9)
        draw.line([(300, 185), (245, 230)], fill=(70, 80, 90), width=9)
        draw.line([(300, 185), (355, 230)], fill=(70, 80, 90), width=9)
        draw.line([(300, 265), (262, 320)], fill=(70, 80, 90), width=9)
        draw.line([(300, 265), (338, 320)], fill=(70, 80, 90), width=9)
        draw.text((228, 330), "sample: \"old man\"", fill=(90, 100, 110))
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    for i in range(0, 600, 24):
        draw.line([(i, 360), (i + 120, 0)], fill=(196, 210, 220), width=8)
    draw.ellipse((150, 190, 250, 290), outline=(70, 80, 90), width=9)
    draw.ellipse((350, 190, 450, 290), outline=(70, 80, 90), width=9)
    draw.line([(200, 240), (300, 165), (400, 240)], fill=(70, 80, 90), width=8)
    draw.line([(300, 165), (330, 150)], fill=(70, 80, 90), width=8)
    draw.text((196, 316), "sample context image", fill=(90, 100, 110))
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def render(template: str, fields: dict[str, str]) -> str:
    """Resolve the Anki template syntax this deck actually uses."""
    out = template

    # {{#Field}} ... {{/Field}} -- keep the body only when the field is filled.
    def section(match: re.Match) -> str:
        name, body = match.group(1), match.group(2)
        return body if fields.get(name) else ""

    out = re.sub(r"\{\{#(\w+)\}\}(.*?)\{\{/\1\}\}", section, out, flags=re.S)
    for name, value in fields.items():
        out = out.replace(f"{{{{{name}}}}}", value)
    return re.sub(r"\{\{[^}]+\}\}", "", out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("build/card-preview.html"))
    args = ap.parse_args()

    single = f'<div class="img"><img src="{sample_image()}"></div>'
    pair = (
        f'<div class="img pair"><img src="{sample_image(kind="person")}">'
        f'<img src="{sample_image()}"></div>'
    )
    img = single
    # Anki turns [sound:x] into its own play button; show a stand-in here.
    play = '<span style="font-size:15px;opacity:.6">[Anki play button]</span>'
    player = '<audio controls preload="none"></audio>'

    forward = {**SAMPLE, "Audio": play, "AudioPlayer": "", "Image": img}
    reverse = {**SAMPLE, "Audio": play, "AudioPlayer": player, "Image": img}

    reverse_pair = {**reverse, "Image": pair}
    forward_pair = {**forward, "Image": pair}

    faces = [
        ("ES&rarr;EN &mdash; question", "the image is hidden until asked for", render(FORWARD_QFMT, forward)),
        ("ES&rarr;EN &mdash; answer", "translation and image revealed", render(FORWARD_AFMT, forward)),
        ("EN&rarr;ES &mdash; question", "image up front, Spanish audio hidden", render(REVERSE_QFMT, reverse)),
        ("EN&rarr;ES &mdash; answer", "Spanish revealed, audio replays", render(REVERSE_AFMT, reverse)),
        ("EN&rarr;ES &mdash; a pair", "no single photo fits, so both words are shown", render(REVERSE_QFMT, reverse_pair)),
        ("ES&rarr;EN &mdash; a pair, revealed", "the same pair behind the hint", render(FORWARD_AFMT, forward_pair)),
    ]

    blocks = "\n".join(
        f'<figure><figcaption><b>{title}</b><span>{note}</span></figcaption>'
        f'<div class="card">{body}</div></figure>'
        for title, note, body in faces
    )

    page = f"""<!doctype html>
<meta charset="utf-8">
<title>Card preview</title>
<style>
body {{ margin:0; padding:28px; background:#e9e7e2; font-family:-apple-system,"Segoe UI",Roboto,sans-serif; }}
h1 {{ font-size:19px; margin:0 0 6px; }}
p.lead {{ margin:0 0 24px; color:#5b564d; font-size:14px; max-width:70ch; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(330px,1fr)); gap:20px; }}
figure {{ margin:0; background:#fff; border-radius:14px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,.12); }}
figcaption {{ padding:10px 14px; background:#3d3a34; color:#fff; font-size:13px; display:flex; justify-content:space-between; gap:12px; }}
figcaption span {{ opacity:.65; }}
{CSS}
</style>
<h1>Card preview</h1>
<p class="lead">Rendered from the same templates and CSS the decks ship with.
Click a hint button to check it opens. Anki substitutes its own play button for
<code>[sound:]</code>; the hinted player on the reverse card is a real
<code>&lt;audio&gt;</code> element, which is what keeps the Spanish from
auto-playing on the question side.</p>
<div class="grid">
{blocks}
</div>
"""
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

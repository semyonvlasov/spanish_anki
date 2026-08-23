"""Find one context image per note and cache it locally.

Providers are tried in order and the first usable hit wins.  Keyed providers
(Pexels / Pixabay / Unsplash) give the nicest results but need a free API key
in the environment; the keyless ones keep the pipeline working without any.
A generated text card is used as a last resort so a build never breaks.

Results are recorded in a manifest so re-runs only fetch what is missing.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont

USER_AGENT = "spanish-anki-deck-builder/1.0 (personal study deck; contact via github.com/semyonvlasov)"
TIMEOUT = 45
MAX_EDGE = 800  # overridden by --max-edge
JPEG_QUALITY = 82
MIN_BYTES = 2_000

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


# --------------------------------------------------------------------------
# query building
# --------------------------------------------------------------------------

STOP_PREFIXES = ("to ", "the ", "a ", "an ")


def build_query(record: dict) -> str:
    """Turn a note into an image-search query (English reads far better)."""
    text = (record.get("english") or record.get("spanish") or "").strip()
    text = re.sub(r"\([^)]*\)", " ", text)          # parenthetical glosses
    text = re.sub(r"[\"“”'’!?.,;:]+", " ", text)
    text = re.sub(r"\s*/\s*", " ", text)             # "hello / hi" -> first sense
    text = " ".join(text.split())
    lowered = text.lower()
    for prefix in STOP_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.strip() or (record.get("spanish") or "").strip()


# --------------------------------------------------------------------------
# providers -- each returns a list of candidate image URLs
# --------------------------------------------------------------------------

def _json(url: str, **kwargs) -> dict:
    resp = session.get(url, timeout=TIMEOUT, **kwargs)
    resp.raise_for_status()
    return resp.json()


def search_pexels(query: str) -> list[str]:
    key = os.environ.get("PEXELS_API_KEY")
    if not key:
        return []
    data = _json(
        "https://api.pexels.com/v1/search",
        params={"query": query, "per_page": 5, "orientation": "landscape"},
        headers={"Authorization": key},
    )
    return [p["src"]["large"] for p in data.get("photos", [])]


def search_pixabay(query: str) -> list[str]:
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        return []
    data = _json(
        "https://pixabay.com/api/",
        params={"key": key, "q": query, "per_page": 5, "image_type": "photo", "safesearch": "true"},
    )
    return [h["largeImageURL"] for h in data.get("hits", [])]


def search_unsplash(query: str) -> list[str]:
    key = os.environ.get("UNSPLASH_ACCESS_KEY")
    if not key:
        return []
    data = _json(
        "https://api.unsplash.com/search/photos",
        params={"query": query, "per_page": 5, "orientation": "landscape"},
        headers={"Authorization": f"Client-ID {key}"},
    )
    return [r["urls"]["regular"] for r in data.get("results", [])]


def search_openverse(query: str) -> list[str]:
    data = _json(
        "https://api.openverse.org/v1/images/",
        params={"q": query, "page_size": 5, "mature": "false"},
    )
    return [r["url"] for r in data.get("results", []) if r.get("url")]


def search_wikimedia(query: str) -> list[str]:
    data = _json(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            "gsrlimit": 5,
            "gsrnamespace": 6,
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": MAX_EDGE,
            "format": "json",
        },
    )
    pages = (data.get("query") or {}).get("pages") or {}
    urls = []
    for page in pages.values():
        for info in page.get("imageinfo", []):
            urls.append(info.get("thumburl") or info.get("url"))
    return [u for u in urls if u]


_ddg_vqd: dict[str, str] = {}


def search_duckduckgo(query: str) -> list[str]:
    """Unofficial DDG image endpoint -- no key, best coverage for phrases."""
    token = _ddg_vqd.get(query)
    if not token:
        resp = session.post("https://duckduckgo.com/", data={"q": query}, timeout=TIMEOUT)
        match = re.search(r'vqd=["\']?([\d-]+)', resp.text)
        if not match:
            return []
        token = match.group(1)
        _ddg_vqd[query] = token
    resp = session.get(
        "https://duckduckgo.com/i.js",
        params={"l": "us-en", "o": "json", "q": query, "vqd": token, "f": ",,,", "p": "1"},
        headers={"Referer": "https://duckduckgo.com/"},
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        return []
    return [r["image"] for r in resp.json().get("results", []) if r.get("image")]


PROVIDERS = [
    ("pexels", search_pexels),
    ("unsplash", search_unsplash),
    ("pixabay", search_pixabay),
    ("duckduckgo", search_duckduckgo),
    ("openverse", search_openverse),
    ("wikimedia", search_wikimedia),
]


# --------------------------------------------------------------------------
# download + normalise
# --------------------------------------------------------------------------

def normalise(blob: bytes) -> bytes | None:
    """Downscale to a sane size and re-encode as JPEG, or ``None`` if unusable."""
    try:
        image = Image.open(io.BytesIO(blob))
        image.load()
    except Exception:
        return None
    if min(image.size) < 80:
        return None
    if image.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", image.size, (255, 255, 255))
        image = image.convert("RGBA")
        background.paste(image, mask=image.split()[-1])
        image = background
    else:
        image = image.convert("RGB")
    image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def placeholder(text: str) -> bytes:
    """A plain typographic card, used when every provider comes up empty."""
    image = Image.new("RGB", (MAX_EDGE, 450), (247, 244, 238))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 44)
    except OSError:
        font = ImageFont.load_default()
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) > MAX_EDGE - 80:
            lines.append(current)
            current = word
        else:
            current = trial
    lines.append(current)
    y = 225 - len(lines) * 28
    for line in lines:
        draw.text(((MAX_EDGE - draw.textlength(line, font=font)) / 2, y), line, font=font, fill=(60, 55, 50))
        y += 56
    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


def fetch_one(target: dict, images_dir: Path, allow_placeholder: bool) -> dict | None:
    query = target["query"]
    if not query:
        return None
    filename = f"{hashlib.sha1(target['key'].encode()).hexdigest()[:16]}.jpg"

    for provider_name, provider in PROVIDERS:
        try:
            urls = provider(query)
        except Exception as exc:
            print(f"    [{provider_name}] {query!r}: {exc}", file=sys.stderr)
            continue
        for url in urls[:4]:
            try:
                resp = session.get(url, timeout=TIMEOUT)
                if resp.status_code != 200 or len(resp.content) < MIN_BYTES:
                    continue
                jpeg = normalise(resp.content)
            except Exception:
                continue
            if not jpeg:
                continue
            (images_dir / filename).write_bytes(jpeg)
            return {
                "key": target["key"],
                "notes": target["notes"],
                "query": query,
                "provider": provider_name,
                "source_url": url,
                "file": filename,
                "bytes": len(jpeg),
                "sha1": hashlib.sha1(jpeg).hexdigest(),
            }

    if not allow_placeholder:
        return None
    jpeg = placeholder(query)
    (images_dir / filename).write_bytes(jpeg)
    return {
        "key": target["key"],
        "notes": target["notes"],
        "query": query,
        "provider": "placeholder",
        "source_url": None,
        "file": filename,
        "bytes": len(jpeg),
        "sha1": hashlib.sha1(jpeg).hexdigest(),
    }


def build_targets(notes: list[dict], tags: dict[str, dict]) -> list[dict]:
    """Collapse notes onto shared concepts, most-reused concept first.

    With a fetch budget, spending it on the concept that covers 40 cards beats
    spending it on 40 one-off sentences.
    """
    grouped: dict[str, dict] = {}
    for record in notes:
        tag = tags.get(record["guid"])
        if tag:
            key, query = tag.get("concept_key", ""), tag.get("query", "")
        else:  # no tagging pass available -- fall back to the raw sentence
            query = build_query(record)
            key = query.lower()
        if not key or not query:
            continue
        entry = grouped.setdefault(key, {"key": key, "query": query, "notes": 0})
        entry["notes"] += 1
    return sorted(grouped.values(), key=lambda t: -t["notes"])


def main() -> None:
    global MAX_EDGE

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notes", type=Path, default=Path("data/notes.json"))
    ap.add_argument("--tags", type=Path, default=Path("data/tags.json"))
    ap.add_argument("--manifest", type=Path, default=Path("data/images.json"))
    ap.add_argument("--images-dir", type=Path, default=Path("build/images"))
    ap.add_argument("--limit", type=int, default=0, help="only fetch N missing concepts (0 = all)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-edge", type=int, default=MAX_EDGE,
                    help="longest image edge in pixels; drives the finished deck's size")
    ap.add_argument("--no-placeholder", action="store_true")
    ap.add_argument("--retry-placeholders", action="store_true",
                    help="re-search concepts that previously fell back to a placeholder")
    args = ap.parse_args()

    MAX_EDGE = args.max_edge
    print(f"normalising images to {MAX_EDGE}px on the longest edge")

    notes = json.loads(args.notes.read_text(encoding="utf-8"))["notes"]
    tags = {}
    if args.tags.exists():
        tags = json.loads(args.tags.read_text(encoding="utf-8")).get("tags", {})
    else:
        print(f"note: {args.tags} not found, falling back to raw-sentence queries")

    targets = build_targets(notes, tags)
    covered = sum(t["notes"] for t in targets)
    print(
        f"{len(notes):,} notes -> {len(targets):,} distinct concepts "
        f"covering {covered:,} notes ({covered / max(len(notes), 1):.0%})"
    )

    args.images_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict] = {}
    if args.manifest.exists():
        manifest = json.loads(args.manifest.read_text(encoding="utf-8")).get("images", {})

    def needs_fetch(target: dict) -> bool:
        entry = manifest.get(target["key"])
        if not entry:
            return True
        if not (args.images_dir / entry["file"]).exists():
            return True
        return args.retry_placeholders and entry.get("provider") == "placeholder"

    pending = [t for t in targets if needs_fetch(t)]
    if args.limit:
        pending = pending[: args.limit]
    print(f"{len(targets) - len(pending):,} already cached, fetching {len(pending):,}")

    def worker(target: dict) -> dict | None:
        time.sleep(random.uniform(0.1, 0.5))  # be polite to the free endpoints
        try:
            return fetch_one(target, args.images_dir, not args.no_placeholder)
        except Exception as exc:
            print(f"  failed {target['key']}: {exc}", file=sys.stderr)
            return None

    def save() -> None:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps({"images": manifest}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(worker, pending):
            done += 1
            if result:
                manifest[result["key"]] = result
            if done % 25 == 0 or done == len(pending):
                print(f"  {done:,}/{len(pending):,}")
                save()
    save()

    by_provider: dict[str, int] = {}
    for entry in manifest.values():
        by_provider[entry["provider"]] = by_provider.get(entry["provider"], 0) + 1
    notes_with_image = sum(
        t["notes"] for t in targets if t["key"] in manifest
    )
    print(
        f"manifest covers {len(manifest):,}/{len(targets):,} concepts "
        f"= {notes_with_image:,}/{len(notes):,} notes ({notes_with_image / max(len(notes), 1):.0%})"
    )
    print(f"  by provider: {by_provider}")


if __name__ == "__main__":
    main()

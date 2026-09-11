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
    # The thumbnail is already close to the size we downscale to; the original
    # is often several megabytes of detail we immediately discard.
    return [
        url
        for r in data.get("results", [])
        for url in [r.get("thumbnail") or r.get("url")]
        if url
    ]


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


PER_CONCEPT_DEADLINE = 90  # seconds; a stalling provider must not stall the run


def fetch_single(query: str, dest: Path, deadline: float) -> dict | None:
    """Download the first usable image for one query, or return None."""
    for provider_name, provider in PROVIDERS:
        if time.monotonic() > deadline:
            return None
        try:
            urls = provider(query)
        except Exception as exc:
            print(f"    [{provider_name}] {query!r}: {exc}", file=sys.stderr)
            continue
        for url in urls[:4]:
            if time.monotonic() > deadline:
                return None
            try:
                resp = session.get(url, timeout=TIMEOUT)
                if resp.status_code != 200 or len(resp.content) < MIN_BYTES:
                    continue
                jpeg = normalise(resp.content)
            except Exception:
                continue
            if not jpeg:
                continue
            dest.write_bytes(jpeg)
            return {
                "file": dest.name,
                "term": query,
                "provider": provider_name,
                "source_url": url,
                "bytes": len(jpeg),
                "sha1": hashlib.sha1(jpeg).hexdigest(),
            }
    return None


def _name_for(*parts: str) -> str:
    return hashlib.sha1(":".join(parts).encode()).hexdigest()[:16] + ".jpg"


def fetch_one(target: dict, images_dir: Path, allow_placeholder: bool) -> dict | None:
    """One image for the whole idea, or one per part when that fails.

    "The old man was riding a bicycle" is two vocabulary items. A single photo
    of an old man on a bicycle says it best, but those are not always to be
    found -- and showing an old man beside a bicycle still teaches both words,
    which a picture of only the bicycle does not.
    """
    deadline = time.monotonic() + PER_CONCEPT_DEADLINE
    query = target.get("query") or ""
    terms = target.get("terms") or []

    images: list[dict] = []
    if query:
        found = fetch_single(query, images_dir / _name_for(target["key"]), deadline)
        if found:
            images.append(found)

    if not images and len(terms) > 1:
        for term in terms:
            if time.monotonic() > deadline:
                break
            found = fetch_single(
                term["query"], images_dir / _name_for(target["key"], term["key"]), deadline
            )
            if found:
                images.append(found)

    # A single part is worth trying on its own only if the combined query and
    # the parts both came up empty.
    if not images and len(terms) == 1 and terms[0]["query"] != query:
        found = fetch_single(
            terms[0]["query"], images_dir / _name_for(target["key"], terms[0]["key"]), deadline
        )
        if found:
            images.append(found)

    if not images:
        if not allow_placeholder:
            return None
        dest = images_dir / _name_for(target["key"])
        jpeg = placeholder(query or target["key"])
        dest.write_bytes(jpeg)
        images.append({
            "file": dest.name,
            "term": query,
            "provider": "placeholder",
            "source_url": None,
            "bytes": len(jpeg),
            "sha1": hashlib.sha1(jpeg).hexdigest(),
        })

    return {
        "key": target["key"],
        "notes": target["notes"],
        "query": query,
        "provider": images[0]["provider"],
        "composed": len(images) > 1,
        "images": images,
    }


def entry_images(entry: dict) -> list[dict]:
    """Read both the current manifest shape and the earlier single-file one."""
    if not entry:
        return []
    if entry.get("images"):
        return entry["images"]
    if entry.get("file"):
        return [{
            "file": entry["file"],
            "term": entry.get("query", ""),
            "provider": entry.get("provider", ""),
            "source_url": entry.get("source_url"),
            "bytes": entry.get("bytes", 0),
            "sha1": entry.get("sha1", ""),
        }]
    return []


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
        entry = grouped.setdefault(
            key,
            {"key": key, "query": query, "notes": 0, "terms": (tag or {}).get("terms", [])},
        )
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
    ap.add_argument("--sample", type=int, default=0,
                    help="fetch a random sample of N concepts instead of the most-reused ones; "
                         "the frequent concepts are generic verbs and judging quality by them "
                         "is misleading")
    ap.add_argument("--seed", type=int, default=1, help="seed for --sample, so runs are repeatable")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--time-budget", type=float, default=0.0,
                    help="stop fetching after this many minutes and keep what was found; "
                         "0 = no limit. Turns a hard job timeout, which loses everything, "
                         "into a partial build that still ships.")
    ap.add_argument("--max-edge", type=int, default=MAX_EDGE,
                    help="longest image edge in pixels; drives the finished deck's size")
    ap.add_argument("--no-placeholder", action="store_true")
    ap.add_argument("--retry-placeholders", action="store_true",
                    help="re-search concepts that previously fell back to a placeholder")
    ap.add_argument("--upgrade-from", default="",
                    help="comma-separated providers whose existing hits should be re-searched, "
                         "so adding an API key can replace weaker results (e.g. 'openverse')")
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

    stale_providers = {p.strip() for p in args.upgrade_from.split(",") if p.strip()}
    if args.retry_placeholders:
        stale_providers.add("placeholder")
    if stale_providers:
        print(f"re-searching anything previously found by: {sorted(stale_providers)}")

    def needs_fetch(target: dict) -> bool:
        entry = manifest.get(target["key"])
        images = entry_images(entry)
        if not images:
            return True
        if any(not (args.images_dir / i["file"]).exists() for i in images):
            return True
        return any(i.get("provider") in stale_providers for i in images)

    pending = [t for t in targets if needs_fetch(t)]
    if args.sample:
        rng = random.Random(args.seed)
        pending = rng.sample(pending, min(args.sample, len(pending)))
        print(f"random sample of {len(pending):,} concepts (seed {args.seed})")
    cached = len(targets) - len(pending)
    if args.limit and len(pending) > args.limit:
        print(f"{cached:,} already cached, {len(pending):,} missing, "
              f"taking the {args.limit:,} most-reused this run")
        pending = pending[: args.limit]
    else:
        print(f"{cached:,} already cached, fetching {len(pending):,}")

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

    started = time.monotonic()
    budget = args.time_budget * 60
    stopping = False

    def worker_guarded(target: dict) -> dict | None:
        # pool.map cannot be cancelled, so past the budget the remaining
        # targets fall through cheaply instead of hitting the network.
        if stopping:
            return None
        return worker(target)

    done = 0
    fetched = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(worker_guarded, pending):
            done += 1
            if result:
                manifest[result["key"]] = result
                fetched += 1
            if done % 25 == 0 or done == len(pending):
                elapsed = time.monotonic() - started
                rate = fetched / elapsed if elapsed else 0
                print(f"  {done:,}/{len(pending):,} attempted, {fetched:,} found, "
                      f"{rate:.1f}/s, {elapsed / 60:.0f} min elapsed", flush=True)
                save()
            if budget and not stopping and time.monotonic() - started > budget:
                print(f"  time budget of {args.time_budget:.0f} min reached -- "
                      f"stopping with {fetched:,} found; re-run to continue", flush=True)
                stopping = True
    save()

    by_provider: dict[str, int] = {}
    composed = 0
    for entry in manifest.values():
        images = entry_images(entry)
        if len(images) > 1:
            composed += 1
        for image in images:
            by_provider[image["provider"]] = by_provider.get(image["provider"], 0) + 1
    notes_with_image = sum(
        t["notes"] for t in targets if t["key"] in manifest
    )
    print(
        f"manifest covers {len(manifest):,}/{len(targets):,} concepts "
        f"= {notes_with_image:,}/{len(notes):,} notes ({notes_with_image / max(len(notes), 1):.0%})"
    )
    print(f"  by provider: {by_provider}")
    print(f"  concepts shown as two images: {composed:,}")


if __name__ == "__main__":
    main()

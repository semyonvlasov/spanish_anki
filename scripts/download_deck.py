"""Fetch the source deck from AnkiWeb (or a mirror given via ``--url``).

AnkiWeb has changed its download endpoint several times, so we try the known
shapes in turn and keep the first response that actually looks like a package.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

DEFAULT_DECK_ID = "1931042534"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
ZIP_MAGIC = b"PK\x03\x04"


def candidate_urls(deck_id: str) -> list[str]:
    return [
        f"https://ankiweb.net/svc/shared/download-deck/{deck_id}",
        f"https://ankiweb.net/shared/downloadDeck/{deck_id}",
        f"https://ankiweb.net/shared/download/{deck_id}",
    ]


def looks_like_package(blob: bytes) -> bool:
    return blob.startswith(ZIP_MAGIC) and len(blob) > 1024


def download_from_drive(file_id: str, out: Path) -> Path:
    """Fetch a public Drive file, handling the >100 MB confirmation redirect."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    url = "https://drive.usercontent.google.com/download"
    params = {"id": file_id, "export": "download", "confirm": "t"}

    with session.get(url, params=params, stream=True, timeout=300) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type:
            # Older interstitial: recover the confirm token and retry once.
            body = resp.text
            token = re.search(r'name="confirm"\s+value="([^"]+)"', body)
            uuid = re.search(r'name="uuid"\s+value="([^"]+)"', body)
            if token:
                params["confirm"] = token.group(1)
            if uuid:
                params["uuid"] = uuid.group(1)
            resp2 = session.get(url, params=params, stream=True, timeout=300)
            resp2.raise_for_status()
            return _stream_to(resp2, out)
        return _stream_to(resp, out)


def _stream_to(resp, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with open(out, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1 << 20):
            fh.write(chunk)
            total += len(chunk)
    head = out.open("rb").read(4)
    if head != ZIP_MAGIC:
        raise SystemExit(
            f"downloaded {total:,} bytes but it is not a package (starts with {head!r}); "
            "is the Drive file shared with 'anyone with the link'?"
        )
    print(f"downloaded {total:,} bytes -> {out}")
    return out


def download(deck_id: str, out: Path, explicit_url: str | None = None) -> Path:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Referer": f"https://ankiweb.net/shared/info/{deck_id}",
            "Accept": "*/*",
        }
    )

    # Priming the info page gives us the cookies the download endpoint expects.
    try:
        session.get(f"https://ankiweb.net/shared/info/{deck_id}", timeout=60)
    except requests.RequestException as exc:
        print(f"  note: could not load info page ({exc})", file=sys.stderr)

    urls = [explicit_url] if explicit_url else candidate_urls(deck_id)
    errors: list[str] = []
    for url in urls:
        for method in ("GET", "POST"):
            try:
                resp = session.request(method, url, timeout=180, allow_redirects=True)
            except requests.RequestException as exc:
                errors.append(f"{method} {url}: {exc}")
                continue
            if resp.status_code != 200:
                errors.append(f"{method} {url}: HTTP {resp.status_code}")
                continue
            if not looks_like_package(resp.content):
                errors.append(
                    f"{method} {url}: not a package "
                    f"(content-type={resp.headers.get('content-type')}, {len(resp.content)} bytes)"
                )
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(resp.content)
            print(f"downloaded {len(resp.content):,} bytes from {method} {url} -> {out}")
            return out

    raise SystemExit("could not download deck:\n  " + "\n  ".join(errors))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deck-id", default=DEFAULT_DECK_ID)
    ap.add_argument("--drive-id", default=None, help="download from a public Google Drive file id")
    ap.add_argument("--url", default=None, help="explicit download URL, bypasses discovery")
    ap.add_argument("--out", type=Path, default=Path("source/source_deck.apkg"))
    args = ap.parse_args()

    if args.out.exists() and args.out.stat().st_size > 1024:
        print(f"{args.out} already present, skipping download")
        return
    if args.drive_id:
        download_from_drive(args.drive_id, args.out)
        return
    download(args.deck_id, args.out, args.url)


if __name__ == "__main__":
    main()

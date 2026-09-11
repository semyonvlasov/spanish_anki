"""Have a vision model look at each fetched picture and say if it fits.

A stock library answers every query with something, so a picture being present
says nothing about whether it shows what the card is about. This pass puts the
sentence and the picture in front of a model and asks. Rejected pictures can be
replaced by the next candidate from the same providers.

Verdicts are written into the manifest so the contact sheet can show them and
the rejection rate can be reported rather than guessed at.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from fetch_images import PER_CONCEPT_DEADLINE, entry_images, fetch_single

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-flash"

SYSTEM = """You check whether a photograph is usable on a language-learning flashcard.

You are given what the picture is meant to show, and the picture. Answer:

  verdict  "ok" if the picture shows that closely enough that a learner would
           connect the two, "reject" otherwise
  reason   one short clause saying why

Reject a picture that is merely topically adjacent -- a keyboard for "writing a
letter", a stock businessman for "elderly man". Reject text-heavy images,
screenshots, logos, collages and charts. Accept a picture that shows the idea
even if the details differ; it is a memory aid, not an illustration.

Judge the picture ONLY against what it is meant to show. When the card carries
two pictures, each one carries half the idea: do not reject a picture for
missing what the other one is there to supply.

Reply with JSON only: {"verdict": "...", "reason": "..."}"""

REQUERY_SYSTEM = """You repair a failed image search for a language-learning flashcard.

A search was run, a picture came back, and a reviewer rejected it. You get the
sentence, the query that was used and why the picture was rejected. Answer:

  query  a different search query, 2-4 words, concrete and visual, that avoids
         the stated problem. Do not repeat the old query.
  skip   true when the idea simply cannot be photographed, and any picture
         would mislead more than it helps

Be decisive about skip. Abstractions -- belonging, truth, names, doubt,
permission, politeness -- have no photograph. A stock library will answer such
a query with motivational text on a background, which is worse than no picture.

When the thing does exist but the library lacks it (a rotten pear), prefer the
nearest thing that is still correct ("rotten fruit"), never a different thing.

Reply with JSON only: {"query": "...", "skip": false}"""


def encode(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def post(body: dict, api_key: str) -> dict | None:
    headers = {"Authorization": f"Bearer {api_key}", "X-Title": "spanish-anki-deck-builder"}
    for attempt in range(3):
        try:
            resp = requests.post(ENDPOINT, json=body, headers=headers, timeout=120)
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
            return json.loads(content)
        except Exception as exc:
            if attempt == 2:
                return {"_error": str(exc)}
            time.sleep(2 * (attempt + 1))
    return None


def requery(sentence: str, old_query: str, reason: str, model: str, api_key: str) -> dict:
    """Turn a rejection into a better query, or a decision not to try again."""
    parsed = post({
        "model": model,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": REQUERY_SYSTEM},
            {"role": "user", "content":
                f"Sentence: {sentence}\nQuery used: {old_query}\nRejected because: {reason}"},
        ],
    }, api_key) or {}
    if parsed.get("_error") or parsed.get("skip"):
        return {"query": "", "skip": True}
    new = " ".join(str(parsed.get("query", "")).split())[:80]
    if not new or new.lower() == old_query.lower():
        return {"query": "", "skip": True}
    return {"query": new, "skip": False}


def ask(sentence: str, query: str, image_path: Path, model: str, api_key: str,
        is_part: bool = False) -> dict:
    if is_part:
        brief = (f'This picture is one of two on the card. It is meant to show only: "{query}".\n'
                 f'For context, the card teaches: {sentence}\n'
                 f'Judge it against "{query}" alone.')
    else:
        brief = f'The picture is meant to show: "{query}".\nThe card teaches: {sentence}'

    parsed = post({
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": brief},
                {"type": "image_url", "image_url": {"url": encode(image_path)}},
            ]},
        ],
    }, api_key) or {}

    if parsed.get("_error") is not None or "verdict" not in parsed:
        # An unreachable judge must not silently delete pictures.
        return {"verdict": "ok", "reason": f"not checked: {parsed.get('_error', 'no verdict')}"[:200]}
    verdict = str(parsed.get("verdict", "")).lower()
    return {
        "verdict": "reject" if verdict.startswith("reject") else "ok",
        "reason": str(parsed.get("reason", ""))[:200],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notes", type=Path, default=Path("data/notes.json"))
    ap.add_argument("--tags", type=Path, default=Path("data/tags.json"))
    ap.add_argument("--manifest", type=Path, default=Path("data/images.json"))
    ap.add_argument("--images-dir", type=Path, default=Path("build/images"))
    ap.add_argument("--report", type=Path, default=Path("build/validation-report.json"))
    ap.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--requery", action="store_true",
                    help="turn each rejection into a fresh query and search again, once")
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set")

    notes = json.loads(args.notes.read_text(encoding="utf-8"))["notes"]
    tags = json.loads(args.tags.read_text(encoding="utf-8"))["tags"]
    doc = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest = doc["images"]

    # One representative sentence per concept, for context in the prompt.
    sentence_for: dict[str, str] = {}
    for note in notes:
        key = (tags.get(note["guid"]) or {}).get("concept_key")
        if key and key not in sentence_for:
            sentence_for[key] = note["english"]

    jobs = [
        (key, index, image)
        for key, entry in manifest.items()
        for index, image in enumerate(entry_images(entry))
    ]
    print(f"checking {len(jobs):,} pictures across {len(manifest):,} concepts with {args.model}")

    log: list[dict] = []

    def check(job) -> dict:
        key, index, image = job
        sentence = sentence_for.get(key, "")
        term = image.get("term") or ""
        is_part = len(entry_images(manifest[key])) > 1
        record = {"key": key, "index": index, "term": term, "is_part": is_part}

        answer = ask(sentence, term, args.images_dir / image["file"], args.model, api_key, is_part) \
            if (args.images_dir / image["file"]).exists() else {"verdict": "ok", "reason": "file missing"}
        image["verdict"] = answer["verdict"]
        image["reason"] = answer["reason"]
        record["verdict"] = answer["verdict"]
        record["reason"] = answer["reason"]

        if answer["verdict"] == "ok" or not args.requery:
            record["outcome"] = "kept" if answer["verdict"] == "ok" else "rejected"
            return record

        # One round of judging: a rejection buys a new query, not another
        # candidate for the query that just failed. The reasons showed the
        # query is usually what is wrong, and the next photo for a bad query
        # is wrong in the same way.
        repair = requery(sentence, term, answer["reason"], args.model, api_key)
        record["new_query"] = repair["query"]
        if repair["skip"]:
            image["dropped"] = True
            record["outcome"] = "dropped"
            return record

        replacement = fetch_single(
            repair["query"], args.images_dir / image["file"],
            time.monotonic() + PER_CONCEPT_DEADLINE,
        )
        if not replacement:
            image["dropped"] = True
            record["outcome"] = "dropped"
            record["note"] = "the new query found nothing either"
            return record

        image.update(replacement)
        image["term"] = repair["query"]
        image["requeried_from"] = term
        image["reason"] = answer["reason"]
        image.pop("verdict", None)          # deliberately not judged again
        record["outcome"] = "requeried"
        return record

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for done, record in enumerate(pool.map(check, jobs), start=1):
            log.append(record)
            if done % 10 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)}", flush=True)

    total = len(log)
    rejected = sum(1 for r in log if r["verdict"] == "reject")
    requeried = sum(1 for r in log if r.get("outcome") == "requeried")
    dropped = sum(1 for r in log if r.get("outcome") == "dropped")
    unchecked = sum(1 for r in log if r["reason"].startswith("not checked"))

    # Pictures the model gave up on must not reach the deck.
    for entry in manifest.values():
        if entry.get("images"):
            entry["images"] = [i for i in entry["images"] if not i.get("dropped")]
    empty = [k for k, e in manifest.items() if not entry_images(e)]
    for key in empty:
        del manifest[key]

    args.manifest.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "model": args.model, "requery": args.requery, "checked": total,
        "rejected": rejected, "requeried": requeried, "dropped": dropped,
        "concepts_left_without_a_picture": len(empty), "details": log,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print()
    print("=" * 58)
    print(f"pictures checked            : {total:,}")
    print(f"rejected by the model       : {rejected:,} ({rejected / max(total, 1):.0%})")
    if args.requery:
        print(f"  searched again, new query : {requeried:,}")
        print(f"  gave up, no picture       : {dropped:,}")
        print(f"concepts left with none     : {len(empty):,}")
    if unchecked:
        print(f"could not reach the model   : {unchecked:,} (kept, not dropped)")
    print("=" * 58)
    print()
    print("every rejection, and what was done about it:")
    for record in log:
        if record["verdict"] != "reject":
            continue
        part = " (one of a pair)" if record.get("is_part") else ""
        print(f"  {record['term'][:30]:<30}{part}")
        print(f"      why      : {record['reason'][:88]}")
        if record.get("outcome") == "requeried":
            print(f"      retried as: {record['new_query']}")
        else:
            print(f"      dropped  : {record.get('note', 'nothing photographable')}")


if __name__ == "__main__":
    main()

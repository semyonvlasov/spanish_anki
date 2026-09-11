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

You are given the sentence the card teaches and one picture. Answer:

  verdict  "ok" if the picture shows what the sentence is about closely enough
           that a learner would connect the two, "reject" otherwise
  reason   one short clause saying why

Reject a picture that is merely topically adjacent -- a keyboard for "writing a
letter", a stock businessman for "elderly man". Reject text-heavy images,
screenshots, logos, collages and charts. Accept a picture that shows the idea
even if the details differ; it is a memory aid, not an illustration.

Reply with JSON only: {"verdict": "...", "reason": "..."}"""


def encode(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()


def ask(sentence: str, query: str, image_path: Path, model: str, api_key: str) -> dict:
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "text",
                 "text": f"Sentence: {sentence}\nThe picture was found by searching: {query}"},
                {"type": "image_url", "image_url": {"url": encode(image_path)}},
            ]},
        ],
    }
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
            parsed = json.loads(content)
            verdict = str(parsed.get("verdict", "")).lower()
            return {
                "verdict": "reject" if verdict.startswith("reject") else "ok",
                "reason": str(parsed.get("reason", ""))[:200],
            }
        except Exception as exc:
            if attempt == 2:
                # An unreachable judge must not silently delete pictures.
                return {"verdict": "ok", "reason": f"not checked: {exc}"[:200]}
            time.sleep(2 * (attempt + 1))
    return {"verdict": "ok", "reason": "not checked"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notes", type=Path, default=Path("data/notes.json"))
    ap.add_argument("--tags", type=Path, default=Path("data/tags.json"))
    ap.add_argument("--manifest", type=Path, default=Path("data/images.json"))
    ap.add_argument("--images-dir", type=Path, default=Path("build/images"))
    ap.add_argument("--report", type=Path, default=Path("build/validation-report.json"))
    ap.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL))
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--refetch", action="store_true",
                    help="replace a rejected picture with the next candidate and re-check it")
    ap.add_argument("--max-attempts", type=int, default=3)
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
        tried = {image.get("source_url")} - {None}
        record = {"key": key, "index": index, "term": term, "attempts": []}

        for attempt in range(args.max_attempts):
            path = args.images_dir / image["file"]
            if not path.exists():
                record["final"] = "missing"
                return record
            answer = ask(sentence, term, path, args.model, api_key)
            record["attempts"].append({
                "file": image["file"], "provider": image.get("provider"),
                "verdict": answer["verdict"], "reason": answer["reason"],
            })
            image["verdict"] = answer["verdict"]
            image["reason"] = answer["reason"]
            if answer["verdict"] == "ok" or not args.refetch or attempt == args.max_attempts - 1:
                record["final"] = answer["verdict"]
                return record

            replacement = fetch_single(
                term, args.images_dir / image["file"], time.monotonic() + PER_CONCEPT_DEADLINE,
                exclude=tried,
            )
            if not replacement:
                record["final"] = "reject"
                record["exhausted"] = True
                return record
            tried.add(replacement["source_url"])
            image.update(replacement)
        record["final"] = image.get("verdict", "ok")
        return record

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for done, record in enumerate(pool.map(check, jobs), start=1):
            log.append(record)
            if done % 10 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)}", flush=True)

    first_pass_rejects = sum(1 for r in log if r["attempts"] and r["attempts"][0]["verdict"] == "reject")
    final_rejects = sum(1 for r in log if r.get("final") == "reject")
    rescued = first_pass_rejects - final_rejects
    total = len(log)
    # Distinguish "the retries were also bad" from "there were no retries".
    retried = sum(1 for r in log if len(r["attempts"]) > 1)
    exhausted = sum(1 for r in log if r.get("exhausted"))
    total_looks = sum(len(r["attempts"]) for r in log)
    unchecked = sum(
        1 for r in log for a in r["attempts"] if a["reason"].startswith("not checked")
    )

    args.manifest.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({
        "model": args.model, "refetch": args.refetch, "checked": total,
        "rejected_first_pass": first_pass_rejects, "rejected_final": final_rejects,
        "replaced_successfully": rescued, "retried": retried, "exhausted": exhausted,
        "total_judgements": total_looks, "details": log,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print()
    print("=" * 58)
    print(f"pictures checked            : {total:,}")
    print(f"rejected on the first look  : {first_pass_rejects:,} "
          f"({first_pass_rejects / max(total, 1):.0%})")
    if args.refetch:
        print(f"  a replacement was fetched : {retried:,} of those")
        print(f"  providers had none left   : {exhausted:,}")
        print(f"  replaced with a better one: {rescued:,}")
        print(f"  still rejected after retry: {final_rejects:,}")
    print(f"total judgements made       : {total_looks:,}"
          + (f" ({unchecked:,} could not reach the model)" if unchecked else ""))
    print("=" * 58)
    print()
    print("what it rejected, and why:")
    for record in log:
        for attempt in record["attempts"]:
            if attempt["verdict"] == "reject":
                print(f"  {record['term'][:34]:<34} [{attempt['provider']}] {attempt['reason'][:90]}")


if __name__ == "__main__":
    main()

"""Derive image queries with a language model, via OpenRouter.

The spaCy pass is mechanical: it lifts lemmas out of the parse tree, so
"El anciano montaba en bicicleta" becomes "riding old man bicycle" -- readable,
but not how anyone searches a photo library. A model reads the sentence, writes
the query the way a stock library is indexed ("elderly man cycling"), and --
this is the part the parser cannot do -- judges whether one photograph can
carry the idea at all, or whether the card needs two.

That judgement matters now that a large provider answers every query with
something: a search no longer fails when it misses the point, so failure can no
longer be the signal to split a concept in two.

Output matches data/tags.json from tag_notes.py, and anything the model
declines or fails on keeps its spaCy tag, so this pass only ever improves on it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-2.5-flash"

SYSTEM = """You turn sentences from a Spanish-English study deck into image-search queries.

For each numbered English sentence, reply with one object per sentence:

  i      the sentence number, unchanged
  query  2-4 words to search a stock photo library with. Write it the way such
         libraries are indexed -- concrete, visual, no articles, no names.
  parts  a list. ONE entry when a single photograph can show the whole idea.
         TWO entries when it cannot, each a separate searchable thing, so the
         card can show them side by side.
  skip   true when the sentence is abstract and no photograph would help

Rules:
- Never use a person's name. "Tom bought a car" is about buying a car.
- Prefer the thing being acted on, and the person or animal doing it when that
  is itself vocabulary worth a picture ("elderly man", "child"), not otherwise.
- Two parts are for sentences whose meaning lives in two separate things that a
  single photo rarely shows together. One part is the common case -- do not
  split an idea a single photo captures.
- skip abstractions: opinions, doubt, politeness, grammar drills. Better no
  picture than a misleading one.

Reply with JSON only: {"results": [ ... ]}. No prose, no code fences."""

EXAMPLE_IN = """1. The old man was riding a bicycle.
2. I don't know what you mean.
3. She is reading a book in the garden.
4. Tom feeds his dog every morning."""

EXAMPLE_OUT = json.dumps({"results": [
    {"i": 1, "query": "elderly man cycling", "parts": ["elderly man", "bicycle"], "skip": False},
    {"i": 2, "query": "", "parts": [], "skip": True},
    {"i": 3, "query": "woman reading book", "parts": ["reading a book"], "skip": False},
    {"i": 4, "query": "feeding a dog", "parts": ["feeding a dog"], "skip": False},
]}, ensure_ascii=False)


# Function words must not reach the key, or "reading a book" and "reading book"
# become two concepts and the same photo is downloaded twice.
KEY_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "with", "and", "or",
    "his", "her", "its", "their", "my", "your", "our", "some", "any", "this",
    "that", "these", "those", "is", "are", "was", "were", "be", "being", "been",
}


def normalise_key(text: str) -> str:
    words = [w for w in re.findall(r"[a-z]+", text.lower()) if w not in KEY_STOPWORDS]
    return "-".join(sorted(set(words)))


def call_model(batch: list[tuple[int, str]], model: str, api_key: str, retries: int = 3) -> dict:
    numbered = "\n".join(f"{i}. {text}" for i, text in batch)
    body = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": EXAMPLE_IN},
            {"role": "assistant", "content": EXAMPLE_OUT},
            {"role": "user", "content": numbered},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Title": "spanish-anki-deck-builder",
        "Content-Type": "application/json",
    }

    for attempt in range(retries):
        try:
            resp = requests.post(ENDPOINT, json=body, headers=headers, timeout=180)
            if resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
            parsed = json.loads(content)
            return {int(r["i"]): r for r in parsed.get("results", []) if "i" in r}
        except Exception as exc:
            if attempt == retries - 1:
                print(f"  batch failed: {exc}", file=sys.stderr)
                return {}
            time.sleep(2 * (attempt + 1))
    return {}


def to_tag(result: dict) -> dict | None:
    """Convert one model answer into the tags.json shape, or None to keep spaCy's."""
    if result.get("skip"):
        return {"keywords": [], "query": "", "concept_key": "", "strategy": "llm-skip",
                "terms": [], "compose": False}

    query = " ".join(str(result.get("query", "")).split())[:80]
    raw_parts = [" ".join(str(p).split())[:60] for p in (result.get("parts") or [])]
    parts = [p for p in raw_parts if p][:2]
    if not query and parts:
        query = parts[0]
    if not query:
        return None

    terms = [{"query": p, "key": normalise_key(p)} for p in parts] or [
        {"query": query, "key": normalise_key(query)}
    ]
    return {
        "keywords": query.split(),
        "query": query,
        "concept_key": normalise_key(query),
        "strategy": "llm",
        "terms": terms,
        # The model was asked outright; do not wait for a search to fail.
        "compose": len(terms) > 1,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notes", type=Path, default=Path("data/notes.json"))
    ap.add_argument("--tags", type=Path, default=Path("data/tags.json"),
                    help="spaCy tags, used as the fallback and written back in place")
    ap.add_argument("--model", default=os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL))
    ap.add_argument("--batch-size", type=int, default=40)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="only tag the first N notes (0 = all)")
    ap.add_argument("--sample", type=int, default=0, help="tag a random sample of N notes")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("OPENROUTER_API_KEY is not set; leaving the spaCy tags alone")
        return

    notes = json.loads(args.notes.read_text(encoding="utf-8"))["notes"]
    tags = json.loads(args.tags.read_text(encoding="utf-8"))["tags"]

    targets = notes
    if args.sample:
        import random
        targets = random.Random(args.seed).sample(notes, min(args.sample, len(notes)))
    elif args.limit:
        targets = notes[: args.limit]

    # The model sees each distinct sentence once, however many notes repeat it.
    by_text: dict[str, list[str]] = {}
    for note in targets:
        by_text.setdefault(note["english"].strip(), []).append(note["guid"])
    texts = list(by_text)
    print(f"{len(targets):,} notes -> {len(texts):,} distinct sentences, model {args.model}")

    batches = [
        list(enumerate(texts[i : i + args.batch_size], start=i))
        for i in range(0, len(texts), args.batch_size)
    ]

    answers: dict[int, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda b: call_model(b, args.model, api_key), batches):
            answers.update(result)
            done += 1
            if done % 10 == 0 or done == len(batches):
                print(f"  {done}/{len(batches)} batches, {len(answers):,} sentences answered",
                      flush=True)

    replaced = skipped = kept = 0
    for index, text in enumerate(texts):
        tag = to_tag(answers[index]) if index in answers else None
        if tag is None:
            kept += len(by_text[text])
            continue
        for guid in by_text[text]:
            tags[guid] = tag
        if tag["strategy"] == "llm-skip":
            skipped += len(by_text[text])
        else:
            replaced += len(by_text[text])

    args.tags.write_text(
        json.dumps({"tags": tags}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    concepts = {t["concept_key"] for t in tags.values() if t.get("concept_key")}
    composed = sum(1 for t in tags.values() if t.get("compose"))
    print(f"rewrote {replaced:,} notes, model skipped {skipped:,}, kept spaCy for {kept:,}")
    print(f"  distinct concepts now: {len(concepts):,}")
    print(f"  notes the model wants shown as a pair: {composed:,}")


if __name__ == "__main__":
    main()

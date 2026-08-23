"""Turn each English sentence into a short, searchable concept.

Tatoeba sentences make terrible image queries verbatim -- "Tom told Mary that
he had to go home to feed his dog" returns noise, while "feeding a dog" returns
what you actually want on the card.  This pass reduces every note to a handful
of content lemmas, entirely offline (spaCy, no LLM involved).

Notes that reduce to the same concept share one ``concept_key``, so the fetch
stage downloads a single image for all of them.  On a sentence corpus that
collapses the image count -- and the finished deck -- dramatically.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import spacy

# Light verbs carry no picture; a query built on them is worse than none.
GENERIC_VERBS = {
    "be", "have", "do", "get", "go", "make", "take", "let", "seem", "become",
    "say", "tell", "think", "know", "want", "need", "try", "keep", "put",
    "come", "give", "look", "feel", "mean", "use", "find", "leave", "call",
    "ask", "turn", "start", "begin", "happen", "like", "must", "will", "can",
}

GENERIC_NOUNS = {
    "thing", "way", "time", "day", "year", "man", "woman", "people", "person",
    "one", "kind", "sort", "lot", "bit", "part", "fact", "case", "point",
    "problem", "question", "idea", "reason", "something", "anything", "nothing",
    "everything", "someone", "anyone", "everyone", "today", "tomorrow",
    "yesterday", "morning", "evening", "night", "week", "month", "hour",
    "minute", "moment", "place", "world", "life", "name", "number",
}

# "a cup of coffee" is about the coffee, not the cup.
MEASURE_NOUNS = {
    "cup", "glass", "bottle", "piece", "slice", "bit", "lot", "pair", "bunch",
    "plate", "bowl", "box", "bag", "can", "loaf", "sheet", "couple",
}

CONTENT_POS = {"NOUN", "PROPN", "VERB", "ADJ"}
MAX_KEYWORDS = 3


def surface(token) -> str:
    """The form that searches best: gerunds beat lemmas ("riding" > "rid")."""
    if token.tag_ == "VBG":
        return token.text.lower()
    return token.lemma_.lower()


def load_nlp():
    # The parser gives us dependency labels; NER lets us drop Tom/Mary/Boston.
    return spacy.load("en_core_web_sm")


def extract(doc) -> tuple[list[str], list[str], str]:
    """Return (query words, dedup lemmas, strategy) for one parsed sentence."""
    person_tokens = {
        token.i
        for ent in doc.ents
        if ent.label_ in {"PERSON", "GPE", "LOC", "ORG", "FAC", "NORP"}
        for token in ent
    }

    def usable(token) -> bool:
        if token.i in person_tokens or token.is_stop or token.is_punct:
            return False
        if token.pos_ not in CONTENT_POS:
            return False
        lemma = token.lemma_.lower()
        if len(lemma) < 3 or not lemma.isalpha():
            return False
        if token.pos_ == "VERB" and lemma in GENERIC_VERBS:
            return False
        if token.pos_ in {"NOUN", "PROPN"} and lemma in GENERIC_NOUNS:
            return False
        return True

    def unwrap_measure(token):
        """'a cup of coffee' -> coffee; leaves anything else untouched."""
        if token.lemma_.lower() not in MEASURE_NOUNS:
            return token
        for prep in token.children:
            if prep.dep_ == "prep":
                for pobj in prep.children:
                    if pobj.dep_ == "pobj" and usable(pobj):
                        return pobj
        return token

    # Preferred shape: the main verb plus what it acts on ("feed dog").
    root = next((t for t in doc if t.dep_ == "ROOT"), None)
    obj = None
    if root is not None:
        for child in root.children:
            if child.dep_ in {"dobj", "pobj", "attr", "obj"} and usable(child):
                obj = unwrap_measure(child)
                break

    picked: list = []
    strategy = "content-words"
    if obj is not None:
        modifiers = [c for c in obj.children if c.pos_ == "ADJ" and usable(c)]
        if root is not None and usable(root):
            picked = [root, *modifiers[:1], obj]
            strategy = "verb+object"
        else:
            picked = [*modifiers[:1], obj]
            strategy = "object"

    if not picked:
        nouns = [t for t in doc if usable(t) and t.pos_ in {"NOUN", "PROPN"}]
        verbs = [t for t in doc if usable(t) and t.pos_ == "VERB"]
        adjs = [t for t in doc if usable(t) and t.pos_ == "ADJ"]
        picked = (nouns + verbs + adjs)[:MAX_KEYWORDS]
        strategy = "content-words" if picked else "none"

    # A lone adjective ("true", "ready") never makes a usable image query.
    if len(picked) == 1 and picked[0].pos_ == "ADJ":
        return [], [], "none"

    seen: set[str] = set()
    query_words: list[str] = []
    key_words: list[str] = []
    for token in picked:
        lemma = token.lemma_.lower()
        if lemma in seen:
            continue
        seen.add(lemma)
        query_words.append(surface(token))
        key_words.append(lemma)

    return query_words[:MAX_KEYWORDS], key_words[:MAX_KEYWORDS], strategy


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--notes", type=Path, default=Path("data/notes.json"))
    ap.add_argument("--out", type=Path, default=Path("data/tags.json"))
    ap.add_argument("--batch-size", type=int, default=200)
    ap.add_argument("--sample", type=int, default=0, help="print this many tagged examples")
    args = ap.parse_args()

    notes = json.loads(args.notes.read_text(encoding="utf-8"))["notes"]
    nlp = load_nlp()

    texts = [re.sub(r"\s+", " ", n["english"]).strip() for n in notes]
    tagged: dict[str, dict] = {}
    strategies: Counter[str] = Counter()
    concepts: Counter[str] = Counter()

    for note, doc in zip(notes, nlp.pipe(texts, batch_size=args.batch_size)):
        query_words, key_words, strategy = extract(doc)
        concept_key = "-".join(sorted(key_words)) if key_words else ""
        strategies[strategy] += 1
        if concept_key:
            concepts[concept_key] += 1
        tagged[note["guid"]] = {
            "keywords": query_words,
            "query": " ".join(query_words),
            "concept_key": concept_key,
            "strategy": strategy,
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"tags": tagged}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    taggable = sum(1 for t in tagged.values() if t["concept_key"])
    print(f"tagged {len(tagged):,} notes -> {args.out}")
    print(f"  strategies      : {dict(strategies)}")
    print(f"  with a concept  : {taggable:,}")
    print(f"  distinct concepts: {len(concepts):,}")
    if taggable:
        print(f"  reuse factor    : {taggable / max(len(concepts), 1):.1f} notes per image")
    print(f"  most common     : {concepts.most_common(10)}")

    if args.sample:
        print("\nexamples:")
        for note, tag in list(zip(notes, tagged.values()))[: args.sample]:
            print(f"  {note['english'][:64]:<64} -> {tag['query']!r} [{tag['strategy']}]")


if __name__ == "__main__":
    main()

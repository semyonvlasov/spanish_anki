# spanish_anki_builder

Rebuilds a Spanish/English Anki deck (Tatoeba sentences with audio) into two
study variants, each with a context image found automatically on the web.

| deck | front | behind the hint button | back |
|---|---|---|---|
| `spanish_es-en_hinted-image.apkg` | Spanish phrase + audio (auto-plays) | the context image | English translation + image |
| `spanish_en-es_hinted-audio.apkg` | English phrase + context image | Spanish audio (manual play) | Spanish phrase + audio |

## Why the reverse deck does not use `[sound:]` on the front

Anki auto-plays every `[sound:...]` tag it finds on the question side. On the
EN→ES deck that would speak the Spanish answer before you had a chance to
guess it, so the hinted player is a plain `<audio controls>` element pointing
at the same media file. The native `[sound:]` tag is still used on the answer
side, where the replay is wanted.

The hint itself is a `<details>/<summary>` pair styled as a button — no
JavaScript, so it behaves the same on desktop Anki, AnkiDroid and AnkiMobile.

## Pipeline

```
download_deck.py -> unpack.py -> tag_notes.py -> fetch_images.py -> gdrive_sync.py -> build_decks.py
    source/          data/         data/tags     build/images/     data/images.json    dist/*.apkg
                                    .json
```

| script | what it does |
|---|---|
| `scripts/anki_pkg.py` | reads an `.apkg` — both the legacy layout and the newer zstd/protobuf one |
| `scripts/download_deck.py` | fetches the source deck from AnkiWeb or a public Drive link |
| `scripts/unpack.py` | extracts notes to `data/notes.json`, media to `build/source_media/`, auto-detecting which field is Spanish / English / audio |
| `scripts/tag_notes.py` | reduces each English sentence to a searchable concept, offline |
| `scripts/estimate.py` | prints concept counts and projected deck size / fetch time before a long run |
| `scripts/fetch_images.py` | one image per **concept**, tried across several providers, normalised to JPEG |
| `scripts/gdrive_sync.py` | mirrors the image cache to Google Drive so git only carries links |
| `scripts/build_decks.py` | writes both `.apkg` files |
| `scripts/verify_decks.py` | resolves every media reference in a built deck against the package's own media table |

### Running it locally

```bash
pip install -r requirements.txt
python scripts/download_deck.py --drive-id <DRIVE_FILE_ID>
python scripts/unpack.py
python scripts/tag_notes.py --sample 20
python scripts/estimate.py          # check the scale before committing to a long run
python scripts/fetch_images.py --limit 200
python scripts/build_decks.py       # -> dist/*.apkg
python scripts/verify_decks.py      # fails if any note points at missing media
```

### Running it in CI

`.github/workflows/build-decks.yml`, triggered manually from the Actions tab:

* `stage: inspect` — downloads, unpacks, tags and reports how many notes and
  concepts there are and what a full image pass would cost. Nothing is built.
* `stage: smoke` — fetches a couple of dozen images, prints which provider
  answered each query and uploads the results as an artifact. Use it to judge
  the image search in two minutes rather than an hour.
* `stage: full` — additionally fetches every image, verifies the built decks
  and publishes them to a release, reusing the image cache attached to the
  previous release.

Useful inputs: `max_edge` (image size, and so most of the deck's size),
`image_limit` (fetch only the N most-reused concepts), `upgrade_from`
(re-search hits from named providers after adding an API key) and
`min_coverage` — a full build refuses to publish if the fetch covered less
than that share of concepts, so a provider outage cannot quietly ship two
decks with no images.

The source deck is cached between runs, so only the first run pays the 247 MB
download.

## Field detection

The source deck's field names are not known ahead of time, so `unpack.py`
guesses the roles: the audio field is the one containing `[sound:]` tags, and
Spanish is told from English by its accented characters and inverted
punctuation. If it guesses wrong, pin the names in `config.json`:

```json
{ "fields": { "spanish": "Front", "english": "Back", "audio": "Audio" } }
```

## Concept tagging

A Tatoeba sentence is a bad image query verbatim: *"Tom told Mary that he had
to go home to feed his dog"* returns noise. `tag_notes.py` reduces it to
`feed dog` before anything is searched. It runs on spaCy — dependency parse
plus named-entity recognition, entirely offline, **no LLM and no API calls**:

* named entities are dropped, which removes Tatoeba's endless Tom / Mary / Boston;
* light verbs (*be, have, get, go, make, …*) and vague nouns (*thing, way, time, …*)
  are filtered out;
* the preferred shape is the main verb plus its object — *"She is reading a book
  in the garden"* becomes `reading book`;
* `"a cup of coffee"` unwraps to `coffee`, because the measure word is not the subject;
* gerunds are kept as-is for the query (`riding bicycle` searches better than
  `ride bicycle`) while the lemma is what concepts are deduplicated on;
* a sentence that reduces to nothing concrete — *"I don't know what you mean"* —
  or to a lone abstract adjective gets no image rather than a misleading one.

### Concepts, not notes

Notes reducing to the same lemma set share one `concept_key` and therefore one
downloaded image. On a sentence corpus this matters a lot: *"Tom feeds his dog
every morning"*, *"Can you feed the dog?"* and the sentence above all collapse
onto `dog-feed` and one JPEG. Fewer downloads, a much smaller deck, and the
`--limit` budget is spent on the most-reused concepts first because
`fetch_images.py` sorts targets by how many notes they cover.

`data/tags.json` maps note guid to `{keywords, query, concept_key, strategy}`.
It is regenerated in seconds from `data/notes.json`, so it is published as a
release asset rather than tracked in git.

## Image sources

Providers are tried in order and the first usable hit wins:

1. Pexels, Unsplash, Pixabay — best quality, need a free API key in
   `PEXELS_API_KEY` / `UNSPLASH_ACCESS_KEY` / `PIXABAY_API_KEY`
2. DuckDuckGo images — no key
3. Openverse, Wikimedia Commons — no key, openly licensed

**What actually happens with no API key set:** a smoke run from a GitHub
runner returned Openverse for all 24 queries and nothing at all from
DuckDuckGo, which blocks datacenter addresses. So without a key the deck rests
on Openverse's openly-licensed corpus — fine for concrete nouns, thin for
abstract ones. Adding a key is the single biggest quality lever, and the
per-provider quotas differ enough to matter over 5.5k queries:

| provider | free quota | 5,556 queries |
|---|---|---|
| Pixabay | 100 / minute | ~1 hour — the practical choice |
| Pexels | 200 / hour | ~28 hours, so several runs |
| Unsplash | 50 / hour (demo) | not viable alone |

Set `PIXABAY_API_KEY` in the repository secrets, then re-run with
`upgrade_from: openverse` to replace the weaker hits while keeping everything
else in the cache.

With `--no-placeholder` a note that matches nothing is simply left without an
image; otherwise a plain typographic card is generated so a build never breaks.

`data/images.json` is the manifest — one entry per concept with the query used,
the provider, the source URL and, once `gdrive_sync.py` has run, the Drive file
id and a download link. That file is what git tracks; the JPEGs themselves stay
in Drive or in the `images.zip` release asset.

### Google Drive credentials

`gdrive_sync.py` authenticates with a service account. Put its JSON key in the
`GDRIVE_SERVICE_ACCOUNT_JSON` secret and the destination folder id in
`GDRIVE_PARENT_ID`, then **share that folder with the service account's e-mail
as Editor** — a service account cannot otherwise write into someone's My Drive.
When the secret is absent the step is skipped and the images travel as the
`images.zip` release asset instead.

## Deck and note type ids

`build_decks.py` pins its deck, model and note ids. Anki matches notes across
imports by these, so re-importing a rebuilt deck updates the existing cards and
keeps your review scheduling instead of creating duplicates. Do not change them.

## Licensing

The source sentences and audio come from Tatoeba. Fetched images keep whatever
licence their source carries — the keyless providers (Openverse, Wikimedia)
return openly licensed material, whereas a general image search does not, so
treat a deck built that way as a personal-use artifact.

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
download_deck.py  ->  unpack.py  ->  fetch_images.py  ->  gdrive_sync.py  ->  build_decks.py
    source/            data/            build/images/       data/images.json      dist/*.apkg
```

| script | what it does |
|---|---|
| `scripts/anki_pkg.py` | reads an `.apkg` — both the legacy layout and the newer zstd/protobuf one |
| `scripts/download_deck.py` | fetches the source deck from AnkiWeb or a public Drive link |
| `scripts/unpack.py` | extracts notes to `data/notes.json`, media to `build/source_media/`, auto-detecting which field is Spanish / English / audio |
| `scripts/estimate.py` | prints note counts and projected deck size / fetch time before a long run |
| `scripts/fetch_images.py` | one image per note, tried across several providers, normalised to JPEG |
| `scripts/gdrive_sync.py` | mirrors the image cache to Google Drive so git only carries links |
| `scripts/build_decks.py` | writes both `.apkg` files |

### Running it locally

```bash
pip install -r requirements.txt
python scripts/download_deck.py --drive-id <DRIVE_FILE_ID>
python scripts/unpack.py
python scripts/estimate.py          # check the scale before committing to a long run
python scripts/fetch_images.py --limit 200
python scripts/build_decks.py       # -> dist/*.apkg
```

### Running it in CI

`.github/workflows/build-decks.yml`, triggered manually from the Actions tab:

* `stage: inspect` — downloads, unpacks and reports how many notes there are
  and what a full image pass would cost. Nothing is built.
* `stage: full` — additionally fetches images, builds both decks and publishes
  them to a release, reusing the image cache attached to the previous release.

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

## Image sources

Providers are tried in order and the first usable hit wins:

1. Pexels, Unsplash, Pixabay — best quality, need a free API key in
   `PEXELS_API_KEY` / `UNSPLASH_ACCESS_KEY` / `PIXABAY_API_KEY`
2. DuckDuckGo images — no key, best coverage for whole phrases
3. Openverse, Wikimedia Commons — no key, openly licensed

With `--no-placeholder` a note that matches nothing is simply left without an
image; otherwise a plain typographic card is generated so a build never breaks.

`data/images.json` is the manifest — one entry per note with the query used,
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

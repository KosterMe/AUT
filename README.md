# TikTok Auto Poster

Turns long videos into vertical TikTok clips and publishes them on a schedule.

```
YouTube URL / local file
        ↓  download
        ↓  transcribe            (if the cutter needs words: captions, Whisper or Riva)
        ↓  cut                   → clips    speech · scene changes · the clock
        ↓  place b-roll from the library on the words that name it
        ↓  score it            music ducked under the speech, sounds on the cuts
        ↓  render 9:16 + karaoke subtitles + cover
        ↓  schedule per account
        ↓  publish               → TikTok
```

Every step after the first runs in the background, so the UI stays responsive
while an hour-long video is being transcribed.

## Profiles

Adding a video asks one question — what kind of video is it — and that fixes
the four decisions underneath: which cutter runs, how the frame is filled,
whether b-roll goes over it, and whether pauses are cut out.

| Profile | Cuts on | Frame | B-roll | Pauses | Soundtrack |
| --- | --- | --- | --- | --- | --- |
| `talking` *(default)* | sentence ends | from the source | yes | removed | music + effects |
| `plain` | sentence ends | from the source | no | kept | none |
| `split` | sentence ends | speaker over filler footage | no | removed | music + effects |
| `film` | scene changes, ranked by loudness | from the source | no | kept | none |

"From the source" means the shape of the video decides: anything already about
as tall and narrow as a phone screen is cropped to fill the frame, and anything
wider is floated over a blurred copy of itself. Giving a vertical video a
backdrop made of itself wastes a frame that was already the right shape.

`film` is the one that needs no speech: a video with no dialogue fails outright
under every other profile, because cutting on sentence ends has nothing to cut
on. It finds every cut in the picture in one decode of the source — roughly
three minutes for a two-hour film — and, when only a handful of clips are
wanted out of it, keeps the loudest stretches.

`split` takes the bottom half of the frame from a library asset tagged
`background`, looped for as long as the clip needs. Without one it falls back
to a blurred backdrop rather than failing.

The soundtrack comes from the same library: a track tagged `music` plays under
the clip and a sound tagged `sfx` fires on its cuts. Both are no-ops until
those assets exist, so turning them on by default changes nothing about an
existing library.

One cost worth knowing about `film`: it never transcribes the whole source, so
every clip is sent through Whisper individually at render time to get subtitle
timings. On CPU that is the slowest thing in the pipeline. Pass
`render.burn_subtitles: false` for material that does not need them — a music
montage, a sport reel — and the transcription disappears with them.

Every switch a profile sets can still be overridden per job through
`render` in `POST /api/jobs` — omitting one means "whatever the profile says",
not "off".

---

## Looks

A profile says what kind of video this is. A **look** says how it should be
rendered: the framing, the grade, the type, how the pauses are cut, how much
b-roll is allowed, how loud the bed sits. Every one of those used to be an
environment variable, which meant changing one required an edit and a restart,
and two accounts could not have two looks at all.

A look is a value that travels with the clip. It is resolved once, when the job
starts, and stored inside the composition the clip is rendered from — so a
re-render a month later cannot drift, and the fragment cache key covers the
framing for free.

**Nothing about it is required.** Layers apply weakest first:

```
code defaults  ->  .env  ->  profile  ->  saved look  ->  this job
```

Anything a layer does not mention passes straight through. A saved look holding
one field is a legitimate look; a job with none renders exactly as its profile
always did. That is what keeps a page of forty controls from turning into forty
decisions somebody has to make before the pipeline will run.

Presets live on the **Looks** page, or at `/api/styles`. `GET
/api/styles/defaults?profile=talking` answers with every value a profile
renders with, which is what the editor shows behind each empty control.

### Seeing it before committing to it

`POST /api/clips/{id}/preview` renders a few seconds of a real clip in a given
look and hands back the mp4. Measured on a 20-second clip: **1.2 s for a
4-second preview at 540x960, against 7.3 s for the whole clip** — and the gap
widens on the 90-second clips the defaults produce. It is composed by exactly
the code the real render uses, so it is the clip, only shorter and smaller.

The one difference is that a preview never starts a transcription: it takes the
words already on disk, because a slider cannot wait several minutes for Whisper.

The Looks page has this wired to whatever is on screen, saved or not, which is
the loop the whole thing exists for — move a control, look at the result, keep
it or don't.

### Rendering one clip again

`POST /api/clips/{id}/render` re-renders a single clip, optionally into a
different look, keeping its boundaries. Re-running the *job* would recut every
clip in it, which is the wrong tool for "make this one look different" — and
the wrong tool for "I have added b-roll since this rendered", which is the
other reason to reach for it.

A finished clip stores what it was made of. `GET /api/clips/{id}/composition`
returns its segments, what was laid over them, and the style that did it.

---

## Running it

### Docker (the intended deployment)

```bash
cp .env.example .env
docker compose up -d --build
```

- UI — http://localhost:3000
- API and interactive docs — http://localhost:8000/docs

Both ports bind to `127.0.0.1`. The API has **no authentication** until you set
`APP_AUTH_TOKEN`, so do not expose those ports without one.

Scaling out is a flag — workers claim tasks with a compare-and-swap, so two of
them never take the same one:

```bash
docker compose up -d --scale worker-media=3
```

### Locally

```bash
pip install -r requirements.txt
python -m alembic upgrade head

python -m uvicorn app.api.main:app --reload      # API on :8000
python -m app.workers                            # workers, in a second shell
cd webapp && pnpm install && pnpm dev            # UI on :5173
```

`python -m app.workers --kinds render` limits a process to one kind of work.
`VITE_API_TARGET` points the dev UI at an API somewhere other than `:8000`.

---

## Getting an account in

TikTok's login is behind a captcha, so you sign in yourself and the app keeps
the resulting session.

**Import cookies (works everywhere, including a headless server).** Sign in to
TikTok in your browser, export the cookies for `tiktok.com` with any
cookie-export extension — JSON or `cookies.txt`, both are accepted — and upload
the file on the Accounts page. It must contain `sessionid`; `tt-target-idc` is
strongly recommended, because uploads without it are noticeably less reliable.

**Local browser (desktop only).** "Open browser login" launches Chrome on the
machine running the API and waits for you to sign in. Needs
`undetected-chromedriver`, which is commented out in `requirements.txt` and
deliberately absent from the server image.

---

## Layout

```
app/
  core/       configuration, errors, time, logging
  db/         tables, sessions, Alembic migrations
  domain/     pure logic: cutting/, profiles, style, compositions, inserts, subtitles
  adapters/   the outside world: tiktok/, youtube/, media/, asr/
  tasks/      the queue: claiming, leases, retries, handlers
  services/   use cases — the only place entity state changes
  api/        HTTP: routers and schemas, no logic
  workers/    worker entrypoint
webapp/       React UI
tests/
```

Dependencies point inwards: `api → services → domain/adapters → db/core`.
Nothing in `domain` imports `db`, `api` or `adapters`, which is what keeps the
cutting and caption rules testable with plain values.

### Things worth knowing before changing something

**A clip is described before it is rendered.** `app/domain/composition.py`
says what a clip is made of — which pieces of which files play in what order,
what is laid over them, what is burned on top — as plain values, with no
ffmpeg anywhere near it. `app/adapters/media/compiler.py` turns that into
ffmpeg runs. The split is what makes it possible to test the montage rules
without rendering a frame, and to change how something renders without
touching what it contains.

**The look is part of that description, not of the process.**
`app/domain/style.py` holds it, and the composition carries it. Nothing under
`adapters/media/` calls `get_settings()` for anything visual any more, which is
the difference between "the renderer is configured" and "this clip looks like
this". A partial override is the normal case: `merged({"subtitles":
{"font_size": 96}})` changes one field and leaves the other forty alone.

**B-roll is placed by rule, not by hand.** Fragments uploaded to the library
(the B-roll page, or `POST /api/assets`) carry tags; `app/domain/inserts.py`
matches those tags against the clip's own subtitle cues and puts an asset on
screen at the instant its word is spoken. Every choice is seeded by the clip id,
so a re-render produces the same edit — otherwise the fragment cache would be
looking at a composition it had never seen before, every time.

**Music ducks, it is not just quiet.** `sidechaincompress` in
`app/adapters/media/compiler.py` uses the speech as the trigger and the bed as
the thing compressed, so the music drops when anybody talks and comes back in
the pauses. A fixed level cannot do both: quiet enough under speech is
inaudible in a pause, and audible in a pause fights the speech. Measured on a
test render, the bed sits about 11 dB lower under speech than in a pause.

**Transition sounds go where the picture already changes** — a cut made by
silence removal, or an insert appearing — and nowhere else. Effects are mixed
rather than spliced, so adding or removing one cannot move the timeline out
from under the subtitles.

**Cutters differ in their signal and agree on their output.**
`app/domain/cutting/` holds three — `speech` reads a transcript, `scenes` reads
cuts in the picture and the loudness of each second, `plain` reads the clock —
and all three return the same `SliceSpec` list. That is what lets a profile
choose one without anything downstream knowing which ran, and it is why adding
a fourth is a file rather than a refactor.

**One queue, one runner.** `download`, `render`, `publish` and `cleanup` are
rows in `tasks` with different `kind`s. A worker claims one with an atomic
compare-and-swap and holds a lease it refreshes while working; if it dies, the
lease expires and the task is requeued or failed.

**Publishing is never retried blindly.** The publish handler marks its task
*irreversible* right before the upload starts. If the worker then dies, the
task fails for a person to look at rather than retrying — a duplicate post is
worse than a job needing a glance. Retrying a failed publication is a manual
action for the same reason.

**A job's status is derived, never written by hand.**
`clip_jobs.refresh_status` computes it from the job's clips and is the only
function that sets it. A job with some clips failed is still `ready`: the rest
are publishable, which is the point of cutting many clips.

**A clip is a row.** `clips` and `publications` are linked by a foreign key, so
"which clips are already scheduled" is one indexed query.

**Transcription is the expensive step**, so transcripts are cached by
(file, settings) and YouTube's own captions are preferred when the source has
them. Re-slicing a video with different clip lengths is nearly free.

YouTube's auto-captions carry a `tOffsetMs` per word, and those are read out as
word timings. That matters more than it sounds: without them, karaoke subtitles
have nothing to align to, and every clip gets sent back through Whisper at
render time — turning one transcription per video into one per clip.

**Settings live in one place.** `app/core/config.py` declares everything and
nothing else calls `os.getenv`. `.env` is read once at a process entrypoint,
never on import — which is what keeps a test run from picking up your local
configuration.

---

## Performance

Measured on 16 logical cores with a 40-second clip rendered to 1080×1920 with
burned subtitles:

| | time | output |
| --- | --- | --- |
| blur at full 1080p, sharpening | 21.4 s | 8.5 MB |
| blur at 1/4 scale, sharpening | 10.9 s | 8.5 MB |
| `h264_amf` (integrated GPU), blur at 1/4 | 11.9 s | 22.5 MB |
| blur at 1/4, no sharpening | 6.9 s | 6.8 MB |

Two things worth knowing from that table. The blurred backdrop is the most
expensive filter in the chain, and blurring it small then scaling up is free
speed — a heavy blur destroys the detail the downscale removed anyway.
And **hardware encoding is not automatically faster**: on an integrated Radeon
the AMF encoder lost to libx264 on both time and file size, which is why the
default is `libx264` rather than `auto`. On a discrete NVIDIA GPU, try
`AUTOCLIPS_VIDEO_ENCODER=auto`.

Renders run in parallel, but the gain flattens quickly because x264 is already
multi-threaded — 6 clips took 46 s at concurrency 1, 35 s at 3, 38 s at 5.

### Compiling a montage

A montage — several segments taken from different points in a source, with
inserts over them — was measured separately, on a 40-second clip built from
four segments spread across a 14-minute 1080p60 AV1 source. Quality is VMAF
against a lossless render of the same composition.

| | time | VMAF |
| --- | --- | --- |
| one input across the whole span, trimmed in the graph | 30.2 s | — |
| one pass, each segment seeked at its own input | 18.4 s | 96.4 |
| two stages, `crf 8` intermediates | 21.4 s | 95.5 |
| two stages, warm fragment cache | ~13 s | 95.5 |

The first row is what this replaced, and the 40% is the decoder walking every
frame between the first segment and the last only to throw it away. Seek at the
input, never trim across a gap.

One pass wins on both time and quality, so it is the default. Two stages exist
for the last row: the segments are cached by content, so a restyle — new
subtitles, new inserts, a different look — costs only the final pass. Set
`AUTOCLIPS_RENDER_STRATEGY=two_stage` while you are iterating on how clips look;
leave it on `auto` when rendering each clip once.

**Intermediates are Matroska, and that is not cosmetic.** Same encoder
settings, same final pass, only the container changed: `.mkv` scored 95.5 where
`.mp4` scored 92.3. MP4 carries timestamp edits through `concat -c copy`, and
the delivery encode ends up working off a shifted frame grid.

Two things that sound like they should help and do not: rendering segments in
parallel (22.1 s against 24.0 s — x264 already saturates the cores), and
lossless intermediates (VMAF 95.54 at 99.9 MB/min against `crf 8`'s 95.51 at
24.6 MB/min).

If a job still feels slow, the order to check things in:

1. Did it use YouTube captions, or did Whisper run? The job's `transcript
   source` in the download task's result says which.
2. Is `AUTOCLIPS_WHISPER_CHUNKED=1`? On CPU with `large-v3`, chunked decoding
   is the difference between hours and minutes for a long video.
3. Is the worker running with `--concurrency 3`?

---

## Configuration

`.env.example` documents every setting with the reasoning behind the defaults.

Anything visual — framing, grade, type, pacing, b-roll, the soundtrack — is now
better set as a [look](#looks), which applies per job and takes effect without a
restart. What is left in `.env` is the *default* a look starts from, plus
everything that is genuinely a property of the machine: paths, credentials,
timeouts, which encoder to use.

The ones that most often need attention:

| Setting | Why you would touch it |
| --- | --- |
| `APP_AUTH_TOKEN` | Required before the API is reachable from anywhere but localhost. |
| `APP_DATABASE_URL` | SQLite by default; set a PostgreSQL URL when one machine stops being enough. |
| `YTDLP_COOKIES_FILE`, `YTDLP_PLAYER_CLIENTS`, `YTDLP_PO_TOKEN` | YouTube returns HTTP 403 mid-download without a PO token. |
| `YTDLP_POT_BASE_URL` | Where the bgutil PO-token provider answers. Compose runs it as a service and points this at it; a local run wants `http://127.0.0.1:4416`. |
| `AUTOCLIPS_ASR_BACKEND` | `whisper` (local CPU/GPU) or `nvidia` (hosted, no local compute). |
| `AUTOCLIPS_WHISPER_MODEL` | `large-v3` is much more accurate and much slower on CPU. |
| `AUTOCLIPS_RENDER_FOREGROUND_ZOOM` | How much of the 9:16 frame the video fills, at the cost of its own left and right edges. |
| `AUTOCLIPS_RENDER_STRATEGY` | `two_stage` while iterating on look — the fragment cache pays for itself from the second render. |
| `AUTOCLIPS_INSERTS_*` | How much of a clip automatic b-roll may cover, and where it may not go. |
| `APP_RETENTION_*` | How long downloads and published clips stay on disk. |

## Language

Nothing in the pipeline is tied to a language, and the defaults already lean
Russian: Russian caption tracks are preferred first, `AUTOCLIPS_TIKTOK_HASHTAGS`
is a Russian tag set, and the bundled Oswald-Bold covers Cyrillic in full. That
last one is why it is bundled at all -- neither Windows nor the Linux containers
ship a condensed display font that does.

| Setting | Default | What it decides |
| --- | --- | --- |
| `AUTOCLIPS_USE_YOUTUBE_CAPTIONS` | `1` | Whether the source's own captions are used at all, before any ASR runs. |
| `AUTOCLIPS_YOUTUBE_CAPTION_LANGS` | `ru,en,*` | Which caption track wins when the video has several. |
| `AUTOCLIPS_WHISPER_LANGUAGE` | *(empty)* | Empty auto-detects per video; `ru` pins it. |
| `AUTOCLIPS_WHISPER_INITIAL_PROMPT` | *(empty)* | Context sentence that fixes the spelling of names and jargon. |
| `AUTOCLIPS_NVIDIA_ASR_LANGUAGE` | `multi` | Only under `AUTOCLIPS_ASR_BACKEND=nvidia`; `ru-RU` pins it. |

Forcing Russian:

```bash
AUTOCLIPS_WHISPER_LANGUAGE=ru
AUTOCLIPS_WHISPER_INITIAL_PROMPT="Речь на русском языке."
```

Auto-detection reads the opening seconds, so it mislabels a Russian video that
starts on music or an English cold open. That is the case worth pinning `ru`
for; on genuinely mixed sources, leaving it empty does better than pinning it.

**The caption languages are an ordering, not a filter.** Every track the video
has is sorted by kind, then language, then format, and the best one is taken --
so a listed language does not have to exist, and an unlisted one still wins when
it is the only track on offer. Kind sorts first of all, which means a
hand-written English track beats an auto-generated Russian one even with `ru` at
the front of the list. When the transcript has to be Russian regardless of what
YouTube offers, the lever is `AUTOCLIPS_USE_YOUTUBE_CAPTIONS=0`: it skips the
tracks entirely and sends the audio to ASR, at the cost of the minutes that
captions would have been free.

`AUTOCLIPS_WHISPER_INITIAL_PROMPT` is context, not an instruction -- Whisper
continues the style it is primed with rather than obeying it, so what works is a
sentence written the way the output should be written, with the names spelled
correctly and the punctuation present.

Subtitles need nothing further: `ё` renders, and
`AUTOCLIPS_SUBTITLE_UPPERCASE=1` uppercases Cyrillic correctly.

**The web UI itself has no language setting.** Its labels are hardcoded English
and there is no i18n layer. Everything above changes what the pipeline produces
-- transcripts, subtitles, captions, hashtags -- not what the interface is
written in.

---

---

## Development

```bash
python -m pytest                                 # 254 tests, no network, no ffmpeg
python -m alembic revision --autogenerate -m "…" # after changing app/db/models.py
cd webapp && npx tsc -b                          # typecheck the UI
```

Tests run against a temporary database with the environment blanked, so they
neither touch your data nor read your `.env`. `tests/test_migrations.py` fails
if the models and the migrations drift apart — which has happened, and produced
a `no such column` against a freshly migrated database.

---

## The b-roll library

Upload fragments on the **B-roll** page and tag them with words a clip might
say. Nothing else is needed: the render pipeline picks them up on its own.

```
asset tagged "машина"  →  clip says "машину" at 00:12  →  insert at 00:12
```

Tags are matched by stem rather than by exact string, so Russian inflection
does not have to be spelled out — `машина` covers `машину` and `машине`, while
`стол` deliberately does not reach `столица`. Tags shorter than four characters
(`bmw`, `ai`) are matched exactly, because a prefix that short fires on half
the dictionary.

What the rules guarantee, and why each one is there:

| | |
| --- | --- |
| nothing in the first 2.5 s | the hook decides whether anybody watches the rest |
| at least 6 s between inserts | a keyword-dense passage would otherwise become a slideshow |
| at most 35% of a clip covered | it is a talking-head video with illustrations, not the reverse |
| one appearance per fragment per clip | the same footage twice reads as a mistake; a plain stretch does not |
| least recently used goes first | otherwise one asset ends up in every clip of a job |

A clip whose words match nothing falls back to a fixed beat, so a library still
gets used on material it has no vocabulary for. `AUTOCLIPS_INSERTS_CADENCE_WHEN_NO_MATCH=0`
leaves those clips clean instead.

Images work as well as video: a still has no timeline of its own, so it is held
on screen for the length of the insert rather than played.

---

## Limits

- TikTok's publish API is reverse-engineered and unofficial. It breaks when
  TikTok changes it; `app/adapters/tiktok/client.py` is the only file that has
  to be repaired when it does.
- Request signing shells out to Node (`app/adapters/tiktok/signature/`) because
  there is no Python equivalent. That is why the image installs Node.
- Slicing is driven entirely by speech. A video with no dialogue produces no
  clips, and fails saying so.

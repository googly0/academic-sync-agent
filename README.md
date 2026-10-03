# Autonomous Academic Management Agent

Extracts assignment deadlines, grading weights, and exam dates from messy course
syllabus PDFs, course emails, and screenshots, and syncs them to Google Calendar
and Notion.

The design goal is **explicit failure over cleverness**. Anything the pipeline
cannot establish with certainty is flagged for a human and written to
`needs_review.json` — it is never guessed at, and never silently dropped. A
fabricated due date is worse than no due date, because nobody checks it.

---

## The web app

```bash
python -m academic_sync.web        # then open http://127.0.0.1:8000
```

A local semester planner built on the same pipeline. Most deadlines never
arrive as a syllabus PDF, so it takes them from wherever they actually show up:

| Input | How |
|---|---|
| **Screenshots / photos** | Drop them in, or press ⌘V anywhere in the app. OCR'd with Tesseract, then the same stages 2–4 as a PDF. |
| **Course emails** | *Scan inbox* searches Gmail (read-only) for instructor and Canvas announcements. Each email is analysed on its own and never scanned twice. |
| **Syllabus PDFs** | The original pipeline. |
| **Typed by hand** | "Week 6 Friday", "Oct 17" — resolved by the same deterministic resolver. |

What you get:

- **Upcoming**: confirmed deadlines on a week-by-week timeline, colour-coded
  by course, each with its source's original wording and where it came from.
- **Inbox**: everything the review gate flagged — `TBD`, "this Friday",
  conflicting dates — as cards you fix by rewording or picking a date. A fix
  goes back through the gate, so an edit can't skip the checks. Conflicting
  dates show both quotes side by side.
- **Sync**: one button pushes confirmed deadlines to **Google Calendar**
  and/or a **Notion database**. Both are idempotent, so re-syncing never
  duplicates. Each task shows whether it's synced to each one. If a task is
  edited after syncing, it's marked as changed; the old event is never deleted
  automatically.

Imports only analyse and store tasks. Nothing leaves your machine until you
press Sync. The server binds to localhost, and every request that changes
something needs a per-launch token, so other websites can't send requests to
it. State lives in `academic_sync.db` (gitignored; `--db` to move it).

**Connections** (in the app): Google needs `credentials.json` as in
[step 4](#4-google-calendar-credentials) below, with the **Gmail API** enabled
too. Press *Connect Google* once to grant Calendar + read-only Gmail access.
Notion needs an [internal integration](https://www.notion.so/my-integrations)
secret and a database shared with it. Missing properties (Course, Due, Weight,
Sync Key) are added for you. With no `ANTHROPIC_API_KEY`, the app defaults to
the offline `stub` backend so you can try it without an API key.

---

## Architecture

Five stages. Each is a separate module, independently testable, and swappable
without touching the others.

```
  syllabus.pdf
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│ 1. PDF EXTRACTION                    extraction/pdf_extractor│
│    PyPDF2 text layer → per-page OCR fallback (Tesseract)     │
│    Output: raw text per page                                 │
└──────────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│ 2. SEMANTIC EXTRACTION (LLM)                  extraction/llm │
│    Abstract LLMExtractor + swappable backends                │
│    Extracts facts as literally stated. Does NOT resolve dates│
│    Output: course, task, raw_date_expression, weight, desc,  │
│            contradiction_detected + both quotes              │
└──────────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│ 3. DATE RESOLUTION (deterministic, no LLM)  resolution/      │
│    "Week 5 Friday" + semester_start → 2026-02-13             │
│    No pattern match → UnresolvableDateError (never a guess)  │
└──────────────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────┐
│ 4. VALIDATION & REVIEW GATE                    models/task   │
│    Pydantic. Three flagging cases:                           │
│      • missing required fields                               │
│      • contradiction_detected = True                         │
│      • UnresolvableDateError                                 │
│    Flagged → needs_review.json, NOT the calendar             │
└──────────────────────────────────────────────────────────────┘
       │  (only tasks that pass)
       ▼
┌──────────────────────────────────────────────────────────────┐
│ 5. CALENDAR SYNC                          calendar_sync/     │
│    Idempotent (dedupe key on each event)                     │
│    Exponential backoff on 429 / 403-quota                    │
│    Checkpointed to sync_state.json after EVERY event         │
└──────────────────────────────────────────────────────────────┘
```

### Why stage 3 is not the LLM's job

"Week 5 Friday" is a calendar computation with one correct answer. An LLM will
get it right most of the time, which is precisely the problem: the failures are
rare, silent, and land in someone's calendar as a confidently wrong date.
Stage 2 is therefore instructed to copy the date phrase *verbatim* and stop, and
stage 3 is 300 lines of testable Python with 80+ tests behind it.

---

## Project structure

```
academic-sync-agent/
├── main.py                             # CLI entry point (argparse)
├── requirements.txt
├── .env.example
├── academic_sync/
│   ├── config.py                       # PipelineConfig
│   ├── logging_setup.py
│   ├── orchestrator.py                 # the only module that spans stages
│   ├── models/
│   │   └── task.py                     # RawExtractedTask, AcademicTask
│   ├── store.py                        # web app: SQLite workspace
│   ├── workspace.py                    # web app: imports, fixes, sync
│   ├── extraction/
│   │   ├── pdf_extractor.py            # stage 1
│   │   ├── image_extractor.py          # stage 1 for screenshots
│   │   └── llm/                        # stage 2
│   │       ├── base.py                 # LLMExtractor ABC  ← the seam
│   │       ├── prompts.py
│   │       ├── anthropic_extractor.py  # default backend
│   │       ├── ollama_extractor.py     # local-model backend
│   │       ├── stub_extractor.py       # offline test backend
│   │       └── registry.py
│   ├── resolution/                     # stage 3
│   │   ├── date_resolver.py
│   │   └── errors.py
│   ├── sources/
│   │   └── gmail.py                    # course emails → PageText
│   ├── calendar_sync/                  # stage 5
│   │   ├── auth.py                     # Google OAuth (Calendar + Gmail)
│   │   ├── state.py                    # checkpoint (no Google deps)
│   │   ├── google_calendar.py          # idempotency + backoff
│   │   └── errors.py
│   ├── notion_sync/
│   │   └── notion.py                   # Notion target, same guarantees
│   └── web/
│       ├── app.py                      # FastAPI, token-protected
│       └── static/                     # index.html, app.css, app.js
└── tests/
    ├── test_date_resolver.py           # 80+ cases — the deepest coverage
    ├── test_models.py
    ├── test_calendar_sync.py
    ├── test_sync_state.py
    ├── test_store.py / test_workspace.py
    ├── test_notion_sync.py / test_sources.py
    └── test_web_api.py
```

---

## Setup

### 1. Python dependencies

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. System dependencies (only needed for OCR)

OCR handles scanned/photocopied syllabi. Two binaries pip cannot install:

```bash
# macOS
brew install tesseract poppler

# Debian / Ubuntu
sudo apt-get install tesseract-ocr poppler-utils
```

Skip this if your PDFs all have a text layer, and pass `--no-ocr`. If OCR is
needed but missing, the run **aborts** rather than handing you a syllabus with
silently blank pages. Use `--ocr-best-effort` to downgrade that to a warning.

### 3. Anthropic API key

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # or put it in .env
```

An unset `ANTHROPIC_API_KEY` does not necessarily mean no credentials — the SDK
also picks up an `ant auth login` profile, and a bare client works with that.

### 4. Google Calendar credentials

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create
   (or select) a project.
2. **APIs & Services → Library →** enable **Google Calendar API** (and
   **Gmail API** if you'll import course emails in the web app).
3. **APIs & Services → OAuth consent screen →** configure it. For personal use
   pick *External* and add your own Google account under **Test users**.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID →**
   application type **Desktop app**.
5. Download the JSON and save it as `credentials.json` in the project root.

On the first live run a browser opens for consent, and the resulting token is
cached in `token.json` (chmod 600) so later runs are silent.

The requested scope is `calendar.events` — permission to manage events only, not
to create, share, or delete calendars.

> `credentials.json` and `token.json` are gitignored. `token.json` is a live
> credential; treat it like a password.

---

## Usage

Always start with a dry run. It executes stages 1–4 and prints exactly what
would be created, without contacting the Calendar API or needing any Google
credentials at all.

```bash
python main.py --pdf syllabus.pdf --semester-start 2026-01-12 --dry-run
```

Then sync for real:

```bash
python main.py --pdf syllabus.pdf --semester-start 2026-01-12 --calendar-id primary
```

Fully offline smoke test (no API key, no network, no Google account) — useful
for verifying your OCR setup and seeing the review flow:

```bash
python main.py --pdf syllabus.pdf --semester-start 2026-01-12 \
    --llm-backend stub --dry-run
```

### Key flags

| Flag | Purpose |
|---|---|
| `--pdf` | Path to the syllabus PDF (required) |
| `--semester-start YYYY-MM-DD` | Anchors all "Week N" arithmetic (required) |
| `--calendar-id` | Target calendar; required unless `--dry-run` |
| `--dry-run` | Stages 1–4 only; the Calendar API is never contacted |
| `--llm-backend` | `anthropic` (default), `ollama`, `stub` |
| `--model` / `--effort` | Backend-specific model id and reasoning effort |
| `--week-start 0-6` | Weekday that begins an academic week (0=Mon) |
| `--day-first` | Read `10/17` as 17 October (default is October 17) |
| `--no-ocr` / `--ocr-best-effort` | OCR behaviour |
| `--state-file` | Checkpoint path (default `sync_state.json`) |
| `--non-interactive` | Never open a browser for OAuth; fail fast (servers, cron) |
| `--output-dir` | Where the two JSON reports go (default `./output`) |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Everything synced (or a clean dry run), nothing flagged |
| `1` | Completed, but some tasks need manual review |
| `2` | Sync was interrupted — **re-run the same command to resume** |
| `3` | A stage failed |
| `4` | Bad arguments |
| `130` | Ctrl-C (progress is still checkpointed) |

---

## Swapping the LLM backend

The orchestrator never imports a concrete backend. It calls
`create_extractor(name, ...)` and receives an `LLMExtractor`. Adding a backend
means **writing one class**, not touching the pipeline.

```python
# academic_sync/extraction/llm/my_backend.py
from .base import ExtractionContext, LLMExtractor
from ...models.task import RawExtractedTask

class MyLocalExtractor(LLMExtractor):
    name = "my-local-model"

    def _extract_from_text(self, text, context) -> list[RawExtractedTask]:
        # Call your model however you like. Return the tasks it found.
        # The base class already handled chunking, merging and de-duplication.
        ...
```

Register it (two lines in `registry.py`, or at runtime for plugins):

```python
from academic_sync.extraction.llm import register_backend
register_backend("my-local-model", lambda **kw: MyLocalExtractor(**kw))
```

Then:

```bash
python main.py --pdf syllabus.pdf --semester-start 2026-01-12 \
    --llm-backend my-local-model --dry-run
```

`ollama_extractor.py` is a complete worked example: ~90 lines, one method, no
changes anywhere else in the project. It runs a local model with the same
JSON-Schema-constrained decoding the API backend uses, so both are held to an
identical contract:

```bash
ollama serve
ollama pull llama3.1:8b
python main.py --pdf syllabus.pdf --semester-start 2026-01-12 \
    --llm-backend ollama --model llama3.1:8b --dry-run
```

**The contract every backend must honour** (enforced by convention and
documented on the ABC):

- Copy `raw_date_expression` verbatim. Never compute, normalise, or resolve it.
- Set `contradiction_detected` plus both verbatim quotes when the source
  disagrees with itself. Never pick a winner.
- Leave a field `None` when the syllabus does not state it. Never invent a
  plausible value.
- Raise `LLMExtractionError` on refusal, truncation, or unparseable output.
  Never return `[]` to paper over a failure — "no deadlines found" and "the
  model call failed" must never look the same.

---

## Checkpoint and resume behaviour

The failure this is built for is real: you sync 40 deadlines, hit a rate limit
at number 23, and the process dies.

**How it works**

`sync_state.json` records the dedupe key of every event the Calendar API has
*confirmed*. It is written after **every single event** — not at the end of the
batch — using a temp file plus `os.replace`, which is atomic. A crash mid-write
leaves the previous good file, never a truncated one.

State is namespaced by calendar id, so syncing the same syllabus to a personal
and a shared calendar are independent.

**On failure**, the run stops cleanly rather than thrashing:

```
[5/5] created: CS 4820 / Problem Set 4 -> https://cal/...
ERROR sync interrupted at CS 4820 / Problem Set 5: insert ... gave up after 5
      attempts (last error: HTTP 429 ...) — 18 task(s) remaining; re-run to resume
```

**On re-run**, the same command resumes:

```
loaded checkpoint sync_state.json: 1 calendar(s), 22 synced task(s)
skip (checkpoint): CS 4820 / Problem Set 1
...
[5/5] created=18 already_synced=22 adopted=0 skipped=0
```

**Three layers of duplicate protection**

1. **Checkpoint hit** — the local file already lists the key. Zero API calls.
2. **Calendar lookup** — the key is not in the checkpoint, so query the calendar
   for an event carrying that key as a private extended property. If it exists,
   *adopt* it and rebuild the checkpoint entry. This is what saves you if you
   delete `sync_state.json`, move machines, or restore from a backup.
3. **Deterministic key** — `sha256(course | task | exact_due_date)`, normalised
   for case and whitespace, so two runs of the LLM producing "Problem Set 3" and
   "problem set 3" yield the same key.

A corrupt or unknown-version checkpoint **raises** rather than starting fresh:
starting fresh would duplicate every existing event.

**Rate limiting.** 429s and 403-quota errors are retried with exponential
backoff plus full jitter, honouring `Retry-After` when present. Non-retryable
4xx errors (bad calendar id, revoked scope) fail immediately — retrying them
just burns quota.

Deleted an event and want it back? Remove its key from `sync_state.json`, or
call `SyncState.forget(key, calendar_id)`.

---

## Date resolution rules

| Pattern | Example | Resolves to |
|---|---|---|
| `Week N [Weekday]` | `Week 5 Friday` | 2026-02-13 |
| `Nth [Weekday] of [Month]` | `second Tuesday of October` | 2026-10-13 |
| `last [Weekday] of [Month]` | `last Friday of November` | 2026-11-27 |
| Month/day, any spelling | `Oct 10`, `October 10th, 2026`, `10 October` | 2026-10-10 |
| ISO 8601 | `2026-10-10` | 2026-10-10 |
| Numeric | `10/10`, `10/10/26` | 2026-10-10 |

Week 1 is the week *containing* `--semester-start`, anchored to Monday
(configurable with `--week-start`).

**What raises instead of guessing**

| Input | Why |
|---|---|
| `TBD`, `TBA`, `see Canvas`, `varies` | No date stated |
| `one week after the midterm` | Relative to another task; cross-task resolution is out of scope |
| `Week 5` | No weekday — could be any of five days |
| `Friday` | Which Friday? |
| `Oct 10 or Oct 17` | Two conflicting dates |
| `Oct 10-12` | A range is not a due date |
| `Friday, Oct 10` | Oct 10 2026 is a **Saturday** — the source contradicts itself |
| `fifth Monday of October` | October 2026 has only four |
| `February 30` | Not a real date |
| `Oct 10, 2006` | Outside the plausibility window — almost always OCR damage |

Two of these are worth calling out because they are free correctness checks the
syllabus hands you:

- `Week 5 Friday (Feb 13)` states the same date twice. The resolver computes
  both and **cross-checks them**. If they disagree (`Week 5 Friday (Feb 20)`) it
  raises rather than trusting either.
- A weekday word that survives parsing is checked against the resolved date,
  which catches the very common `Friday, Oct 10` typo.

---

## Output files

**`output/extracted_tasks.json`** — every task, synced or not, with its
resolution outcome.

**`output/needs_review.json`** — tasks that did *not* reach the calendar, each
with a `review_reason` naming which gate it failed:

```json
{
  "course_name": "CS 4820",
  "task_name": "Final Project",
  "exact_due_date": null,
  "raw_date_expression": "TBD",
  "requires_manual_review": true,
  "review_reason": "unresolvable_date: 'TBD' — expression is a placeholder (TBD/TBA/varies) — no date stated",
  "contradiction_detected": false,
  "source_page": 2
}
```

Rewritten on every run, including when empty, so a stale file can never be
mistaken for the current one.

---

## Testing

```bash
python -m pytest tests/ -q
python -m pytest tests/test_date_resolver.py -v
```

Coverage is deliberately lopsided toward the two modules where a bug would be
*silent*:

- **`test_date_resolver.py`** — every pattern, every spelling variant, every
  refusal path, plus the ambiguity and plausibility-window branches. Each
  expected date is justified by a calendar fact stated in a comment, and a
  sanity test asserts those facts independently, so the tests cannot simply
  agree with a buggy implementation.
- **`test_calendar_sync.py`** — idempotency, adoption after a lost state file,
  retryable vs non-retryable errors, backoff exhaustion, and a full
  interrupt-then-resume cycle against a fake Calendar service.

The calendar tests need no credentials and make no network calls.

---

## Known limitations

- **Cross-task dates are not resolved.** "One week after the midterm" is flagged,
  not computed. Resolving it would require a dependency graph across tasks and
  a way to be sure which "midterm" is meant.
- **Numeric dates default to US M/D.** `--day-first` switches the convention;
  the resolver will not switch on its own, because guessing here is invisible
  and wrong half the time.
- **Words like "before" and "after" force a review flag** even when a resolvable
  date is present (`Oct 10 (before class)`). Stripping them would silently turn
  "the day before Oct 10" into "Oct 10", so the conservative reading wins.
- **Events are all-day.** Syllabi rarely state a meaningful time, and inventing
  "23:59 in some timezone" is the same class of fabrication the pipeline avoids
  elsewhere.
- **Existing events are never updated.** If a deadline moves, the new date
  produces a new dedupe key and therefore a new event; the stale one is left
  alone rather than being deleted from someone's calendar automatically.
- **OCR quality bounds everything.** A bad scan yields bad text, which yields
  bad extraction. The window check catches the worst of it; the rest surfaces
  in `needs_review.json`.

"""Prompt text for stage 2, kept in one file so it can be tuned without
touching backend code — and so a local-SLM backend can reuse or adapt exactly
the same instructions.

The single most important rule encoded here: **the model does not do date
arithmetic**. Stage 3 is deterministic Python precisely so that "Week 5 Friday"
is resolved by code that can be unit-tested, not by a model that will be
confidently wrong once in a while.
"""

EXTRACTION_SYSTEM_PROMPT = """\
You extract graded work and deadlines from university course syllabi and \
academic calendars.

Return every assignment, problem set, project, quiz, exam, presentation, and \
lab report that has its own deadline or exam window.

## What is not a task

Several things look like tasks but are not. Leave them out:

- **Course listings.** "CS511: DBMS Lab" or "CS514: Mini-Project I" sitting in \
a curriculum, credit, or timetable table is a course a student enrols in, not \
a dated deliverable. A course belongs in the output only when the document \
gives that course its own deadline or exam window.
- **Policy and grading-scheme sentences.** "Laboratories are evaluated on \
day-to-day execution and an end-semester practical exam" explains how marks \
are awarded. It is prose about assessment, not something handed in on a date.
- **Section headings, table column labels, office hours, lecture topics, and \
readings with no deliverable.**

`task_name` must be a short noun phrase that would read sensibly on a calendar \
— "Problem Set 3", "Midterm Exam", "Final Project Proposal". If what you are \
about to return is a full sentence, it is prose and does not belong here.

## Worked example

Syllabus line:

    Problem Set 1 (10%) due Week 3 Friday - prove the greedy algorithm optimal

Correct output for that line:

    {
      "course_name": "CS 4820",
      "task_name": "Problem Set 1",
      "raw_date_expression": "Week 3 Friday",
      "grading_weight": "10%",
      "task_description": "Prove the greedy algorithm optimal.",
      "contradiction_detected": false,
      "contradiction_quotes": [],
      "source_page": 1
    }

Note where the date text went: into `raw_date_expression`, copied as written. \
It does not appear in `task_description`.

## Copy date wording, do not resolve it

Put the due-date wording in `raw_date_expression` exactly as the syllabus \
writes it, then stop.

- "Week 5 Friday"             -> "Week 5 Friday"
- "second Tuesday of October" -> "second Tuesday of October"
- "Oct 10"                    -> "Oct 10"
- "TBD"                       -> "TBD"

A separate deterministic component turns those phrases into calendar dates, \
and it needs the original wording to do so. Leave the arithmetic to it: keep \
relative expressions relative, keep the syllabus's own format, and add no year \
it did not print.

If the syllabus genuinely states no due date for a task, set \
`raw_date_expression` to null rather than borrowing a date from elsewhere.

## Contradictions

Syllabi frequently disagree with themselves — prose says one date, the \
schedule table says another. When you find two genuinely different dates for \
the same task:

- set `contradiction_detected` to true
- put both quotes, verbatim and with enough surrounding words to locate them, \
in `contradiction_quotes`
- put the one you consider primary in `raw_date_expression`

Do not decide which date is correct. A human resolves it.

Only flag real conflicts. The same date written two ways ("Oct 10" and \
"October 10th") is not a contradiction.

## Missing information

Leave a field null when the syllabus does not state it. A null is useful — it \
routes the task to human review. A plausible guess is not: it produces a \
calendar event nobody checks. This applies to `grading_weight` especially, \
which is often stated only in a summary table far from the task itself.

## Every field

- `raw_date_expression`: the due-date wording, copied verbatim. See above.
- `course_name`: the course as named in the document (code, title, or both), \
identical on every task from this document. When the document covers a whole \
programme or semester rather than a single course — an academic calendar, a \
timetable, an exam schedule — use the programme name ("B.Tech Computer \
Science & Engineering") on every task. Never borrow a course code from a \
nearby unrelated line just because it is the closest heading.
- `task_name`: short and specific — "Problem Set 3", "Midterm Exam", \
"Final Project Proposal".
- `grading_weight`: verbatim — "20%", "150 points", "pass/fail".
- `task_description`: what the student has to produce. This is the deliverable, \
not the deadline — if the syllabus says nothing about the work itself, use null \
here rather than repeating the date wording.
- `contradiction_detected` / `contradiction_quotes`: see above.
- `source_page`: the PAGE marker the task appeared under.

Extract only what the document states. Do not infer tasks that "should" exist.\
"""


def build_user_prompt(
    text: str,
    *,
    source_name: str,
    chunk_index: int,
    chunk_count: int,
    page_range: tuple,
    course_hint: str | None = None,
) -> str:
    """Assemble the per-chunk user message.

    The chunk framing is explicit so the model does not invent tasks it thinks
    are "missing" from a partial document, and does not repeat a task it can
    tell it has already seen the top half of.
    """
    header = [
        f"Source document: {source_name}",
        f"Chunk {chunk_index} of {chunk_count} (pages {page_range[0]}-{page_range[1]}).",
    ]
    if chunk_count > 1:
        header.append(
            "This is a portion of a longer syllabus. Extract only what appears "
            "in the text below; do not speculate about other sections."
        )
    if course_hint:
        header.append(f"The user believes this course is: {course_hint}")

    return (
        "\n".join(header)
        + "\n\nSyllabus text:\n<syllabus>\n"
        + text
        + "\n</syllabus>"
    )

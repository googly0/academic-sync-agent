"""Tests for the Notion target, against an in-memory fake of the REST API."""

from __future__ import annotations

from datetime import date

import pytest

from academic_sync.models import AcademicTask
from academic_sync.notion_sync import NotionAPIError, NotionClient, NotionSyncer, NotionSyncError
from academic_sync.notion_sync.notion import PROP_KEY, _normalise_id


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeNotion:
    """Just enough of api.notion.com: one database, its pages, and queries."""

    DB = "0123456789abcdef0123456789abcdef"

    def __init__(self, properties=None, failures=()):
        self.properties = properties or {"Name": {"type": "title"}}
        self.pages = []
        self.calls = []
        self.failures = list(failures)  # statuses to return, in order, before succeeding

    def client(self, **kwargs):
        kwargs.setdefault("sleep", lambda s: self.calls.append(("sleep", s)))
        return NotionClient("secret_x", session=self, **kwargs)

    def request(self, method, url, headers=None, json=None, timeout=None):
        path = url.split("/v1", 1)[1]
        self.calls.append((method, path))
        assert headers["Authorization"] == "Bearer secret_x"
        if self.failures:
            status = self.failures.pop(0)
            return FakeResponse(status, {"message": "nope"}, {"Retry-After": "2"})
        if method == "GET" and path == f"/databases/{self.DB}":
            props = {k: {"type": v["type"]} for k, v in self.properties.items()}
            return FakeResponse(200, {"properties": props, "title": [{"plain_text": "Deadlines"}]})
        if method == "PATCH":
            for name, spec in json["properties"].items():
                self.properties[name] = {"type": next(iter(spec))}
            return FakeResponse(200, {})
        if method == "POST" and path.endswith("/query"):
            key = json["filter"]["rich_text"]["equals"]
            hits = [p for p in self.pages if p["key"] == key]
            return FakeResponse(200, {"results": hits[:1]})
        if method == "POST" and path == "/pages":
            key = json["properties"][PROP_KEY]["rich_text"][0]["text"]["content"]
            page = {"id": f"page-{len(self.pages)}", "url": f"https://notion.so/p{len(self.pages)}", "key": key, "body": json}
            self.pages.append(page)
            return FakeResponse(200, page)
        return FakeResponse(404, {"message": "not found"})


def task(name="PS1", day=13, **kw):
    return AcademicTask(course_name="CS 1, Intro", task_name=name, exact_due_date=date(2026, 2, day), **kw)


def test_creates_pages_and_adds_missing_properties():
    fake = FakeNotion()
    report = NotionSyncer(fake.client(), FakeNotion.DB).sync_tasks([task(), task("PS2", 20)])
    assert len(report.created) == 2
    assert {"Course", "Due", "Weight", "Source wording", "Sync Key"} <= set(fake.properties)
    props = fake.pages[0]["body"]["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "PS1"
    assert props["Course"]["select"]["name"] == "CS 1  Intro"  # commas are illegal in selects
    assert props["Due"]["date"] == {"start": "2026-02-13"}


def test_existing_page_is_adopted_not_duplicated():
    fake = FakeNotion()
    NotionSyncer(fake.client(), FakeNotion.DB).sync_tasks([task()])
    report = NotionSyncer(fake.client(), FakeNotion.DB).sync_tasks([task()])
    assert len(report.adopted) == 1 and len(fake.pages) == 1


def test_known_keys_skip_without_any_api_call():
    fake = FakeNotion()
    syncer = NotionSyncer(fake.client(), FakeNotion.DB)
    syncer.ensure_schema()
    fake.calls.clear()
    report = syncer.sync_tasks([task()], known_keys=[task().sync_dedupe_key])
    assert report.already_synced and fake.calls == []


def test_flagged_tasks_are_never_sent():
    fake = FakeNotion()
    flagged = AcademicTask(course_name="CS 1", task_name="Final", raw_date_expression="TBD")
    report = NotionSyncer(fake.client(), FakeNotion.DB).sync_tasks([flagged])
    assert report.skipped_unsyncable and fake.pages == []


def test_spans_carry_an_end_date():
    fake = FakeNotion()
    NotionSyncer(fake.client(), FakeNotion.DB).sync_tasks([task(end_date=date(2026, 2, 20))])
    assert fake.pages[0]["body"]["properties"]["Due"]["date"]["end"] == "2026-02-20"


def test_429_is_retried_honouring_retry_after():
    fake = FakeNotion(failures=[429, 429])
    syncer = NotionSyncer(fake.client(), FakeNotion.DB)
    syncer.ensure_schema()
    assert ("sleep", 2.0) in fake.calls


def test_401_fails_immediately():
    fake = FakeNotion(failures=[401])
    with pytest.raises(NotionAPIError) as info:
        NotionSyncer(fake.client(), FakeNotion.DB).ensure_schema()
    assert info.value.status == 401
    assert not any(c[0] == "sleep" for c in fake.calls)


def test_exhausted_retries_interrupt_cleanly_mid_run():
    fake = FakeNotion()
    syncer = NotionSyncer(fake.client(max_attempts=2), FakeNotion.DB)
    syncer.ensure_schema()
    syncer.sync_tasks([task()])
    fake.failures = [503, 503]
    report = syncer.sync_tasks([task("PS2", 20), task("PS3", 27)])
    assert report.interrupted and len(report.remaining) == 2


def test_wrong_property_type_is_refused():
    fake = FakeNotion(properties={"Name": {"type": "title"}, "Due": {"type": "rich_text"}})
    with pytest.raises(NotionSyncError, match="'Due' property"):
        NotionSyncer(fake.client(), FakeNotion.DB).ensure_schema()


@pytest.mark.parametrize(
    "value",
    [
        "0123456789abcdef0123456789abcdef",
        "01234567-89ab-cdef-0123-456789abcdef",
        "https://www.notion.so/me/Deadlines-0123456789abcdef0123456789abcdef?v=99",
    ],
)
def test_database_id_accepts_ids_and_links(value):
    assert _normalise_id(value) == "0123456789abcdef0123456789abcdef"


def test_garbage_database_id_is_rejected():
    with pytest.raises(NotionSyncError):
        _normalise_id("my deadlines")

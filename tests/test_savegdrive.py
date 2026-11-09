import fcntl
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import savegdrive  # noqa: E402

from googleapiclient.errors import HttpError  # noqa: E402
from rich.console import Console  # noqa: E402
from textual.widgets import DirectoryTree, Input, Static, Tree  # noqa: E402
from textual.widgets._toast import Toast  # noqa: E402

FOLDER = savegdrive.FOLDER_MIME_TYPE
DOC = "application/vnd.google-apps.document"
FORM = "application/vnd.google-apps.form"


def raise_(exception):
    raise exception


def http_error(status, reason=None):
    response = mock.Mock(status=status, reason="error")
    if reason is None:
        return HttpError(response, b"{}")
    # The documented Drive API error body, e.g. a 403 userRateLimitExceeded.
    body = {"error": {"code": status, "message": "Error",
                      "errors": [{"domain": "usageLimits", "reason": reason, "message": "Error"}]}}
    return HttpError(response, json.dumps(body).encode())


def parse_fields(fields):
    """Parse a Drive partial-response selector ("a,b(c,d)") into {name: sub-selection or None}."""
    selection, stack, name = {}, [], ""
    current = selection
    for char in fields + ",":
        if char in ",()":
            if name.strip():
                current[name.strip()] = None
            if char == "(":
                current[name.strip()] = {}
                stack.append(current)
                current = current[name.strip()]
            elif char == ")":
                current = stack.pop()
            name = ""
        else:
            name += char
    return selection


def select_fields(value, selection):
    """Keep only the selected fields of a response, as Drive does: unasked fields never come back."""
    if selection is None:
        return value
    if isinstance(value, list):
        return [select_fields(element, selection) for element in value]
    if isinstance(value, dict):
        return {key: select_fields(value[key], sub) for key, sub in selection.items() if key in value}
    return value


def fields_request(run, fields):
    """A FakeRequest whose response is cut down to `fields`, when the caller gave some."""
    if fields is None:
        return FakeRequest(run)
    return FakeRequest(lambda: select_fields(run(), parse_fields(fields)))


class FakeRequest:
    def __init__(self, run):
        self.run = run

    def execute(self, num_retries=0):
        return self.run()


class FakeBatch:
    def __init__(self, drive, callback):
        self.drive = drive
        self.callback = callback
        self.requests = []

    def add(self, request, request_id):
        self.requests.append((request_id, request))

    def execute(self):
        self.drive.batches.append(len(self.requests))
        for request_id, request in self.requests:
            try:
                response, error = request.execute(), None
            except HttpError as exception:
                response, error = None, exception
            self.callback(request_id, response, error)


class FakeChanges:
    def __init__(self, drive):
        self.drive = drive

    def getStartPageToken(self, **kwargs):
        return FakeRequest(lambda: {"startPageToken": str(len(self.drive.change_log))})

    def list(self, pageToken, **kwargs):
        def run():
            if pageToken == "expired":
                raise http_error(410)
            start = int(pageToken)
            end = start + min(kwargs.get("pageSize", 1000), self.drive.changes_page_size)
            changes = self.drive.change_log[start:end]
            if not kwargs.get("includeItemsFromAllDrives"):
                # Like the API: shared drive items are left out unless asked for.
                changes = [change for change in changes if not self.drive.in_shared_drive(change.get("fileId"))]
            response = {"changes": changes}
            if end < len(self.drive.change_log):
                response["nextPageToken"] = str(end)
            else:
                response["newStartPageToken"] = str(len(self.drive.change_log))
            return response

        return fields_request(run, kwargs.get("fields"))


class FakeDrives:
    def __init__(self, drive):
        self.drive = drive

    def list(self, **kwargs):
        return fields_request(lambda: {"drives": self.drive.shared_drives}, kwargs.get("fields"))


class FakeDrive:
    """In-memory Drive: items are dicts with id, name, mimeType, parent and optional content."""

    def __init__(self, items, page_size=1000, shared_drives=()):
        self.items = {item["id"]: dict(item) for item in items}
        self.page_size = page_size
        self.batches = []
        self.failures = {}  # folder id, or (folder id, page token), -> errors raised by its next listings
        self.create_failures = {}  # folder name -> errors raised by its next creations
        self.change_log = []
        self.changes_page_size = 2  # small, so the changes feed comes in several pages
        self.shared_drives = list(shared_drives)
        self.uploads = []

    def files(self):
        return self

    def changes(self):
        return FakeChanges(self)

    def drives(self):
        return FakeDrives(self)

    def change(self, item_id):
        """Record a change of item_id, as Drive's changes feed would."""
        item = self.items.get(item_id)
        if item is None:
            self.change_log.append({"fileId": item_id, "removed": True})
        else:
            self.change_log.append(
                {"fileId": item_id, "removed": False,
                 "file": dict(self.public(item), parents=[item["parent"]], trashed=self.is_trashed(item))}
            )

    def new_batch_http_request(self, callback):
        return FakeBatch(self, callback)

    def public(self, item):
        fields = ("id", "name", "mimeType", "size", "modifiedTime", "md5Checksum", "shortcutDetails", "capabilities")
        public = {key: item[key] for key in fields if key in item}
        if item.get("parent"):
            public["parents"] = [item["parent"]]
        return public

    def in_shared_drive(self, item_id):
        drive_ids = {shared_drive["id"] for shared_drive in self.shared_drives}
        seen = set()
        while item_id is not None and item_id not in seen:
            if item_id in drive_ids:
                return True
            seen.add(item_id)
            item_id = self.items.get(item_id, {}).get("parent")
        return False

    def is_trashed(self, item):
        while item is not None:
            if item.get("trashed"):
                return True
            item = self.items.get(item.get("parent"))
        return False

    def list(self, q, pageToken=None, **kwargs):
        if q.startswith("sharedWithMe"):
            folder_id = savegdrive.SHARED_WITH_ME
        elif q == "trashed = true":
            folder_id = savegdrive.TRASH
        else:
            folder_id = q.split("'")[1]
        # Like the API: without a trashed clause, a folder lists its trashed children too.
        want_trashed = True if q.endswith("trashed = true") else False if q.endswith("trashed = false") else None

        def run():
            failures = self.failures.get((folder_id, pageToken)) or self.failures.get(folder_id)
            if failures:
                raise failures.pop(0)
            if folder_id == savegdrive.SHARED_WITH_ME:
                children = [self.public(item) for item in self.items.values() if item.get("shared")]
            elif folder_id == savegdrive.TRASH:
                children = [
                    dict(self.public(item), explicitlyTrashed=bool(item.get("trashed")))
                    for item in self.items.values() if self.is_trashed(item)
                ]
            else:
                children = [
                    self.public(item) for item in self.items.values()
                    if item.get("parent") == folder_id and want_trashed in (None, self.is_trashed(item))
                ]
            start = int(pageToken or 0)
            page = children[start:start + self.page_size]
            response = {"files": page}
            if start + self.page_size < len(children):
                response["nextPageToken"] = str(start + self.page_size)
            return response

        return fields_request(run, kwargs.get("fields"))

    def get(self, fileId, **kwargs):
        def run():
            if fileId == "root":
                return {"id": "root"}
            if fileId not in self.items:
                raise http_error(404)
            if not kwargs.get("supportsAllDrives") and self.in_shared_drive(fileId):
                raise http_error(404)  # like the API: shared drive items are not found unless supported
            item = self.items[fileId]
            return dict(self.public(item), trashed=self.is_trashed(item))  # asked for by ITEM_FIELDS

        return fields_request(run, kwargs.get("fields"))

    def create(self, body, **kwargs):
        """A new folder: files are sent by fake_upload."""
        def run():
            failures = self.create_failures.get(body["name"])
            if failures:
                raise failures.pop(0)
            folder_id = f"dir{len(self.items)}"
            self.items[folder_id] = {"id": folder_id, "name": body["name"], "mimeType": body["mimeType"],
                                     "parent": body["parents"][0]}
            self.change(folder_id)
            return self.public(self.items[folder_id])

        return fields_request(run, kwargs.get("fields"))

    def update(self, fileId, body, **kwargs):
        def run():
            if fileId not in self.items:
                raise http_error(404)
            self.items[fileId]["trashed"] = body["trashed"]
            self.change(fileId)
            return {}

        return FakeRequest(run)

    def delete(self, fileId, **kwargs):
        def run():
            removed = {fileId}
            while True:  # a folder takes its content with it
                more = {key for key, item in self.items.items() if item.get("parent") in removed} - removed
                if not more:
                    break
                removed |= more
            for key in removed:
                self.items.pop(key, None)
                self.change(key)
            return {}

        return FakeRequest(run)

    def emptyTrash(self, **kwargs):
        def run():
            for key in [key for key, item in self.items.items() if item.get("trashed")]:
                self.delete(key).execute()
            return {}

        return FakeRequest(run)

    def get_media(self, fileId, **kwargs):
        return self.items[fileId]["content"]

    def export_media(self, fileId, mimeType):
        return self.items[fileId]["content"]


REAL_FETCH_MEDIA = savegdrive.fetch_media  # SyncTest replaces it with fake_fetch


def fake_fetch(service, item, file_handle, stop=None):
    file_handle.write(service.items[savegdrive.effective_id(item)]["content"])


def fake_upload(service, path, file_id=None, name=None, parent_id=None, stop=None, on_bytes=lambda count: None):
    import hashlib

    content = path.read_bytes()
    if file_id is None:
        file_id = f"up{len(service.items)}"
        service.items[file_id] = {"id": file_id, "name": name, "mimeType": "text/plain", "parent": parent_id}
    item = service.items[file_id]
    service.uploads.append(file_id)
    item.update(content=content, size=str(len(content)), md5Checksum=hashlib.md5(content).hexdigest(),
                modifiedTime=f"2026-02-0{len(service.uploads) % 9 + 1}T00:00:00.000Z")
    service.change(file_id)  # in Drive's changes feed, like any other change
    return service.public(item)


def file_item(item_id, name, parent, content, **extra):
    import hashlib

    item = {"id": item_id, "name": name, "mimeType": "text/plain", "parent": parent, "content": content,
            "size": str(len(content)), "md5Checksum": hashlib.md5(content).hexdigest(),
            "modifiedTime": "2025-01-02T03:04:05.000Z"}
    item.update(extra)
    return item


def crossed_shortcuts(levels):
    """Team/L0, then in every level two shortcuts a and b to the next one; video.mp4 in the last."""
    items = [{"id": "team", "name": "Team", "mimeType": FOLDER, "parent": "root"},
             {"id": "l0", "name": "L0", "mimeType": FOLDER, "parent": "team"}]
    for level in range(levels):
        items.append({"id": f"l{level + 1}", "name": f"L{level + 1}", "mimeType": FOLDER, "parent": "elsewhere"})
        for name in ("a", "b"):
            items.append({"id": f"{name}{level}", "name": name, "mimeType": savegdrive.SHORTCUT_MIME_TYPE,
                          "parent": f"l{level}",
                          "shortcutDetails": {"targetId": f"l{level + 1}", "targetMimeType": FOLDER}})
    items.append(file_item("video", "video.mp4", f"l{levels}", b"video"))
    return items


def demo_drive():
    return FakeDrive([
        {"id": "docs", "name": "Docs", "mimeType": FOLDER, "parent": "root"},
        {"id": "photos", "name": "Photos", "mimeType": FOLDER, "parent": "root"},
        file_item("a", "a.txt", "docs", b"alpha"),
        file_item("b", "b.txt", "docs", b"bravo"),
        {"id": "report", "name": "Report", "mimeType": DOC, "parent": "docs", "content": b"docx bytes",
         "modifiedTime": "2025-01-02T03:04:05.000Z"},
        {"id": "form", "name": "Survey", "mimeType": FORM, "parent": "docs"},
        file_item("p", "beach.jpg", "photos", b"jpeg"),
        file_item("n", "notes.txt", "root", b"notes"),
    ])


class NamesTest(unittest.TestCase):
    def test_local_names(self):
        self.assertEqual(savegdrive.local_name({"name": "a/b", "mimeType": "text/plain"}), "a_b")
        self.assertEqual(savegdrive.local_name({"name": "..", "mimeType": "text/plain"}), "_unnamed_")
        self.assertEqual(savegdrive.local_name({"name": "Plan", "mimeType": DOC}), "Plan.docx")
        self.assertEqual(savegdrive.local_name({"name": "Plan.DOCX", "mimeType": DOC}), "Plan.DOCX")

    def test_human_size(self):
        self.assertEqual(savegdrive.human_size(999), "999 B")
        self.assertEqual(savegdrive.human_size(2_400_000), "2.4 MB")
        self.assertEqual(savegdrive.human_size(340_000_000), "340 MB")

    def test_shortcut_follows_target(self):
        shortcut = {"id": "s", "name": "Link", "mimeType": savegdrive.SHORTCUT_MIME_TYPE,
                    "shortcutDetails": {"targetId": "t", "targetMimeType": FOLDER}}
        self.assertTrue(savegdrive.is_folder(shortcut))
        self.assertEqual(savegdrive.effective_id(shortcut), "t")
        self.assertIn("/folders/t", savegdrive.drive_url(shortcut))

    def test_unsupported(self):
        self.assertTrue(savegdrive.is_unsupported({"mimeType": FORM}))
        self.assertFalse(savegdrive.is_unsupported({"mimeType": DOC}))
        self.assertFalse(savegdrive.is_unsupported({"mimeType": FOLDER}))


class ListingTest(unittest.TestCase):
    def test_batches_pages_and_sorts(self):
        items = [{"id": f"f{i}", "name": f"Folder {i}", "mimeType": FOLDER, "parent": "root"} for i in range(60)]
        items += [file_item(f"x{i}", f"z{i}.txt", "f0", b"x") for i in range(5)]
        items.append({"id": "sub", "name": "A sub", "mimeType": FOLDER, "parent": "f0"})
        drive = FakeDrive(items, page_size=2)
        cache = {}
        savegdrive.list_folders(drive, [f"f{i}" for i in range(60)], cache)
        self.assertEqual(len(cache), 60)
        self.assertEqual(len(cache["f0"]), 6)
        self.assertEqual(cache["f0"][0]["id"], "sub")  # folders first
        self.assertEqual(drive.batches[0], 50)  # one call per folder and page, batched
        self.assertEqual(sum(drive.batches), 62)

    def test_retries_rate_limits(self):
        drive = demo_drive()
        drive.failures["docs"] = [http_error(429)]
        with mock.patch.object(savegdrive.time, "sleep"):
            cache = {}
            savegdrive.list_folders(drive, ["docs"], cache)
        self.assertEqual(len(cache["docs"]), 4)

    def test_retries_a_later_page_from_its_own_token(self):
        drive = demo_drive()
        drive.page_size = 2  # Docs has 4 children: pages None and "2"
        drive.failures[("docs", "2")] = [http_error(429)]
        with mock.patch.object(savegdrive.time, "sleep"):
            cache = {}
            savegdrive.list_folders(drive, ["docs"], cache)
        self.assertEqual(drive.failures[("docs", "2")], [])  # the second page did fail once
        ids = [item["id"] for item in cache["docs"]]
        self.assertEqual(sorted(ids), ["a", "b", "form", "report"])  # each once, none listed twice

    def test_retries_403_rate_limits(self):
        for reason in ("userRateLimitExceeded", "rateLimitExceeded"):
            with self.subTest(reason=reason):
                drive = demo_drive()
                drive.failures["docs"] = [http_error(403, reason)]
                with mock.patch.object(savegdrive.time, "sleep"):
                    cache = {}
                    savegdrive.list_folders(drive, ["docs"], cache)
                self.assertEqual(len(cache["docs"]), 4)
                self.assertEqual(drive.failures["docs"], [])

    def test_gives_up_after_retries(self):
        drive = demo_drive()
        drive.failures["docs"] = [http_error(503) for _ in range(10)]
        with mock.patch.object(savegdrive.time, "sleep"):
            with self.assertRaises(HttpError):
                savegdrive.list_folders(drive, ["docs"], {})
        # One first try plus API_RETRIES retries, not one more.
        self.assertEqual(len(drive.failures["docs"]), 10 - 1 - savegdrive.API_RETRIES)

    def test_raises_other_errors(self):
        drive = demo_drive()
        drive.failures["docs"] = [http_error(404)]
        with self.assertRaises(HttpError):
            savegdrive.list_folders(drive, ["docs"], {})

    def test_raises_403_without_rate_limit(self):
        drive = demo_drive()
        drive.failures["docs"] = [http_error(403, "insufficientFilePermissions"), http_error(429)]
        with mock.patch.object(savegdrive.time, "sleep"):
            with self.assertRaises(HttpError) as raised:
                savegdrive.list_folders(drive, ["docs"], {})
        self.assertEqual(raised.exception.status_code, 403)

    def test_tree_order_and_totals(self):
        nodes, browsed = savegdrive.collect_drive_tree(demo_drive(), "root", {})
        paths = [node["display_path"] for node in nodes]
        self.assertEqual(paths[:3], ["Docs", "Photos", "notes.txt"])
        self.assertIn("Docs/Report", paths)
        for node in nodes:
            if node["parent"] is not None:
                self.assertLess(node["parent"], node["index"])
        self.assertEqual(len(browsed[Path()]), 3)
        totals = savegdrive.folder_totals(nodes)
        self.assertEqual(totals[0], [4, 10])  # Docs: a, b, Report, Survey; 5 + 5 known bytes

    def test_shortcut_cycle_stops(self):
        drive = FakeDrive([
            {"id": "loop", "name": "Loop", "mimeType": FOLDER, "parent": "root"},
            {"id": "back", "name": "Back", "mimeType": savegdrive.SHORTCUT_MIME_TYPE, "parent": "loop",
             "shortcutDetails": {"targetId": "loop", "targetMimeType": FOLDER}},
        ])
        nodes, _ = savegdrive.collect_drive_tree(drive, "root", {})
        self.assertEqual([node["display_path"] for node in nodes], ["Loop", "Loop/Back"])


    def test_folder_reached_twice_is_loaded_once(self):
        # Two shortcuts per level to the next one: 2^levels paths, but every folder is listed once.
        nodes, _ = savegdrive.collect_drive_tree(FakeDrive(crossed_shortcuts(12)), "root", {})
        self.assertLess(len(nodes), 50)
        self.assertEqual([node["display_path"] for node in nodes].count("Team/L0/a/a/a/a/a/a/a/a/a/a/a/a/video.mp4"), 1)
        self.assertEqual(sum(node["item"]["name"] == "video.mp4" for node in nodes), 1)


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.folder = {"name": "Docs", "mimeType": FOLDER}
        self.child = {"name": "a.txt", "mimeType": "text/plain"}

    def test_parent_replaces_children(self):
        selections = []
        savegdrive.add_selected_item(selections, self.child, Path("Docs"))
        savegdrive.add_selected_item(selections, self.folder, Path())
        self.assertEqual([savegdrive.selected_path(entry) for entry in selections], [Path("Docs")])
        self.assertFalse(savegdrive.add_selected_item(selections, self.child, Path("Docs")))
        self.assertEqual(savegdrive.path_selection_state(Path("Docs/a.txt"), selections), "*")

    def test_scopes(self):
        root_items = [self.folder, {"name": "n.txt", "mimeType": "text/plain"}]
        selections = []
        savegdrive.add_selected_item(selections, self.folder, Path())
        self.assertEqual(savegdrive.build_local_scopes(selections, {Path(): root_items}), [Path("Docs")])
        savegdrive.add_selected_item(selections, root_items[1], Path())
        self.assertEqual(savegdrive.build_local_scopes(selections, {Path(): root_items}), [Path()])


KILLED_SYNC = """
import json, signal, sys, tempfile, types
from unittest import mock
sys.path.insert(0, sys.argv[1])
tempfile.tempdir = sys.argv[2]
tempfile.TemporaryDirectory = lambda: types.SimpleNamespace(name=tempfile.mkdtemp(), cleanup=lambda: None)  # kept for the parent
import savegdrive
import test_savegdrive as t

case = t.SyncTest("test_first_run_then_unchanged")
case.setUp()
case.run_sync()
for item_id, name in [("a", "a.txt"), ("b", "b.txt")]:
    case.drive.items[item_id].update(t.file_item(item_id, name, "docs", name.encode() + b" v2",
                                                 modifiedTime="2026-03-01T00:00:00.000Z"))
print(json.dumps({"root": str(case.root),
                  "a": savegdrive.remote_signature(case.drive.public(case.drive.items["a"]))}), flush=True)

def fetch(service, item, file_handle, stop=None):
    content = service.items[savegdrive.effective_id(item)]["content"]
    if item["name"] != "b.txt":
        file_handle.write(content)
        return
    file_handle.write(content[:3])
    signal.raise_signal(getattr(signal, sys.argv[3]))  # the terminal closed, or the system shuts down
    if stop is not None and stop.wait(10):
        raise savegdrive.Stopped()
    file_handle.write(content[3:])

plan, state = case.preview()
with mock.patch.object(savegdrive, "fetch_media", fetch):
    case.apply(plan, state, jobs=1)
"""


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "GDrive"
        savegdrive.initialize_managed_destination(self.root)
        self.trash = Path(self.temp.name) / "system trash"
        self.trash.mkdir()

        def fake_trash_local(path):
            os.replace(path, self.trash / f"{len(list(self.trash.iterdir()))}-{path.name}")

        for name, fake in [("fetch_media", fake_fetch), ("upload_file", fake_upload), ("trash_local", fake_trash_local)]:
            patcher = mock.patch.object(savegdrive, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        self.drive = demo_drive()

    def preview(self, only=None):
        """The plan the preview shows, and the state it was computed against (only: the top-level names chosen)."""
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None and (only is None or node["item"]["name"] in only):
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        return savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache), state

    def apply(self, plan, state, jobs=1):
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            return savegdrive.apply_plan(self.drive, plan, self.root, state, jobs, lambda: self.drive)

    def run_sync(self, jobs=1):
        plan, state = self.preview()
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        return statuses, self.apply(plan, state, jobs)

    def test_first_run_then_unchanged(self):
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "NEW")
        self.assertEqual(statuses["Docs/Report.docx"], "NEW")
        self.assertEqual(statuses["Docs/Survey"], "SKIPPED")
        self.assertEqual(results["FILE_NEW"], 5)
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha")
        self.assertEqual((self.root / "Docs/Report.docx").read_bytes(), b"docx bytes")

        statuses, _ = self.run_sync()
        self.assertEqual({statuses[path] for path in ("Docs", "Docs/a.txt", "Docs/Report.docx")}, {"UNCHANGED"})

    def test_listing_snapshot_written_after_a_sync_is_never_sent_to_drive(self):
        # tests 74: main's run_sync saves the --again snapshot at the root of the folder after each sync
        self.run_sync()
        savegdrive.save_snapshot(self.root, "token", {}, ["root"])
        self.assertTrue((self.root / savegdrive.REMOTE_CACHE_NAME).exists())
        statuses, _ = self.run_sync()
        self.assertNotIn(savegdrive.REMOTE_CACHE_NAME, statuses)
        self.assertNotIn(f"{savegdrive.REMOTE_CACHE_NAME}.tmp", statuses)
        self.assertNotIn(savegdrive.REMOTE_CACHE_NAME, [item["name"] for item in self.drive.items.values()])

    def test_printed_plan_progress_and_results_stay_readable_on_a_light_terminal(self):
        # design 18: yellow, green and cyan text is 1.1:1 to 2:1 on a white terminal. Printed words, counts
        # and paths keep the terminal's own color; only a ↓ or the spinner may stay green or cyan.
        pale = {"yellow", "bright_yellow", "green", "bright_green", "cyan", "bright_cyan"}

        def pale_texts(console):
            return [segment.text for segment in console._record_buffer
                    if segment.style and segment.style.color and segment.style.color.name in pale
                    and segment.text.strip() not in {"", "↓"} and not "⠀" <= segment.text <= "⣿"]

        def terminal():
            return Console(file=io.StringIO(), width=200, force_terminal=True, color_system="standard", record=True,
                           highlight=False)  # as savegdrive.console

        self.run_sync()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        (self.root / "Docs/b.txt").write_bytes(b"bravo, edited here")
        (self.root / "Docs/new.txt").write_bytes(b"made here")
        (self.root / "Ideas").mkdir()
        self.drive.items["p"].update(file_item("p", "beach.jpg", "photos", b"jpeg v2"))
        (self.root / "Photos/beach.jpg").write_bytes(b"jpeg, edited here")
        plan, state = self.preview()
        conflict = next(entry for entry in plan if entry["relative_path"].as_posix() == "Photos/beach.jpg")
        self.assertEqual(conflict["status"], "CONFLICT")
        savegdrive.set_keep_both(plan, [conflict], True)
        statuses = {entry["status"] for entry in plan}
        self.assertTrue({"UPDATE", "UPLOAD", "UPLOAD_NEW", "KEEP_BOTH"} <= statuses, statuses)
        extra = [{"kind": "FILE", "status": status, "relative_path": Path(f"{status}.txt")}
                 for status in ("NEW", "CONFLICT", "LOCAL_ONLY", "SKIPPED")]

        console = terminal()
        with mock.patch.object(savegdrive, "console", console):
            savegdrive.print_plan(plan + extra, self.root)
        self.assertIn("keep both", console.export_text(clear=False))
        self.assertEqual(pale_texts(console), [])

        console = terminal()
        with mock.patch.object(savegdrive, "console", console):
            results = savegdrive.apply_plan(self.drive, plan, self.root, state, 1, lambda: self.drive)
            savegdrive.print_results(results + savegdrive.Counter(FILE_CONFLICT=1, FILE_NEW=1, FOLDER_NEW=1))
        self.assertIn("conflict kept both ways", console.export_text(clear=False))
        self.assertEqual(pale_texts(console), [])
        # The M/N counter of the progress bar: Rich's MofNCompleteColumn prints it green.
        with mock.patch.object(savegdrive, "Progress", wraps=savegdrive.Progress) as progress:
            with mock.patch.object(savegdrive, "console", terminal()):
                savegdrive.apply_plan(self.drive, [], self.root, state, 1, lambda: self.drive)
        self.assertNotIn("MofNCompleteColumn", [type(column).__name__ for column in progress.call_args.args])

    def test_new_version_from_drive_keeps_the_local_file_mode(self):
        # bugs 46: a new version from Drive replaced the file with a default-mode one (chmod +x lost).
        self.run_sync()
        os.chmod(self.root / "Docs/a.txt", 0o755)
        os.chmod(self.root / "Docs/b.txt", 0o600)
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"bravo v2"))
        statuses, _ = self.run_sync()
        self.assertEqual((statuses["Docs/a.txt"], statuses["Docs/b.txt"]), ("UPDATE", "UPDATE"))
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha v2")
        self.assertEqual(stat.S_IMODE((self.root / "Docs/a.txt").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((self.root / "Docs/b.txt").stat().st_mode), 0o600)

    def test_changes_go_both_ways(self):
        self.run_sync()
        # On Drive: a changed, beach.jpg deleted. Here: b changed, notes deleted, new.txt and a new folder.
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        del self.drive.items["p"]
        (self.root / "Docs/b.txt").write_bytes(b"bravo, edited here")
        (self.root / "notes.txt").unlink()
        (self.root / "Docs/new.txt").write_bytes(b"made here")
        (self.root / "Ideas/2026").mkdir(parents=True)
        (self.root / "Ideas/2026/plan.txt").write_bytes(b"plan")
        (self.root / "Photos/mine.txt").write_bytes(b"local")

        statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "UPDATE")
        self.assertEqual(statuses["Docs/b.txt"], "UPLOAD")
        self.assertEqual(statuses["notes.txt"], "TRASH_REMOTE")
        self.assertEqual(statuses["Photos/beach.jpg"], "TRASH_LOCAL")
        self.assertEqual(statuses["Docs/new.txt"], "UPLOAD_NEW")
        self.assertEqual(statuses["Ideas"], "UPLOAD_NEW")
        self.assertEqual(statuses["Ideas/2026/plan.txt"], "UPLOAD_NEW")
        self.assertEqual(statuses["Photos/mine.txt"], "UPLOAD_NEW")

        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha v2")
        self.assertEqual(self.drive.items["b"]["content"], b"bravo, edited here")
        self.assertTrue(self.drive.items["n"]["trashed"])
        self.assertFalse((self.root / "Photos/beach.jpg").exists())
        self.assertEqual(sorted(path.name.split("-", 1)[1] for path in self.trash.iterdir()), ["beach.jpg"])
        names = {item["name"]: item for item in self.drive.items.values()}
        self.assertEqual(names["new.txt"]["parent"], "docs")
        self.assertEqual(names["plan.txt"]["parent"], names["2026"]["id"])
        self.assertEqual(names["2026"]["parent"], names["Ideas"]["id"])
        self.assertEqual(names["Ideas"]["parent"], "root")
        self.assertEqual(results["FILE_UPLOAD_NEW"], 3)

        # Everything is now the same on both sides, and the tree marks the sent files as synced.
        nodes, _ = savegdrive.collect_drive_tree(self.drive, "root", {})
        marks = savegdrive.local_marks(nodes, self.root, savegdrive.load_state(self.root))
        b_node = next(node for node in nodes if node["display_path"] == "Docs/b.txt")
        self.assertEqual(marks.get(b_node["index"]), "synced")
        statuses, _ = self.run_sync()
        self.assertEqual({status for path, status in statuses.items() if path != "Docs/Survey"}, {"UNCHANGED"})

    def test_new_local_folders_are_created_on_drive_one_batch_per_level(self):
        # perf 10: 20 years x 12 months were 261 sequential files.create before the first upload.
        self.run_sync()
        for year in range(3):
            for month in range(1, 61):
                (self.root / f"Docs/Archive/{2000 + year}/{month:02d}").mkdir(parents=True)
        (self.root / "Docs/Archive/2000/01/a.txt").write_bytes(b"a")
        self.drive.create_failures["2001"] = [http_error(429), http_error(403)]  # rate limited, then refused
        plan, state = self.preview()
        self.drive.batches.clear()
        with mock.patch.object(savegdrive.time, "sleep"):
            results = self.apply(plan, state, jobs=4)
        # Archive, then 3 years (2001 rate limited, retried, refused), then 120 months in batches of API_BATCH_SIZE.
        self.assertEqual(self.drive.batches, [1, 3, 1, 50, 50, 20])
        self.assertEqual(results["FOLDER_UPLOAD_NEW"], 1 + 2 + 120)
        self.assertEqual(results["FOLDER_ERROR"], 1 + 60)
        folders = {(item["name"], item["parent"]): item["id"] for item in self.drive.items.values()}
        archive = folders[("Archive", "docs")]
        self.assertNotIn(("2001", archive), folders)
        january = folders[("01", folders[("2000", archive)])]
        self.assertIn(("a.txt", january), folders)

    def test_drive_folder_that_cannot_be_made_here_is_an_error_and_comes_back(self):
        # tests 51: a failed local mkdir of a new empty Drive folder must count as an error, not pass as synced.
        self.run_sync()
        self.drive.items["new"] = {"id": "new", "name": "New", "mimeType": FOLDER, "parent": "docs"}
        docs = self.root / "Docs"
        docs.chmod(0o555)
        try:
            statuses, results = self.run_sync()
            self.assertEqual(statuses["Docs/New"], "NEW")
            self.assertEqual(results["FOLDER_ERROR"], 1)
            self.assertEqual(results["FOLDER_NEW"], 0)
            self.assertFalse((docs / "New").exists())
        finally:
            docs.chmod(0o755)
        statuses, results = self.run_sync()  # tried again on the next run
        self.assertEqual(statuses["Docs/New"], "NEW")
        self.assertEqual((results["FOLDER_NEW"], results["FOLDER_ERROR"]), (1, 0))
        self.assertTrue((docs / "New").is_dir())

    def test_conflicts(self):
        self.run_sync()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha from Drive"))
        (self.root / "Docs/a.txt").write_bytes(b"alpha from here")
        (self.root / "Docs/Report.docx").write_bytes(b"edited export")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "CONFLICT")  # changed on both sides
        self.assertEqual(statuses["Docs/Report.docx"], "CONFLICT")  # exports never go back up
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha from here")
        self.assertEqual(self.drive.items["a"]["content"], b"alpha from Drive")
        self.assertEqual(self.drive.uploads, [])

    def test_same_names_at_the_top_of_my_drive_are_conflicts(self):
        # Two "Untitled document"s, and a folder and a file both named "notes.txt": none is dropped
        # from the plan or synced with the other's content.
        self.drive.items["u1"] = file_item("u1", "dup.txt", "root", b"first")
        self.drive.items["u2"] = file_item("u2", "dup.txt", "root", b"second")
        self.drive.items["nf"] = {"id": "nf", "name": "notes.txt", "mimeType": FOLDER, "parent": "root"}
        self.drive.items["in"] = file_item("in", "inside.txt", "nf", b"inside")
        plan, state = self.preview()
        rows = sorted((entry["relative_path"].as_posix(), entry["kind"], entry["status"]) for entry in plan
                      if entry["relative_path"].parts[0] in {"dup.txt", "notes.txt"})
        self.assertEqual(rows, [("dup.txt", "FILE", "CONFLICT"), ("dup.txt", "FILE", "CONFLICT"),
                                ("notes.txt", "FILE", "CONFLICT"), ("notes.txt", "FOLDER", "CONFLICT")])
        self.apply(plan, state)
        self.assertFalse((self.root / "dup.txt").exists())
        self.assertFalse((self.root / "notes.txt").exists())

    def test_file_replaced_on_drive_under_the_same_name_comes_down(self):
        self.run_sync()
        # On Drive: a.txt deleted and sent again (new id, new content); the whole Photos folder too.
        del self.drive.items["a"]
        self.drive.items["a2"] = file_item("a2", "a.txt", "docs", b"alpha v2")
        del self.drive.items["photos"], self.drive.items["p"]
        self.drive.items["photos2"] = {"id": "photos2", "name": "Photos", "mimeType": FOLDER, "parent": "root"}
        self.drive.items["p2"] = file_item("p2", "beach.jpg", "photos2", b"jpeg retouched")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "UPDATE")
        self.assertEqual(statuses["Photos/beach.jpg"], "UPDATE")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha v2")
        self.assertEqual((self.root / "Photos/beach.jpg").read_bytes(), b"jpeg retouched")
        paths = [record["path"] for record in savegdrive.load_state(self.root)["files"].values()]
        self.assertEqual(paths.count("Docs/a.txt"), 1)  # the replaced file's record is gone
        statuses, _ = self.run_sync()
        self.assertEqual({statuses["Docs/a.txt"], statuses["Photos/beach.jpg"]}, {"UNCHANGED"})
        # Another file takes the name on Drive; the copy here was not changed: it comes down.
        self.drive.items["a2"]["name"] = "a-old.txt"
        self.drive.items["b"]["name"] = "a.txt"
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "UPDATE")
        self.assertEqual(statuses["Docs/a-old.txt"], "NEW")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"bravo")
        # Changed here too: a real conflict, the copy here is kept.
        (self.root / "Docs/a.txt").write_bytes(b"edited here")
        del self.drive.items["b"]
        self.drive.items["b2"] = file_item("b2", "a.txt", "docs", b"from Drive")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "CONFLICT")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"edited here")

    def test_broken_file_shortcut_skipped_not_whole_sync(self):
        # An editor adds a shortcut to one of their files, syncs, then deletes that file.
        self.drive.items.update({
            "q4": {"id": "q4", "name": "Q4 numbers.txt", "mimeType": savegdrive.SHORTCUT_MIME_TYPE,
                   "parent": "docs", "shortcutDetails": {"targetId": "theirs", "targetMimeType": "text/plain"}},
            "theirs": file_item("theirs", "numbers.txt", "elsewhere", b"numbers"),
        })
        self.run_sync()
        self.assertEqual((self.root / "Docs/Q4 numbers.txt").read_bytes(), b"numbers")
        del self.drive.items["theirs"]
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        (self.root / "Docs/b.txt").write_bytes(b"bravo, edited here")

        output = io.StringIO()
        with mock.patch.object(savegdrive, "console", Console(file=output, width=200)):
            plan, state = self.preview()
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertIn("Docs/Q4 numbers.txt: shortcut target not available, skipped", output.getvalue())
        self.assertEqual(statuses["Docs/Q4 numbers.txt"], "SKIPPED")
        self.assertEqual(statuses["Docs/a.txt"], "UPDATE")
        self.assertEqual(statuses["Docs/b.txt"], "UPLOAD")
        self.apply(plan, state)
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha v2")
        self.assertEqual(self.drive.items["b"]["content"], b"bravo, edited here")
        # The copy synced before stays here, and is not sent to Drive as a new file.
        self.assertEqual((self.root / "Docs/Q4 numbers.txt").read_bytes(), b"numbers")
        self.assertEqual(list(self.trash.iterdir()), [])
        self.assertNotIn("Q4 numbers.txt", {item["name"] for item in self.drive.items.values()
                                             if item["mimeType"] != savegdrive.SHORTCUT_MIME_TYPE})

        # Any other Drive error still stops the run.
        with mock.patch.object(savegdrive, "resolve_file_shortcut", side_effect=http_error(500)):
            with self.assertRaises(HttpError):
                self.preview()

    def test_folder_shortcut_content_only_comes_down(self):
        # My Drive holds a shortcut to someone else's folder: its content never goes back up.
        self.drive.items.update({
            "link": {"id": "link", "name": "Team", "mimeType": savegdrive.SHORTCUT_MIME_TYPE, "parent": "root",
                     "shortcutDetails": {"targetId": "team", "targetMimeType": FOLDER}},
            "team": {"id": "team", "name": "team", "mimeType": FOLDER, "parent": "elsewhere"},
            "sub": {"id": "sub", "name": "Sub", "mimeType": FOLDER, "parent": "team"},
            "budget": file_item("budget", "budget.xlsx", "team", b"budget"),
            "plan": file_item("plan", "plan.txt", "team", b"plan"),
            "deep": file_item("deep", "deep.txt", "sub", b"deep"),
        })
        self.run_sync()
        self.assertEqual((self.root / "Team/Sub/deep.txt").read_bytes(), b"deep")
        (self.root / "Team/plan.txt").unlink()
        (self.root / "Team/budget.xlsx").write_bytes(b"local edit")
        (self.root / "Team/new.txt").write_bytes(b"made here")
        for path in (self.root / "Team/Sub").iterdir():
            path.unlink()
        (self.root / "Team/Sub").rmdir()

        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Team/plan.txt"], "NEW")  # deleted here: it comes back
        self.assertEqual(statuses["Team/budget.xlsx"], "CONFLICT")
        self.assertEqual(statuses["Team/new.txt"], "LOCAL_ONLY")
        self.assertEqual(statuses["Team/Sub"], "NEW")
        self.assertFalse(any(item.get("trashed") for item in self.drive.items.values()))
        self.assertEqual(self.drive.items["budget"]["content"], b"budget")
        self.assertEqual(self.drive.uploads, [])
        self.assertNotIn("new.txt", {item["name"] for item in self.drive.items.values()})
        self.assertEqual((self.root / "Team/plan.txt").read_bytes(), b"plan")
        self.assertEqual((self.root / "Team/Sub/deep.txt").read_bytes(), b"deep")

    def test_folder_shortcut_to_its_parent_skipped_not_whole_sync(self):
        # A "back to the project" shortcut Docs/Back -> Docs: skipped, never followed forever.
        self.drive.items["back"] = {"id": "back", "name": "Back", "mimeType": savegdrive.SHORTCUT_MIME_TYPE,
                                    "parent": "docs", "shortcutDetails": {"targetId": "docs", "targetMimeType": FOLDER}}
        real_list_folders, calls = savegdrive.list_folders, []

        def bounded_list_folders(*args):
            calls.append(args)
            if len(calls) > 50:
                raise AssertionError("listing never ends")
            return real_list_folders(*args)

        with mock.patch.object(savegdrive, "list_folders", bounded_list_folders):
            # A fresh cache, as on a first plan: build_plan lists every level itself.
            nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", {})
            selections = []
            for node in nodes:
                if node["parent"] is None:
                    savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
            plan = savegdrive.build_plan(self.drive, selections, savegdrive.build_local_scopes(selections, browsed),
                                         self.root, savegdrive.load_state(self.root), {})
            self.assertEqual({entry["relative_path"].as_posix(): entry["status"] for entry in plan}["Docs/Back"],
                             "SKIPPED")
            # The same stop without build_plan's record of planned folders.
            alone = []
            savegdrive.collect_plan(self.drive, self.drive.items["docs"], Path(), self.root,
                                    savegdrive.load_state(self.root), alone, {})
            self.assertEqual({entry["relative_path"].as_posix(): entry["status"] for entry in alone}["Docs/Back"],
                             "SKIPPED")

            statuses, results = self.run_sync()
            self.assertEqual(statuses["Docs/Back"], "SKIPPED")
            self.assertEqual(results["FILE_NEW"], 5)
            self.assertFalse((self.root / "Docs/Back").exists())

            # Docs deleted here: the loop is not checked forever, and Docs is not trashed on Drive.
            shutil.rmtree(self.root / "Docs")
            statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs"], "NEW")
        self.assertEqual(statuses["Docs/Back"], "SKIPPED")
        self.assertFalse(self.drive.items["docs"].get("trashed"))
        self.assertFalse(self.drive.items["back"].get("trashed"))

    def test_folder_reached_twice_syncs_once(self):
        # Crossed shortcuts from an editor of a shared folder: one copy, not 2^levels.
        self.drive.items.update({item["id"]: item for item in crossed_shortcuts(12)})
        self.drive.items.update({
            "sub": {"id": "sub", "name": "Sub", "mimeType": FOLDER, "parent": "team"},
            "link": {"id": "link", "name": "Link", "mimeType": savegdrive.SHORTCUT_MIME_TYPE, "parent": "team",
                     "shortcutDetails": {"targetId": "sub", "targetMimeType": FOLDER}},
            "inside": file_item("inside", "inside.txt", "sub", b"inside"),
        })
        statuses, results = self.run_sync()
        self.assertLess(len(statuses), 60)
        self.assertEqual(results["FILE_NEW"], 7)  # the demo's 5, video.mp4 and inside.txt once each
        self.assertEqual(len(list(self.root.rglob("video.mp4"))), 1)
        self.assertEqual(statuses["Team/L0/a/b"], "SKIPPED")
        self.assertEqual(statuses["Team/Sub"], "SKIPPED")  # already synced as Team/Link
        self.assertEqual((self.root / "Team/Link/inside.txt").read_bytes(), b"inside")

        # Whatever is on disk under the second path stays there: never sent, never trashed.
        (self.root / "Team/Sub").mkdir()
        (self.root / "Team/Sub/mine.txt").write_bytes(b"mine")
        statuses, _ = self.run_sync()
        self.assertNotIn("Team/Sub/mine.txt", statuses)
        self.assertEqual((self.root / "Team/Sub/mine.txt").read_bytes(), b"mine")
        self.assertEqual(list(self.trash.iterdir()), [])
        self.assertEqual(self.drive.uploads, [])

    def test_shortcut_chain_too_deep_skipped_not_whole_sync(self):
        # An editor of a shared folder chains 1000 folder shortcuts: Team/L0/a/a/.../deep.txt.
        levels = 1000
        self.drive.items.update({
            "team": {"id": "team", "name": "Team", "mimeType": FOLDER, "parent": "root"},
            "s0": {"id": "s0", "name": "L0", "mimeType": FOLDER, "parent": "team"},
            "deep": file_item("deep", "deep.txt", f"s{levels}", b"deep"),
        })
        for level in range(levels):
            self.drive.items[f"s{level + 1}"] = {"id": f"s{level + 1}", "name": f"s{level + 1}",
                                                  "mimeType": FOLDER, "parent": "elsewhere"}
            self.drive.items[f"a{level}"] = {
                "id": f"a{level}", "name": "a", "mimeType": savegdrive.SHORTCUT_MIME_TYPE, "parent": f"s{level}",
                "shortcutDetails": {"targetId": f"s{level + 1}", "targetMimeType": FOLDER}}
        nodes, _ = savegdrive.collect_drive_tree(self.drive, "root", {})
        self.assertLessEqual(max(len(node["display_path"].split("/")) for node in nodes),
                             savegdrive.MAX_FOLDER_DEPTH)

        bound = Path("Team/L0", *["a"] * (savegdrive.MAX_FOLDER_DEPTH - 2))  # the first folder too deep
        (self.root / bound).mkdir(parents=True)
        (self.root / bound / "mine.txt").write_bytes(b"mine")
        plan, state = self.preview()
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertEqual(statuses["Docs/a.txt"], "NEW")  # the rest still syncs
        self.assertEqual(statuses[bound.as_posix()], "SKIPPED")
        self.assertNotIn((bound / "a").as_posix(), statuses)
        self.assertNotIn((bound / "mine.txt").as_posix(), statuses)  # below it: left as it is
        self.assertTrue(any("too deep" in note for note in savegdrive.plan_notes(plan)))
        self.apply(plan, state)
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha")
        self.assertEqual((self.root / bound / "mine.txt").read_bytes(), b"mine")
        self.assertEqual(self.drive.uploads, [])

        # A whole deep chain deleted here is not checked past the bound, so never trashed on Drive.
        self.assertFalse(savegdrive.remote_subtree_unchanged(self.drive, "s0", Path("Team/L0"), state, {}))

    def test_deleted_on_one_side_but_changed_on_the_other_comes_back(self):
        self.run_sync()
        (self.root / "Docs/a.txt").unlink()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        (self.root / "Docs/b.txt").write_bytes(b"bravo, edited here")
        del self.drive.items["b"]
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "NEW")  # deleted here, changed on Drive: downloaded
        self.assertEqual(statuses["Docs/b.txt"], "UPLOAD_NEW")  # deleted on Drive, changed here: sent
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha v2")

    def test_folder_deleted_here_goes_to_the_drive_trash(self):
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Photos")
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Photos"], "TRASH_REMOTE")
        self.assertNotIn("Photos/beach.jpg", statuses)
        self.assertTrue(self.drive.items["photos"]["trashed"])
        self.assertEqual(results["FOLDER_TRASH_REMOTE"], 1)

    def test_folder_deleted_here_keeps_a_file_added_on_drive(self):
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Photos")
        self.drive.items["pn"] = file_item("pn", "new.jpg", "photos", b"a colleague's photo")
        statuses, _ = self.run_sync()
        self.assertNotEqual(statuses["Photos"], "TRASH_REMOTE")
        self.assertFalse(self.drive.is_trashed(self.drive.items["photos"]))
        self.assertFalse(self.drive.is_trashed(self.drive.items["pn"]))
        self.assertEqual((self.root / "Photos/new.jpg").read_bytes(), b"a colleague's photo")

    def test_folder_deleted_here_keeps_a_file_changed_on_drive(self):
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Photos")
        self.drive.items["p"].update(file_item("p", "beach.jpg", "photos", b"jpeg v2",
                                               modifiedTime="2025-06-01T00:00:00.000Z"))
        statuses, _ = self.run_sync()
        self.assertNotEqual(statuses["Photos"], "TRASH_REMOTE")
        self.assertEqual(statuses["Photos/beach.jpg"], "NEW")
        self.assertFalse(self.drive.is_trashed(self.drive.items["photos"]))
        self.assertEqual((self.root / "Photos/beach.jpg").read_bytes(), b"jpeg v2")

    def test_folder_deleted_here_keeps_its_google_form(self):
        self.drive.items["pf"] = {"id": "pf", "name": "Customer survey", "mimeType": FORM, "parent": "photos"}
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Photos")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos"], "NEW")
        self.assertEqual(statuses["Photos/beach.jpg"], "TRASH_REMOTE")
        self.assertEqual(statuses["Photos/Customer survey"], "SKIPPED")
        self.assertFalse(self.drive.is_trashed(self.drive.items["pf"]))

    def test_export_deleted_here_comes_back(self):
        self.drive.items["minutes"] = {"id": "minutes", "name": "Minutes", "mimeType": FOLDER, "parent": "root"}
        self.drive.items["board"] = {"id": "board", "name": "Board", "mimeType": DOC, "parent": "minutes",
                                     "content": b"board docx", "modifiedTime": "2025-01-02T03:04:05.000Z"}
        self.run_sync()
        import shutil
        (self.root / "Docs/Report.docx").unlink()
        shutil.rmtree(self.root / "Minutes")  # only exports inside
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/Report.docx"], "NEW")
        self.assertEqual(statuses["Minutes"], "NEW")
        self.assertEqual(statuses["Minutes/Board.docx"], "NEW")
        self.assertFalse(self.drive.is_trashed(self.drive.items["report"]))
        self.assertFalse(self.drive.is_trashed(self.drive.items["board"]))
        self.assertEqual((self.root / "Minutes/Board.docx").read_bytes(), b"board docx")

    def test_folder_deleted_here_keeps_files_you_cannot_trash(self):
        self.drive.items["drafts"] = {"id": "drafts", "name": "Drafts", "mimeType": FOLDER, "parent": "photos",
                                      "capabilities": {"canTrash": False}}
        self.drive.items["spec"] = file_item("spec", "spec.pdf", "photos", b"spec", capabilities={"canTrash": False})
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Photos")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos"], "NEW")
        self.assertEqual(statuses["Photos/beach.jpg"], "TRASH_REMOTE")
        self.assertEqual(statuses["Photos/spec.pdf"], "NEW")  # a colleague's file comes back
        self.assertFalse(self.drive.is_trashed(self.drive.items["spec"]))
        self.assertFalse(self.drive.is_trashed(self.drive.items["drafts"]))
        self.assertEqual((self.root / "Photos/spec.pdf").read_bytes(), b"spec")

    def test_folder_you_cannot_trash_deleted_here_is_kept(self):
        self.drive.items["photos"]["capabilities"] = {"canEdit": False, "canTrash": False, "canAddChildren": False}
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Photos")
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Photos"], "NEW")
        self.assertFalse(self.drive.items["photos"].get("trashed"))
        self.assertTrue((self.root / "Photos").is_dir())
        self.assertEqual(results["FILE_ERROR"], 0)

    def test_folder_deleted_here_keeps_the_state_of_a_folder_named_like_it(self):
        # "Photos 2" starts with "Photos": forgetting Photos must not forget what was synced in Photos 2.
        self.drive.items["photos2"] = {"id": "photos2", "name": "Photos 2", "mimeType": FOLDER, "parent": "root"}
        self.drive.items["x"] = file_item("x", "x.txt", "photos2", b"x")
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Photos")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos"], "TRASH_REMOTE")
        del self.drive.items["x"]  # a colleague deletes it on Drive
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos 2/x.txt"], "TRASH_LOCAL")
        self.assertFalse((self.root / "Photos 2/x.txt").exists())
        self.assertFalse(any(item["name"] == "x.txt" for item in self.drive.items.values()))

    def test_new_drive_folder_named_like_the_start_of_a_synced_one_comes_down(self):
        # "Project 2/y.txt" is below "Project 2", not below "Project": Project is new, not deleted here.
        self.drive.items["project2"] = {"id": "project2", "name": "Project 2", "mimeType": FOLDER, "parent": "root"}
        self.drive.items["y"] = file_item("y", "y.txt", "project2", b"y")
        self.run_sync()
        self.drive.items["project"] = {"id": "project", "name": "Project", "mimeType": FOLDER, "parent": "root"}
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Project"], "NEW")
        self.assertFalse(self.drive.is_trashed(self.drive.items["project"]))
        self.assertTrue((self.root / "Project").is_dir())

    def test_folder_deleted_here_counts_only_its_own_files(self):
        # The files of "Photos 2" are not below "Photos": deleting Photos is one file, no mass-deletion check.
        self.drive.items["photos2"] = {"id": "photos2", "name": "Photos 2", "mimeType": FOLDER, "parent": "root"}
        for i in range(30):
            self.drive.items[f"m{i}"] = file_item(f"m{i}", f"m{i}.txt", "photos2", b"x")
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Photos")
        plan, state = self.preview()
        self.assertEqual(savegdrive.deletion_count(plan, state), (1, len(state["files"])))
        self.assertFalse(savegdrive.needs_deletion_check(plan, state))

    def test_empty_folder_named_like_a_deleted_one_is_still_tracked(self):
        # Forgetting Photos must not forget the empty folder Photos 2: deleted here next, it goes to the Drive trash.
        self.drive.items["photos2"] = {"id": "photos2", "name": "Photos 2", "mimeType": FOLDER, "parent": "root"}
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Photos")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos"], "TRASH_REMOTE")
        (self.root / "Photos 2").rmdir()
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos 2"], "TRASH_REMOTE")
        self.assertTrue(self.drive.is_trashed(self.drive.items["photos2"]))
        self.assertFalse((self.root / "Photos 2").exists())

    def test_file_trashed_on_drive_goes_to_your_trash(self):
        self.run_sync()
        self.drive.items["p"]["trashed"] = True  # deleting on Drive trashes it, the item stays
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos/beach.jpg"], "TRASH_LOCAL")
        self.assertFalse((self.root / "Photos/beach.jpg").exists())
        self.assertEqual(sorted(path.name.split("-", 1)[1] for path in self.trash.iterdir()), ["beach.jpg"])

    def test_printed_preview_log_and_results_name_what_goes_to_a_trash(self):
        # tests 70: with --yes or --again outside the TUI, the printed preview and log are the only trace of what
        # went to a trash. Each must name the deletions on both sides.
        self.run_sync()
        (self.root / "Docs/b.txt").unlink()  # deleted here: to the Drive trash
        self.drive.items["p"]["trashed"] = True  # deleted on Drive: to your trash
        plan, state = self.preview()
        output = io.StringIO()
        with mock.patch.object(savegdrive, "console", Console(file=output, width=200, highlight=False)):
            savegdrive.print_plan(plan, self.root)
            results = savegdrive.apply_plan(self.drive, plan, self.root, state, 1, lambda: self.drive)
            savegdrive.print_results(results)
        lines = [" ".join(line.split()) for line in output.getvalue().splitlines()]
        self.assertIn("↑ to Drive trash Docs/b.txt 5 B", lines)
        self.assertIn("↓ to your trash Photos/beach.jpg", lines)
        preview = next(line for line in lines if line.startswith("Preview "))
        self.assertIn("1 ↑ to Drive trash", preview)
        self.assertIn("1 ↓ to your trash", preview)
        self.assertIn("✗ Docs/b.txt → Drive trash", lines)
        self.assertIn("✗ Photos/beach.jpg → your trash", lines)
        done = next(line for line in lines if line.startswith("Done "))
        self.assertIn("1 moved to the Drive trash", done)
        self.assertIn("1 moved to your trash", done)

    def test_folder_deleted_on_drive_goes_to_your_trash(self):
        self.run_sync()
        for key in ["photos", "p"]:
            del self.drive.items[key]
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos"], "TRASH_LOCAL")  # perf 11: the whole folder, in one call
        self.assertNotIn("Photos/beach.jpg", statuses)
        self.assertFalse((self.root / "Photos").exists())

    def test_folder_deleted_on_drive_goes_to_your_trash_in_one_call(self):
        # perf 11: one system-trash call per file was 14 s for 10,000 files, and the trash got them in bulk.
        self.drive.items["trip"] = {"id": "trip", "name": "Trip", "mimeType": FOLDER, "parent": "photos"}
        for index in range(3):
            self.drive.items[f"t{index}"] = file_item(f"t{index}", f"day{index}.jpg", "trip", b"jpg")
        self.drive.items["empty"] = {"id": "empty", "name": "Empty", "mimeType": FOLDER, "parent": "photos"}
        self.run_sync()
        for key in ["photos", "p", "trip", "t0", "t1", "t2", "empty"]:
            del self.drive.items[key]
        plan, state = self.preview()
        [folder] = [entry for entry in plan if entry["relative_path"].parts[:1] == ("Photos",)]
        self.assertEqual((folder["relative_path"], folder["status"]), (Path("Photos"), "TRASH_LOCAL"))
        self.assertEqual(savegdrive.deletion_count(plan, state)[0], 5)  # as file by file: 4 files, 1 empty folder
        with mock.patch.object(savegdrive, "trash_local", wraps=savegdrive.trash_local) as trash_local:
            results = self.apply(plan, state)
        self.assertEqual(trash_local.call_args_list, [mock.call(self.root / "Photos")])
        self.assertEqual(results["FOLDER_TRASH_LOCAL"], 1)
        [trashed] = list(self.trash.iterdir())
        self.assertEqual(sorted(path.relative_to(trashed).as_posix() for path in trashed.rglob("*")),
                         ["Empty", "Trip", "Trip/day0.jpg", "Trip/day1.jpg", "Trip/day2.jpg", "beach.jpg"])
        state = savegdrive.load_state(self.root)
        self.assertFalse([record for record in state["files"].values() if record["path"].startswith("Photos/")])

    def test_folder_deleted_on_drive_with_a_file_saved_after_the_preview_is_kept(self):
        self.run_sync()
        for key in ["photos", "p"]:
            del self.drive.items[key]
        plan, state = self.preview()
        self.assertEqual([entry["status"] for entry in plan if entry["relative_path"] == Path("Photos")],
                         ["TRASH_LOCAL"])
        (self.root / "Photos/new.jpg").write_bytes(b"saved while the preview is open")
        results = self.apply(plan, state)
        self.assertEqual(results["FILE_TRASH_LOCAL"] + results["FOLDER_TRASH_LOCAL"], 0)
        self.assertEqual(list(self.trash.iterdir()), [])
        self.assertEqual((self.root / "Photos/beach.jpg").read_bytes(), b"jpeg")

    def test_folder_deleted_on_drive_with_changes_here_comes_back(self):
        self.run_sync()
        (self.root / "Photos/beach.jpg").write_bytes(b"edited here")
        (self.root / "Photos/2026").mkdir()
        (self.root / "Photos/2026/new.txt").write_bytes(b"made here")
        for key in ["photos", "p"]:
            del self.drive.items[key]
        statuses, results = self.run_sync()
        for path in ("Photos", "Photos/beach.jpg", "Photos/2026", "Photos/2026/new.txt"):
            self.assertEqual(statuses[path], "UPLOAD_NEW", path)
        self.assertEqual(results["FILE_ERROR"] + results["FOLDER_ERROR"], 0)
        names = {item["name"]: item for item in self.drive.items.values() if not item.get("trashed")}
        self.assertEqual(names["Photos"]["parent"], "root")
        self.assertEqual(names["beach.jpg"]["content"], b"edited here")
        self.assertEqual(names["beach.jpg"]["parent"], names["Photos"]["id"])
        self.assertEqual(names["new.txt"]["parent"], names["2026"]["id"])
        self.assertEqual(names["2026"]["parent"], names["Photos"]["id"])
        statuses, _ = self.run_sync()
        self.assertEqual({path: status for path, status in statuses.items() if status not in {"UNCHANGED", "SKIPPED"}},
                         {})

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads every folder")
    def test_unreadable_folder_or_file_is_a_conflict_and_the_rest_syncs(self):
        # bugs 45: one unreadable folder (a Docker volume, made with sudo) or changed file stopped the whole plan.
        self.run_sync()
        locked = self.root / "Docs/lockeddir"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        (self.root / "Docs/b.txt").write_bytes(b"bravo, edited here")

        plan, state = self.preview()
        entries = {entry["relative_path"].as_posix(): entry for entry in plan}
        self.assertEqual(entries["Docs/lockeddir"]["status"], "CONFLICT")
        self.assertTrue(entries["Docs/lockeddir"].get("unreadable"))
        self.assertFalse(savegdrive.can_keep_both(entries["Docs/lockeddir"]))
        console = Console(file=io.StringIO(), width=200)
        with mock.patch.object(savegdrive, "console", console):
            savegdrive.print_plan(plan, self.root)
        printed = console.file.getvalue()
        self.assertIn("Docs/lockeddir/  (cannot read it here)", printed)
        self.assertIn("cannot be read here", printed)
        self.apply(plan, state)
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha v2")
        self.assertEqual(self.drive.items["b"]["content"], b"bravo, edited here")

        # A synced file changed here, then unreadable: a conflict, never sent nor overwritten.
        b_file = self.root / "Docs/b.txt"
        b_file.write_bytes(b"bravo, edited again")
        b_file.chmod(0)
        self.addCleanup(b_file.chmod, 0o644)
        self.drive.items["n"].update(file_item("n", "notes.txt", "root", b"notes v2"))
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs/b.txt"], "CONFLICT")
        self.assertEqual(self.drive.items["b"]["content"], b"bravo, edited here")
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"notes v2")
        b_file.chmod(0o644)

        # The whole synced folder unreadable: its files look deleted here, but nothing goes to the Drive trash.
        docs = self.root / "Docs"
        docs.chmod(0)
        self.addCleanup(docs.chmod, 0o755)
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs"], "CONFLICT")
        self.assertEqual({statuses[path] for path in ("Docs/a.txt", "Docs/b.txt", "Docs/Report.docx")}, {"CONFLICT"})
        self.assertFalse(any(item.get("trashed") for item in self.drive.items.values()))
        self.assertEqual(results["FILE_ERROR"], 0)
        docs.chmod(0o755)
        statuses, _ = self.run_sync()  # readable again: nothing was forgotten in between
        self.assertEqual(statuses["Docs/a.txt"], "UNCHANGED")
        self.assertEqual(statuses["Docs/b.txt"], "UPLOAD")  # readable again: its change goes up
        self.assertEqual(self.drive.items["b"]["content"], b"bravo, edited again")

    def test_deleted_on_both_sides_then_made_again_here_is_sent(self):
        self.run_sync()
        del self.drive.items["a"]
        os.replace(self.root / "Docs/a.txt", self.trash / "a.txt")  # deleted on Drive and here
        for key in ["photos", "p"]:
            del self.drive.items[key]
        import shutil
        shutil.rmtree(self.root / "Photos")
        self.run_sync()
        os.replace(self.trash / "a.txt", self.root / "Docs/a.txt")  # restored from the system trash
        (self.root / "Photos").mkdir()
        (self.root / "Photos/new.jpg").write_bytes(b"made here")
        statuses, results = self.run_sync()
        for path in ("Docs/a.txt", "Photos", "Photos/new.jpg"):
            self.assertEqual(statuses[path], "UPLOAD_NEW", path)
        self.assertEqual(results["FILE_ERROR"] + results["FOLDER_ERROR"], 0)
        names = {item["name"]: item for item in self.drive.items.values() if not item.get("trashed")}
        self.assertEqual(names["a.txt"]["content"], b"alpha")
        self.assertEqual(names["new.jpg"]["parent"], names["Photos"]["id"])

    def test_restored_from_a_trash_after_a_sync_stays(self):
        self.run_sync()
        (self.root / "Docs/a.txt").unlink()  # deleted here: goes to the Drive trash
        del self.drive.items["b"]  # deleted on Drive: goes to your system trash
        import shutil
        shutil.rmtree(self.root / "Photos")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "TRASH_REMOTE")
        self.assertEqual(statuses["Docs/b.txt"], "TRASH_LOCAL")
        self.assertEqual(statuses["Photos"], "TRASH_REMOTE")
        savegdrive.restore_items(self.drive, ["a"])  # r in the Trash view
        [kept] = list(self.trash.iterdir())
        os.replace(kept, self.root / "Docs/b.txt")  # put back from the system trash
        (self.root / "Photos").mkdir()
        (self.root / "Photos/new.jpg").write_bytes(b"made here")
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "NEW")
        self.assertEqual(statuses["Docs/b.txt"], "UPLOAD_NEW")
        self.assertEqual(statuses["Photos/new.jpg"], "UPLOAD_NEW")
        self.assertEqual(results["FILE_ERROR"] + results["FOLDER_ERROR"], 0)
        self.assertFalse(self.drive.items["a"].get("trashed"))
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha")
        self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"bravo")
        self.assertEqual(list(self.trash.iterdir()), [])
        names = {item["name"]: item for item in self.drive.items.values() if not item.get("trashed")}
        self.assertEqual(names["b.txt"]["parent"], "docs")
        self.assertEqual(names["new.jpg"]["parent"], names["Photos"]["id"])

    def test_picked_folder_renamed_on_drive_sends_the_changes_back(self):
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        by_path = {node["display_path"]: node for node in nodes}
        selections = []
        savegdrive.add_selected_item(selections, by_path["Docs"]["item"], by_path["Docs"]["relative_parent"])
        state = savegdrive.load_state(self.root)
        savegdrive.remember_selection(state, selections, savegdrive.build_local_scopes(selections, browsed))
        self.apply(savegdrive.build_plan(self.drive, selections, [Path("Docs")], self.root, state, cache), state)
        (self.root / "Docs/a.txt").write_bytes(b"edited here")
        self.drive.items["docs"]["name"] = "Docs 2026"

        def again():
            state = savegdrive.load_state(self.root)
            with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
                selections, scopes = savegdrive.last_selection(self.drive, state, {})
            plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, {})
            statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
            return statuses, self.apply(plan, state)

        statuses, _ = again()
        self.assertEqual(statuses["Docs"], "UPLOAD_NEW")
        self.assertEqual(statuses["Docs/a.txt"], "UPLOAD_NEW")
        statuses, _ = again()
        self.assertNotIn("CONFLICT", statuses.values())
        sent = [item for item in self.drive.items.values() if item.get("content") == b"edited here"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.drive.items[sent[0]["parent"]]["name"], "Docs")

    def sync_picked(self, *paths, again=False):
        """Sync the picked tree paths (or, with again, the last selection) like run_sync: plan, apply, remember."""
        state = savegdrive.load_state(self.root)
        if again:
            with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
                selections, scopes = savegdrive.last_selection(self.drive, state, {})
        else:
            nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", {})
            by_path = {node["display_path"]: node for node in nodes}
            selections = []
            for path in paths:
                savegdrive.add_selected_item(selections, by_path[path]["item"], by_path[path]["relative_parent"])
            scopes = savegdrive.build_local_scopes(selections, browsed)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, {})
        savegdrive.remember_selection(state, selections, scopes)
        self.apply(plan, state)
        return {entry["relative_path"].as_posix(): entry["status"] for entry in plan}

    def test_picked_items_renamed_on_drive_leave_no_old_copy(self):
        # A file picked alone, renamed on Drive, then --again: the old copy goes to your trash.
        self.sync_picked("notes.txt")
        self.drive.items["n"]["name"] = "notes-2026.txt"
        statuses = self.sync_picked(again=True)
        self.assertEqual(statuses["notes.txt"], "TRASH_LOCAL")
        self.assertFalse((self.root / "notes.txt").exists())
        self.assertEqual((self.root / "notes-2026.txt").read_bytes(), b"notes")
        # A folder picked in the tree, edited here, renamed on Drive, picked again under its new name.
        self.sync_picked("Photos")
        (self.root / "Photos/beach.jpg").write_bytes(b"retouched here")
        self.drive.items["photos"]["name"] = "Photos 2026"
        statuses = self.sync_picked("Photos 2026")
        self.assertEqual(statuses["Photos/beach.jpg"], "UPLOAD_NEW")  # the change is not lost
        self.assertIn(b"retouched here", [item.get("content") for item in self.drive.items.values()])
        self.assertEqual((self.root / "Photos 2026/beach.jpg").read_bytes(), b"jpeg")

    def test_picked_items_renamed_here_show_their_new_name_as_not_sent(self):
        # Photos and notes.txt picked alone, synced, then renamed here: the old names go to the Drive trash,
        # and the new ones, outside the selection, are listed rather than hidden.
        self.sync_picked("Photos", "notes.txt")
        (self.root / "Photos").rename(self.root / "Photos-old")
        (self.root / "notes.txt").rename(self.root / "notes-2026.txt")
        state = savegdrive.load_state(self.root)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            selections, scopes = savegdrive.last_selection(self.drive, state, {})
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, {})
        by_path = {entry["relative_path"].as_posix(): entry for entry in plan}
        self.assertEqual(by_path["Photos"]["status"], "TRASH_REMOTE")
        self.assertEqual(by_path["notes.txt"]["status"], "TRASH_REMOTE")
        for path in ("Photos-old", "notes-2026.txt"):
            self.assertEqual(by_path[path]["status"], "LOCAL_ONLY")
            self.assertTrue(by_path[path].get("outside_selection"))
        self.assertNotIn("Photos-old/beach.jpg", by_path)
        notes = savegdrive.plan_notes(plan)
        self.assertTrue(any(note.startswith("Not in your selection, so not sent") for note in notes))
        self.assertFalse(any(note.startswith("Local only:") for note in notes))
        self.assertEqual(savegdrive.plan_notes([e for e in plan if not e.get("outside_selection")]),
                         [note for note in notes if not note.startswith("Not in your selection")])
        # Nothing is sent for them.
        uploads_before = len(self.drive.uploads)
        self.apply(plan, state)
        self.assertEqual(len(self.drive.uploads), uploads_before)
        self.assertTrue((self.root / "Photos-old/beach.jpg").exists())

    def test_picked_items_moved_on_drive_into_another_pick_are_synced_once(self):
        # Docs, Photos and notes.txt picked; on Drive, Photos and notes.txt are moved into Docs, then --again.
        self.sync_picked("Docs", "Photos", "notes.txt")
        (self.root / "notes.txt").write_bytes(b"edited here")
        self.drive.items["photos"]["parent"] = "docs"
        self.drive.items["n"]["parent"] = "docs"
        statuses = self.sync_picked(again=True)
        self.assertEqual(statuses["Photos"], "TRASH_LOCAL")
        self.assertFalse((self.root / "Photos").exists())
        self.assertEqual((self.root / "Docs/Photos/beach.jpg").read_bytes(), b"jpeg")
        self.assertEqual(statuses["notes.txt"], "UPLOAD_NEW")  # the change made here is not lost
        self.assertEqual((self.root / "Docs/notes.txt").read_bytes(), b"notes")
        # The next run keeps one copy of each.
        statuses = self.sync_picked(again=True)
        self.assertNotIn("Photos/beach.jpg", statuses)
        self.assertEqual(statuses["Docs/Photos/beach.jpg"], "UNCHANGED")

    def test_file_picked_alone_deleted_on_drive_but_changed_here_goes_back(self):
        # notes.txt and Docs/a.txt picked one by one in the tree, synced, then deleted on Drive and edited here.
        cache = {}
        views = [savegdrive.DriveView("My Drive", "root")]
        savegdrive.load_view(self.drive, views[0], cache)
        app = savegdrive.DriveSelectorApp(views, self.root, savegdrive.load_state(self.root), self.drive, None, cache)
        by_path = {node["display_path"]: node for node in app.nodes}
        app.add_entry(by_path["notes.txt"])
        app.add_entry(by_path["Docs/a.txt"])
        state = savegdrive.load_state(self.root)
        scopes = savegdrive.build_local_scopes(app.selections, views[0].browsed)
        plan = savegdrive.build_plan(self.drive, app.selections, scopes, self.root, state, cache)
        savegdrive.remember_selection(state, app.selections, scopes)
        self.apply(plan, state)
        for item_id, path in (("n", "notes.txt"), ("a", "Docs/a.txt")):
            del self.drive.items[item_id]
            self.drive.change(item_id)
            (self.root / path).write_bytes(b"edited " + path.encode())
        self.run_main("--again", "--yes")
        sent = {item["content"]: item["parent"] for item in self.drive.items.values()
                if item.get("content", b"").startswith(b"edited")}
        self.assertEqual(sent, {b"edited notes.txt": "root", b"edited Docs/a.txt": "docs"})
        self.run_main("--again", "--yes")  # picked again under their new ids: synced, not deleted here
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"edited notes.txt")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"edited Docs/a.txt")
        self.assertEqual(len(self.drive.uploads), 2)
        picked = savegdrive.load_state(self.root)["last_selection"]["items"]
        self.assertEqual(sorted(saved["path"] for saved in picked), ["Docs/a.txt", "notes.txt"])

    def test_picked_items_trashed_then_restored_on_drive_come_back(self):
        # Photos and notes.txt picked, put in the Drive trash, --again, then restored on Drive.
        self.sync_picked("Docs", "Photos", "notes.txt")
        for item_id in ("photos", "n"):
            self.drive.items[item_id]["trashed"] = True
            self.drive.change(item_id)
        self.run_main("--again", "--yes")
        self.assertFalse((self.root / "Photos").exists())
        self.assertFalse((self.root / "notes.txt").exists())
        for item_id in ("photos", "n"):
            self.drive.items[item_id]["trashed"] = False
            self.drive.change(item_id)
        self.run_main("--again", "--yes")
        self.run_main("--again", "--yes")
        self.assertEqual((self.root / "Photos/beach.jpg").read_bytes(), b"jpeg")
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"notes")
        picked = savegdrive.load_state(self.root)["last_selection"]["items"]
        self.assertEqual(sorted(saved["path"] for saved in picked), ["Docs", "Photos", "notes.txt"])
        # Deleted for good: it leaves the selection.
        del self.drive.items["n"]
        self.drive.change("n")
        self.run_main("--again", "--yes")
        picked = savegdrive.load_state(self.root)["last_selection"]["items"]
        self.assertEqual(sorted(saved["path"] for saved in picked), ["Docs", "Photos"])

    def test_again_after_the_whole_selection_left_drive_says_so(self):
        # Only Photos picked, then deleted on Drive: the first --again trashes the copy, the next one has nothing.
        self.sync_picked("Photos")
        del self.drive.items["photos"]
        self.drive.change("photos")
        self.run_main("--again", "--yes")
        self.assertFalse((self.root / "Photos").exists())
        with self.assertRaises(SystemExit) as raised:  # "No previous selection", not a TypeError
            self.run_main("--again", "--yes")
        self.assertEqual(raised.exception.code, 1)
        # The tree does not offer to check it either.
        app = savegdrive.DriveSelectorApp([savegdrive.DriveView("My Drive", "root")], self.root,
                                          savegdrive.load_state(self.root), self.drive, None, {})
        with mock.patch.object(app, "run_worker") as worker, mock.patch.object(app, "update_notice"):
            app.check_last_sync()
        worker.assert_not_called()
        self.assertIsNone(app.last_sync_text)

    def test_folder_removed_here_by_the_sync_is_not_trashed_on_drive(self):
        self.drive.items["club"] = {"id": "club", "name": "Club", "mimeType": FOLDER, "parent": "root"}
        self.drive.items["club_photos"] = {"id": "club_photos", "name": "Photos 2026", "mimeType": FOLDER,
                                           "parent": "club"}
        self.run_sync()
        self.drive.items["club"]["trashed"] = True  # x in the tree, or a colleague
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Club"], "TRASH_LOCAL")
        self.assertFalse((self.root / "Club").exists())
        self.drive.items["club"]["trashed"] = False  # r in the Trash view
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Club"], "NEW")
        self.assertFalse(self.drive.items["club"].get("trashed"))
        self.assertTrue((self.root / "Club/Photos 2026").is_dir())

    def test_folder_made_again_on_drive_under_a_synced_name_comes_down(self):
        self.run_sync()
        import shutil
        for key in ["photos", "p"]:
            del self.drive.items[key]
        shutil.rmtree(self.root / "Photos")  # deleted on Drive and here
        self.drive.items["photos2"] = {"id": "photos2", "name": "Photos", "mimeType": FOLDER, "parent": "root"}
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Photos"], "NEW")
        self.assertFalse(self.drive.is_trashed(self.drive.items["photos2"]))
        self.assertEqual(results["FOLDER_TRASH_REMOTE"], 0)
        self.assertTrue((self.root / "Photos").is_dir())

    def test_read_only_items_are_never_sent_or_trashed(self):
        self.drive.items["docs"]["capabilities"] = {"canAddChildren": False, "canTrash": True, "canEdit": True}
        for key in ["a", "b"]:
            self.drive.items[key]["capabilities"] = {"canEdit": False, "canTrash": False}
        self.run_sync()
        (self.root / "Docs/a.txt").write_bytes(b"edited here")  # cannot edit on Drive
        (self.root / "Docs/b.txt").unlink()  # cannot trash on Drive
        (self.root / "Docs/new.txt").write_bytes(b"new")  # cannot add to Docs
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "CONFLICT")
        self.assertEqual(statuses["Docs/b.txt"], "NEW")  # comes back instead of an error
        self.assertEqual(statuses["Docs/new.txt"], "LOCAL_ONLY")
        self.assertEqual(results["FILE_ERROR"], 0)
        self.assertEqual(self.drive.uploads, [])

    def test_deleted_here_but_not_trashable_on_drive_says_it_comes_back(self):
        # A shared drive contributor: may edit, may not trash. Deleting here cannot delete on Drive.
        for key in ["photos", "a"]:
            self.drive.items[key]["capabilities"] = {"canEdit": True, "canAddChildren": True, "canTrash": False}
        self.run_sync()
        plan, _ = self.preview()
        self.assertFalse(any(entry.get("comes_back") for entry in plan))  # nothing deleted: no note
        (self.root / "Docs/a.txt").unlink()
        import shutil
        shutil.rmtree(self.root / "Photos")
        plan, _ = self.preview()
        entries = {entry["relative_path"].as_posix(): entry for entry in plan}
        self.assertEqual(entries["Docs/a.txt"]["status"], "NEW")
        self.assertTrue(entries["Docs/a.txt"]["comes_back"])
        self.assertEqual(entries["Photos"]["status"], "NEW")
        self.assertTrue(entries["Photos"]["comes_back"])
        self.assertFalse(entries["Docs/b.txt"].get("comes_back"))
        self.assertTrue(any("Drive does not let you delete them" in note for note in savegdrive.plan_notes(plan)))

    def test_edited_here_deleted_on_drive_in_read_only_folder_is_kept(self):
        # The only copy of the edit is here: it cannot go up, so it must not go to the system trash either.
        self.drive.items["docs"]["capabilities"] = {"canAddChildren": False, "canTrash": True, "canEdit": True}
        self.run_sync()
        (self.root / "Docs/a.txt").write_bytes(b"edited here")
        del self.drive.items["a"]
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "CONFLICT")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"edited here")
        self.assertEqual(list(self.trash.iterdir()), [])
        self.assertEqual(results["FILE_ERROR"], 0)
        self.assertEqual(self.drive.uploads, [])

    def test_local_symlinks_are_never_followed(self):
        # Links to places outside the synced folder (~/Documents, ~, a secret) never send anything to Drive.
        outside = Path(self.temp.name) / "outside"
        (outside / ".ssh").mkdir(parents=True)
        (outside / ".ssh/id_rsa").write_bytes(b"secret key")
        self.run_sync()
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        (self.root / "Docs/key").symlink_to(outside / ".ssh/id_rsa")
        names_before = {item["name"] for item in self.drive.items.values()}
        plan, _ = self.preview()
        # ux 19: the links are listed (local only) and a note says they are not followed, instead of nothing.
        self.assertTrue(any("Symbolic links are never followed" in note for note in savegdrive.plan_notes(plan)))
        statuses, results = self.run_sync()
        self.assertEqual([path for path in statuses if path.startswith("link/")], [])
        self.assertEqual((statuses["link"], statuses["Docs/key"]), ("LOCAL_ONLY", "LOCAL_ONLY"))
        self.assertEqual(self.drive.uploads, [])
        self.assertEqual({item["name"] for item in self.drive.items.values()}, names_before)
        self.assertEqual(results["FILE_ERROR"], 0)
        self.assertTrue((outside / ".ssh/id_rsa").exists())

    def test_local_name_not_in_utf8_is_listed_not_sent_and_the_preview_prints(self):
        # bugs 17: a name in Latin-1 (from an old zip) crashed the printed preview, and so the whole sync.
        self.run_sync()
        (self.root / os.fsdecode(b"Docs/caf\xe9.txt")).write_bytes(b"latin-1 name")
        (self.root / os.fsdecode(b"r\xe9sum\xe9")).mkdir()
        (self.root / os.fsdecode(b"r\xe9sum\xe9/notes.txt")).write_bytes(b"inside")
        plan, state = self.preview()
        statuses = {entry["relative_path"]: entry["status"] for entry in plan}
        self.assertEqual(statuses[Path(os.fsdecode(b"Docs/caf\xe9.txt"))], "LOCAL_ONLY")
        self.assertEqual(statuses[Path(os.fsdecode(b"r\xe9sum\xe9"))], "LOCAL_ONLY")
        self.assertNotIn(Path(os.fsdecode(b"r\xe9sum\xe9/notes.txt")), statuses)
        self.assertTrue(any("not UTF-8" in note for note in savegdrive.plan_notes(plan)))
        output = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")  # a real terminal encodes what it prints
        with mock.patch.object(savegdrive, "console", Console(file=output, width=200, highlight=False)):
            savegdrive.print_plan(plan, self.root)
        output.flush()
        self.assertIn("caf\\xe9.txt", output.buffer.getvalue().decode())
        names_before = {item["name"] for item in self.drive.items.values()}
        results = self.apply(plan, state)
        self.assertEqual((self.drive.uploads, results["FILE_ERROR"]), ([], 0))
        self.assertEqual({item["name"] for item in self.drive.items.values()}, names_before)
        self.assertEqual((self.root / os.fsdecode(b"Docs/caf\xe9.txt")).read_bytes(), b"latin-1 name")

    def test_preview_shows_the_size_of_what_is_sent(self):
        # bugs 47: a "↑ changed" row showed the size of the Drive version it replaces, a "↑ new" row none.
        self.run_sync()
        (self.root / "Photos/beach.jpg").write_bytes(b"x" * 3_000_000)  # retouched here: 4 B -> 3 MB
        (self.root / "Docs/a.txt").write_bytes(b"")  # emptied here
        (self.root / "draft.txt").write_bytes(b"d" * 1500)
        plan, _ = self.preview()
        output = io.StringIO()
        with mock.patch.object(savegdrive, "console", Console(file=output, width=200)):
            savegdrive.print_plan(plan, self.root)
        rows = {line.split()[2]: line.split()[3:] for line in output.getvalue().splitlines()
                if line.startswith("↑")}
        self.assertEqual(rows, {"Docs/a.txt": ["0", "B"], "Photos/beach.jpg": ["3.0", "MB"],
                                "draft.txt": ["1.5", "KB"]})

    def test_synced_folder_replaced_by_a_symlink_is_a_conflict_not_half_synced(self):
        # bugs 39: Photos moved to another disk, a link left in its place. Before, new files there were never
        # sent and deletions on Drive never came down, but downloads and deletions here went through the link.
        self.run_sync()
        elsewhere = Path(self.temp.name) / "other disk/Photos"
        elsewhere.parent.mkdir()
        os.replace(self.root / "Photos", elsewhere)
        (self.root / "Photos").symlink_to(elsewhere, target_is_directory=True)
        (elsewhere / "new.jpg").write_bytes(b"new here")
        self.drive.items["d"] = file_item("d", "drive.jpg", "photos", b"from drive")
        plan, _ = self.preview()
        self.assertTrue(any("Symbolic links are never followed" in note for note in savegdrive.plan_notes(plan)))
        self.assertFalse(any("Conflicts are left" in note for note in savegdrive.plan_notes(plan)))
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Photos"], "CONFLICT")
        self.assertEqual([path for path in statuses if path.startswith("Photos/")], [])
        self.assertFalse((elsewhere / "drive.jpg").exists())
        self.drive.items["p"]["trashed"] = True  # deleted on Drive: nothing goes through the link either
        (elsewhere / "beach.jpg").unlink()
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Photos"], "CONFLICT")
        self.assertFalse(self.drive.items["d"].get("trashed"))
        self.assertEqual(self.drive.uploads, [])
        self.assertEqual(list(self.trash.iterdir()), [])
        # A Drive file over a link, or picked below a linked folder: a conflict, never read nor replaced.
        (self.root / "notes.txt").unlink()
        (self.root / "notes.txt").symlink_to(elsewhere / "new.jpg")
        statuses, results = self.run_sync()
        self.assertEqual(statuses["notes.txt"], "CONFLICT")
        self.assertEqual((elsewhere / "new.jpg").read_bytes(), b"new here")
        plan = savegdrive.build_plan(
            self.drive, [{"item": self.drive.items["d"], "relative_parent": Path("Photos")}],
            [Path("Photos/drive.jpg")], self.root, savegdrive.load_state(self.root), {},
        )
        self.assertEqual([(entry["relative_path"].as_posix(), entry["status"]) for entry in plan],
                         [("Photos/drive.jpg", "CONFLICT")])
        self.assertFalse(savegdrive.can_keep_both(plan[0]))
        # The link replaced by the folder: syncs again.
        (self.root / "Photos").unlink()
        os.replace(elsewhere, self.root / "Photos")
        statuses, _ = self.run_sync()
        self.assertEqual((statuses["Photos"], statuses["Photos/new.jpg"]), ("UNCHANGED", "UPLOAD_NEW"))

    def test_file_shortcut_and_trash_view_only_come_down(self):
        # Photos holds a shortcut to Docs/a.txt: deleting or editing it here must not touch a.txt on Drive.
        self.drive.items["link"] = {"id": "link", "name": "Link.txt", "mimeType": savegdrive.SHORTCUT_MIME_TYPE,
                                    "parent": "photos",
                                    "shortcutDetails": {"targetId": "a", "targetMimeType": "text/plain"}}
        self.run_sync()
        self.assertEqual((self.root / "Photos/Link.txt").read_bytes(), b"alpha")

        (self.root / "Photos/Link.txt").unlink()
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos/Link.txt"], "NEW")  # comes back
        self.assertFalse(self.drive.is_trashed(self.drive.items["a"]))
        self.assertFalse(self.drive.items["link"].get("trashed"))
        self.assertEqual((self.root / "Photos/Link.txt").read_bytes(), b"alpha")

        (self.root / "Photos/Link.txt").write_bytes(b"edited through the shortcut")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos/Link.txt"], "CONFLICT")
        self.assertEqual(self.drive.items["a"]["content"], b"alpha")
        self.assertEqual(self.drive.uploads, [])

        self.assertTrue(savegdrive.is_readonly_path(Path(savegdrive.TRASH_DIR) / "x.txt"))
        self.assertTrue(savegdrive.is_readonly_path(Path(savegdrive.SHARED_WITH_ME_DIR) / "x.txt"))
        self.assertFalse(savegdrive.is_readonly_path(Path(savegdrive.SHARED_WITH_ME_DIR) / "Team/x.txt"))
        self.assertFalse(savegdrive.is_readonly_path(Path("Docs/x.txt")))

    def test_file_and_a_shortcut_to_it_both_come_down_when_it_changes_on_drive(self):
        # tests 79: Docs/a.txt and Photos/a-link.txt -> a share one Drive id; each copy keeps its own state.
        self.drive.items["lnk"] = {"id": "lnk", "name": "a-link.txt", "mimeType": savegdrive.SHORTCUT_MIME_TYPE,
                                   "parent": "photos",
                                   "shortcutDetails": {"targetId": "a", "targetMimeType": "text/plain"}}
        self.run_sync()
        self.run_sync()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))

        statuses, _ = self.run_sync()
        self.assertEqual((statuses["Docs/a.txt"], statuses["Photos/a-link.txt"]), ("UPDATE", "UPDATE"))
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha v2")
        self.assertEqual((self.root / "Photos/a-link.txt").read_bytes(), b"alpha v2")
        statuses, _ = self.run_sync()
        self.assertEqual((statuses["Docs/a.txt"], statuses["Photos/a-link.txt"]), ("UNCHANGED", "UNCHANGED"))

    def test_drive_folder_named_like_a_read_only_view_is_a_conflict_not_silently_read_only(self):
        # bugs 14: a My Drive folder "Trash" or "Shared with me" shares its local folder with that view:
        # edits there were conflicts, deletions came back, new files stayed local, and nothing said why.
        for folder_id, name in [("vt", savegdrive.TRASH_DIR), ("vs", savegdrive.SHARED_WITH_ME_DIR)]:
            self.drive.items[folder_id] = {"id": folder_id, "name": name, "mimeType": FOLDER, "parent": "root"}
            self.drive.items[f"{folder_id}x"] = file_item(f"{folder_id}x", "x.txt", folder_id, b"x")
            self.drive.items[f"{folder_id}y"] = file_item(f"{folder_id}y", "y.txt", folder_id, b"y")
        with mock.patch.object(savegdrive, "VIEW_NAME_PATHS", set(), create=True):
            self.run_sync()  # synced by an older version, as plain folders
        for name in (savegdrive.TRASH_DIR, savegdrive.SHARED_WITH_ME_DIR):
            (self.root / name / "x.txt").write_bytes(b"x edited here")
            (self.root / name / "y.txt").unlink()
            (self.root / name / "new.txt").write_bytes(b"new")

        plan, state = self.preview()
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        for name in (savegdrive.TRASH_DIR, savegdrive.SHARED_WITH_ME_DIR):
            self.assertEqual(statuses[name], "CONFLICT")
            self.assertFalse([path for path in statuses if path.startswith(f"{name}/")])
        self.assertTrue(any("named like a view" in note for note in savegdrive.plan_notes(plan)))
        self.apply(plan, state)
        self.assertEqual(self.drive.uploads, [])
        self.assertEqual(list(self.trash.iterdir()), [])
        for name in (savegdrive.TRASH_DIR, savegdrive.SHARED_WITH_ME_DIR):
            self.assertEqual((self.root / name / "x.txt").read_bytes(), b"x edited here")
            self.assertFalse((self.root / name / "y.txt").exists())
            self.assertTrue((self.root / name / "new.txt").exists())
        self.assertFalse(any(self.drive.is_trashed(item) for item in self.drive.items.values()))

    def test_unchanged_files_are_not_hashed_again(self):
        self.run_sync()
        (self.root / "Docs/b.txt").write_bytes(b"bravo, edited here")
        hashed = []
        real_hash = savegdrive.file_hash

        def counting_hash(path, algorithm="sha256"):
            hashed.append(path.relative_to(self.root).as_posix())
            return real_hash(path, algorithm)

        with mock.patch.object(savegdrive, "file_hash", counting_hash):
            statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/b.txt"], "UPLOAD")
        self.assertEqual(hashed, ["Docs/b.txt", "Docs/b.txt"])  # the plan, then the state after sending

    def edit_keeping_size(self):
        """Sync, then edit Docs/a.txt here without changing its size."""
        self.run_sync()
        path = self.root / "Docs/a.txt"
        before = path.stat().st_mtime_ns
        path.write_bytes(b"alphA")
        os.utime(path, ns=(before + 2_000_000_000, before + 2_000_000_000))  # a later save, same size

    def test_same_size_edit_is_sent(self):
        self.edit_keeping_size()
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "UPLOAD")
        self.assertEqual(self.drive.items["a"]["content"], b"alphA")

    def test_same_size_edit_changed_on_drive_too_is_a_conflict(self):
        self.edit_keeping_size()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha from Drive"))
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "CONFLICT")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alphA")

    def test_same_size_edit_deleted_on_drive_is_sent_back(self):
        self.edit_keeping_size()
        del self.drive.items["a"]
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "UPLOAD_NEW")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alphA")
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_edit_past_the_first_mib_is_seen(self):
        big = b"x" * (3 * 1024 * 1024)
        self.drive.items["v"] = file_item("v", "video.bin", "docs", big)
        self.run_sync()
        path = self.root / "Docs/video.bin"
        before = path.stat().st_mtime_ns
        path.write_bytes(big[:-1] + b"Y")  # same size, last byte changed
        os.utime(path, ns=(before + 2_000_000_000, before + 2_000_000_000))
        self.drive.items["v"].update(file_item("v", "video.bin", "docs", b"z" * len(big)))
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/video.bin"], "CONFLICT")
        self.assertEqual(path.read_bytes()[-1:], b"Y")

        self.drive.items["w"] = file_item("w", "log.bin", "docs", big)
        self.run_sync()
        path = self.root / "Docs/log.bin"
        before = path.stat().st_mtime_ns
        path.write_bytes(big[:-1] + b"Y")  # changed here only
        os.utime(path, ns=(before + 2_000_000_000, before + 2_000_000_000))
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/log.bin"], "UPLOAD")
        self.assertEqual(self.drive.items["w"]["content"][-1:], b"Y")

    def test_empty_folders_are_synced(self):
        self.run_sync()
        (self.root / "Empty here").mkdir()
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Empty here"], "UPLOAD_NEW")
        folder_id = next(key for key, item in self.drive.items.items() if item["name"] == "Empty here")
        statuses, _ = self.run_sync()
        self.assertNotIn("Empty here", [path for path, status in statuses.items() if status != "UNCHANGED"])

        del self.drive.items[folder_id]  # deleted on Drive: not sent back up
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Empty here"], "TRASH_LOCAL")
        self.assertFalse((self.root / "Empty here").exists())

        self.drive.items["empty"] = {"id": "empty", "name": "Empty there", "mimeType": FOLDER, "parent": "root"}
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Empty there"], "NEW")
        (self.root / "Empty there").rmdir()  # deleted here: to the Drive trash, not downloaded again
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Empty there"], "TRASH_REMOTE")
        self.assertTrue(self.drive.items["empty"]["trashed"])

    def test_empty_folder_is_tracked_right_after_upload_or_adoption(self):
        # tests 75: no sync in between, so each of UPLOAD_NEW and UNCHANGED must record the folder by itself
        def taxes_on_drive():
            return [key for key, item in self.drive.items.items() if item["name"] == "Taxes" and not item.get("trashed")]

        self.run_sync()
        (self.root / "Taxes").mkdir()
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Taxes"], "UPLOAD_NEW")
        del self.drive.items[taxes_on_drive()[0]]  # (A) sent, then deleted on Drive
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Taxes"], "TRASH_LOCAL")
        self.assertFalse((self.root / "Taxes").exists())
        self.assertEqual(taxes_on_drive(), [])

        (self.root / "Taxes").mkdir()
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Taxes"], "UPLOAD_NEW")
        (self.root / "Taxes").rmdir()  # (B) sent, then deleted here
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Taxes"], "TRASH_REMOTE")
        self.assertEqual(taxes_on_drive(), [])
        self.assertFalse((self.root / "Taxes").exists())

        (self.root / "Taxes").mkdir()  # (C) made on both sides, then deleted on Drive
        self.drive.items["taxes"] = {"id": "taxes", "name": "Taxes", "mimeType": FOLDER, "parent": "root"}
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Taxes"], "UNCHANGED")
        del self.drive.items["taxes"]
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Taxes"], "TRASH_LOCAL")
        self.assertFalse((self.root / "Taxes").exists())
        self.assertEqual(taxes_on_drive(), [])

    def test_empty_folder_deleted_on_drive_but_filled_here_is_kept(self):
        # tests 12: a synced empty folder gone from Drive follows the deletion only while it is still empty.
        # A new file here is sent back up; a link (never sent) keeps the folder here.
        self.drive.items["empty"] = {"id": "empty", "name": "Empty", "mimeType": FOLDER, "parent": "root"}
        self.drive.items["empty2"] = {"id": "empty2", "name": "Empty 2", "mimeType": FOLDER, "parent": "root"}
        self.run_sync()
        del self.drive.items["empty"], self.drive.items["empty2"]
        (self.root / "Empty/mine.txt").write_bytes(b"made here")
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_bytes(b"outside")
        (self.root / "Empty 2/link").symlink_to(outside)
        statuses, results = self.run_sync()
        self.assertEqual((statuses["Empty"], statuses["Empty/mine.txt"]), ("UPLOAD_NEW", "UPLOAD_NEW"))
        self.assertNotIn("Empty 2", statuses)
        self.assertEqual(statuses["Empty 2/link"], "LOCAL_ONLY")
        self.assertEqual(results["FILE_ERROR"] + results["FOLDER_ERROR"], 0)
        self.assertEqual((self.root / "Empty/mine.txt").read_bytes(), b"made here")
        self.assertTrue((self.root / "Empty 2/link").is_symlink())
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_folder_deleted_on_drive_with_something_new_here_keeps_the_folder(self):
        # tests 12: the folder emptied into your trash is removed only if nothing else is left in it.
        self.run_sync()
        for key in ["photos", "p"]:
            del self.drive.items[key]
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_bytes(b"outside")
        (self.root / "Photos/link").symlink_to(outside)  # never sent: Photos is not created again on Drive
        statuses, results = self.run_sync()
        self.assertEqual(statuses["Photos/beach.jpg"], "TRASH_LOCAL")
        self.assertEqual(statuses["Photos/link"], "LOCAL_ONLY")
        self.assertNotIn("Photos", statuses)
        self.assertEqual(results["FILE_ERROR"] + results["FOLDER_ERROR"], 0)
        self.assertFalse((self.root / "Photos/beach.jpg").exists())
        self.assertTrue((self.root / "Photos/link").is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_mass_deletion_needs_a_check(self):
        for i in range(30):
            self.drive.items[f"m{i}"] = file_item(f"m{i}", f"m{i}.txt", "photos", b"x")
        self.run_sync()
        import shutil
        for path in (self.root / "Photos").iterdir():
            path.unlink()
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = [{"item": node["item"], "relative_parent": node["relative_parent"]}
                      for node in nodes if node["parent"] is None]
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(
            self.drive, selections, savegdrive.build_local_scopes(selections, browsed), self.root, state, cache
        )
        self.assertEqual(savegdrive.deletion_count(plan, state)[0], 31)
        self.assertTrue(savegdrive.needs_deletion_check(plan, state))
        args = mock.Mock(yes=True, allow_deletions=False)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            with self.assertRaises(SystemExit):
                savegdrive.confirm_in_terminal(args, plan, state)
            args.allow_deletions = True
            self.assertTrue(savegdrive.confirm_in_terminal(args, plan, state))
        shutil.rmtree(self.root / "Photos")

    def deletion_plan(self):
        plan, state = self.preview()
        return plan, state, savegdrive.deletion_count(plan, state), savegdrive.needs_deletion_check(plan, state)

    def test_folder_deleted_here_counts_its_files(self):
        for i in range(30):
            self.drive.items[f"m{i}"] = file_item(f"m{i}", f"m{i}.txt", "photos", b"x")
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Photos")  # one folder in the plan, 31 synced files gone
        _, _, counts, check = self.deletion_plan()
        self.assertEqual(counts, (31, 35))
        self.assertTrue(check)

    def test_deleting_more_than_a_quarter_needs_a_check(self):
        for i in range(36):
            self.drive.items[f"m{i}"] = file_item(f"m{i}", f"m{i}.txt", "photos", b"x")
        self.run_sync()
        for i in range(11):  # 11 of 41: fewer than 20, more than a quarter
            (self.root / f"Photos/m{i}.txt").unlink()
        _, _, counts, check = self.deletion_plan()
        self.assertEqual(counts, (11, 41))
        self.assertTrue(check)

    def test_deleting_more_than_20_files_needs_a_check(self):
        for i in range(96):
            self.drive.items[f"m{i}"] = file_item(f"m{i}", f"m{i}.txt", "photos", b"x")
        self.run_sync()
        for i in range(20):  # 20 of 101: not more than 20, under a quarter
            (self.root / f"Photos/m{i}.txt").unlink()
        _, _, counts, check = self.deletion_plan()
        self.assertEqual(counts, (20, 101))
        self.assertFalse(check)
        for i in range(20, 25):  # 25 of 101: more than 20, still under a quarter
            (self.root / f"Photos/m{i}.txt").unlink()
        plan, state, counts, check = self.deletion_plan()
        self.assertEqual(counts, (25, 101))
        self.assertTrue(check)
        args = mock.Mock(yes=True, allow_deletions=False)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            with self.assertRaises(SystemExit):
                savegdrive.confirm_in_terminal(args, plan, state)

    def test_deletion_share_counts_only_the_files_in_play_not_older_selections(self):
        # bugs 44: Photos (100 files) synced before, then Docs alone; Docs emptied here is 100 % of what is in play.
        for i in range(99):
            self.drive.items[f"m{i}"] = file_item(f"m{i}", f"m{i}.txt", "photos", b"x")
        for i in range(12):
            self.drive.items[f"d{i}"] = file_item(f"d{i}", f"d{i}.txt", "docs", b"d")
        self.apply(*self.preview(only={"Photos"}))
        self.apply(*self.preview(only={"Docs"}))
        for path in (self.root / "Docs").iterdir():
            path.unlink()
        plan, state = self.preview(only={"Docs"})
        self.assertEqual(len(state["files"]), 115)
        self.assertEqual(savegdrive.deletion_count(plan, state), (14, 15))  # Report.docx, an export, stays on Drive
        self.assertTrue(savegdrive.needs_deletion_check(plan, state))
        args = mock.Mock(yes=True, allow_deletions=False)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            with self.assertRaises(SystemExit):
                savegdrive.confirm_in_terminal(args, plan, state)

    def test_one_or_two_deletions_pass_and_the_docs_say_so(self):
        self.run_sync()
        (self.root / "Docs/a.txt").unlink()
        (self.root / "Docs/b.txt").unlink()  # 2 of 5: more than a quarter, but only 2 files
        _, _, counts, check = self.deletion_plan()
        self.assertEqual(counts, (2, 5))
        self.assertFalse(check)
        (self.root / "Photos/beach.jpg").unlink()  # 3 of 5
        _, _, counts, check = self.deletion_plan()
        self.assertEqual(counts, (3, 5))
        self.assertTrue(check)
        output = io.StringIO()
        with mock.patch.object(sys, "argv", ["savegdrive", "--help"]), \
                mock.patch.dict(os.environ, {"COLUMNS": "1000"}), \
                mock.patch.object(sys, "stdout", output), self.assertRaises(SystemExit):
            savegdrive.main()
        self.assertIn("more than 2 files and a quarter of the synced ones", output.getvalue())
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        self.assertIn("more than 2 files and more than a quarter", " ".join(readme.split()))

    def test_mass_deletion_on_drive_needs_a_check(self):
        for i in range(30):
            self.drive.items[f"m{i}"] = file_item(f"m{i}", f"m{i}.txt", "photos", b"x")
        self.run_sync()
        for key in ["p"] + [f"m{i}" for i in range(30)]:
            del self.drive.items[key]
        plan, state, counts, check = self.deletion_plan()
        self.assertEqual(counts, (31, 35))
        self.assertTrue(check)
        args = mock.Mock(yes=True, allow_deletions=False)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            with self.assertRaises(SystemExit):
                savegdrive.confirm_in_terminal(args, plan, state)
        self.assertEqual(list(self.trash.iterdir()), [])

    def test_mass_deletion_in_the_terminal_needs_the_word_delete(self):
        self.run_sync()
        import shutil
        shutil.rmtree(self.root / "Docs")  # a.txt, b.txt: Report.docx is an export, it comes back
        shutil.rmtree(self.root / "Photos")  # and beach.jpg: 3 of 5 synced files
        plan, state, _, check = self.deletion_plan()
        self.assertTrue(check)
        args = mock.Mock(yes=False, allow_deletions=False)
        for answer, expected in [("y", False), ("yes", False), ("delete", True)]:
            quiet = Console(file=io.StringIO())
            with mock.patch.object(savegdrive, "console", quiet), \
                    mock.patch.object(quiet, "input", return_value=answer):
                self.assertEqual(savegdrive.confirm_in_terminal(args, plan, state), expected, answer)

    def test_no_answer_in_the_terminal_cancels(self):
        """--again without --yes and without a terminal: an empty stdin is a no, not an EOFError."""
        self.run_sync()
        (self.root / "Docs/a.txt").write_text("changed here")
        plan, state, _, check = self.deletion_plan()
        self.assertFalse(check)
        self.assertTrue(savegdrive.has_actions(plan))
        import shutil
        shutil.rmtree(self.root / "Docs")
        shutil.rmtree(self.root / "Photos")
        deletions, deletion_state, _, check = self.deletion_plan()
        self.assertTrue(check)
        args = mock.Mock(yes=False, allow_deletions=False)
        for plan, state in [(plan, state), (deletions, deletion_state)]:
            out = io.StringIO()
            quiet = Console(file=out)
            with mock.patch.object(savegdrive, "console", quiet), \
                    mock.patch.object(quiet, "input", side_effect=EOFError):
                self.assertFalse(savegdrive.confirm_in_terminal(args, plan, state))
            self.assertIn("Cancelled. Nothing was changed.", out.getvalue())

    def sync_everything(self):
        """A first sync like the tree's, which also saves the selection for --again."""
        plan, state = self.preview()
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            savegdrive.run_sync(mock.Mock(jobs=1), self.drive, lambda: self.drive, plan, selections, scopes,
                                [("root", Path())], self.root, state, cache, "0")

    def run_main(self, *options, isatty=False):
        self.addCleanup(savegdrive.TRASHED_FOLDERS.clear)
        argv = ["savegdrive", "--download-path", str(self.root), "--jobs", "1", *options]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(savegdrive, "authenticate", lambda: None), \
                mock.patch.object(savegdrive, "build", lambda *args, **kwargs: self.drive), \
                mock.patch.object(savegdrive, "signed_in_account", lambda service: None), \
                mock.patch.object(savegdrive, "console", Console(file=io.StringIO())) as output, \
                mock.patch.object(sys.stdin, "isatty", lambda: isatty), \
                mock.patch.object(sys.stdout, "isatty", lambda: isatty):
            savegdrive.main()
        return output.file.getvalue()

    def test_help_and_usage_errors_are_not_colored_for_a_dark_terminal(self):
        # Python 3.14 argparse colors options cyan and arguments yellow: invisible on a light terminal.
        for argv, stream in ((["savegdrive", "--help"], "stdout"), (["savegdrive", "--nope"], "stderr")):
            output = io.StringIO()
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, {"FORCE_COLOR": "1", "PYTHON_COLORS": "1"}), \
                    mock.patch.object(sys, stream, output), self.assertRaises(SystemExit):
                savegdrive.main()
            self.assertIn("--download-path", output.getvalue())
            self.assertNotIn("\x1b", output.getvalue())

    def test_items_kept_from_the_trash_view_stay_with_again(self):
        # Picked in the Trash view before Drive purges them, then a cron --again --yes: they stay here.
        self.addCleanup(savegdrive.TRASHED_FOLDERS.clear)
        self.drive.items.update({
            "old": {"id": "old", "name": "Old", "mimeType": FOLDER, "parent": "root", "trashed": True},
            "o": file_item("o", "old.txt", "old", b"old"),
            "sub": {"id": "sub", "name": "Sub", "mimeType": FOLDER, "parent": "old"},
            "deep": file_item("deep", "deep.txt", "sub", b"deep"),
            "gone": file_item("gone", "gone.txt", "root", b"gone", trashed=True),
        })
        cache = {}
        token = savegdrive.start_page_token(self.drive)
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, savegdrive.TRASH, cache,
                                                       base=Path(savegdrive.TRASH_DIR))
        selections = []
        for node in nodes:
            if node["parent"] is None:
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            savegdrive.run_sync(mock.Mock(jobs=1), self.drive, lambda: self.drive, plan, selections, scopes,
                                [], self.root, state, cache, token)
        trash = self.root / savegdrive.TRASH_DIR
        self.assertEqual((trash / "Old/Sub/deep.txt").read_bytes(), b"deep")  # the trashed folder's subfolders too
        self.assertEqual((trash / "gone.txt").read_bytes(), b"gone")

        savegdrive.TRASHED_FOLDERS.clear()  # a new run
        self.run_main("--again", "--yes")
        self.assertEqual(list(self.trash.iterdir()), [])
        for path in ("Old/old.txt", "Old/Sub/deep.txt", "gone.txt"):
            self.assertTrue((trash / path).is_file(), path)

    def test_item_restored_from_the_trash_view_leaves_trash_with_again(self):
        # The whole Trash view synced, then a.txt restored on Drive: --again moves the copy out of Trash/.
        self.drive.items.update({
            "a": file_item("a", "a.txt", "root", b"a", trashed=True),
            "old": file_item("old", "old.txt", "root", b"old", trashed=True),
        })
        cache = {}
        token = savegdrive.start_page_token(self.drive)
        trash_dir = Path(savegdrive.TRASH_DIR)
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, savegdrive.TRASH, cache, base=trash_dir)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            savegdrive.run_sync(mock.Mock(jobs=1), self.drive, lambda: self.drive, plan, selections, scopes,
                                [(savegdrive.TRASH, trash_dir)], self.root, state, cache, token)
        trash = self.root / trash_dir
        self.assertEqual(sorted(path.name for path in trash.iterdir()), ["a.txt", "old.txt"])

        self.drive.items["a"]["trashed"] = False
        self.drive.change("a")
        self.run_main("--again", "--yes")
        self.assertEqual(sorted(path.name for path in trash.iterdir()), ["old.txt"])
        self.assertEqual([path.name for path in self.trash.iterdir()], ["0-a.txt"])
        self.run_main("--again", "--yes")  # not brought back into Trash/
        self.assertEqual(sorted(path.name for path in trash.iterdir()), ["old.txt"])

    def test_again_after_an_upload_keeps_the_sent_file(self):
        self.sync_everything()
        (self.root / "Docs/new.txt").write_bytes(b"made here")
        self.run_main("--again", "--yes")  # sends it
        sent = [item_id for item_id, item in self.drive.items.items() if item["name"] == "new.txt"]
        self.assertEqual(len(sent), 1)
        self.run_main("--again", "--yes")  # its saved listing replays the sync's own changes
        self.assertEqual(list(self.trash.iterdir()), [])  # not taken for deleted on Drive
        self.assertEqual((self.root / "Docs/new.txt").read_bytes(), b"made here")
        self.assertFalse(self.drive.items[sent[0]].get("trashed"))

    def sync_round(self, cache):
        """One round of the tree's loop in main: same cache, listed again when the sync changed Drive."""
        self.addCleanup(savegdrive.TRASHED_FOLDERS.clear)
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache)
        self.apply(plan, state)
        if savegdrive.changes_drive(plan):
            cache.clear()
            savegdrive.TRASHED_FOLDERS.clear()
        return {entry["relative_path"].as_posix(): entry["status"] for entry in plan}

    def test_next_round_after_an_upload_keeps_the_sent_file(self):
        cache = {}
        self.sync_round(cache)
        (self.root / "Docs/new.txt").write_bytes(b"made here")
        self.assertEqual(self.sync_round(cache)["Docs/new.txt"], "UPLOAD_NEW")
        self.assertEqual(self.sync_round(cache)["Docs/new.txt"], "UNCHANGED")
        self.assertTrue((self.root / "Docs/new.txt").exists())

    def test_next_round_after_a_drive_trash_does_not_bring_the_file_back(self):
        cache = {}
        self.sync_round(cache)
        (self.root / "Docs/a.txt").unlink()
        self.assertEqual(self.sync_round(cache)["Docs/a.txt"], "TRASH_REMOTE")
        self.assertNotIn("Docs/a.txt", self.sync_round(cache))
        self.assertFalse((self.root / "Docs/a.txt").exists())

    def test_failed_drive_trash_is_an_error_and_is_tried_again(self):
        # tests 40: a trash Drive refuses is not reported done, nor forgotten, nor downloaded back next time.
        self.run_sync()
        (self.root / "Docs/a.txt").unlink()
        update = self.drive.update

        def failing_update(fileId, body, **kwargs):
            if fileId == "a":
                return FakeRequest(lambda: raise_(http_error(500)))
            return update(fileId, body, **kwargs)

        with mock.patch.object(self.drive, "update", failing_update):
            statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "TRASH_REMOTE")
        self.assertEqual((results["FILE_ERROR"], results["FILE_TRASH_REMOTE"]), (1, 0))
        self.assertNotIn("trashed", self.drive.items["a"])

        statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "TRASH_REMOTE")  # still known, not NEW
        self.assertEqual(results["FILE_TRASH_REMOTE"], 1)
        self.assertTrue(self.drive.items["a"]["trashed"])
        self.assertFalse((self.root / "Docs/a.txt").exists())

    def test_failed_download_is_counted_in_the_result(self):
        # tests 54: the result above the tree and in the terminal counts the failed transfer, not just "Done".
        self.run_sync()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))

        def failing_fetch(service, item, file_handle, stop=None):
            if item["id"] == "a":
                raise http_error(500)
            fake_fetch(service, item, file_handle, stop)

        plan, state = self.preview()
        with mock.patch.object(savegdrive, "fetch_media", failing_fetch):
            results = self.apply(plan, state)
        self.assertEqual(results["FILE_ERROR"], 1)
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha")
        self.assertIn("1 error", savegdrive.result_notice(plan, results).plain)
        console = Console(file=io.StringIO(), width=200)
        with mock.patch.object(savegdrive, "console", console):
            savegdrive.print_results(results)
        self.assertIn("1 error", console.file.getvalue())

    def test_again_with_yes_refuses_a_mass_deletion(self):
        self.sync_everything()
        import shutil
        shutil.rmtree(self.root / "Docs")  # a.txt, b.txt: Report.docx is an export, it comes back
        shutil.rmtree(self.root / "Photos")  # and beach.jpg: 3 of 5 synced files
        with self.assertRaises(SystemExit) as raised:
            self.run_main("--again", "--yes")
        self.assertEqual(raised.exception.code, 1)
        self.assertFalse(self.drive.items["docs"].get("trashed"))

    def test_network_loss_during_a_transfer_is_a_file_error(self):
        import httplib2
        from google.auth.exceptions import RefreshError

        for error in (httplib2.ServerNotFoundError("Unable to find the server at www.googleapis.com"),
                      RefreshError("invalid_grant")):
            with self.subTest(error=type(error).__name__):
                self.start_afresh()
                self.sync_everything()
                for item_id, name, parent in (("a", "a.txt", "docs"), ("b", "b.txt", "docs"),
                                              ("p", "beach.jpg", "photos")):
                    self.drive.items[item_id].update(file_item(item_id, name, parent, b"v2 " + name.encode(),
                                                               modifiedTime="2026-03-01T00:00:00.000Z"))
                    self.drive.change(item_id)
                (self.root / "notes.txt").unlink()

                def fetch_without_network(service, item, file_handle, stop=None):
                    if item["id"] == "b":
                        raise error
                    fake_fetch(service, item, file_handle, stop)

                with mock.patch.object(savegdrive, "fetch_media", fetch_without_network):
                    with self.assertRaises(SystemExit) as raised:  # a failed file: exit 1, not a traceback
                        self.run_main("--again", "--yes")
                self.assertEqual(raised.exception.code, 1)
                self.assertTrue(self.drive.items["n"].get("trashed"))  # the deletions still ran
                self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"v2 a.txt")
                self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"bravo")
                statuses, _ = self.run_sync()  # the transfers that finished were recorded
                self.assertEqual(statuses["Docs/a.txt"], "UNCHANGED")
                self.assertEqual(statuses["Photos/beach.jpg"], "UNCHANGED")
                self.assertEqual(statuses["Docs/b.txt"], "UPDATE")

    def start_afresh(self):
        """A fresh Drive and destination, for one more round in the same test."""
        shutil.rmtree(self.root)
        savegdrive.initialize_managed_destination(self.root)
        self.drive = demo_drive()

    def test_tree_after_a_sync_replays_drive_changes_instead_of_listing_it_again(self):
        self.sync_everything()
        import asyncio
        (self.root / "Docs/new.txt").write_bytes(b"made here")
        listings, after_sync, trees = [], [], []
        list_folder = self.drive.list
        self.drive.list = lambda q, **kwargs: listings.append(q) or list_folder(q, **kwargs)
        run_sync = savegdrive.run_sync

        def counted_sync(*args, **kwargs):
            results = run_sync(*args, **kwargs)
            after_sync.append(len(listings))
            return results

        def run(app):
            if after_sync:  # the tree back after the sync: look at it, then quit
                trees.append([node["display_path"] for node in app.views[0].nodes])
                return None

            async def drive_it():
                async with app.run_test() as pilot:
                    await pilot.press("down", "space", "d")  # Docs: sends new.txt
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                    await pilot.press("y")
            asyncio.run(drive_it())
            return app.return_value

        with mock.patch.object(savegdrive.DriveSelectorApp, "run", run), \
                mock.patch.object(savegdrive, "run_sync", counted_sync):
            self.run_main(isatty=True)
        self.assertTrue(any(item["name"] == "new.txt" for item in self.drive.items.values()))
        self.assertIn("Docs/new.txt", trees[0])  # the sync's own upload is in the tree
        self.assertEqual(listings[after_sync[0]:], [])  # from the Drive changes, no folder listed again

    def test_tree_with_yes_refuses_a_mass_deletion(self):
        self.sync_everything()
        import asyncio
        import shutil
        shutil.rmtree(self.root / "Docs")  # a.txt, b.txt: Report.docx is an export, it comes back
        shutil.rmtree(self.root / "Photos")  # and beach.jpg: 3 of 5 synced files

        def run(app):
            async def drive_it():
                async with app.run_test() as pilot:
                    await pilot.press("a", "d")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
            asyncio.run(drive_it())
            return app.return_value

        with mock.patch.object(savegdrive.DriveSelectorApp, "run", run):
            with self.assertRaises(SystemExit) as raised:
                self.run_main("--yes")
        self.assertEqual(raised.exception.code, 1)
        self.assertFalse(self.drive.items["docs"].get("trashed"))

    def test_preview_asks_to_type_delete_for_a_mass_deletion(self):
        self.sync_everything()
        import asyncio
        import shutil
        shutil.rmtree(self.root / "Docs")  # a.txt, b.txt: Report.docx is an export, it comes back
        shutil.rmtree(self.root / "Photos")  # and beach.jpg: 3 of 5 synced files
        self.addCleanup(savegdrive.TRASHED_FOLDERS.clear)
        cache = {}
        views = [savegdrive.DriveView("My Drive", "root")]
        savegdrive.load_view(self.drive, views[0], cache)
        app = savegdrive.DriveSelectorApp(views, self.root, savegdrive.load_state(self.root), self.drive, None, cache)
        screens = []

        async def drive_it():
            async with app.run_test() as pilot:
                await pilot.press("a", "d")
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("y")
                await pilot.pause()
                screens.append(app.screen)
        asyncio.run(drive_it())
        self.assertIsInstance(screens[0], savegdrive.ConfirmScreen)
        self.assertEqual(screens[0].word, "delete")
        self.assertIsNone(app.return_value)
        self.assertFalse(self.drive.items["docs"].get("trashed"))

    def tree_left_open_while_drive_changes(self, token):
        """Tree loaded after a sync; then a colleague edits a.txt, b.txt and notes.txt on Drive while you edit
        a.txt and notes.txt and delete b.txt here; then Docs and notes.txt are picked and compared (d)."""
        import asyncio
        import hashlib
        self.run_sync()
        cache = {}
        views = [savegdrive.DriveView("My Drive", "root")]
        savegdrive.load_view(self.drive, views[0], cache)
        for item_id in ("a", "b", "n"):
            content = b"colleague's edit on Drive"
            self.drive.items[item_id].update(content=content, size=str(len(content)),
                                             md5Checksum=hashlib.md5(content).hexdigest(),
                                             modifiedTime="2026-03-01T00:00:00.000Z")
            self.drive.change(item_id)
        (self.root / "Docs/a.txt").write_bytes(b"my local edit")
        (self.root / "notes.txt").write_bytes(b"my local notes")
        (self.root / "Docs/b.txt").unlink()
        app = savegdrive.DriveSelectorApp(views, self.root, savegdrive.load_state(self.root), self.drive, None,
                                          cache, token=token)

        async def drive_it():
            async with app.run_test() as pilot:
                await pilot.press("down", "space", "down", "down", "space", "d")  # Docs, notes.txt
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("y")
        asyncio.run(drive_it())
        plan = app.return_value["plan"]
        return {entry["relative_path"].as_posix(): entry["status"] for entry in plan}

    def test_tree_left_open_compares_with_drive_changes_made_since(self):
        token = savegdrive.start_page_token(self.drive)
        statuses = self.tree_left_open_while_drive_changes(token)
        self.assertEqual(statuses["Docs/a.txt"], "CONFLICT")  # not sent over the colleague's edit
        self.assertEqual(statuses["notes.txt"], "CONFLICT")  # a file picked on its own too
        self.assertEqual(statuses["Docs/b.txt"], "NEW")  # the change wins over your deletion: not trashed

    def test_tree_left_open_lists_drive_again_when_its_changes_token_expired(self):
        statuses = self.tree_left_open_while_drive_changes("expired")
        self.assertEqual(statuses["Docs/a.txt"], "CONFLICT")
        self.assertEqual(statuses["Docs/b.txt"], "NEW")

    def test_again_preview_asks_to_type_delete_for_a_mass_deletion(self):
        self.sync_everything()
        import asyncio
        import shutil
        shutil.rmtree(self.root / "Docs")  # a.txt, b.txt: Report.docx is an export, it comes back
        shutil.rmtree(self.root / "Photos")  # and beach.jpg: 3 of 5 synced files
        screens = []

        def run(app):
            async def drive_it():
                async with app.run_test() as pilot:
                    await pilot.pause()
                    await pilot.press("y")
                    await pilot.pause()
                    screens.append(app.screen)
            asyncio.run(drive_it())
            return app.return_value

        with mock.patch.object(savegdrive.PreviewApp, "run", run):
            self.run_main("--again", isatty=True)
        self.assertIsInstance(screens[0], savegdrive.ConfirmScreen)
        self.assertEqual(screens[0].word, "delete")
        self.assertFalse(self.drive.items["docs"].get("trashed"))

    def test_again_preview_cancelled_changes_nothing(self):
        # The --again preview in a terminal: esc, n and q cancel it, y applies it.
        self.sync_everything()
        import asyncio
        (self.root / "Docs" / "a.txt").unlink()

        def press(key):
            def run(app):
                async def drive_it():
                    async with app.run_test() as pilot:
                        await pilot.pause()
                        self.assertIsInstance(app.screen, savegdrive.StandalonePreviewScreen)
                        await pilot.press(key)
                        await pilot.pause()
                asyncio.run(drive_it())
                return app.return_value
            return run

        for key in ("q", "escape", "n"):
            with self.subTest(key=key), mock.patch.object(savegdrive.PreviewApp, "run", press(key)):
                output = self.run_main("--again", isatty=True)
                self.assertIn("Cancelled. Nothing was changed.", output)
                self.assertFalse(self.drive.items["a"].get("trashed"))
        with mock.patch.object(savegdrive.PreviewApp, "run", press("y")):
            output = self.run_main("--again", isatty=True)
        self.assertNotIn("Cancelled", output)
        self.assertTrue(self.drive.items["a"].get("trashed"))

    def test_a_pending_conflict_is_not_reported_in_sync(self):
        # bugs 21: a file changed on both sides and nothing else to do: the result still counts the conflict.
        self.sync_everything()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha from Drive"))
        self.drive.change("a")
        (self.root / "Docs/a.txt").write_bytes(b"alpha from here")
        plan, _ = self.preview()
        self.assertFalse(savegdrive.has_actions(plan))
        self.assertFalse(savegdrive.in_sync(plan))
        output = self.run_main("--again", "--yes")
        self.assertNotIn("Everything is in sync", output)
        self.assertIn("1 conflict kept", output)
        results = Counter(FILE_CONFLICT=1)
        notice = savegdrive.result_notice(plan, results).plain
        self.assertNotIn("Everything was in sync", notice)
        self.assertIn("1 conflict kept", notice)

    def test_last_sync_summary_line(self):
        # tests 43: the start line counts conflicts and Drive deletions, and says in sync only when it is.
        def summary(*entries):
            return savegdrive.last_sync_summary([{"kind": kind, "status": status} for kind, status in entries]).plain

        self.assertEqual(summary(("FILE", "UNCHANGED"), ("FOLDER", "UNCHANGED")), "✓ Your last selection is in sync.")
        conflict = summary(("FILE", "UNCHANGED"), ("FILE", "CONFLICT"))
        self.assertNotIn("in sync", conflict)
        self.assertIn("Since the last sync: 1 conflict", conflict)
        deleted = summary(("FILE", "UNCHANGED"), ("FILE", "TRASH_LOCAL"))
        self.assertIn("Since the last sync: 1 deleted on Drive", deleted)
        mixed = summary(("FOLDER", "NEW"), ("FILE", "NEW"), ("FILE", "UPLOAD"), ("FOLDER", "TRASH_REMOTE"),
                        ("FILE", "CONFLICT"), ("FILE", "CONFLICT"))
        self.assertIn("1 ↓ to download · 1 ↑ to send · 1 deleted here · 2 conflicts", mixed)

    def test_parallel_downloads(self):
        for i in range(20):
            self.drive.items[f"m{i}"] = file_item(f"m{i}", f"m{i}.txt", "photos", f"file {i}".encode())
        statuses, results = self.run_sync(jobs=4)
        self.assertEqual(results["FILE_NEW"], 25)
        self.assertEqual((self.root / "Photos/m7.txt").read_bytes(), b"file 7")
        state = savegdrive.load_state(self.root)
        self.assertEqual(len(state["files"]), 25)

    def test_marks(self):
        self.run_sync()
        nodes, _ = savegdrive.collect_drive_tree(self.drive, "root", {})
        by_path = {node["display_path"]: node["index"] for node in nodes}

        def marks():
            state = savegdrive.load_state(self.root)
            found = savegdrive.local_marks(nodes, self.root, state)
            return {path: found.get(index) for path, index in by_path.items()}

        self.assertEqual(marks()["Docs/a.txt"], "synced")
        self.assertEqual(marks()["Docs"], "synced")
        self.assertIsNone(marks()["Docs/Survey"])
        os.utime(self.root / "Docs/b.txt", (0, 0))  # edited locally
        self.assertEqual(marks()["Docs/b.txt"], "edited")
        self.assertEqual(marks()["Docs"], "edited")
        nodes[by_path["Photos/beach.jpg"]]["item"]["md5Checksum"] = "new"  # changed on Drive
        self.assertEqual(marks()["Photos/beach.jpg"], "changed")
        self.assertEqual(marks()["Photos"], "changed")
        (self.root / "notes.txt").unlink()  # deleted here: goes to the Drive trash
        self.assertEqual(marks()["notes.txt"], "deleted")

    def test_marks_folder_with_a_new_file_on_drive_as_changed(self):
        # README: ✓ only when every file is the same on both sides; ↻ for a folder with new files.
        self.run_sync()
        self.drive.items["c"] = file_item("c", "c.txt", "docs", b"gamma")  # added on Drive after the sync
        nodes, _ = savegdrive.collect_drive_tree(self.drive, "root", {})
        by_path = {node["display_path"]: node["index"] for node in nodes}
        found = savegdrive.local_marks(nodes, self.root, savegdrive.load_state(self.root))
        self.assertIsNone(found.get(by_path["Docs/c.txt"]))  # not downloaded yet
        self.assertEqual(found.get(by_path["Docs"]), "changed")

    def test_marks_for_changes_made_here(self):
        # Each mark tells what the next sync does: up, to the Drive trash, or a conflict that never goes up.
        self.run_sync()
        nodes, _ = savegdrive.collect_drive_tree(self.drive, "root", {})
        by_path = {node["display_path"]: node["index"] for node in nodes}

        def marks():
            found = savegdrive.local_marks(nodes, self.root, savegdrive.load_state(self.root))
            return {path: found.get(index) for path, index in by_path.items()}

        def statuses():
            plan, _ = self.preview()
            return {entry["relative_path"].as_posix(): entry["status"] for entry in plan}

        # A new file here: the folder is no longer the same on both sides, and the file goes up.
        (self.root / "Docs/new.txt").write_text("new")
        self.assertEqual(marks()["Docs"], "edited")
        self.assertEqual(statuses()["Docs/new.txt"], "UPLOAD_NEW")
        (self.root / "Docs/new.txt").unlink()
        self.assertEqual(marks()["Docs"], "synced")
        # Only local files the sync would send count: not a part file, not a link.
        (self.root / "Docs/x.txt.savegdrive.part").write_text("partial")
        (self.root / "Docs/link").symlink_to(self.root / "notes.txt")
        self.assertEqual(marks()["Docs"], "synced")
        (self.root / "Docs/x.txt.savegdrive.part").unlink()
        (self.root / "Docs/link").unlink()

        # Deleted here: goes to the Drive trash, not a change on Drive.
        (self.root / "Docs/b.txt").unlink()
        self.assertEqual(marks()["Docs/b.txt"], "deleted")
        self.assertEqual(marks()["Docs"], "edited")
        self.assertEqual(statuses()["Docs/b.txt"], "TRASH_REMOTE")
        # An export deleted here comes back down: no mark, like a file not downloaded yet.
        (self.root / "Docs/Report.docx").unlink()
        self.assertIsNone(marks()["Docs/Report"])
        self.assertEqual(statuses()["Docs/Report.docx"], "NEW")

        # An export edited here never goes back to Drive: a conflict, for the file and its folder.
        (self.root / "Docs/Report.docx").write_bytes(b"edited docx")
        self.run_sync()  # b.txt to the Drive trash, the export down again
        nodes[:] = savegdrive.collect_drive_tree(self.drive, "root", {})[0]
        by_path = {node["display_path"]: node["index"] for node in nodes}
        os.utime(self.root / "Docs/Report.docx", (0, 0))
        self.assertEqual(marks()["Docs/Report"], "conflict")
        self.assertEqual(marks()["Docs"], "conflict")
        self.assertEqual(statuses()["Docs/Report.docx"], "CONFLICT")

    def test_marks_for_a_read_only_file_edited_here(self):
        self.run_sync()
        nodes, _ = savegdrive.collect_drive_tree(self.drive, "root", {})
        a_node = next(node for node in nodes if node["display_path"] == "Docs/a.txt")
        a_node["item"]["capabilities"] = {"canEdit": False}  # shared with you read only
        os.utime(self.root / "Docs/a.txt", (0, 0))
        marks = savegdrive.local_marks(nodes, self.root, savegdrive.load_state(self.root))
        self.assertEqual(marks.get(a_node["index"]), "conflict")
        # Deleted here, but you may not trash it: it comes down again.
        a_node["item"]["capabilities"] = {"canTrash": False}
        (self.root / "Docs/a.txt").unlink()
        marks = savegdrive.local_marks(nodes, self.root, savegdrive.load_state(self.root))
        self.assertIsNone(marks.get(a_node["index"]))

    def test_google_doc_edited_on_drive_comes_down(self):
        # A Google Doc has no md5: only its modifiedTime says it changed on Drive.
        self.run_sync()
        self.drive.items["report"].update(content=b"docx bytes v2", modifiedTime="2025-02-03T04:05:06.000Z")
        nodes, _ = savegdrive.collect_drive_tree(self.drive, "root", {})
        report = next(node for node in nodes if node["display_path"] == "Docs/Report")
        marks = savegdrive.local_marks(nodes, self.root, savegdrive.load_state(self.root))
        self.assertEqual(marks.get(report["index"]), "changed")

        statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs/Report.docx"], "UPDATE")
        self.assertEqual((self.root / "Docs/Report.docx").read_bytes(), b"docx bytes v2")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/Report.docx"], "UNCHANGED")

    def test_again(self):
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        by_path = {node["display_path"]: node for node in nodes}
        selections = []
        for path in ("Photos", "Docs/a.txt", "notes.txt"):
            savegdrive.add_selected_item(selections, by_path[path]["item"], by_path[path]["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        savegdrive.remember_selection(state, selections, scopes)
        savegdrive.save_state(self.root, state)

        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        del self.drive.items["photos"]
        self.drive.items["n"]["trashed"] = True
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            again, again_scopes = savegdrive.last_selection(self.drive, savegdrive.load_state(self.root), {})
        # Picked inside Docs: --again must sync it at Docs/a.txt, not at the root.
        self.assertEqual([savegdrive.selected_path(entry).as_posix() for entry in again], ["Docs/a.txt"])
        self.assertEqual(again[0]["item"]["md5Checksum"], self.drive.items["a"]["md5Checksum"])
        # Gone from Drive or in its trash: still scanned locally.
        self.assertEqual(again_scopes, [Path("Photos"), Path("notes.txt")])
        self.assertIsNone(savegdrive.last_selection(self.drive, {"files": {}}, {}))

    def test_again_scans_the_picked_folder_here(self):
        # Only Docs synced: --again must still look at the local Docs folder, not just at Drive.
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        by_path = {node["display_path"]: node for node in nodes}
        selections = []
        savegdrive.add_selected_item(selections, by_path["Docs"]["item"], by_path["Docs"]["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            savegdrive.run_sync(mock.Mock(jobs=1), self.drive, lambda: self.drive, plan, selections, scopes,
                                [], self.root, state, cache, "0")
        self.assertTrue((self.root / "Docs/b.txt").exists())

        (self.root / "Docs/new.txt").write_bytes(b"made here")
        del self.drive.items["b"]  # deleted on Drive: its copy here must go too
        state = savegdrive.load_state(self.root)
        cache = {}
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            again, again_scopes = savegdrive.last_selection(self.drive, state, cache)
            plan = savegdrive.build_plan(self.drive, again, again_scopes, self.root, state, cache)
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertEqual(statuses.get("Docs/new.txt"), "UPLOAD_NEW")
        self.assertEqual(statuses.get("Docs/b.txt"), "TRASH_LOCAL")

    def test_again_stops_on_a_drive_error(self):
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        by_path = {node["display_path"]: node for node in nodes}
        selections = []
        savegdrive.add_selected_item(selections, by_path["Docs"]["item"], by_path["Docs"]["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        savegdrive.remember_selection(state, selections, scopes)
        savegdrive.save_state(self.root, state)

        real_get = self.drive.get
        for status in (401, 403, 500, 503):
            with self.subTest(status=status):
                def failing_get(fileId, **kwargs):
                    if fileId != "docs":
                        return real_get(fileId, **kwargs)

                    def run():
                        raise http_error(status)
                    return FakeRequest(run)

                self.drive.get = failing_get
                with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())), \
                        mock.patch.object(savegdrive.time, "sleep"):  # 500 and 503 are retried first
                    with self.assertRaises(HttpError):  # not "no longer on Drive": its copies would be trashed
                        savegdrive.last_selection(self.drive, savegdrive.load_state(self.root), {})
        self.drive.get = real_get
        del self.drive.items["docs"]  # a real 404: Docs becomes a local scope
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            again, again_scopes = savegdrive.last_selection(self.drive, savegdrive.load_state(self.root), {})
        self.assertEqual(again, [])
        self.assertEqual(again_scopes, [Path("Docs")])

    def test_again_with_whole_view_takes_new_items(self):
        state = {"last_selection": {"roots": [{"id": "root", "base": "."}], "items": [], "scopes": ["."]}}
        self.drive.items["new"] = file_item("new", "new.txt", "root", b"new")
        selections, scopes = savegdrive.last_selection(self.drive, state, {})
        self.assertEqual(
            sorted(savegdrive.selected_path(entry).as_posix() for entry in selections),
            ["Docs", "Photos", "new.txt", "notes.txt"],
        )
        self.assertEqual(scopes, [Path()])

    def test_sync_of_whole_view_remembers_its_root_for_again(self):
        # A cron --again --yes on all of My Drive: run_sync must save the view's root, or new items are skipped.
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            savegdrive.run_sync(mock.Mock(jobs=1), self.drive, lambda: self.drive, plan, selections, scopes,
                                [("root", Path())], self.root, state, cache, "0")
        state = savegdrive.load_state(self.root)
        self.assertEqual(state["last_selection"]["roots"], [{"id": "root", "base": "."}])

        self.drive.items["new"] = file_item("new", "new.txt", "root", b"new")
        cache = {}
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            again, again_scopes = savegdrive.last_selection(self.drive, state, cache)
            plan = savegdrive.build_plan(self.drive, again, again_scopes, self.root, state, cache)
        self.assertIn("new.txt", [savegdrive.selected_path(entry).as_posix() for entry in again])
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertEqual(statuses.get("new.txt"), "NEW")

    def test_again_with_whole_shared_with_me_takes_new_shares(self):
        # A cron --again on all of Shared with me: a new share lives in its owner's folder, never in the snapshot.
        shared = savegdrive.SHARED_WITH_ME
        self.drive.items["s"] = file_item("s", "from Ann.pdf", "ann", b"pdf", shared=True)
        cache = {}
        token = savegdrive.start_page_token(self.drive)
        savegdrive.prefetch_folders(self.drive, [shared], cache)
        savegdrive.save_snapshot(self.root, token, cache, [shared])
        self.assertIn(shared, savegdrive.load_snapshot(self.root)["folders"])

        self.drive.items["s2"] = file_item("s2", "from Bob.pdf", "bob", b"pdf", shared=True)
        self.drive.change("s2")
        replayed, _ = savegdrive.apply_changes(self.drive, savegdrive.load_snapshot(self.root))
        state = {"last_selection": {"roots": [{"id": shared, "base": savegdrive.SHARED_WITH_ME_DIR}],
                                    "items": [], "scopes": [savegdrive.SHARED_WITH_ME_DIR]}}
        selections, _ = savegdrive.last_selection(self.drive, state, replayed)
        self.assertEqual(
            sorted(savegdrive.selected_path(entry).name for entry in selections),
            ["from Ann.pdf", "from Bob.pdf"],
        )

    def test_changes_replay_matches_a_fresh_listing(self):
        cache = {}
        token = savegdrive.start_page_token(self.drive)
        savegdrive.prefetch_folders(self.drive, ["root"], cache)
        savegdrive.save_snapshot(self.root, token, cache, ["root"])

        self.drive.items["a"].update(file_item("a", "a renamed.txt", "docs", b"alpha v2"))
        self.drive.change("a")
        del self.drive.items["b"]
        self.drive.change("b")
        self.drive.items["c"] = file_item("c", "c.txt", "photos", b"charlie")
        self.drive.change("c")
        self.drive.items["p"]["parent"] = "docs"  # moved
        self.drive.change("p")
        self.drive.items["x"] = file_item("x", "elsewhere.txt", "unknown-folder", b"x")
        self.drive.change("x")
        self.drive.items["n"]["trashed"] = True  # put in the Drive trash
        self.drive.change("n")
        self.drive.items["form"]["parent"] = "unknown-folder"  # moved out of the listed folders
        self.drive.change("form")

        self.assertGreater(len(self.drive.change_log), self.drive.changes_page_size)  # several pages
        replayed, new_token = savegdrive.apply_changes(self.drive, savegdrive.load_snapshot(self.root))
        fresh = {}
        savegdrive.prefetch_folders(self.drive, ["root"], fresh)
        self.assertNotIn("n", [item["id"] for item in fresh["root"]])
        self.assertNotIn("form", [item["id"] for item in replayed["docs"]])
        self.assertEqual(replayed, fresh)
        self.assertEqual(new_token, "7")
        self.assertIsNone(savegdrive.apply_changes(self.drive, {"token": "expired", "folders": {}}))

    def test_again_reads_drive_changes_past_the_first_page(self):
        token = savegdrive.start_page_token(self.drive)
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            savegdrive.run_sync(mock.Mock(jobs=1), self.drive, lambda: self.drive, plan, selections, scopes,
                                [("root", Path())], self.root, state, cache, token)

        for index in range(5):  # changes elsewhere on Drive fill the first pages
            self.drive.items[f"c{index}"] = file_item(f"c{index}", f"c{index}.txt", "photos", b"new")
            self.drive.change(f"c{index}")
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"bravo from a colleague"))
        self.drive.change("b")  # last: on the last page
        (self.root / "Docs" / "b.txt").write_bytes(b"bravo edited here")
        self.assertGreater(len(self.drive.change_log), self.drive.changes_page_size * 2)

        state = savegdrive.load_state(self.root)
        cache, new_token = savegdrive.apply_changes(self.drive, savegdrive.load_snapshot(self.root))
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            selections, scopes = savegdrive.last_selection(self.drive, state, cache, [])
        roots = [(root["id"], Path(root["base"])) for root in state["last_selection"]["roots"]]
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache,
                                     roots=savegdrive.upload_roots(roots))
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertEqual(statuses["Docs/b.txt"], "CONFLICT")  # not UPLOAD over the colleague's version
        self.assertEqual(statuses["Photos/c0.txt"], "NEW")
        self.assertEqual(new_token, str(len(self.drive.change_log)))

    def test_changes_replay_skips_shared_drive_changes(self):
        cache = {}
        token = savegdrive.start_page_token(self.drive)
        savegdrive.prefetch_folders(self.drive, ["root"], cache)
        savegdrive.save_snapshot(self.root, token, cache, ["root"])
        self.drive.change_log.append({"removed": False})  # a shared drive renamed: changeType "drive", no fileId
        self.drive.items["c"] = file_item("c", "c.txt", "photos", b"charlie")
        self.drive.change("c")

        replayed, new_token = savegdrive.apply_changes(self.drive, savegdrive.load_snapshot(self.root))
        fresh = {}
        savegdrive.prefetch_folders(self.drive, ["root"], fresh)
        self.assertEqual(replayed, fresh)
        self.assertEqual(new_token, "2")

    def test_again_on_a_shared_drive_sees_a_colleague_change(self):
        # A cron --again --yes on a Shared drive folder: Ann's edit must come back as a conflict, not be overwritten.
        self.drive.shared_drives = [{"id": "team", "name": "Team"}]
        self.drive.items.update({
            "proj": {"id": "proj", "name": "Projects", "mimeType": FOLDER, "parent": "team"},
            "t": file_item("t", "plan.txt", "proj", b"plan"),
            "u": file_item("u", "budget.txt", "proj", b"budget"),
        })
        views = savegdrive.drive_views(self.drive, "root")
        team = next(view for view in views if view.root_id == "team")
        cache = {}
        savegdrive.load_view(self.drive, team, cache)
        selections = []
        for entry in team.children_of.get(None, []):
            savegdrive.add_selected_item(selections, entry["item"], entry["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, team.browsed, [team.base])
        state = savegdrive.load_state(self.root)
        token = savegdrive.start_page_token(self.drive)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            savegdrive.run_sync(mock.Mock(jobs=1), self.drive, lambda: self.drive, plan, selections, scopes,
                                [], self.root, state, cache, token)
        local = self.root / "Shared drives/Team/Projects/plan.txt"
        self.assertEqual(local.read_bytes(), b"plan")

        self.drive.items["t"].update(file_item("t", "plan.txt", "proj", b"plan by Ann",
                                               modifiedTime="2025-02-03T04:05:06.000Z"))
        self.drive.change("t")
        local.write_bytes(b"plan by me")

        state = savegdrive.load_state(self.root)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            cache, token = savegdrive.apply_changes(self.drive, savegdrive.load_snapshot(self.root))
            again, again_scopes = savegdrive.last_selection(self.drive, state, cache, [])
            plan = savegdrive.build_plan(self.drive, again, again_scopes, self.root, state, cache,
                                         roots=savegdrive.upload_roots([]))
            savegdrive.apply_plan(self.drive, plan, self.root, state, 1, lambda: self.drive)
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertEqual(statuses["Shared drives/Team/Projects/plan.txt"], "CONFLICT")
        self.assertEqual(self.drive.items["t"]["content"], b"plan by Ann")
        self.assertEqual(list(self.trash.iterdir()), [])
        self.assertEqual((self.root / "Shared drives/Team/Projects/budget.txt").read_bytes(), b"budget")

    def test_new_item_at_the_top_of_a_read_only_view_stays_local(self):
        # A shared drive or a --folder-id folder you only read: what you add at its top cannot go up.
        readonly = {"capabilities": {"canEdit": False, "canAddChildren": False, "canTrash": False}}
        self.drive.shared_drives = [{"id": "team", "name": "Team", **readonly}]
        self.drive.items.update({
            "sub": {"id": "sub", "name": "Sub", "mimeType": FOLDER, "parent": "team", **readonly},
            "r": file_item("r", "rules.pdf", "team", b"rules", **readonly),
            "shared": {"id": "shared", "name": "Club", "mimeType": FOLDER, "parent": "nobody", **readonly},
            "n": file_item("n", "notes.txt", "shared", b"notes", **readonly),
        })
        views = savegdrive.drive_views(self.drive, "root")
        team = next(view for view in views if view.root_id == "team")
        cache = {}
        savegdrive.load_view(self.drive, team, cache)
        selections = []
        for entry in team.children_of.get(None, []):
            savegdrive.add_selected_item(selections, entry["item"], entry["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, team.browsed, [team.base])
        state = savegdrive.load_state(self.root)
        token = savegdrive.start_page_token(self.drive)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache,
                                     roots=savegdrive.view_roots(views))
        roots = [(team.root_id, team.base, team.can_add)]  # a whole view, as the selector saves it
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            savegdrive.run_sync(mock.Mock(jobs=1), self.drive, lambda: self.drive, plan, selections, scopes,
                                roots, self.root, state, cache, token)
        top = self.root / "Shared drives/Team"
        (top / "mine.txt").write_bytes(b"mine")
        (top / "Sub/mine2.txt").write_bytes(b"mine too")
        (top / "NewDir").mkdir()
        (top / "NewDir/x.txt").write_bytes(b"x")
        new = ["Shared drives/Team/mine.txt", "Shared drives/Team/NewDir",  # NewDir/x.txt comes with it
               "Shared drives/Team/Sub/mine2.txt"]

        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, {},
                                     roots=savegdrive.view_roots(views))
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertEqual({path: statuses[path] for path in new}, dict.fromkeys(new, "LOCAL_ONLY"))
        # --again and s: the saved root remembers it is read only.
        self.assertIs(state["last_selection"]["roots"][0]["can_add"], False)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            again, again_scopes = savegdrive.last_selection(self.drive, state, {}, [])
        plan = savegdrive.build_plan(self.drive, again, again_scopes, self.root, state, {},
                                     roots=savegdrive.upload_roots(savegdrive.saved_roots(state)))
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertEqual({path: statuses[path] for path in new}, dict.fromkeys(new, "LOCAL_ONLY"))
        # An older saved root, without can_add, keeps sending there.
        self.assertEqual(savegdrive.upload_roots([("team", team.base)]), {team.base: "team"})

        [club] = savegdrive.drive_views(self.drive, "shared")  # --folder-id on a folder shared read only
        self.assertEqual((club.name, club.can_add), ("Club", False))
        self.assertEqual(savegdrive.view_roots([club]), {Path(): None})

    def test_shared_views_get_their_own_folders(self):
        self.drive.items["s"] = file_item("s", "from Ann.pdf", "nobody", b"pdf", shared=True)
        self.drive.items["t"] = file_item("t", "plan.txt", "team", b"plan")
        self.drive.shared_drives = [{"id": "team", "name": "Team"}]
        views = savegdrive.drive_views(self.drive, "root")
        self.assertEqual([view.name for view in views], ["My Drive", "Shared with me", "Team", "Trash"])
        cache = {}
        selections = []
        for view in views:
            savegdrive.load_view(self.drive, view, cache)
            for entry in view.children_of.get(None, []):
                savegdrive.add_selected_item(selections, entry["item"], entry["relative_parent"])
        browsed = {}
        for view in views:
            browsed.update(view.browsed)
        scopes = savegdrive.build_local_scopes(selections, browsed, [view.base for view in views])
        self.assertEqual(scopes, [Path(), Path("Shared with me"), Path("Shared drives/Team")])
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache)
        paths = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertEqual(paths["Shared with me/from Ann.pdf"], "NEW")
        self.assertEqual(paths["Shared drives/Team/plan.txt"], "NEW")
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            savegdrive.apply_plan(self.drive, plan, self.root, state)
        # A later My Drive-only run does not report the view folders as local only.
        statuses, results = self.run_sync()
        self.assertEqual(results["FOLDER_LOCAL_ONLY"], 0)
        # Nor does it touch the copies kept for the other views.
        self.assertEqual(
            [path for path in statuses if path.split("/")[0] in ("Shared with me", "Shared drives")], []
        )
        self.assertEqual((self.root / "Shared with me/from Ann.pdf").read_bytes(), b"pdf")
        self.assertEqual((self.root / "Shared drives/Team/plan.txt").read_bytes(), b"plan")

    def sync_views(self, views, picked):
        """Select every item at the top of the picked views, like a whole tab, then plan and apply."""
        cache, selections, browsed = {}, [], {}
        for view in views:
            if view.root_id in picked:
                savegdrive.load_view(self.drive, view, cache)
                for entry in view.children_of.get(None, []):
                    savegdrive.add_selected_item(selections, entry["item"], entry["relative_parent"])
                browsed.update(view.browsed)
        scopes = savegdrive.build_local_scopes(selections, browsed, [view.base for view in views if view.nodes])
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(
            self.drive, selections, scopes, self.root, state, cache, roots=savegdrive.view_roots(views)
        )
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            savegdrive.apply_plan(self.drive, plan, self.root, state)
        return {entry["relative_path"].as_posix(): entry for entry in plan}

    def test_shared_drive_named_like_yours_never_gets_your_files(self):
        # Someone adds you to their own drive "Finance": what you put in your Finance folder stays in yours.
        self.drive.items["q"] = file_item("q", "q3.xlsx", "corp", b"q3")
        self.drive.shared_drives = [{"id": "corp", "name": "Finance"}, {"id": "evil", "name": "Finance"}]
        views = savegdrive.drive_views(self.drive, "root")
        corp = next(view for view in views if view.root_id == "corp")
        evil = next(view for view in views if view.root_id == "evil")
        self.assertNotEqual(corp.base, evil.base)
        self.assertNotEqual(corp.name, evil.name)
        self.assertEqual(savegdrive.view_roots(views)[corp.base], "corp")
        self.sync_views(views, {"corp"})
        (self.root / corp.base / "salaries.xlsx").write_bytes(b"SALARIES")
        plan = self.sync_views(views, {"corp"})
        self.assertEqual(plan[(corp.base / "salaries.xlsx").as_posix()]["parent_id"], "corp")
        self.assertEqual(self.drive.items[self.drive.uploads[0]]["parent"], "corp")
        # A base two saved roots share (--again) sends nothing.
        self.assertEqual(savegdrive.upload_roots([("corp", Path("x")), ("evil", Path("x"))]), {Path("x"): None})

    def test_shared_folder_named_like_yours_is_a_conflict(self):
        # A stranger shares a "Finance" folder listed before your colleague's: neither syncs any more.
        colleague = {"id": "corp", "name": "Finance", "mimeType": FOLDER, "parent": "ann", "shared": True}
        self.drive.items.update({"corp": colleague, "q": file_item("q", "q3.xlsx", "corp", b"q3")})
        views = savegdrive.drive_views(self.drive, "root")
        self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        self.assertEqual((self.root / "Shared with me/Finance/q3.xlsx").read_bytes(), b"q3")
        stranger = {"id": "evil", "name": "Finance", "mimeType": FOLDER, "parent": "mallory", "shared": True}
        self.drive.items = {"evil": stranger, **self.drive.items,
                            "r": file_item("r", "README.txt", "evil", b"hi")}
        (self.root / "Shared with me/Finance/salaries.xlsx").write_bytes(b"SALARIES")
        plan = self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        finance = plan["Shared with me/Finance"]
        self.assertEqual((finance["status"], finance.get("duplicate")), ("CONFLICT", True))
        self.assertFalse([path for path in plan if path.startswith("Shared with me/Finance/")])
        self.assertEqual(self.drive.uploads, [])
        self.assertEqual(list(self.trash.iterdir()), [])
        self.assertEqual((self.root / "Shared with me/Finance/q3.xlsx").read_bytes(), b"q3")
        self.assertFalse((self.root / "Shared with me/Finance/README.txt").exists())

    def test_drive_folder_arriving_over_local_only_folder_gets_nothing(self):
        # Your "Private" folder at the top of Shared with me is local only; a stranger then shares a
        # "Private" folder with you, editable: your files must not go into it.
        self.drive.items["s"] = file_item("s", "from Ann.pdf", "ann", b"pdf", shared=True)
        views = savegdrive.drive_views(self.drive, "root")
        self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        (self.root / "Shared with me/Private").mkdir()
        (self.root / "Shared with me/Private/tax-2025.pdf").write_bytes(b"MY TAX RETURN")
        plan = self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        self.assertEqual(plan["Shared with me/Private"]["status"], "LOCAL_ONLY")
        self.drive.items["evil"] = {"id": "evil", "name": "Private", "mimeType": FOLDER, "parent": "mallory",
                                    "shared": True}
        plan = self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        self.assertEqual(plan["Shared with me/Private"]["status"], "CONFLICT")
        self.assertEqual(plan["Shared with me/Private/tax-2025.pdf"]["status"], "LOCAL_ONLY")
        self.assertTrue(any("local only folder" in note for note in savegdrive.plan_notes(list(plan.values()))))
        self.assertEqual(self.drive.uploads, [])

        # Your notes kept in "mine" inside a folder shared read only; its owner then adds an editable "mine".
        self.drive.items["team"] = {"id": "team", "name": "Team", "mimeType": FOLDER, "parent": "ann",
                                    "shared": True, "capabilities": {"canAddChildren": False, "canTrash": False}}
        self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        (self.root / "Shared with me/Team/mine").mkdir()
        (self.root / "Shared with me/Team/mine/notes.txt").write_bytes(b"MY PRIVATE NOTES")
        self.drive.items["mine"] = {"id": "mine", "name": "mine", "mimeType": FOLDER, "parent": "team"}
        plan = self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        self.assertEqual(plan["Shared with me/Team/mine"]["status"], "CONFLICT")
        self.assertEqual(plan["Shared with me/Team/mine/notes.txt"]["status"], "LOCAL_ONLY")
        self.assertEqual(self.drive.uploads, [])

        # A Drive folder synced before is still adopted: a new local file in it goes up.
        self.drive.items["team"]["capabilities"] = {"canAddChildren": False, "canTrash": False}
        self.drive.items["sub"] = {"id": "sub", "name": "sub", "mimeType": FOLDER, "parent": "team"}
        self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        (self.root / "Shared with me/Team/sub/new.txt").write_bytes(b"new")
        plan = self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        self.assertEqual(plan["Shared with me/Team/sub"]["status"], "UNCHANGED")
        self.assertEqual(plan["Shared with me/Team/sub/new.txt"]["status"], "UPLOAD_NEW")

    def test_editable_file_at_the_top_of_shared_with_me_goes_back_up(self):
        # A colleague shares one file with you as an editor; another one only as a reader.
        self.drive.items["budget"] = file_item("budget", "budget.xlsx", "ann", b"budget v1", shared=True,
                                               capabilities={"canEdit": True, "canTrash": False})
        self.drive.items["memo"] = file_item("memo", "memo.txt", "ann", b"memo v1", shared=True,
                                             capabilities={"canEdit": False, "canTrash": False})
        views = savegdrive.drive_views(self.drive, "root")
        self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        (self.root / "Shared with me/budget.xlsx").write_bytes(b"budget v2 edited here")
        (self.root / "Shared with me/memo.txt").write_bytes(b"memo edited here")
        plan = self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        self.assertEqual(plan["Shared with me/budget.xlsx"]["status"], "UPLOAD")
        self.assertEqual(plan["Shared with me/memo.txt"]["status"], "CONFLICT")
        self.assertEqual(self.drive.items["budget"]["content"], b"budget v2 edited here")
        self.assertEqual(self.drive.items["memo"]["content"], b"memo v1")
        plan = self.sync_views(views, {savegdrive.SHARED_WITH_ME})
        self.assertEqual(plan["Shared with me/budget.xlsx"]["status"], "UNCHANGED")

    def test_names_differing_only_by_case_are_a_conflict(self):
        # A case-insensitive disk (macOS, exFAT...) stores these as one file: rename one on Drive.
        self.drive.items.update({
            "up": file_item("up", "IMG_0001.JPG", "photos", b"camera photo"),
            "low": file_item("low", "img_0001.jpg", "photos", b"phone photo"),
            "nfc": file_item("nfc", "Caf\u00e9.txt", "docs", b"nfc"),
            "nfd": file_item("nfd", "Cafe\u0301.txt", "docs", b"nfd"),
            "f1": {"id": "f1", "name": "Album", "mimeType": FOLDER, "parent": "root"},
            "f2": {"id": "f2", "name": "album", "mimeType": FOLDER, "parent": "root"},
            "x1": file_item("x1", "a.jpg", "f1", b"one"),
            "x2": file_item("x2", "a.jpg", "f2", b"two"),
        })
        statuses, _ = self.run_sync()
        for path in ("Photos/IMG_0001.JPG", "Photos/img_0001.jpg", "Docs/Caf\u00e9.txt", "Docs/Cafe\u0301.txt",
                     "Album", "album"):
            self.assertEqual(statuses[path], "CONFLICT", path)
        self.assertFalse([path for path in statuses if path.casefold().startswith("album/")])
        self.assertFalse((self.root / "Photos/IMG_0001.JPG").exists())
        self.assertFalse((self.root / "Photos/img_0001.jpg").exists())
        self.run_sync()
        self.assertEqual(self.drive.uploads, [])
        self.assertEqual(self.drive.items["up"]["content"], b"camera photo")
        self.assertEqual(self.drive.items["low"]["content"], b"phone photo")

    def test_case_only_rename_on_drive_is_not_a_deletion(self):
        # On a case-insensitive disk, "report.txt" and "Report.txt" are one file: a hard link
        # (and a folder symlink) gives this disk the same two names for one file.
        self.drive.items.update({
            "r": file_item("r", "report.txt", "docs", b"report"),
            "cl": {"id": "cl", "name": "clients", "mimeType": FOLDER, "parent": "root"},
            "c0": file_item("c0", "contract0.pdf", "cl", b"contract 0"),
            "c1": file_item("c1", "contract1.pdf", "cl", b"contract 1"),
        })
        self.run_sync()
        self.drive.items["r"]["name"] = "Report.txt"
        self.drive.items["cl"]["name"] = "Clients"
        os.link(self.root / "Docs/report.txt", self.root / "Docs/Report.txt")
        (self.root / "Clients").symlink_to("clients")
        # On that disk "Clients" is the folder itself, not a link: links are otherwise never followed.
        patcher = mock.patch.object(savegdrive, "has_symlink", lambda *args: False)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / "clients/mine.txt").write_bytes(b"made here")
        for _ in range(2):
            statuses, _ = self.run_sync()
            self.assertEqual(statuses["Docs/Report.txt"], "UNCHANGED")
            self.assertEqual(statuses["Clients/contract0.pdf"], "UNCHANGED")
            self.assertFalse([path for path in statuses if path.startswith(("Docs/report", "clients"))])
        self.assertEqual(statuses["Clients/mine.txt"], "UNCHANGED")  # still seen: sent once
        self.assertFalse([item for item in self.drive.items.values() if item.get("trashed")])
        self.assertEqual(list(self.trash.iterdir()), [])
        self.assertEqual((self.root / "Docs/report.txt").read_bytes(), b"report")
        self.assertEqual((self.root / "clients/contract0.pdf").read_bytes(), b"contract 0")

    def test_space_line(self):
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        plan = savegdrive.build_plan(
            self.drive, selections, savegdrive.build_local_scopes(selections, browsed),
            self.root, savegdrive.load_state(self.root), cache,
        )
        self.assertEqual(savegdrive.download_size(plan), (19, 1))  # a, b, beach, notes; Report has no size
        with mock.patch.object(savegdrive.shutil, "disk_usage", return_value=mock.Mock(free=2_000_000)):
            text, fits = savegdrive.space_line(plan, self.root)
        self.assertEqual(text, "19 B to download + 1 exported file of unknown size · 2.0 MB free")
        self.assertTrue(fits)
        with mock.patch.object(savegdrive.shutil, "disk_usage", return_value=mock.Mock(free=10)):
            self.assertFalse(savegdrive.space_line(plan, self.root)[1])
        self.assertEqual(savegdrive.space_line([], self.root), (None, True))
        # tests 60: the terminal preview (--yes, --again) flags a download the disk cannot hold.
        for free, warned in ((2_000_000, False), (10, True)):
            output = io.StringIO()
            with mock.patch.object(savegdrive, "console", Console(file=output, width=200, highlight=False)), \
                    mock.patch.object(savegdrive.shutil, "disk_usage", return_value=mock.Mock(free=free)):
                savegdrive.print_plan(plan, self.root)
            line = next(line for line in output.getvalue().splitlines() if "to download" in line)
            self.assertEqual("not enough disk space" in line, warned, line)
            self.assertEqual(line.startswith("! "), warned, line)

    def test_header_text(self):
        home = Path.home()
        self.assertEqual(
            savegdrive.header_text("you@gmail.com", home / "GDrive", "preview"),
            "preview · syncs with ~/GDrive · you@gmail.com",
        )
        self.assertEqual(savegdrive.header_text(None, Path("/data/x")), "syncs with /data/x")

    def test_destination_option(self):
        target = Path(self.temp.name) / "Once"
        self.assertEqual(savegdrive.resolve_destination(str(target)), target)
        self.assertTrue(savegdrive.has_valid_state(target))
        with self.assertRaises(OSError):
            savegdrive.resolve_destination(str(Path(self.temp.name) / "missing" / "x"))

        # The default ~/GDrive, and --download-path=~/x, which the shell leaves unexpanded after "=".
        home = Path(self.temp.name) / "home"
        workdir = Path(self.temp.name) / "workdir"
        home.mkdir()
        workdir.mkdir()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(workdir)
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(savegdrive.resolve_destination(savegdrive.DOWNLOAD_PATH), home / "GDrive")
            self.assertEqual(savegdrive.resolve_destination("~/x"), home / "x")
        self.assertTrue(savegdrive.has_valid_state(home / "GDrive"))
        self.assertEqual(list(workdir.iterdir()), [])

    def test_folder_synced_with_my_drive_refuses_another_folder_id(self):
        self.run_sync()  # My Drive, in a folder made before the Drive root was recorded
        for folder_id in ("root", "docs", None):  # None: --again, which replays the folder's own roots
            with mock.patch.object(savegdrive, "DRIVE_ROOT", folder_id):
                if folder_id == "docs":
                    with self.assertRaisesRegex(ValueError, "syncs My Drive, not the Drive folder docs"):
                        savegdrive.resolve_destination(str(self.root))
                else:
                    self.assertEqual(savegdrive.resolve_destination(str(self.root)), self.root)
        self.assertEqual(savegdrive.load_state(self.root)["drive_root"], "root")
        self.assertEqual(sorted(path.name for path in self.root.iterdir() if not path.name.startswith(".")),
                         ["Docs", "Photos", "notes.txt"])

        docs_only = Path(self.temp.name) / "Docs only"
        with mock.patch.object(savegdrive, "DRIVE_ROOT", "docs"):
            savegdrive.resolve_destination(str(docs_only))
            self.assertEqual(savegdrive.resolve_destination(str(docs_only)), docs_only)
        with mock.patch.object(savegdrive, "DRIVE_ROOT", "root"):
            with self.assertRaisesRegex(ValueError, "syncs the Drive folder docs, not My Drive"):
                savegdrive.resolve_destination(str(docs_only))

    def test_google_doc_next_to_its_docx_names_both_drive_items(self):
        # "Report" (Google Doc) and "Report.docx" (Word) look different on Drive but share one local name.
        self.drive.items["w"] = file_item("w", "Report.docx", "docs", b"word")
        plan, _ = self.preview()
        clashing = [entry for entry in plan if entry["relative_path"].as_posix() == "Docs/Report.docx"]
        self.assertEqual(sorted(savegdrive.duplicate_label(entry) for entry in clashing), [
            "(Drive: Report · same local name as Report.docx)",
            "(Drive: Report.docx · same local name as Report)",
        ])
        notes = savegdrive.plan_notes(plan)
        self.assertTrue(any("a Google Doc next to its .docx" in note for note in notes))
        self.assertFalse(any("keep both" in note for note in notes))  # b does nothing for these
        output = io.StringIO()
        with mock.patch.object(savegdrive, "console", Console(file=output, width=200)):
            savegdrive.print_plan(plan, self.root)
        self.assertIn("Docs/Report.docx  (Drive: Report · same local name as Report.docx)", output.getvalue())

    def test_drive_names_cannot_send_terminal_sequences(self):
        # security 5: a name chosen by whoever shares a file reaches the terminal through the preview, the
        # report line of each transfer and warnings: its ESC sequences (title, clipboard, erase line) and
        # line breaks must be shown, not sent.
        hostile = "invoice.pdf\x1b]0;PWNED-TITLE\x1b\\\x1b]52;c;ZWNobyBoaQ==\x1b\\\x1b[1A\x1b[2K"
        self.drive.items["evil"] = file_item("evil", hostile, "docs", b"evil")
        self.drive.items["fake"] = file_item("fake", "a\n\u2713 0 errors\x9b2K", "docs", b"fake")
        output = io.StringIO()
        console = Console(file=output, width=400)  # not a terminal: Rich adds no ESC of its own
        with mock.patch.object(savegdrive, "console", console):
            plan, state = self.preview()
            savegdrive.print_plan(plan, self.root)
            savegdrive.apply_plan(self.drive, plan, self.root, state, 1, lambda: self.drive)
            savegdrive.warn(f"Could not compare {hostile}: 500 Internal Error")
        text = output.getvalue()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x9b", text)
        self.assertNotIn("\u2713 0 errors", [line.strip() for line in text.splitlines()])
        self.assertIn("invoice.pdf\\x1b]0;PWNED-TITLE", text)
        self.assertIn("a\\x0a\u2713 0 errors\\x9b2K", text)
        self.assertIn("\x1b", (self.root / "Docs" / hostile).name)  # the file on disk keeps its Drive name
        self.assertEqual(savegdrive.printable("tab\tstays"), "tab\tstays")

    def test_read_only_files_with_one_name_all_come_down(self):
        # Ann's folder, shared read only, holds two Scan.pdf: you cannot rename either on Drive.
        read_only = {"canEdit": False, "canTrash": False, "canAddChildren": False}
        self.drive.items["course"] = {"id": "course", "name": "Course", "mimeType": FOLDER, "parent": "root",
                                      "capabilities": read_only}
        self.drive.items["scan2"] = file_item("scan2", "Scan.pdf", "course", b"second", capabilities=read_only)
        self.drive.items["scan1"] = file_item("scan1", "Scan.pdf", "course", b"first", capabilities=read_only)
        self.drive.items["d"] = file_item("d", "a.txt", "docs", b"yours")  # yours: rename one on Drive
        plan, _ = self.preview()
        by_path = {}
        for entry in plan:
            by_path.setdefault(entry["relative_path"].as_posix(), []).append(entry["status"])
        self.assertEqual(by_path["Course/Scan.pdf"], ["NEW"])
        self.assertEqual(by_path["Course/Scan (2).pdf"], ["NEW"])
        self.assertEqual(by_path["Docs/a.txt"], ["CONFLICT", "CONFLICT"])
        notes = savegdrive.plan_notes(plan)
        self.assertTrue(any("rename one on Drive" in note for note in notes))
        self.assertTrue(any("'name (2).ext'" in note for note in notes))

        self.run_sync()
        self.assertEqual((self.root / "Course/Scan.pdf").read_bytes(), b"first")
        self.assertEqual((self.root / "Course/Scan (2).pdf").read_bytes(), b"second")
        statuses, _ = self.run_sync()  # each one is found again under its name
        self.assertEqual((statuses["Course/Scan.pdf"], statuses["Course/Scan (2).pdf"]), ("UNCHANGED", "UNCHANGED"))
        # A third one with a smaller id: the two synced keep their names, the new one gets the next.
        self.drive.items["scan0"] = file_item("scan0", "Scan.pdf", "course", b"third", capabilities=read_only)
        statuses, _ = self.run_sync()
        self.assertEqual((statuses["Course/Scan.pdf"], statuses["Course/Scan (2).pdf"]), ("UNCHANGED", "UNCHANGED"))
        self.assertEqual(statuses["Course/Scan (3).pdf"], "NEW")
        self.assertEqual((self.root / "Course/Scan (3).pdf").read_bytes(), b"third")

    def test_keep_both(self):
        self.run_sync()
        (self.root / "Docs/b.txt").write_bytes(b"edited locally")
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"bravo v2"))  # changed on both sides
        (self.root / "Docs/Report.docx").write_bytes(b"edited export")
        self.drive.items["d"] = file_item("d", "a.txt", "docs", b"same name")  # two Drive files, one name
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(
            self.drive, selections, savegdrive.build_local_scopes(selections, browsed), self.root, state, cache
        )
        conflicts = {entry["relative_path"].as_posix() for entry in plan if entry["status"] == "CONFLICT"}
        self.assertEqual(conflicts, {"Docs/a.txt", "Docs/b.txt", "Docs/Report.docx"})
        savegdrive.set_keep_both(plan, plan, True)
        kept = sorted(entry["local_copy"].name for entry in plan if entry["status"] == "KEEP_BOTH")
        self.assertEqual(kept, ["Report (local).docx", "b (local).txt"])  # same-name Drive files: not here
        savegdrive.set_keep_both(plan, plan, False)
        self.assertEqual(len([entry for entry in plan if entry["status"] == "CONFLICT"]), 4)
        savegdrive.set_keep_both(plan, plan, True)

        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            results = savegdrive.apply_plan(self.drive, plan, self.root, state)
        self.assertEqual(results["FILE_KEEP_BOTH"], 2)
        self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"bravo v2")
        self.assertEqual((self.root / "Docs/b (local).txt").read_bytes(), b"edited locally")
        self.assertEqual((self.root / "Docs/Report (local).docx").read_bytes(), b"edited export")

        # Next sync: the conflict is gone, your version goes up as a new file.
        del self.drive.items["d"]
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/b.txt"], "UNCHANGED")
        self.assertEqual(statuses["Docs/Report.docx"], "UNCHANGED")
        self.assertEqual(statuses["Docs/b (local).txt"], "UPLOAD_NEW")
        self.assertEqual(statuses["Docs/Report (local).docx"], "UPLOAD_NEW")
        self.assertEqual(savegdrive.local_copy_path(Path("/x/.env"), set()), Path("/x/.env (local)"))

    def conflict_after_docs_sync(self):
        """Docs synced and remembered for --again and s, then b.txt changed on both sides."""
        self.sync_picked("Docs")
        (self.root / "Docs/b.txt").write_bytes(b"edited locally")
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"bravo v2"))
        self.drive.change("b")

    def test_keep_both_option_in_the_tree_s_and_again(self):
        # tests 42: --keep-both turns every conflict into KEEP_BOTH on each path: tree, s at start, --again.
        import asyncio
        self.conflict_after_docs_sync()

        def b_status(plan):
            return next(entry["status"] for entry in plan if entry["relative_path"].as_posix() == "Docs/b.txt")

        async def tree_and_s():
            app = savegdrive.DriveSelectorApp([savegdrive.DriveView("My Drive", "root")], self.root,
                                              savegdrive.load_state(self.root), self.drive, None, {},
                                              skip_preview=True, keep_both=True)
            savegdrive.load_view(self.drive, app.views[0], app.cache)
            async with app.run_test() as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIsNotNone(app.last_sync)  # what s would sync
                last_sync = b_status(app.last_sync["plan"])
                await pilot.press("down", "space", "d")  # Docs, then sync without the preview
                await app.workers.wait_for_complete()
                await pilot.pause()
            return last_sync, app.return_value

        last_sync, result = asyncio.run(tree_and_s())
        self.assertEqual(last_sync, "KEEP_BOTH")
        self.assertEqual(self.selected_paths(result), ["Docs"])
        self.assertEqual(b_status(result["plan"]), "KEEP_BOTH")

        self.run_main("--again", "--yes", "--keep-both")
        self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"bravo v2")
        self.assertEqual((self.root / "Docs/b (local).txt").read_bytes(), b"edited locally")

    def selected_paths(self, result):
        return [savegdrive.selected_path(entry).as_posix() for entry in result["selections"]]

    def test_keep_both_failed_download_puts_your_version_back(self):
        # tests 13: the download of Drive's version fails after yours stepped aside as "b (local).txt".
        self.run_sync()
        (self.root / "Docs/b.txt").write_bytes(b"edited locally")
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"bravo v2"))
        plan, state = self.preview()
        conflict = next(entry for entry in plan if entry["relative_path"].as_posix() == "Docs/b.txt")
        self.assertEqual(conflict["status"], "CONFLICT")
        savegdrive.set_keep_both(plan, [conflict], True)

        def failing_fetch(service, item, file_handle, stop=None):
            if savegdrive.effective_id(item) == "b":
                file_handle.write(b"partial")
                raise OSError("network down")
            fake_fetch(service, item, file_handle, stop)

        with mock.patch.object(savegdrive, "fetch_media", failing_fetch):
            results = self.apply(plan, state)
        self.assertEqual(results["FILE_ERROR"], 1)
        self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"edited locally")
        self.assertFalse(os.path.lexists(self.root / "Docs/b (local).txt"))
        self.assertEqual(sorted(path.name for path in (self.root / "Docs").iterdir() if "b" in path.name),
                         ["b.txt"])

    def test_keep_both_never_overwrites_the_copy_kept_last_time(self):
        self.run_sync()
        (self.root / "Docs/b (local).txt").write_bytes(b"kept last time")  # an earlier keep both
        (self.root / "Docs/b.txt").write_bytes(b"edited locally again")
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"bravo v3"))  # a new conflict
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["item"]["id"] == "b":  # only b.txt chosen: the earlier copy is not in this plan
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(
            self.drive, selections, savegdrive.build_local_scopes(selections, browsed), self.root, state, cache
        )
        entries = {entry["relative_path"].as_posix(): entry for entry in plan}
        self.assertNotIn("Docs/b (local).txt", entries)
        self.assertEqual(entries["Docs/b.txt"]["status"], "CONFLICT")
        savegdrive.set_keep_both(plan, [entries["Docs/b.txt"]], True)
        self.assertEqual(entries["Docs/b.txt"]["local_copy"].name, "b (local 2).txt")
        results = self.apply(plan, state)
        self.assertEqual(results["FILE_ERROR"], 0)
        self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"bravo v3")
        self.assertEqual((self.root / "Docs/b (local).txt").read_bytes(), b"kept last time")
        self.assertEqual((self.root / "Docs/b (local 2).txt").read_bytes(), b"edited locally again")

    def test_keep_both_never_takes_a_name_the_plan_downloads(self):
        self.run_sync()
        (self.root / "Docs/a.txt").write_bytes(b"MY UNSAVED WORK")
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        self.drive.items["al"] = file_item("al", "a (local).txt", "docs", b"other computer's copy")
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                plan, state = self.preview()
                entries = {entry["relative_path"].as_posix(): entry for entry in plan}
                self.assertEqual(entries["Docs/a (local).txt"]["status"], "NEW")
                savegdrive.set_keep_both(plan, [entries["Docs/a.txt"]], True)
                self.assertEqual(entries["Docs/a.txt"]["local_copy"].name, "a (local 2).txt")
                if jobs == 1:
                    # Plan without the download: the name is free again on undo, still avoided on redo.
                    savegdrive.set_keep_both(plan, [entries["Docs/a.txt"]], False)
                    savegdrive.set_keep_both(plan, [entries["Docs/a.txt"]], True)
                    self.assertEqual(entries["Docs/a.txt"]["local_copy"].name, "a (local 2).txt")
                    continue
                results = self.apply(plan, state, jobs)
                self.assertEqual(results["FILE_ERROR"], 0)
                self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha v2")
                self.assertEqual((self.root / "Docs/a (local).txt").read_bytes(), b"other computer's copy")
                self.assertEqual((self.root / "Docs/a (local 2).txt").read_bytes(), b"MY UNSAVED WORK")

    def test_local_edit_after_the_preview_is_never_overwritten(self):
        self.run_sync()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2 from Drive"))
        self.drive.items["c"] = file_item("c", "c.txt", "docs", b"charlie from Drive")
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"bravo v2 from Drive"))
        (self.root / "Docs/b.txt").write_bytes(b"bravo from here")
        plan, state = self.preview()
        statuses = {entry["relative_path"].as_posix(): entry for entry in plan}
        self.assertEqual(statuses["Docs/a.txt"]["status"], "UPDATE")
        self.assertEqual(statuses["Docs/c.txt"]["status"], "NEW")
        savegdrive.set_keep_both(plan, [statuses["Docs/b.txt"]], True)

        # While the preview is open, you save a.txt and b.txt, and create c.txt here.
        (self.root / "Docs/a.txt").write_bytes(b"TODAY'S EDITS")
        (self.root / "Docs/b.txt").write_bytes(b"bravo from here, edited again")
        (self.root / "Docs/c.txt").write_bytes(b"charlie made here")
        results = self.apply(plan, state)
        self.assertEqual(results["FILE_ERROR"], 3)
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"TODAY'S EDITS")
        self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"bravo from here, edited again")
        self.assertFalse((self.root / "Docs/b (local).txt").exists())
        self.assertEqual((self.root / "Docs/c.txt").read_bytes(), b"charlie made here")

        statuses, _ = self.run_sync()  # changed on both sides: conflicts, nothing overwritten
        self.assertEqual({statuses[path] for path in ("Docs/a.txt", "Docs/b.txt", "Docs/c.txt")}, {"CONFLICT"})
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"TODAY'S EDITS")

    def test_upload_never_replaces_a_drive_change_made_after_the_preview(self):
        self.run_sync()
        (self.root / "Docs/b.txt").write_bytes(b"b2 mine")
        plan, state = self.preview()
        self.assertEqual(next(entry for entry in plan if entry["relative_path"] == Path("Docs/b.txt"))["status"],
                         "UPLOAD")
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"COLLEAGUE EDIT",
                                               modifiedTime="2026-03-01T00:00:00.000Z"))
        results = self.apply(plan, state)
        self.assertEqual(results["FILE_ERROR"], 1)
        self.assertEqual(self.drive.items["b"]["content"], b"COLLEAGUE EDIT")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/b.txt"], "CONFLICT")

    def test_edit_saved_during_the_preview_of_an_unchanged_file_is_sent(self):
        self.run_sync()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        plan, state = self.preview()
        self.assertEqual(next(entry for entry in plan if entry["relative_path"] == Path("Docs/b.txt"))["status"],
                         "UNCHANGED")
        (self.root / "Docs/b.txt").write_bytes(b"BRAVO")  # same size as the synced b"bravo"
        self.apply(plan, state)
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/b.txt"], "UPLOAD")
        self.assertEqual(self.drive.items["b"]["content"], b"BRAVO")

    def test_edit_saved_during_its_upload_is_sent_next_time(self):
        self.run_sync()
        video = self.root / "Docs/b.txt"
        video.write_bytes(b"take 2")

        def upload_then_save(service, path, **kwargs):
            item = fake_upload(service, path, **kwargs)
            path.write_bytes(b"take 2 + more edits saved during the upload")
            return item

        with mock.patch.object(savegdrive, "upload_file", upload_then_save):
            statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/b.txt"], "UPLOAD")
        self.assertEqual(self.drive.items["b"]["content"], b"take 2")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/b.txt"], "UPLOAD")
        self.assertEqual(self.drive.items["b"]["content"], b"take 2 + more edits saved during the upload")

    def test_deletions_skip_what_changed_after_the_preview(self):
        import shutil

        self.drive.items["inbox"] = {"id": "inbox", "name": "Inbox", "mimeType": FOLDER, "parent": "docs"}
        self.run_sync()
        shutil.rmtree(self.root / "Photos")  # deleted here
        (self.root / "Docs/b.txt").unlink()  # deleted here
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))  # a download runs first
        del self.drive.items["n"]  # deleted on Drive
        del self.drive.items["inbox"]  # an empty synced folder, deleted on Drive
        plan, state = self.preview()
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertEqual([statuses[path] for path in ("Photos", "Docs/b.txt", "notes.txt", "Docs/Inbox")],
                         ["TRASH_REMOTE", "TRASH_REMOTE", "TRASH_LOCAL", "TRASH_LOCAL"])

        # While the preview is open: a colleague adds a file to Photos, you edit notes.txt, you save into Inbox.
        self.drive.items["c1"] = file_item("c1", "contract-signed.pdf", "photos", b"signed")
        (self.root / "notes.txt").write_bytes(b"notes + TODAY'S EDITS")
        (self.root / "Docs/Inbox/scan-0001.pdf").write_bytes(b"scan")

        def fetch_while_a_colleague_edits_b(service, item, file_handle, stop=None):
            service.items["b"].update(file_item("b", "b.txt", "docs", b"budget v2, a colleague's work",
                                                modifiedTime="2026-03-01T00:00:00.000Z"))
            fake_fetch(service, item, file_handle, stop)

        with mock.patch.object(savegdrive, "fetch_media", fetch_while_a_colleague_edits_b):
            results = self.apply(plan, state)
        self.assertEqual(results["FILE_TRASH_REMOTE"] + results["FOLDER_TRASH_REMOTE"], 0)
        self.assertEqual(results["FILE_TRASH_LOCAL"] + results["FOLDER_TRASH_LOCAL"], 0)
        for item_id in ("photos", "c1", "p", "b"):
            self.assertFalse(self.drive.is_trashed(self.drive.items[item_id]), item_id)
        self.assertEqual(list(self.trash.iterdir()), [])
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"notes + TODAY'S EDITS")
        self.assertEqual((self.root / "Docs/Inbox/scan-0001.pdf").read_bytes(), b"scan")

        statuses, _ = self.run_sync()  # compared again: nothing new is lost
        self.assertEqual(statuses["Photos/contract-signed.pdf"], "NEW")
        self.assertEqual(statuses["Docs/b.txt"], "NEW")
        self.assertEqual(statuses["notes.txt"], "UPLOAD_NEW")
        self.assertEqual(statuses["Docs/Inbox"], "UPLOAD_NEW")  # gone from Drive, but saved into: created again
        self.assertEqual(statuses["Docs/Inbox/scan-0001.pdf"], "UPLOAD_NEW")  # kept here and sent, not trashed
        self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"budget v2, a colleague's work")

    def test_keep_both_in_a_read_only_folder_says_so(self):
        self.drive.items["docs"]["capabilities"] = {"canAddChildren": False}
        self.run_sync()
        (self.root / "Docs/b.txt").write_bytes(b"edited locally")
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"bravo v2"))
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = [{"item": node["item"], "relative_parent": node["relative_parent"]}
                      for node in nodes if node["parent"] is None]
        plan = savegdrive.build_plan(
            self.drive, selections, savegdrive.build_local_scopes(selections, browsed), self.root,
            savegdrive.load_state(self.root), cache,
        )
        savegdrive.set_keep_both(plan, plan, True)
        self.assertTrue(any("stay on your disk only" in note for note in savegdrive.plan_notes(plan)))

    def test_keep_both_of_a_file_picked_alone_goes_up_with_again(self):
        # bugs 25: a file picked alone, changed on both sides, kept both ways. Its folder is not scanned, so the
        # preview must not promise the next sync sends "(local)", nor call a writable folder read only; --again
        # then sends it.
        for path, parent_id, item_id in (("notes.txt", "root", "n"), ("Docs/a.txt", "docs", "a")):
            with self.subTest(path=path):
                cache = {}
                nodes, _ = savegdrive.collect_drive_tree(self.drive, "root", cache)
                node = next(node for node in nodes if node["display_path"] == path)
                # Picked in the tree: sendable_parent_id gives its Drive folder.
                selections = [{"item": node["item"], "relative_parent": node["relative_parent"],
                               "parent_id": parent_id}]
                state = savegdrive.load_state(self.root)
                plan = savegdrive.build_plan(self.drive, selections, [], self.root, state, cache, roots={})
                savegdrive.remember_selection(state, selections, [])
                self.apply(plan, state)
                mine = f"edited here: {path}".encode()
                (self.root / path).write_bytes(mine)
                self.drive.items[item_id].update(file_item(item_id, Path(path).name, parent_id, b"edited on Drive"))

                cache = {}
                nodes, _ = savegdrive.collect_drive_tree(self.drive, "root", cache)
                node = next(node for node in nodes if node["display_path"] == path)
                selections = [{"item": node["item"], "relative_parent": node["relative_parent"],
                               "parent_id": parent_id}]
                state = savegdrive.load_state(self.root)
                plan = savegdrive.build_plan(self.drive, selections, [], self.root, state, cache, roots={})
                self.assertEqual([entry["status"] for entry in plan], ["CONFLICT"])
                savegdrive.set_keep_both(plan, plan, True)
                notes = savegdrive.plan_notes(plan)
                self.assertFalse(any("read only on Drive" in note for note in notes), notes)
                self.assertTrue(any("--again" in note for note in notes), notes)
                savegdrive.remember_selection(state, selections, [])
                self.apply(plan, state)
                copy = Path(path).parent / f"{Path(path).stem} (local).txt"
                self.assertEqual((self.root / copy).read_bytes(), mine)

                statuses = self.sync_picked(again=True)
                self.assertEqual(statuses[copy.as_posix()], "UPLOAD_NEW")
                sent = [item for item in self.drive.items.values() if item.get("content") == mine]
                self.assertEqual([(item["name"], item["parent"]) for item in sent], [(copy.name, parent_id)])
                statuses = self.sync_picked(again=True)  # sent once, then in sync
                self.assertEqual(statuses[copy.as_posix()], "UNCHANGED")

    def test_existing_identical_file_is_adopted(self):
        (self.root / "notes.txt").write_bytes(b"notes")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["notes.txt"], "UNCHANGED")
        # tests 48: the adopted copy is tracked, so deleting it on Drive trashes it here instead of sending it back.
        del self.drive.items["n"]
        statuses, results = self.run_sync()
        self.assertEqual(statuses["notes.txt"], "TRASH_LOCAL")
        self.assertEqual(results["FILE_TRASH_LOCAL"], 1)
        self.assertFalse((self.root / "notes.txt").exists())
        self.assertEqual(self.drive.uploads, [])

    def test_first_sync_different_file_is_a_conflict(self):
        (self.root / "notes.txt").write_bytes(b"my own notes")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["notes.txt"], "CONFLICT")
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"my own notes")

    def test_first_sync_identical_export_is_adopted(self):
        (self.root / "Docs").mkdir()
        (self.root / "Docs/Report.docx").write_bytes(b"docx bytes")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/Report.docx"], "UNCHANGED")

    def test_first_sync_different_export_is_a_conflict(self):
        (self.root / "Docs").mkdir()
        (self.root / "Docs/Report.docx").write_bytes(b"my local report")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/Report.docx"], "CONFLICT")
        self.assertEqual((self.root / "Docs/Report.docx").read_bytes(), b"my local report")

    def test_first_sync_export_compare_error_is_a_conflict(self):
        (self.root / "Docs").mkdir()
        (self.root / "Docs/Report.docx").write_bytes(b"my local report")

        def failing_fetch(service, item, file_handle, stop=None):
            if savegdrive.effective_id(item) == "report":
                raise http_error(403)
            fake_fetch(service, item, file_handle, stop)

        with mock.patch.object(savegdrive, "fetch_media", failing_fetch), \
                mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/Report.docx"], "CONFLICT")
        self.assertEqual((self.root / "Docs/Report.docx").read_bytes(), b"my local report")

    def test_interrupted_download_leaves_no_part_file(self):
        def interrupted(service, item, file_handle, stop=None):
            file_handle.write(b"partial")
            raise KeyboardInterrupt

        # The local copy being updated must stay intact: only the .part file is written and removed.
        (self.root / "x.txt").write_bytes(b"old")
        entry = {"destination": self.root / "x.txt", "item": self.drive.items["a"]}
        with mock.patch.object(savegdrive, "fetch_media", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                savegdrive.download_file_atomically(self.drive, entry)
        self.assertEqual((self.root / "x.txt").read_bytes(), b"old")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         sorted([savegdrive.STATE_FILE_NAME, "x.txt"]))

    def interrupted_while_reading_the_first_result(self):
        """Ctrl+C while the loop reads the first finished transfer: the others are finished but not read yet."""
        import concurrent.futures

        def stop_after_the_first(futures):
            concurrent.futures.wait(futures)
            yield next(iter(futures))
            raise KeyboardInterrupt

        self.run_sync()
        for item_id, name, parent in [("a", "a.txt", "docs"), ("b", "b.txt", "docs"), ("p", "beach.jpg", "photos")]:
            self.drive.items[item_id].update(file_item(item_id, name, parent, b"v2 of " + name.encode(),
                                                       modifiedTime="2026-03-01T00:00:00.000Z"))
        (self.root / "notes.txt").write_bytes(b"notes, edited here")
        plan, state = self.preview()
        with mock.patch.object(savegdrive, "as_completed", stop_after_the_first):
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan, state, jobs=4)

    def test_transfers_finished_before_ctrl_c_are_saved(self):
        self.interrupted_while_reading_the_first_result()
        saved = {record["path"]: record for record in savegdrive.load_state(self.root)["files"].values()}
        for item_id, path in [("a", "Docs/a.txt"), ("b", "Docs/b.txt"), ("p", "Photos/beach.jpg"), ("n", "notes.txt")]:
            with self.subTest(path=path):
                self.assertEqual(saved[path]["remote_signature"],
                                 savegdrive.remote_signature(self.drive.public(self.drive.items[item_id])))
        statuses, _ = self.run_sync()
        self.assertEqual({statuses[path] for path in ("Docs/a.txt", "Docs/b.txt", "Photos/beach.jpg", "notes.txt")},
                         {"UNCHANGED"})


    def test_sigterm_or_sighup_saves_finished_transfers_and_removes_part_file(self):
        tests_dir = Path(__file__).resolve().parent
        for name in ("SIGTERM", "SIGHUP"):
            with self.subTest(signal=name), tempfile.TemporaryDirectory() as temp:
                result = subprocess.run(
                    [sys.executable, "-c", KILLED_SYNC, str(tests_dir.parent), temp, name],
                    env=dict(os.environ, PYTHONPATH=str(tests_dir)), capture_output=True, text=True, timeout=60,
                )
                self.assertIn("KeyboardInterrupt", result.stderr)  # the Ctrl+C path, not a silent death
                shown = json.loads(result.stdout.splitlines()[0])
                root = Path(shown["root"])
                self.assertEqual(list(root.rglob(f"*{savegdrive.PART_SUFFIX}")), [])
                saved = {record["path"]: record for record in savegdrive.load_state(root)["files"].values()}
                self.assertEqual(saved["Docs/a.txt"]["remote_signature"], shown["a"])  # downloaded before: no conflict

    def test_ctrl_c_stops_running_downloads_at_their_next_chunk(self):
        # tests 78: apply_plan sets stop, download_file_atomically passes it to the real fetch_media, which
        # checks it before each chunk; a regression on any of the three lets b.txt download to the end.
        self.run_sync()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2",
                                               modifiedTime="2026-03-01T00:00:00.000Z"))
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"bravo v2",
                                               modifiedTime="2026-03-01T00:00:00.000Z"))
        b_started = threading.Event()
        b_chunks = []
        drive = self.drive

        class Downloader:
            def __init__(self, file_handle, request, chunksize):
                self.file_handle = file_handle
                self.item_id = request

            def next_chunk(self, num_retries=0):
                if self.item_id == "a":
                    b_started.wait(5)
                    raise KeyboardInterrupt
                b_started.set()
                b_chunks.append(1)
                time.sleep(0.05)
                if len(b_chunks) == 40:
                    self.file_handle.write(drive.items["b"]["content"])
                return None, len(b_chunks) == 40

        with mock.patch.object(savegdrive, "fetch_media", REAL_FETCH_MEDIA), \
                mock.patch.object(savegdrive, "download_request", lambda service, item: savegdrive.effective_id(item)), \
                mock.patch.object(savegdrive, "MediaIoBaseDownload", Downloader):
            plan, state = self.preview()
            with self.assertRaises(KeyboardInterrupt):
                self.apply(plan, state, jobs=2)
        self.assertLess(len(b_chunks), 40)
        self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"bravo")
        self.assertEqual(list(self.root.rglob(f"*{savegdrive.PART_SUFFIX}")), [])
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/b.txt"], "UPDATE")

    def test_disk_trash_at_the_destination_root_is_never_sent(self):
        # security 18: with a disk root as the destination, send2trash puts what it removes in <root>/.Trash-<uid>
        def disk_trash_local(path):
            trash = self.root / ".Trash-1000"
            (trash / "files").mkdir(parents=True, exist_ok=True)
            (trash / "info").mkdir(exist_ok=True)
            (trash / "info" / f"{path.name}.trashinfo").write_text(f"[Trash Info]\nPath={path}\n")
            os.replace(path, trash / "files" / path.name)

        self.run_sync()
        for name in (".Trash/1000", ".Trashes/501", "$RECYCLE.BIN/S-1-5-21"):
            (self.root / name).mkdir(parents=True)
            (self.root / name / "old.txt").write_bytes(b"deleted here")
        del self.drive.items["a"]
        with mock.patch.object(savegdrive, "trash_local", disk_trash_local):
            statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "TRASH_LOCAL")
        self.assertTrue((self.root / ".Trash-1000/files/a.txt").exists())
        names_before = {item["name"] for item in self.drive.items.values()}
        statuses, _ = self.run_sync()
        trash_names = (".Trash-1000", ".Trash", ".Trashes", "$RECYCLE.BIN")
        self.assertEqual([path for path in statuses if path.split("/")[0] in trash_names], [])
        self.assertEqual({item["name"] for item in self.drive.items.values()}, names_before)

    def test_part_file_left_by_a_killed_run_is_removed(self):
        self.run_sync()
        left = self.root / "Docs" / f".b.txt{savegdrive.PART_SUFFIX}"
        left.write_bytes(b"half")
        statuses, _ = self.run_sync()
        self.assertFalse(left.exists())
        self.assertNotIn(f"Docs/{left.name}", statuses)

    def test_drive_name_near_the_255_byte_limit_comes_down(self):
        # bugs 16: ".<name>.savegdrive.part" is 17 bytes longer than the name: 239 to 255 bytes failed (Errno 36)
        name = "会議録" * 27 + ".pdf"  # 247 bytes in UTF-8
        self.drive.items["long"] = file_item("long", name, "docs", b"minutes")
        statuses, results = self.run_sync()
        self.assertEqual(statuses[f"Docs/{name}"], "NEW")
        self.assertEqual((self.root / "Docs" / name).read_bytes(), b"minutes")
        self.assertEqual(list(self.root.rglob(f"*{savegdrive.PART_SUFFIX}")), [])
        statuses, _ = self.run_sync()
        self.assertEqual(statuses[f"Docs/{name}"], "UNCHANGED")

    def test_same_content_changed_on_both_sides_is_not_a_conflict(self):
        self.run_sync()
        # Sent and downloaded, but the state could not be written (disk full): both sides look changed.
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2",
                                               modifiedTime="2026-03-01T00:00:00.000Z"))
        (self.root / "Docs/a.txt").write_bytes(b"alpha v2")
        self.drive.items["b"].update(file_item("b", "b.txt", "docs", b"bravo v2",
                                               modifiedTime="2026-03-01T00:00:00.000Z"))
        (self.root / "Docs/b.txt").write_bytes(b"bravo, edited here")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "UNCHANGED")
        self.assertEqual(statuses["Docs/b.txt"], "CONFLICT")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "UNCHANGED")

    def test_destination_must_be_empty_or_managed(self):
        other = Path(self.temp.name) / "Other"
        other.mkdir()
        (other / "file").write_text("x")
        with self.assertRaises(ValueError):
            savegdrive.initialize_managed_destination(other)
        self.assertEqual(savegdrive.initialize_managed_destination(self.root), self.root)

    def test_folder_inside_a_managed_folder_is_refused_and_never_sent(self):
        self.run_sync()
        # --download-path ~/GDrive/Work (or f): refused, before anything is created.
        with self.assertRaisesRegex(ValueError, "already synced by SaveGDrive"):
            savegdrive.initialize_managed_destination(self.root / "Work")
        self.assertFalse((self.root / "Work").exists())
        self.assertEqual(savegdrive.FOLDER_LOCK["path"], self.root)
        # One made before this check: the outer folder's sync never sends it to Drive.
        work = self.root / "Work"
        (work / "Docs").mkdir(parents=True)
        state = {"version": savegdrive.STATE_VERSION, "files": {}}
        (work / savegdrive.STATE_FILE_NAME).write_text(json.dumps(state))
        (work / "Docs/a.txt").write_bytes(b"alpha")
        statuses, _ = self.run_sync()
        self.assertEqual([path for path in statuses if path.split("/")[0] == "Work"], [])
        self.assertNotIn(savegdrive.STATE_FILE_NAME, {item["name"] for item in self.drive.items.values()})

    def test_two_runs_never_sync_the_same_folder(self):
        # This run holds self.root: another process cannot take it.
        script = "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDONLY); fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"
        other_run = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, text=True)
        self.assertNotEqual(other_run.returncode, 0)
        self.assertIn("BlockingIOError", other_run.stderr)
        # A folder another run holds is refused, before its state is read or written.
        busy = Path(self.temp.name) / "Busy"
        savegdrive.initialize_managed_destination(busy)
        savegdrive.initialize_managed_destination(self.root)  # f back: Busy is released
        state_before = (busy / savegdrive.STATE_FILE_NAME).read_bytes()
        fd = os.open(busy, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(ValueError, "Another SaveGDrive run is using this folder"):
            savegdrive.initialize_managed_destination(busy)
        self.assertEqual((busy / savegdrive.STATE_FILE_NAME).read_bytes(), state_before)
        self.assertEqual(savegdrive.FOLDER_LOCK["path"], self.root)

    def test_gdrive_pull_folder_is_adopted(self):
        legacy = Path(self.temp.name) / "Legacy"
        legacy.mkdir()
        (legacy / "a.txt").write_text("a")
        state = {"version": savegdrive.STATE_VERSION, "files": {"id::a.txt": {"sha256": "x"}}}
        (legacy / savegdrive.LEGACY_STATE_FILE_NAME).write_text(json.dumps(state))
        (legacy / savegdrive.LEGACY_REMOTE_CACHE_NAME).write_text("{}")
        self.assertEqual(savegdrive.initialize_managed_destination(legacy), legacy)
        self.assertEqual(
            sorted(path.name for path in legacy.iterdir()),
            [savegdrive.STATE_FILE_NAME, "a.txt"],
        )
        self.assertEqual(savegdrive.load_state(legacy)["files"], state["files"])
        self.assertIn(savegdrive.LEGACY_REMOTE_CACHE_NAME, savegdrive.RESERVED_NAMES)


class CountingFiles(dict):
    """state["files"] that counts the passes over it."""
    passes = 0

    def items(self):
        self.passes += 1
        return super().items()

    def values(self):
        self.passes += 1
        return super().values()


class DeletionScalingTest(unittest.TestCase):
    """Deletions and moved folders read the state once, not once per file or folder (perf 1)."""

    def state(self, folders, per_folder):
        files = CountingFiles(
            (f"id-{folder}-{index}", {"path": f"Old{folder}/f{index}.txt"})
            for folder in range(folders) for index in range(per_folder)
        )
        files["keep"] = {"path": "Old0x/kept.txt"}  # same start as Old0, not below it
        return {"files": files, "folders": {"Old0": "d0", "Old0/sub": "s0", "Old0x": "k"}}

    def apply_local_trash(self, count):
        state = self.state(1, count)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Old0").mkdir()
            plan = [
                {"status": "TRASH_LOCAL", "kind": "FILE", "item": None, "state_key": f"id-0-{index}",
                 "relative_path": Path(f"Old0/f{index}.txt"), "destination": root / f"Old0/f{index}.txt",
                 "local_sha256": savegdrive.hashlib.sha256(b"").hexdigest()}
                for index in range(count)
            ]
            for entry in plan:
                entry["destination"].write_bytes(b"")  # still the synced copy when the trash runs
            with mock.patch.object(savegdrive, "trash_local", lambda path: None), \
                    mock.patch.object(savegdrive, "save_state", lambda *args: None), \
                    mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
                savegdrive.apply_plan(None, plan, root, state)
        self.assertEqual(dict(state["files"]), {"keep": {"path": "Old0x/kept.txt"}})
        return state["files"].passes

    def test_apply_plan_forgets_deletions_in_one_pass(self):
        self.assertEqual(self.apply_local_trash(3), self.apply_local_trash(40))

    def test_forget_all_below_drops_paths_and_everything_below(self):
        state = self.state(3, 2)
        savegdrive.forget_all_below(state, [Path("Old0"), Path("Old1/f0.txt")])
        self.assertEqual(sorted(record["path"] for record in state["files"].values()),
                         ["Old0x/kept.txt", "Old1/f1.txt", "Old2/f0.txt", "Old2/f1.txt"])
        self.assertEqual(state["folders"], {"Old0x": "k"})

    def plan_moved_folders(self, count):
        state = self.state(count, 2)
        selections = [
            {"item": {"id": f"d{folder}", "name": f"Old{folder}", "mimeType": FOLDER,
                      "capabilities": {"canTrash": True, "canAddChildren": True}}, "relative_parent": Path()}
            for folder in range(count)
        ]
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(savegdrive, "prefetch_folders", lambda *args: None), \
                mock.patch.object(savegdrive, "list_children", lambda *args: []), \
                mock.patch.object(savegdrive, "remote_subtree_unchanged", lambda *args: True):
            state["files"].passes = 0
            plan = savegdrive.build_plan(None, selections, [Path()], Path(temp), state, {})
        self.assertEqual({entry["status"] for entry in plan if entry["kind"] == "FOLDER"}, {"TRASH_REMOTE"})
        return state["files"].passes

    def test_build_plan_reads_the_state_once_for_all_folders(self):
        self.assertEqual(self.plan_moved_folders(2), self.plan_moved_folders(30))


class CountingPath(type(Path())):
    """A trashed folder path that counts the comparisons made with it."""
    comparisons = 0

    def __eq__(self, other):
        CountingPath.comparisons += 1
        return super().__eq__(other)

    __hash__ = type(Path()).__hash__


class TrashedFoldersScalingTest(unittest.TestCase):
    """Local paths are checked against the folders deleted here by lookup, not one by one (perf 3)."""

    def local_entries(self, deleted):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for index in range(10):
                (root / "Kept" / f"f{index}.txt").parent.mkdir(parents=True, exist_ok=True)
                (root / "Kept" / f"f{index}.txt").write_text("x")
            (root / "Twice" / "inside.txt").parent.mkdir(parents=True)  # two Drive folders on this path
            (root / "Twice" / "inside.txt").write_text("x")
            plan = [
                {"status": "TRASH_REMOTE", "kind": "FOLDER", "item": {"id": f"g{index}"},
                 "relative_path": CountingPath(f"Gone{index}")}
                for index in range(deleted)
            ]
            plan.append({"status": "CONFLICT", "kind": "FOLDER", "item": {"id": "t"}, "duplicate": True,
                         "relative_path": CountingPath("Twice")})
            state = {
                "files": {f"s{index}": {"path": f"Gone0/old{index}.txt"} for index in range(10)}
                | {"other": {"path": "Elsewhere/old.txt"}},
                "folders": {},
            }
            CountingPath.comparisons = 0
            savegdrive.add_local_entries(plan, root, state, [Path()], {Path(): "root"})
        new_paths = sorted(str(entry["relative_path"]) for entry in plan if entry["item"] is None)
        self.assertEqual(new_paths, ["Kept"] + [f"Kept/f{index}.txt" for index in range(10)])
        self.assertEqual(sorted(state["files"]), [f"s{index}" for index in range(10)])  # kept below a trashed one
        return CountingPath.comparisons

    def test_local_paths_are_not_compared_with_each_deleted_folder(self):
        self.assertEqual(self.local_entries(2), self.local_entries(40))


class DriveCallBatchingTest(unittest.TestCase):
    """--again and planning fetch saved items and shortcut targets in batches, not one call each (perf 4)."""

    def counting_gets(self, drive):
        gets = []
        real = drive.get

        def get(fileId, **kwargs):
            gets.append(fileId)
            return real(fileId, **kwargs)

        drive.get = get
        drive.batches.clear()
        return gets

    def test_again_gets_the_saved_items_in_batches(self):
        drive = FakeDrive([{"id": "docs", "name": "Docs", "mimeType": FOLDER, "parent": "root"}]
                          + [file_item(f"f{index}", f"f{index}.txt", "docs", b"x") for index in range(120)])
        state = {"last_selection": {"scopes": [], "items": [
            {"id": f"f{index}", "relative_parent": "Docs", "path": f"Docs/f{index}.txt",
             "parents": ["docs"], "parent_id": "docs"}
            for index in range(120)
        ]}}
        gets = self.counting_gets(drive)
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            again, _ = savegdrive.last_selection(drive, state, {})
        self.assertEqual(len(again), 120)
        self.assertEqual(len(gets), 120)
        self.assertEqual(drive.batches, [50, 50, 20])  # every get went through a batch

    def test_plan_resolves_file_shortcuts_in_batches(self):
        drive = FakeDrive([{"id": "links", "name": "Links", "mimeType": FOLDER, "parent": "root"}] + [
            item for index in range(60) for item in (
                file_item(f"t{index}", f"target{index}.txt", "elsewhere", f"t{index}".encode()),
                {"id": f"s{index}", "name": f"Link {index}.txt", "mimeType": savegdrive.SHORTCUT_MIME_TYPE,
                 "parent": "links", "shortcutDetails": {"targetId": f"t{index}", "targetMimeType": "text/plain"}},
            )
        ])
        cache = {}
        nodes, _ = savegdrive.collect_drive_tree(drive, "root", cache)
        links = next(node for node in nodes if node["display_path"] == "Links")
        selections = []
        savegdrive.add_selected_item(selections, links["item"], links["relative_parent"])
        gets = self.counting_gets(drive)
        with tempfile.TemporaryDirectory() as temp:
            plan = savegdrive.build_plan(drive, selections, [], Path(temp), {"files": {}, "folders": {}}, cache)
        files = {entry["relative_path"].as_posix(): entry for entry in plan if entry["kind"] == "FILE"}
        self.assertEqual(len(files), 60)
        self.assertEqual(files["Links/Link 7.txt"]["item"]["md5Checksum"], drive.items["t7"]["md5Checksum"])
        self.assertTrue(files["Links/Link 7.txt"]["item"]["readonly"])
        self.assertEqual(len(gets), 60)
        self.assertEqual(drive.batches, [50, 10])  # every get went through a batch

    def test_trash_restore_and_delete_handle_every_batch(self):
        # A sort of more than API_BATCH_SIZE photos: no batch after the first may be skipped (tests 47).
        ids = [f"f{index}" for index in range(60)]
        drive = FakeDrive([file_item(item_id, f"{item_id}.jpg", "root", b"x") for item_id in ids])
        for run, check in (
            (savegdrive.trash_items, lambda: all(drive.items[item_id].get("trashed") for item_id in ids)),
            (savegdrive.restore_items, lambda: not any(drive.items[item_id]["trashed"] for item_id in ids)),
            (savegdrive.delete_items, lambda: not drive.items),
        ):
            drive.batches.clear()
            self.assertEqual(run(drive, ids), {}, run.__name__)
            self.assertTrue(check(), run.__name__)
            self.assertEqual(drive.batches, [50, 10], run.__name__)


class ApplyPlanPhasesTest(unittest.TestCase):
    """apply_plan's phases, each tested on its own (maint 1, maint 3)."""

    def test_settle_untransferred_files_counts_kept_files_and_returns_transfers(self):
        plan = [
            {"kind": "FILE", "status": "CONFLICT", "relative_path": Path("a.txt")},
            {"kind": "FILE", "status": "LOCAL_ONLY", "relative_path": Path("b.txt")},
            {"kind": "FILE", "status": "NEW", "relative_path": Path("c.txt")},
            {"kind": "FILE", "status": "UPLOAD_NEW", "relative_path": Path("d.txt")},
            {"kind": "FOLDER", "status": "NEW", "relative_path": Path("e")},
        ]
        results = savegdrive.Counter()
        transfers = savegdrive.settle_untransferred_files(plan, {"files": {}}, results, mock.Mock())
        self.assertEqual([entry["relative_path"] for entry in transfers], [Path("c.txt"), Path("d.txt")])
        self.assertEqual(results, {"FILE_CONFLICT": 1, "FILE_LOCAL_ONLY": 1})

    def test_prune_emptied_dirs_is_the_one_pruning_and_stops_at_drive_folders(self):
        self.assertFalse(hasattr(savegdrive, "prune_empty_dirs"))  # one implementation of the pruning
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "Kept/Gone/Deeper").mkdir(parents=True)
            (root / "Other/Busy").mkdir(parents=True)
            (root / "Other/Busy/file.txt").write_text("x")
            (root / "Top").mkdir()
            state = {"folders": {"Kept": "k", "Kept/Gone": "g", "Kept/Gone/Deeper": "d", "Top": "t"}}
            savegdrive.prune_emptied_dirs(
                root, {Path("Kept/Gone/Deeper"), Path("Other/Busy"), Path("Top")}, {Path("Kept")}, state)
            self.assertTrue((root / "Kept").is_dir())  # still on Drive: kept, even empty
            self.assertFalse((root / "Kept/Gone").exists())
            self.assertTrue((root / "Other/Busy/file.txt").exists())
            self.assertFalse((root / "Top").exists())
            self.assertTrue(root.is_dir())  # never the destination root itself
        self.assertEqual(state["folders"], {"Kept": "k"})


class SelectionScalingTest(unittest.TestCase):
    """Big selections: a, --again and d index the selection instead of rescanning it per item (perf 2)."""

    def count_paths(self, run):
        calls = [0]
        real = savegdrive.selected_path

        def counting(entry):
            calls[0] += 1
            return real(entry)

        with mock.patch.object(savegdrive, "selected_path", counting):
            result = run()
        return calls[0], result

    def candidates(self, count):
        folders = [{"item": {"name": f"D{index}", "mimeType": FOLDER}, "relative_parent": Path()}
                   for index in range(count)]
        files = [{"item": {"name": f"f{index}.txt", "mimeType": "text/plain"}, "relative_parent": Path(f"D{index}")}
                 for index in range(count)]
        loose = [{"item": {"name": f"g{index}.txt", "mimeType": "text/plain"}, "relative_parent": Path("E")}
                 for index in range(count)]
        # Files first, then their folders (which replace them), then files under the folders (skipped).
        return files + folders + files + loose

    def test_add_selected_items_matches_one_by_one(self):
        one_by_one, at_once = [], []
        for candidate in self.candidates(5):
            savegdrive.add_selected_item(one_by_one, candidate["item"], candidate["relative_parent"])
        added = savegdrive.add_selected_items(at_once, [dict(candidate) for candidate in self.candidates(5)])
        paths = [savegdrive.selected_path(entry) for entry in one_by_one]
        self.assertEqual([savegdrive.selected_path(entry) for entry in at_once], paths)
        self.assertEqual(sorted(savegdrive.selected_path(entry) for entry in added), sorted(paths))
        self.assertEqual(savegdrive.add_selected_items(at_once, self.candidates(5)), [])

    def test_add_selected_items_is_linear(self):
        small, _ = self.count_paths(lambda: savegdrive.add_selected_items([], self.candidates(20)))
        big, _ = self.count_paths(lambda: savegdrive.add_selected_items([], self.candidates(400)))
        self.assertLess(big, small * 30)  # 20x the items; one by one, it was 400x

    def last_selection(self, count):
        drive = FakeDrive(
            [{"id": "f", "name": "F", "mimeType": FOLDER, "parent": "root"}]
            + [file_item(f"x{index}", f"x{index}.txt", "f", b"x") for index in range(count)]
        )
        saved = [{"id": f"x{index}", "path": f"F/x{index}.txt", "relative_parent": "F"} for index in range(count)]
        saved.append({"id": "x0", "path": "F/x0.txt", "relative_parent": "F"})  # twice: added once
        state = {"last_selection": {"roots": [], "items": saved, "scopes": []}}
        with mock.patch.object(savegdrive, "note_drives", lambda items: None):
            calls, (selections, _) = self.count_paths(lambda: savegdrive.last_selection(drive, state, {}))
        self.assertEqual(len(selections), count)
        return calls

    def test_last_selection_is_linear(self):
        self.assertLess(self.last_selection(400), self.last_selection(20) * 30)

    def fully_selected_view(self, count):
        base = Path("Shared with me")
        items = [{"id": f"i{index}", "name": f"f{index}.txt", "mimeType": "text/plain"} for index in range(count)]
        selections = []
        savegdrive.add_selected_items(selections, [{"item": item, "relative_parent": base} for item in items])
        calls, scopes = self.count_paths(lambda: savegdrive.build_local_scopes(selections, {base: items}, (Path(), base)))
        self.assertEqual(scopes, [base])
        partial = savegdrive.build_local_scopes(selections[1:], {base: items}, (Path(), base))
        self.assertEqual(partial, [])
        return calls

    def test_fully_selected_view_is_linear(self):
        # perf 14: every top item of the view selected (a, then d or y) rescanned the selection per item.
        self.assertLess(self.fully_selected_view(400), self.fully_selected_view(20) * 30)

    def test_compact_scopes_is_linear(self):
        calls = [0]
        real = savegdrive.scope_covers

        def counting(parent, scope):
            calls[0] += 1
            return real(parent, scope)

        scopes = [Path(f"D{index}") for index in range(300)] + [Path(f"D{index}/sub") for index in range(300)]
        with mock.patch.object(savegdrive, "scope_covers", counting):
            compacted = savegdrive.compact_scopes(scopes)
        self.assertEqual(compacted, sorted(scopes[:300], key=lambda path: path.as_posix()))
        self.assertLess(calls[0], 1000)  # it compared each scope with each kept one: 135 000 calls
        self.assertEqual(savegdrive.compact_scopes([Path(), Path("Docs"), Path("Shared with me/x")]),
                         [Path(), Path("Shared with me/x")])


class UploadTest(unittest.TestCase):
    def test_upload_file_sends_every_chunk(self):
        class Request:
            def __init__(self):
                self.calls = 0

            def next_chunk(self, num_retries=0):
                self.calls += 1
                if self.calls < 3:
                    return mock.Mock(resumable_progress=4 * self.calls), None
                return None, {"id": "new", "name": "x.bin"}

        request = Request()
        service = mock.Mock()
        service.files.return_value.create.return_value = request
        sent = []
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.bin"
            path.write_bytes(b"0123456789")
            os.utime(path, (1563098400.25, 1563098400.25))
            item = savegdrive.upload_file(service, path, name="x.bin", parent_id="docs", on_bytes=sent.append)
        self.assertEqual(item["id"], "new")
        self.assertEqual(sum(sent), 10)
        body = service.files.return_value.create.call_args.kwargs["body"]
        self.assertEqual(body, {"name": "x.bin", "parents": ["docs"], "modifiedTime": "2019-07-14T10:00:00.250Z"})

    def test_upload_file_keeps_the_local_date(self):
        # Without modifiedTime, Drive dates the file at the upload time, and that date comes back here.
        service = mock.Mock()
        files = service.files.return_value
        files.update.return_value.next_chunk.return_value = (None, {"id": "a"})
        files.create.return_value.next_chunk.return_value = (None, {"id": "b"})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "notes.txt"
            path.write_bytes(b"x")
            os.utime(path, (1563098400.25, 1563098400.25))
            savegdrive.upload_file(service, path, file_id="a")
            savegdrive.upload_file(service, path, name="notes.txt", parent_id="docs")
        self.assertEqual(files.update.call_args.kwargs["body"], {"modifiedTime": "2019-07-14T10:00:00.250Z"})
        self.assertEqual(files.create.call_args.kwargs["body"]["modifiedTime"], "2019-07-14T10:00:00.250Z")
        item = {"modifiedTime": "2019-07-14T10:00:00.250Z"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "notes.txt"
            path.write_bytes(b"x")
            savegdrive.set_remote_mtime(path, item)
            self.assertEqual(path.stat().st_mtime, 1563098400.25)

    def test_create_remote_folder_makes_a_folder_in_its_parent(self):
        # Without parents, Drive puts the folder at the top of My Drive; without mimeType, it makes a file.
        service = mock.Mock()
        files = service.files.return_value
        savegdrive.remote_folder_request(service, "Ideas", "docs")
        kwargs = files.create.call_args.kwargs
        self.assertEqual(kwargs["body"], {"name": "Ideas", "parents": ["docs"], "mimeType": savegdrive.FOLDER_MIME_TYPE})
        self.assertTrue(kwargs["supportsAllDrives"])

    def test_upload_stops_on_ctrl_c(self):
        import threading

        stop = threading.Event()
        stop.set()
        service = mock.Mock()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.bin"
            path.write_bytes(b"x")
            with self.assertRaises(savegdrive.Stopped):
                savegdrive.upload_file(service, path, file_id="a", stop=stop)


class DownloadTest(unittest.TestCase):
    def test_download_request_exports_google_files_and_gets_others(self):
        service = mock.Mock()
        files = service.files.return_value
        savegdrive.download_request(service, {"id": "d1", "mimeType": DOC})
        files.export_media.assert_called_once_with(
            fileId="d1", mimeType=savegdrive.EXPORT_FORMATS[DOC][0]
        )
        files.get_media.assert_not_called()

        files.reset_mock()
        shortcut = {
            "id": "s1",
            "mimeType": savegdrive.SHORTCUT_MIME_TYPE,
            "shortcutDetails": {"targetId": "d2", "targetMimeType": DOC},
        }
        savegdrive.download_request(service, shortcut)
        files.export_media.assert_called_once_with(
            fileId="d2", mimeType=savegdrive.EXPORT_FORMATS[DOC][0]
        )
        files.get_media.assert_not_called()

        files.reset_mock()
        savegdrive.download_request(service, {"id": "f1", "mimeType": "image/jpeg"})
        files.get_media.assert_called_once_with(fileId="f1", supportsAllDrives=True)
        files.export_media.assert_not_called()

    def test_fetch_media_reads_every_chunk(self):
        chunks = [b"aaa", b"bbb", b"cc"]

        class Downloader:
            def __init__(self, file_handle, request, chunksize):
                self.file_handle = file_handle
                self.calls = 0

            def next_chunk(self, num_retries=0):
                self.file_handle.write(chunks[self.calls])
                self.calls += 1
                return None, self.calls == len(chunks)

        handle = io.BytesIO()
        with mock.patch.object(savegdrive, "MediaIoBaseDownload", Downloader):
            savegdrive.fetch_media(mock.Mock(), {"id": "f1", "mimeType": "image/jpeg"}, handle)
        self.assertEqual(handle.getvalue(), b"aaabbbcc")


class LocalTrashTest(unittest.TestCase):
    def test_trash_local_sends_to_system_trash_without_deleting(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "folder"
            folder.mkdir()
            (folder / "inside.txt").write_text("kept")
            file = Path(temp) / "file.txt"
            file.write_text("kept")
            with mock.patch.object(savegdrive, "send2trash") as send2trash:
                savegdrive.trash_local(folder)
                savegdrive.trash_local(file)
            self.assertEqual(send2trash.call_args_list, [mock.call(str(folder)), mock.call(str(file))])
            self.assertEqual((folder / "inside.txt").read_text(), "kept")
            self.assertEqual(file.read_text(), "kept")


class SignInTest(unittest.TestCase):
    def test_read_only_token_asks_for_full_access(self):
        with tempfile.TemporaryDirectory() as temp:
            token = Path(temp) / "token.json"
            token.write_text(
                '{"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s", '
                '"scopes": ["https://www.googleapis.com/auth/drive.readonly"], "expiry": "2999-01-01T00:00:00Z"}'
            )
            new = mock.Mock(to_json=lambda: "{}")
            with mock.patch.object(savegdrive, "TOKEN_PATH", token), \
                    mock.patch.object(sys.stdin, "isatty", lambda: True), \
                    mock.patch.object(savegdrive, "sign_in", return_value=new) as sign_in:
                self.assertIs(savegdrive.authenticate(), new)
            self.assertIn("full Drive access", sign_in.call_args[0][0])

    def authenticate_expired(self, refresh_error, isatty):
        """authenticate with an expired token whose refresh raises refresh_error."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        token = Path(temp.name) / "token.json"
        token.write_text(
            '{"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s", '
            '"scopes": ["https://www.googleapis.com/auth/drive"], "expiry": "2000-01-01T00:00:00Z"}'
        )
        output = io.StringIO()
        new = mock.Mock(to_json=lambda: "{}")
        with mock.patch.object(savegdrive, "TOKEN_PATH", token), \
                mock.patch.object(savegdrive.Credentials, "refresh", side_effect=refresh_error), \
                mock.patch.object(savegdrive, "console", Console(file=output, width=200)), \
                mock.patch.object(sys.stdin, "isatty", lambda: isatty), \
                mock.patch.object(savegdrive, "sign_in", return_value=new) as sign_in:
            try:
                result = savegdrive.authenticate()
            except SystemExit as error:
                result = error
        return result, sign_in, output.getvalue(), token.read_text()

    def test_unattended_run_exits_instead_of_waiting_for_a_browser_sign_in(self):
        from google.auth.exceptions import RefreshError
        result, sign_in, output, token = self.authenticate_expired(
            RefreshError("invalid_grant: Token has been expired or revoked."), isatty=False
        )
        sign_in.assert_not_called()
        self.assertIsInstance(result, SystemExit)
        self.assertEqual(result.code, 1)
        self.assertIn("session expired", output)
        self.assertIn("in a terminal", output)
        self.assertIn('"refresh_token": "r"', token)  # the saved token is left as it was

    def test_network_error_on_refresh_is_not_an_expired_session(self):
        from google.auth.exceptions import TransportError
        for isatty in (False, True):
            with self.subTest(isatty=isatty):
                result, sign_in, output, _ = self.authenticate_expired(TransportError("DNS down"), isatty)
                sign_in.assert_not_called()
                self.assertIsInstance(result, SystemExit)
                self.assertEqual(result.code, 1)
                self.assertIn("Could not reach Google", output)

    def test_expired_session_in_a_terminal_still_signs_in(self):
        from google.auth.exceptions import RefreshError
        result, sign_in, _, token = self.authenticate_expired(RefreshError("invalid_grant"), isatty=True)
        self.assertIs(result, sign_in.return_value)
        self.assertIn("session expired", sign_in.call_args[0][0])
        self.assertEqual(token, "{}")

    def test_saved_token_is_private_and_kept_when_the_write_fails(self):
        # security 7: token.json carries a full-Drive refresh token, so only its owner may read it.
        # bugs 42: a failed write (disk full) must leave the previous token.json as it was.
        from google.auth.exceptions import RefreshError
        old_umask = os.umask(0o022)
        try:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            token = Path(temp.name) / "token.json"
            token.write_text('{"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s", '
                             '"scopes": ["https://www.googleapis.com/auth/drive"], "expiry": "2000-01-01T00:00:00Z"}')
            token.chmod(0o644)  # a token.json saved by an earlier version
            with mock.patch.object(savegdrive, "TOKEN_PATH", token), \
                    mock.patch.object(savegdrive.Credentials, "refresh", side_effect=RefreshError("invalid_grant")), \
                    mock.patch.object(savegdrive, "console", Console(file=io.StringIO())), \
                    mock.patch.object(sys.stdin, "isatty", lambda: True), \
                    mock.patch.object(savegdrive, "sign_in", return_value=mock.Mock(to_json=lambda: "{}")):
                savegdrive.authenticate()
            self.assertEqual(token.read_text(), "{}")
            self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
            token.unlink()
            savegdrive.save_token(mock.Mock(to_json=lambda: '{"token": "new"}'), token)
            self.assertEqual(token.read_text(), '{"token": "new"}')
            self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
            savegdrive.save_token(mock.Mock(to_json=lambda: '{"token": "newer"}'), token)
            self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
            # A lone surrogate cannot be encoded: the write fails after the file is opened, like ENOSPC.
            with self.assertRaises(UnicodeEncodeError):
                savegdrive.save_token(mock.Mock(to_json=lambda: '{"token": "\udc80"}'), token)
            self.assertEqual(token.read_text(), '{"token": "newer"}')
            self.assertEqual(sorted(path.name for path in Path(temp.name).iterdir()), ["token.json"])
        finally:
            os.umask(old_umask)

    def test_unreadable_token_signs_in_again_and_names_the_file(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        token = Path(temp.name) / "token.json"
        token.write_text("")  # left empty by a write that failed in an older version
        for isatty in (False, True):
            with self.subTest(isatty=isatty):
                output = io.StringIO()
                new = mock.Mock(to_json=lambda: "{}")
                with mock.patch.object(savegdrive, "TOKEN_PATH", token), \
                        mock.patch.object(savegdrive, "console", Console(file=output, width=200)), \
                        mock.patch.object(sys.stdin, "isatty", lambda: isatty), \
                        mock.patch.object(savegdrive, "sign_in", return_value=new) as sign_in:
                    try:
                        result = savegdrive.authenticate()
                    except SystemExit as error:
                        result = error
                if isatty:
                    self.assertIs(result, new)
                    self.assertIn("token.json", sign_in.call_args[0][0])
                    self.assertEqual(token.read_text(), "{}")
                else:
                    sign_in.assert_not_called()
                    self.assertIsInstance(result, SystemExit)
                    self.assertIn("token.json", output.getvalue())


class KeyListTest(unittest.TestCase):
    NAMES = {"question_mark": "?", "slash": "/", "escape": "esc", "B": "shift+b", "T": "shift+t", "X": "shift+x",
             "up": "↑", "down": "↓", "left": "←", "right": "→"}

    def listed(self, context):
        return {
            token
            for _, entries in savegdrive.KEYS[context][1]
            for keys, _, description in entries
            for token in keys.split() + [description]
        }

    def test_every_binding_is_listed(self):
        screens = {
            ("tree", "trash"): [savegdrive.DriveSelectorApp, savegdrive.DriveTree],
            ("confirm", "confirm-word"): [savegdrive.ConfirmScreen],
            "preview": [savegdrive.PreviewScreen],
            "preview-again": [savegdrive.StandalonePreviewScreen],
            "folder": [savegdrive.DestinationSetupScreen],
            "help": [savegdrive.HelpScreen],
        }
        for context, classes in screens.items():
            contexts = context if isinstance(context, tuple) else (context,)
            listed = set().union(*(self.listed(name) for name in contexts))
            text = " ".join(listed)
            for cls in classes:
                for binding in cls.BINDINGS:
                    for key in binding.key.split(","):
                        name = self.NAMES.get(key, key)
                        self.assertTrue(name in listed or name in text, f"{context}: {key} is not listed")

    def test_help_mentions_every_context(self):
        lines = "\n".join(savegdrive.help_lines())
        for title, _ in savegdrive.KEYS.values():
            self.assertIn(title.lower(), lines)


class SelectorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(savegdrive.TRASHED_FOLDERS.clear)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = savegdrive.initialize_managed_destination(Path(self.temp.name) / "GDrive")

    def make_app(self, drive=None, views=None, token=None):
        drive = drive or demo_drive()
        cache = {}
        views = views or [savegdrive.DriveView("My Drive", "root")]
        savegdrive.load_view(drive, views[0], cache)
        return savegdrive.DriveSelectorApp(views, self.root, savegdrive.load_state(self.root), drive, None, cache,
                                           token=token)

    def selected(self, app):
        return [savegdrive.selected_path(entry).as_posix() for entry in app.selections]

    async def test_drive_names_cannot_send_terminal_sequences(self):
        # security 5: the tree and the preview show a hostile name's ESC sequences instead of emitting them.
        drive = demo_drive()
        drive.items["evil"] = file_item("evil", "invoice.pdf\x1b]52;c;dHJlZS1jbGlw\x1b\\", "root", b"evil")
        app = self.make_app(drive)
        async with app.run_test(size=(120, 30)) as pilot:
            entry = next(node.data for node in app.tree_nodes.values() if node.data["item"]["id"] == "evil")
            label = next(node.label.plain for node in app.tree_nodes.values() if node.data is entry)
            self.assertNotIn("\x1b", label)
            self.assertIn("invoice.pdf\\x1b]52;c;", label)
            app.toggle_entry(entry)
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIsInstance(app.screen, savegdrive.PreviewScreen)
            table = app.screen.query_one("DataTable")
            items = [table.get_row_at(row)[1].plain for row in range(table.row_count)]
            self.assertTrue(any("invoice.pdf\\x1b]52" in item for item in items), items)
            self.assertFalse(any("\x1b" in item for item in items), items)

    async def test_select_expand_and_download(self):
        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.press("down", "space")  # Docs
            self.assertEqual(self.selected(app), ["Docs"])
            await pilot.press("right", "right")  # expand Docs, then its first child
            tree = app.query_one(Tree)
            self.assertEqual(tree.cursor_node.data["display_path"], "Docs/a.txt")
            await pilot.press("space")  # included by Docs: stays selected through the folder
            self.assertEqual(self.selected(app), ["Docs"])
            await pilot.press("left")  # back to Docs
            self.assertEqual(tree.cursor_node.data["display_path"], "Docs")
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIsInstance(app.screen, savegdrive.PreviewScreen)
            self.assertIn("to download", str(app.screen.query_one("#preview-summary").render()))
            table = app.screen.query_one("DataTable")
            self.assertEqual(table.row_count, 5)  # Docs/, a, b, Report, and Survey skipped
            await pilot.press("tab", "tab")  # everything, then each status: new
            self.assertEqual(table.row_count, 4)
            await pilot.press("escape")  # back to the tree, selection kept
            self.assertNotIsInstance(app.screen, savegdrive.PreviewScreen)
            self.assertEqual(self.selected(app), ["Docs"])
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIsInstance(app.screen, savegdrive.PreviewScreen)
            await pilot.press("n")  # n also goes back, selection kept, nothing applied
            self.assertNotIsInstance(app.screen, savegdrive.PreviewScreen)
            self.assertIsNone(app.return_value)
            self.assertEqual(self.selected(app), ["Docs"])
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("b", "a", "c", "z")  # no conflict here; tree keys do nothing behind the preview
            self.assertEqual(self.selected(app), ["Docs"])
            self.assertEqual(app.sort, "name")
            await pilot.press("y")
        result = app.return_value
        self.assertEqual([savegdrive.selected_path(entry).as_posix() for entry in result["selections"]], ["Docs"])
        self.assertEqual(result["destination"], self.root)
        self.assertEqual(len(result["plan"]), 5)

    async def test_checking_one_of_two_same_name_rows_takes_both(self):
        # Two "Finance" folders look the same in the tree: checking either takes both, so the plan
        # reports a conflict instead of syncing your folder with whichever was checked.
        drive = demo_drive()
        drive.items["twin"] = {"id": "twin", "name": "Docs", "mimeType": FOLDER, "parent": "root"}
        app = self.make_app(drive)
        async with app.run_test() as pilot:
            await pilot.press("down", "space")
            self.assertEqual(sorted(entry["item"]["id"] for entry in app.selections), ["docs", "twin"])
            await pilot.press("space")
            self.assertEqual(app.selections, [])
            await pilot.press("q")

    async def test_views_load_on_tab(self):
        drive = demo_drive()
        drive.items["t"] = file_item("t", "plan.txt", "team", b"plan")
        drive.shared_drives = [{"id": "team", "name": "Team"}]
        views = savegdrive.drive_views(drive, "root")
        app = self.make_app(drive, views)
        async with app.run_test() as pilot:
            await pilot.press("down", "space", "tab")  # Docs, then Shared with me (empty)
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIs(app.view, views[1])
            await pilot.press("tab")
            await app.workers.wait_for_complete()
            await pilot.pause()
            tree = app.query_one(Tree)
            self.assertEqual([node.data["display_path"] for node in tree.root.children], ["plan.txt"])
            await pilot.press("down", "space")
            self.assertEqual(self.selected(app), ["Docs", "Shared drives/Team/plan.txt"])
            self.assertIn("Team (1)", str(app.query_one("#views").render()))
            await pilot.press("shift+tab", "shift+tab")
            self.assertIs(app.view, views[0])
            await pilot.press("q")

    async def test_filter_select_all_and_clear(self):
        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.press("slash", *"beach", "enter")
            tree = app.query_one(Tree)
            visible = [node.data["display_path"] for node in tree.root.children]
            self.assertEqual(visible, ["Photos"])
            self.assertTrue(tree.root.children[0].is_expanded)
            await pilot.press("a")
            self.assertEqual(self.selected(app), ["Photos/beach.jpg"])
            await pilot.press("escape")  # clears the filter, keeps the selection
            self.assertFalse(app.query_one(Input).display)
            self.assertEqual(len(tree.root.children), 3)
            self.assertEqual(self.selected(app), ["Photos/beach.jpg"])
            await pilot.press("a")
            self.assertEqual(self.selected(app), ["Docs", "Photos", "notes.txt"])
            await pilot.press("a")  # everything selected: a unselects all
            self.assertEqual(self.selected(app), [])
            await pilot.press("slash", *"beach", "enter", "a", "a")  # same with a filter
            self.assertEqual(self.selected(app), [])
            await pilot.press("a", "escape", "c")
            self.assertEqual(self.selected(app), [])
            await pilot.press("escape")
        self.assertIsNone(app.return_value)

    async def test_filter_rebuilds_the_tree_once_typing_stops(self):
        # Each rebuild walks the whole tree (seconds on a large Drive): typing a filter must not
        # rebuild it after every character, only once typing stops or on enter.
        app = self.make_app()
        async with app.run_test() as pilot:
            rebuilds = []
            original = app.rebuild
            app.rebuild = lambda: (rebuilds.append(app.filter_terms[:]), original())
            await pilot.press("slash", *"beach")
            self.assertEqual(rebuilds, [])
            await pilot.pause(savegdrive.FILTER_DELAY + 0.2)
            self.assertEqual(rebuilds, [["beach"]])
            tree = app.query_one(Tree)
            self.assertEqual([node.data["display_path"] for node in tree.root.children], ["Photos"])
            await pilot.press("backspace", "backspace", "enter")  # enter applies at once
            self.assertEqual(rebuilds, [["beach"], ["bea"]])
            await pilot.pause(savegdrive.FILTER_DELAY + 0.2)
            self.assertEqual(len(rebuilds), 2)
            await pilot.press("slash", "x", "escape")  # escape clears without a late rebuild
            await pilot.pause(savegdrive.FILTER_DELAY + 0.2)
            self.assertEqual(rebuilds[2:], [[]])
            self.assertEqual(len(tree.root.children), 3)
            await pilot.press("escape")

    async def test_selection_relabels_only_the_nodes_whose_checkbox_changes(self):
        # Each set_label repaints (seconds for every node of a large unfolded tree): space, a and c
        # must relabel only the nodes whose checkbox changes, and get every one of them right.
        drive = demo_drive()
        drive.items["docs2"] = {"id": "docs2", "name": "Docs", "mimeType": FOLDER, "parent": "root"}
        drive.items["c"] = file_item("c", "c.txt", "docs2", b"charlie")
        app = self.make_app(drive)
        async with app.run_test(size=(120, 30)) as pilot:
            app.action_expand_all()
            await pilot.pause()
            labels = {index: node.label for index, node in app.tree_nodes.items()}
            ids = {index: node.data["item"]["id"] for index, node in app.tree_nodes.items()}

            def changed():
                relabeled = {ids[index] for index, node in app.tree_nodes.items() if node.label is not labels[index]}
                labels.update({index: node.label for index, node in app.tree_nodes.items()})
                return relabeled

            def boxes():
                return {ids[index]: node.label.plain[0] for index, node in app.tree_nodes.items()}

            entries = {ids[index]: node.data for index, node in app.tree_nodes.items()}
            app.toggle_entry(entries["docs"])  # and its twin Docs
            self.assertEqual(changed(), {"docs", "docs2", "a", "b", "report", "form", "c"})
            self.assertEqual(boxes(), {"docs": "☑", "docs2": "☑", "a": "◩", "b": "◩", "report": "◩",
                                       "form": "◩", "c": "◩", "photos": "☐", "p": "☐", "n": "☐"})
            app.toggle_entry(entries["a"])  # included by Docs: nothing changes
            self.assertEqual(changed(), set())
            app.action_select_all()
            self.assertEqual(changed(), {"photos", "p", "n"})
            app.action_clear_selection()
            self.assertEqual(changed(), set(ids.values()))
            self.assertEqual(set(boxes().values()), {"☐"})
            await pilot.press("escape")

    async def test_columns_are_aligned(self):
        app = self.make_app()
        async with app.run_test(size=(120, 20)) as pilot:
            app.action_expand_all()
            await pilot.pause()
            tree = app.query_one(Tree)
            lines = ["".join(segment.text for segment in tree.render_line(y)) for y in range(9)]
        columns = {line.index(" 5 B") for line in lines if " 5 B" in line}
        self.assertEqual(len(columns), 1, lines)

    async def test_long_names_fit_the_tree_width(self):
        # Names take the room the terminal gives: whole on a wide one, never pushing sizes off a narrow one.
        drive = demo_drive()
        drive.items["v4"] = file_item("v4", "Copy of Q3 marketing plan - final - reviewed by legal (v4).docx",
                                      "docs", b"four")
        drive.items["v5"] = file_item("v5", "Copy of Q3 marketing plan - final - reviewed by legal (v5).docx",
                                      "docs", b"five")
        app = self.make_app(drive)
        async with app.run_test(size=(220, 55)) as pilot:
            app.action_expand_all()
            await pilot.pause()
            labels = [node.label.plain for node in app.tree_nodes.values()]
            self.assertTrue(any("legal (v4).docx" in label for label in labels), labels)
            self.assertTrue(any("legal (v5).docx" in label for label in labels), labels)
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            tree = app.query_one(Tree)
            self.assertLessEqual(tree.virtual_size.width, tree.scrollable_content_region.width)
            lines = ["".join(segment.text for segment in tree.render_line(y)) for y in range(12)]
            self.assertTrue(any("…" in line and "4 B  2025-01-02" in line for line in lines), lines)

    async def test_names_cut_alike_in_every_view(self):
        # A shared drive, Shared with me or Trash has a base path: it is not tree depth, names keep their room.
        drive = demo_drive()
        drive.items["v4"] = file_item("v4", "Copy of Q3 marketing plan - final - reviewed by legal (v4).docx",
                                      "docs", b"four")
        labels = {}
        for base in (Path(), Path("Shared with me"), Path("Shared drives/Team")):
            app = self.make_app(drive, [savegdrive.DriveView("View", "root", base)])
            async with app.run_test(size=(80, 24)) as pilot:
                app.action_expand_all()
                await pilot.pause()
                labels[base] = sorted(node.label.plain for node in app.tree_nodes.values())
        self.assertEqual(labels[Path("Shared with me")], labels[Path()])
        self.assertEqual(labels[Path("Shared drives/Team")], labels[Path()])

    async def test_preview_keeps_size_in_view_with_long_paths(self):
        path = "Clients/Acme/2026/Q3/Campaign/Assets/Copy of Q3 marketing plan - final - reviewed by legal.docx"
        plan = [{
            "status": "KEEP_BOTH", "relative_path": Path(path), "kind": "FILE",
            "item": {"name": Path(path).name, "size": "1234"}, "destination": self.root / path,
            "local_copy": self.root / "Copy (local).docx", "copy_goes_up": True,
        }]
        app = savegdrive.PreviewApp(plan, self.root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#preview-table")
            self.assertLessEqual(table.virtual_size.width, table.scrollable_content_region.width)
            item = table.get_row_at(0)[1].plain
            self.assertTrue(item.startswith("…") and item.endswith("(yours → Copy (local).docx)"), item)
            await pilot.resize_terminal(220, 55)
            await pilot.pause()
            self.assertEqual(table.get_row_at(0)[1].plain,
                             f"{path}  (yours → Copy (local).docx)")

    async def test_preview_size_column_shows_the_local_file_for_an_upload(self):
        # bugs 47: the SIZE cell of an upload came from the Drive item, the version being replaced.
        (self.root / "beach.jpg").write_bytes(b"x" * 5_000_000)
        (self.root / "new.txt").write_bytes(b"n" * 1500)
        plan = [
            {"status": "UPLOAD", "relative_path": Path("beach.jpg"), "kind": "FILE",
             "item": {"name": "beach.jpg", "size": "4"}, "destination": self.root / "beach.jpg"},
            {"status": "UPLOAD_NEW", "relative_path": Path("new.txt"), "kind": "FILE",
             "item": None, "destination": self.root / "new.txt"},
            {"status": "NEW", "relative_path": Path("down.txt"), "kind": "FILE",
             "item": {"name": "down.txt", "size": "2000"}, "destination": self.root / "down.txt"},
        ]
        app = savegdrive.PreviewApp(plan, self.root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#preview-table")
            sizes = {table.get_row_at(index)[1].plain: table.get_row_at(index)[-1].plain
                     for index in range(table.row_count)}
        self.assertEqual(sizes, {"beach.jpg": "5.0 MB", "new.txt": "1.5 KB", "down.txt": "2.0 KB"})

    async def test_preview_survives_a_file_to_send_deleted_after_the_plan(self):
        (self.root / "kept.txt").write_bytes(b"x" * 7)
        (self.root / "draft.txt").write_bytes(b"draft")
        plan = [{
            "status": "UPLOAD_NEW", "relative_path": Path(name), "kind": "FILE",
            "item": None, "destination": self.root / name,
        } for name in ("kept.txt", "draft.txt")]
        app = savegdrive.PreviewApp(plan, self.root)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            (self.root / "draft.txt").unlink()  # gone while the preview is open
            await pilot.press("tab")
            await pilot.pause()
            summary = str(app.screen.query_one("#preview-summary").render())
            self.assertIn("7 B to send to Drive", summary)
        self.assertEqual(savegdrive.upload_size(plan, self.root), 7)

    async def test_preview_flags_a_download_the_disk_cannot_hold(self):
        # tests 60: the summary line turns red with a warning only when the known download exceeds free space.
        plan = [{
            "status": "NEW", "relative_path": Path("big.iso"), "kind": "FILE",
            "item": {"name": "big.iso", "size": "19"}, "destination": self.root / "big.iso",
        }]
        for free, warned in ((2_000_000, False), (10, True)):
            app = savegdrive.PreviewApp(plan, self.root)
            with mock.patch.object(savegdrive.shutil, "disk_usage", return_value=mock.Mock(free=free)):
                async with app.run_test(size=(120, 40)) as pilot:
                    await pilot.pause()
                    summary = app.screen.query_one("#preview-summary").render()
            line = next(line for line in summary.plain.splitlines() if "to download" in line)
            self.assertEqual(line == "! 19 B to download · 10 B free: not enough disk space", warned, line)
            red = [span for span in summary.spans if "red" in str(span.style)]
            self.assertEqual(bool(red), warned, summary.spans)

    async def test_preview_shows_the_local_folder_in_80x24(self):
        destination = Path.home() / "Documents" / "Google Drive"
        plan = [{
            "status": "NEW", "relative_path": Path("a.txt"), "kind": "FILE",
            "item": {"name": "a.txt", "size": "10"}, "destination": destination / "a.txt",
        }]
        app = savegdrive.PreviewApp(plan, destination, "jean.dupont@gmail.com")
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            # A narrow header cuts the account, not where the sync writes and deletes.
            header = f"{app.title} — {app.screen.sub_title}"[:80]
            self.assertIn("syncs with ~/Documents/Google Drive", header)
            summary = str(app.screen.query_one("#preview-summary").render())
            self.assertIn("Destination: ~/Documents/Google Drive", summary)

    async def test_preview_rows_stay_visible_in_80x24(self):
        def entry(status, name, **extra):
            return {
                "status": status, "relative_path": Path(name), "kind": "FILE",
                "item": {"name": name, "size": "10"}, "destination": self.root / name, **extra,
            }
        plan = [  # every note at once: they would push the table out of view
            entry("CONFLICT", "a.txt"),
            entry("KEEP_BOTH", "b.txt", local_copy=self.root / "b (local).txt", copy_goes_up=False),
            entry("TRASH_REMOTE", "c.txt"),
            entry("TRASH_LOCAL", "d.txt"),
            entry("LOCAL_ONLY", "e.txt", item=None),
        ]
        app = savegdrive.PreviewApp(plan, self.root)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#preview-table")
            self.assertGreaterEqual(table.region.height, 6)  # border, header and some rows
            keys = app.screen.query_one(savegdrive.KeyBar)
            self.assertLessEqual(keys.region.bottom, 24)

    async def test_session_destination(self):
        app = self.make_app()
        with tempfile.TemporaryDirectory() as temp:
            async with app.run_test() as pilot:
                await pilot.press("f")
                screen = app.screen
                self.assertIsInstance(screen, savegdrive.DestinationSetupScreen)
                # tests 53: a non-empty folder without a state file is refused, the screen stays open.
                (Path(temp) / "Documents").mkdir()
                (Path(temp) / "Documents" / "taxes.pdf").write_text("x")
                original = app.destination
                screen.query_one("#parent-path", Input).value = temp
                screen.query_one("#folder-name", Input).value = "Documents"
                await pilot.press("ctrl+s")
                await pilot.pause(0.3)
                self.assertIs(app.screen, screen)
                self.assertEqual(app.destination, original)
                self.assertFalse(savegdrive.has_valid_state(Path(temp) / "Documents"))
                self.assertEqual(sorted(path.name for path in (Path(temp) / "Documents").iterdir()), ["taxes.pdf"])
                app.screen.query_one("#parent-path", Input).value = temp
                app.screen.query_one("#folder-name", Input).value = "Session"
                await pilot.press("ctrl+s")
                await pilot.pause()
                self.assertEqual(app.destination, Path(temp).resolve() / "Session")
                self.assertTrue(savegdrive.has_valid_state(app.destination))
                self.assertIn("Session", app.sub_title)
                await pilot.press("q")

    async def test_destination_refusal_and_button_stay_visible_on_small_terminals(self):
        for size in [(80, 24), (110, 27)]:
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temp:
                (Path(temp) / "GDrive").mkdir()
                (Path(temp) / "GDrive" / "file.txt").write_text("x")  # not empty, no state: refused
                app = self.make_app()
                async with app.run_test(size=size) as pilot:
                    await pilot.press("f")
                    screen = app.screen
                    self.assertIsInstance(screen, savegdrive.DestinationSetupScreen)
                    screen.query_one("#parent-path", Input).value = temp
                    screen.query_one("#folder-name", Input).value = "GDrive"
                    await pilot.pause()
                    form = screen.query_one("#setup-form").content_region
                    if size == (110, 27):  # the final path fits before saving
                        self.assertTrue(form.contains_region(screen.query_one("#final-path").region))
                    await pilot.press("ctrl+s")
                    await pilot.pause(0.3)
                    status = screen.query_one("#setup-status")
                    self.assertIn("already contains files", str(status.render()))
                    form = screen.query_one("#setup-form").content_region
                    self.assertTrue(form.contains_region(status.region), (form, status.region))
                    button = screen.query_one("#save-destination").region
                    self.assertTrue(form.contains_region(button), (form, button))
                    await pilot.press("escape")
                    await pilot.press("q")

    async def test_folding_a_directory_on_the_destination_screen_keeps_the_app_open(self):
        (Path(self.temp.name) / "Documents").mkdir()
        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.press("down", "space")  # Docs
            await pilot.press("f")
            self.assertIsInstance(app.screen, savegdrive.DestinationSetupScreen)
            directories = app.screen.query_one("#directory-tree", DirectoryTree)
            directories.focus()
            await pilot.pause(0.3)  # directory listing
            directories.move_cursor(next(node for node in directories.root.children if node.label.plain == "Documents"))
            await pilot.press("enter")  # unfold
            await pilot.pause(0.3)
            await pilot.press("enter")  # fold again: used to crash the whole app
            await pilot.pause(0.3)
            self.assertTrue(app.is_running)
            self.assertIsInstance(app.screen, savegdrive.DestinationSetupScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(self.selected(app), ["Docs"])
            await pilot.press("q")

    async def test_sort_by_size_and_date(self):
        drive = demo_drive()
        drive.items["big"] = file_item("big", "big.bin", "root", b"x" * 50, modifiedTime="2024-01-01T00:00:00Z")
        drive.items["new"] = file_item("new", "new.txt", "root", b"y", modifiedTime="2026-01-01T00:00:00Z")
        drive.items["undated"] = {"id": "undated", "name": "a undated", "mimeType": FORM, "parent": "root"}
        app = self.make_app(drive)

        def top():
            return [node.data["item"]["name"] for node in app.query_one(Tree).root.children]

        async with app.run_test() as pilot:
            self.assertEqual(top(), ["Docs", "Photos", "a undated", "big.bin", "new.txt", "notes.txt"])
            await pilot.press("z")
            self.assertEqual(top(), ["Docs", "Photos", "big.bin", "notes.txt", "new.txt", "a undated"])
            self.assertIn("size", str(app.query_one("#selection-summary").render()))
            await pilot.press("m")  # Docs and Photos both end in 2025-01-02 files: by name
            self.assertEqual(top(), ["Docs", "Photos", "new.txt", "notes.txt", "big.bin", "a undated"])
            await pilot.press("m")
            self.assertEqual(top(), ["Docs", "Photos", "a undated", "big.bin", "new.txt", "notes.txt"])
            await pilot.press("q")

    async def test_preview_note_when_nothing_to_transfer(self):
        # bugs 21 and 40: "up to date" only without a conflict; "and quits" only where y quits (--again).
        def entry(status, name):
            return {"status": status, "relative_path": Path(name), "kind": "FILE",
                    "item": {"name": name, "size": "10"}, "destination": self.root / name}

        class SelectorPreview(savegdrive.App):
            def __init__(self, plan, root):
                super().__init__()
                self.plan, self.root = plan, root

            def on_mount(self):
                self.push_screen(savegdrive.PreviewScreen(self.plan, self.root))

        synced = [entry("UNCHANGED", "a.txt")]
        conflict = synced + [entry("CONFLICT", "b.txt")]
        notes = {}
        for name, app in (
            ("again", savegdrive.PreviewApp(synced, self.root)),
            ("selector", SelectorPreview(synced, self.root)),
            ("conflict", savegdrive.PreviewApp(conflict, self.root)),
        ):
            async with app.run_test() as pilot:
                await pilot.pause()
                notes[name] = str(app.screen.query_one("#preview-notes").render())
        self.assertIn("Everything is up to date: y saves the state and quits.", notes["again"])
        self.assertIn("Everything is up to date: y saves the state.", notes["selector"])
        self.assertNotIn("quits", notes["selector"])
        self.assertNotIn("up to date", notes["conflict"])
        self.assertIn("y saves the state and quits", notes["conflict"])

    async def test_tree_comes_back_after_a_download(self):
        drive = demo_drive()
        views = [savegdrive.DriveView("My Drive", "root")]
        app = self.make_app(drive, views)
        async with app.run_test() as pilot:
            await pilot.press("z", "down", "space", "d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("y")
        result = app.return_value
        with mock.patch.object(savegdrive, "fetch_media", fake_fetch), \
                mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            args = mock.Mock(jobs=1)
            results = savegdrive.run_sync(
                args, drive, lambda: drive, result["plan"], result["selections"], result["scopes"], [],
                self.root, result["state"], app.cache, "0",
            )
        notice = savegdrive.result_notice(result["plan"], results)
        self.assertIn("3 downloaded", notice.plain)

        # The next round: same view and sort, the result on top, marks from the download, nothing selected.
        again = savegdrive.DriveSelectorApp(
            views, self.root, savegdrive.load_state(self.root), drive, None, app.cache,
            view=app.view, sort=app.sort, notice=notice,
        )
        async with again.run_test() as pilot:
            self.assertIn("3 downloaded", str(again.query_one("#notice").render()))
            self.assertEqual(again.sort, "size")
            self.assertEqual(again.selections, [])
            docs = next(node for node in views[0].nodes if node["display_path"] == "Docs")
            self.assertEqual(again.marks.get(docs["index"]), "synced")
            await pilot.press("q")
        self.assertIsNone(again.return_value)

    async def wait(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_drive_trash_restore_and_delete(self):
        await self.drive_trash_restore_and_delete(token=None)

    async def test_drive_trash_restore_and_delete_from_drive_changes(self):
        await self.drive_trash_restore_and_delete(token="0")

    async def test_drive_trash_rebuilds_the_tree_without_listing_drive_again(self):
        drive = demo_drive()
        views = savegdrive.drive_views(drive, "root")
        listings = []
        list_folder = drive.list
        drive.list = lambda q, **kwargs: listings.append(q) or list_folder(q, **kwargs)
        app = self.make_app(drive, views, token=savegdrive.start_page_token(drive))
        tree_names = lambda: [node.data["item"]["name"] for node in app.query_one(Tree).root.children]  # noqa: E731
        async with app.run_test() as pilot:
            listings.clear()
            await pilot.press("down", "down", "x", "y")  # Photos
            await self.wait(app, pilot)
            self.assertTrue(drive.items["photos"]["trashed"])
            self.assertEqual(tree_names(), ["Docs", "notes.txt"])
            self.assertEqual(listings, [])  # the Drive changes only, not every folder again
            await pilot.press("q")

    async def drive_trash_restore_and_delete(self, token):
        drive = demo_drive()
        views = savegdrive.drive_views(drive, "root")
        app = self.make_app(drive, views, token)
        tree_names = lambda: [node.data["item"]["name"] for node in app.query_one(Tree).root.children]  # noqa: E731
        async with app.run_test() as pilot:
            await pilot.press("down", "down", "x")  # Photos, moved to the Drive trash after y
            self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
            await pilot.press("y")
            await self.wait(app, pilot)
            self.assertTrue(drive.items["photos"]["trashed"])
            self.assertEqual(tree_names(), ["Docs", "notes.txt"])

            await pilot.press("shift+tab")  # Trash is the last view
            await self.wait(app, pilot)
            self.assertEqual(app.view.name, "Trash")
            self.assertEqual(app.query_one("#keys").context, "trash")
            self.assertEqual(tree_names(), ["Photos"])
            await pilot.press("e")  # what was inside shows below it
            self.assertIn("beach.jpg", [n.data["item"]["name"] for n in app.query_one(Tree).root.children[0].children])

            await pilot.press("down", "r", "y")
            await self.wait(app, pilot)
            self.assertFalse(drive.items["photos"]["trashed"])
            self.assertEqual(tree_names(), [])
            await pilot.press("tab")  # back in My Drive, the restored folder lists its content again
            await self.wait(app, pilot)
            await pilot.press("e")
            await self.wait(app, pilot)
            photos = next(n for n in app.query_one(Tree).root.children if n.data["item"]["id"] == "photos")
            self.assertEqual([n.data["item"]["name"] for n in photos.children], ["beach.jpg"])
            await pilot.press("shift+tab")
            await self.wait(app, pilot)
            self.assertEqual(app.view.name, "Trash")

            drive.items["n"]["trashed"] = True
            app.drive_changed("", {})  # as if trashed elsewhere: reload
            await self.wait(app, pilot)
            await pilot.press("down", "x")
            await pilot.press(*"delet", "enter")  # wrong word: still asking
            self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
            await pilot.press("backspace", "backspace", "backspace", "backspace", "backspace", *"delete", "enter")
            await self.wait(app, pilot)
            self.assertNotIn("n", drive.items)

            drive.items["a"]["trashed"] = True
            app.drive_changed("", {})
            await self.wait(app, pilot)
            await pilot.press("T", "y")  # y alone is no answer: the word must be typed
            await self.wait(app, pilot)
            self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
            self.assertTrue(drive.items["a"]["trashed"])
            await pilot.press("backspace", *"empty", "enter")
            await self.wait(app, pilot)
            self.assertNotIn("a", drive.items)
            self.assertEqual(tree_names(), [])
            await pilot.press("r")  # only in the Trash view; here: nothing to restore, no question
            await pilot.press("tab")  # back to My Drive
            await self.wait(app, pilot)
            await pilot.press("r")
            self.assertNotIsInstance(app.screen, savegdrive.ConfirmScreen)
            await pilot.press("q")

    async def test_empty_trash_waits_for_the_trash_to_be_listed(self):
        drive = demo_drive()
        drive.items["a"]["trashed"] = True
        views = savegdrive.drive_views(drive, "root")
        app = self.make_app(drive, views)
        listed, failing = threading.Event(), [True]
        load_view = savegdrive.load_view

        def slow_trash(service, view, cache):
            if view.root_id == savegdrive.TRASH:
                if failing[0]:
                    raise RuntimeError("network down")
                listed.wait(10)
            return load_view(service, view, cache)

        def toasts():
            return [toast.render().plain for toast in app.screen.query(Toast)]

        with mock.patch.object(savegdrive, "load_view", slow_trash):
            async with app.run_test(size=(140, 30), notifications=True) as pilot:
                await pilot.press("shift+tab")  # Trash, whose listing fails
                await self.wait(app, pilot)
                await pilot.press("T")  # no "0 items" question on a list that never came
                self.assertNotIsInstance(app.screen, savegdrive.ConfirmScreen)
                self.assertTrue(any("could not be listed" in toast for toast in toasts()))

                failing[0] = False
                app.views[-1].error = None
                await pilot.press("tab", "shift+tab")  # Trash again, still loading
                await pilot.pause()
                self.assertIsNone(app.view.nodes)
                await pilot.press("T")
                self.assertNotIsInstance(app.screen, savegdrive.ConfirmScreen)
                self.assertTrue(any("still loading" in toast for toast in toasts()))

                listed.set()
                await self.wait(app, pilot)
                await pilot.press("T")  # listed: the real count
                self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
                self.assertIn("(1 item at its top)",
                              app.screen.query_one("#confirm").query(Static).first().render().plain)
                await pilot.press("escape")
                self.assertTrue(drive.items["a"]["trashed"])
                await pilot.press("q")

    async def test_x_on_a_shortcut_trashes_the_shortcut(self):
        drive = demo_drive()
        drive.items["link"] = {"id": "link", "name": "Link", "mimeType": savegdrive.SHORTCUT_MIME_TYPE,
                               "parent": "root",
                               "shortcutDetails": {"targetId": "docs", "targetMimeType": FOLDER}}
        views = savegdrive.drive_views(drive, "root")
        app = self.make_app(drive, views)
        tree = lambda: app.query_one(Tree)  # noqa: E731
        async with app.run_test() as pilot:
            await pilot.press("down", "down")
            self.assertEqual(tree().cursor_node.data["item"]["id"], "link")
            await pilot.press("x")
            self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
            await pilot.press("y")
            await self.wait(app, pilot)
            self.assertTrue(drive.items["link"]["trashed"])  # the shortcut, not its target
            self.assertNotIn("trashed", drive.items["docs"])

            await pilot.press("shift+tab")  # Trash view
            await self.wait(app, pilot)
            self.assertEqual([node.data["item"]["name"] for node in tree().root.children], ["Link"])
            await pilot.press("down", "x", *"delete", "enter")
            await self.wait(app, pilot)
            self.assertNotIn("link", drive.items)
            self.assertIn("docs", drive.items)  # the shared folder and its content stay
            self.assertIn("a", drive.items)
            await pilot.press("q")

    async def test_cancelled_drive_questions_change_nothing(self):
        drive = demo_drive()
        drive.items["a"]["trashed"] = True
        drive.items["n"]["trashed"] = True
        views = savegdrive.drive_views(drive, "root")
        app = self.make_app(drive, views)
        async with app.run_test() as pilot:
            await pilot.press("down", "down")  # Photos
            for cancel in ("n", "escape"):
                await pilot.press("x")
                self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
                await pilot.press(cancel)
                await self.wait(app, pilot)
                self.assertNotIsInstance(app.screen, savegdrive.ConfirmScreen)
                self.assertNotIn("trashed", drive.items["photos"])

            await pilot.press("shift+tab")  # Trash
            await self.wait(app, pilot)
            self.assertEqual(app.view.name, "Trash")
            await pilot.press("down", "x")  # delete forever, asks for a word
            self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
            await pilot.press("escape")
            await self.wait(app, pilot)
            await pilot.press("T")  # empty the Drive trash, asks for a word
            self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
            await pilot.press("escape")
            await self.wait(app, pilot)
            self.assertNotIsInstance(app.screen, savegdrive.ConfirmScreen)
            await pilot.press("q")
        for item_id in ("a", "n"):
            self.assertIn(item_id, drive.items)
            self.assertTrue(drive.items[item_id]["trashed"])
        self.assertNotIn("trashed", drive.items["photos"])

    def trash_view_app(self, drive):
        drive.items["n"]["trashed"] = True  # notes.txt is in the Drive trash
        views = savegdrive.drive_views(drive, "root")
        return self.make_app(drive, views)

    async def test_delete_forever_ignores_the_selection_of_other_views(self):
        drive = demo_drive()
        app = self.trash_view_app(drive)
        async with app.run_test() as pilot:
            await pilot.press("down", "space")  # Docs, selected in My Drive
            await pilot.press("shift+tab")  # Trash
            await self.wait(app, pilot)
            await pilot.press("down", "x")  # notes.txt, under the cursor
            self.assertIn("“notes.txt”", str(app.screen.message))
            await pilot.press(*"delete", "enter")
            await self.wait(app, pilot)
        self.assertNotIn("n", drive.items)
        for item_id in ("docs", "a", "b", "report", "form"):
            self.assertIn(item_id, drive.items)
            self.assertNotIn("trashed", drive.items[item_id])

    async def test_names_with_brackets_show_whole_in_questions_and_toasts(self):
        name = "[DRAFT] Q3 [/] budget.xlsx"  # [DRAFT] would vanish as markup, [/] would crash
        drive = demo_drive()
        drive.items["n"]["name"] = name
        views = savegdrive.drive_views(drive, "root")
        app = self.make_app(drive, views)
        tree = lambda: app.query_one(Tree)  # noqa: E731

        def question():
            return app.screen.query_one("#confirm").query(Static).first().render().plain

        def toasts():
            return [toast.render().plain for toast in app.screen.query(Toast)]

        async with app.run_test(size=(140, 30), notifications=True) as pilot:
            while (tree().cursor_node.data or {}).get("item", {}).get("id") != "n":
                await pilot.press("down")
            with mock.patch.object(savegdrive, "open_quietly", return_value=True):
                await pilot.press("o")
            await pilot.pause()
            self.assertIn(f"Opened {name} in your browser.", toasts())

            await pilot.press("x")
            self.assertIn(f"Move “{name}” to the Google Drive trash?", question())
            await pilot.press("y")
            await self.wait(app, pilot)
            self.assertTrue(drive.items["n"]["trashed"])
            self.assertIn(f"Moved “{name}” to the Drive trash.", toasts())

            await pilot.press("shift+tab")  # Trash
            await self.wait(app, pilot)
            await pilot.press("down", "r")
            self.assertIn(f"Restore “{name}” on Google Drive", question())
            await pilot.press("escape", "x")
            self.assertIn(f"Delete “{name}” forever from Google Drive?", question())
            await pilot.press("escape", "q")

    async def test_restore_ignores_the_selection_of_other_views(self):
        drive = demo_drive()
        app = self.trash_view_app(drive)
        async with app.run_test() as pilot:
            await pilot.press("down", "space", "shift+tab")  # Docs selected in My Drive, then Trash
            await self.wait(app, pilot)
            await pilot.press("down", "r")
            self.assertIn("“notes.txt”", str(app.screen.message))
            await pilot.press("y")
            await self.wait(app, pilot)
        self.assertFalse(drive.items["n"]["trashed"])
        self.assertNotIn("trashed", drive.items["docs"])

    async def test_failed_restore_is_reported_as_an_error(self):
        # tests 40: a restore Drive refuses (403) is an error, not "Restored".
        drive = demo_drive()
        drive.items["photos"]["trashed"] = True
        update = drive.update

        def failing_update(fileId, body, **kwargs):
            if fileId == "photos":
                return FakeRequest(lambda: raise_(http_error(403)))
            return update(fileId, body, **kwargs)

        drive.update = failing_update
        views = savegdrive.drive_views(drive, "root")
        app = self.make_app(drive, views)
        tree = lambda: app.query_one(Tree)  # noqa: E731
        notes = []
        async with app.run_test() as pilot:
            app.notify = lambda message, **kwargs: notes.append((str(message), kwargs.get("severity")))
            await pilot.press("shift+tab")  # Trash
            await self.wait(app, pilot)
            while (tree().cursor_node.data or {}).get("item", {}).get("id") != "photos":
                await pilot.press("down")
            await pilot.press("r")
            self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
            await pilot.press("y")
            await self.wait(app, pilot)
            await pilot.press("q")
        self.assertTrue(drive.items["photos"]["trashed"])
        self.assertTrue(any(severity == "error" for _, severity in notes), notes)
        self.assertFalse(any(message.startswith("Restored") for message, _ in notes), notes)

    async def test_drive_trash_ignores_the_selection_of_the_trash_view(self):
        drive = demo_drive()
        app = self.trash_view_app(drive)
        async with app.run_test() as pilot:
            await pilot.press("shift+tab")  # Trash
            await self.wait(app, pilot)
            await pilot.press("down", "space")  # notes.txt, selected in Trash
            await pilot.press("tab")  # back to My Drive
            await self.wait(app, pilot)
            await pilot.press("home", "down", "x")  # Docs, under the cursor
            self.assertIn("“Docs”", str(app.screen.message))
            await pilot.press("y")
            await self.wait(app, pilot)
        self.assertTrue(drive.items["docs"]["trashed"])
        self.assertIn("n", drive.items)

    async def test_read_only_items_are_not_offered_to_the_drive_trash(self):
        drive = demo_drive()
        drive.items["n"]["capabilities"] = {"canTrash": False}
        drive.items["photos"]["capabilities"] = {"canTrash": False}
        app = self.make_app(drive)
        async with app.run_test() as pilot:
            with mock.patch.object(app, "notify") as notify:
                await pilot.press("end", "x")  # notes.txt, read only: no question
            self.assertNotIsInstance(app.screen, savegdrive.ConfirmScreen)
            refusal = notify.call_args.args[0]
            self.assertNotIn("shared with you read only", refusal)  # a contributor may edit it
            self.assertIn("Drive does not let you", refusal)
            await pilot.press("home", "down", "space", "down", "space", "down", "space", "x")
            self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
            self.assertIn("2 items you cannot trash on Drive stay", str(app.screen.message))
            await pilot.press("y")
            await self.wait(app, pilot)
        self.assertTrue(drive.items["docs"]["trashed"])
        self.assertNotIn("trashed", drive.items["photos"])

    async def test_unfold_the_whole_view(self):
        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.press("down", "down", "down", "e")  # anywhere: the whole view
            tree = app.query_one(Tree)
            self.assertTrue(all(node.is_expanded for node in tree.root.children if node.allow_expand))
            await pilot.press("e")
            self.assertFalse(any(node.is_expanded for node in tree.root.children if node.allow_expand))
            await pilot.press("q")

    async def test_open_folders_stay_open_after_a_sync_lists_drive_again(self):
        # bugs 30: a new folder sent to Drive shifts the indexes of the listed again view.
        def open_folders(app):
            return sorted(savegdrive.selected_path(node.data).as_posix()
                          for node in app.tree_nodes.values() if node.is_expanded)

        drive = demo_drive()
        app = self.make_app(drive)
        async with app.run_test() as pilot:
            photos = next(node for node in app.tree_nodes.values() if node.data["item"]["id"] == "photos")
            photos.expand()
            await pilot.pause()
            self.assertEqual(open_folders(app), ["Photos"])
            await pilot.press("q")
        drive.items["archive"] = {"id": "archive", "name": "Archive", "mimeType": FOLDER, "parent": "root"}
        drive.items["old"] = file_item("old", "old.txt", "archive", b"old")
        view, cache = app.view, {}  # what main does after a sync that changed Drive
        view.nodes, view.error, view.sorted_by = None, None, "name"
        savegdrive.load_view(drive, view, cache)
        again = savegdrive.DriveSelectorApp(app.views, self.root, savegdrive.load_state(self.root), drive, None,
                                            cache, view=view)
        async with again.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(open_folders(again), ["Photos"])
            await pilot.press("q")

    async def test_last_selection_is_checked_at_start(self):
        drive = demo_drive()
        app = self.make_app(drive)
        state = savegdrive.load_state(self.root)
        selections = [{"item": dict(drive.public(drive.items["photos"])), "relative_parent": Path()}]
        savegdrive.remember_selection(state, selections, [Path("Photos")])
        savegdrive.save_state(self.root, state)
        app = self.make_app(drive)
        async with app.run_test() as pilot:
            await self.wait(app, pilot)
            line = str(app.query_one("#notice").render())
            self.assertIn("Since the last sync: 1 ↓ to download", line)
            await pilot.press("s")
            self.assertIsInstance(app.screen, savegdrive.PreviewScreen)
            await pilot.press("y")
        result = app.return_value
        self.assertEqual(result["roots"], [])
        self.assertEqual([entry["relative_path"].as_posix() for entry in result["plan"]],
                         ["Photos", "Photos/beach.jpg"])

    async def test_no_last_selection_no_line(self):
        app = self.make_app()
        async with app.run_test() as pilot:
            await self.wait(app, pilot)
            self.assertFalse(app.query_one("#notice").display)
            await pilot.press("s")  # nothing to review
            self.assertNotIsInstance(app.screen, savegdrive.PreviewScreen)
            await pilot.press("q")


    async def test_big_selection_keys_scale_linearly(self):
        """a, a again and z with a whole view selected: linear in the selection (perf 2)."""
        async def calls(count):
            drive = FakeDrive([file_item(f"n{index}", f"n{index}.txt", "root", b"n") for index in range(count)])
            app = self.make_app(drive)
            counts = []
            real = savegdrive.selected_path

            def counting(entry):
                counts[-1] += 1
                return real(entry)

            async with app.run_test() as pilot:
                with mock.patch.object(savegdrive, "selected_path", counting):
                    for key in ("a", "z", "a"):
                        counts.append(0)
                        await pilot.press(key)
                        if key == "a":
                            self.assertEqual(len(app.selections), count if len(counts) == 1 else 0)
                await pilot.press("q")
            return counts

        small, big = await calls(20), await calls(200)
        for small_count, big_count in zip(small, big):
            self.assertLess(big_count, small_count * 20)  # 10x the items; it was about 100x

    async def test_help_and_expand_all(self):
        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.press("question_mark")
            self.assertIsInstance(app.screen, savegdrive.HelpScreen)
            await pilot.press("escape")
            self.assertNotIsInstance(app.screen, savegdrive.HelpScreen)
            await pilot.press("e")
            tree = app.query_one(Tree)
            self.assertTrue(all(node.is_expanded for node in tree.root.children if node.allow_expand))
            await pilot.press("q")


FAKE_PYTHON = """#!/bin/sh
case "$1" in
    -V) echo "Python $FAKE_PY_VERSION"; exit 0 ;;
    -c) case "$2" in *version_info*) [ "$FAKE_PY_OK" = 1 ]; exit $? ;; esac
        [ "$FAKE_IMPORTS_OK" = 1 ]; exit $? ;;
    -m) case "$2" in
            venv) {mkdir} -p "$3/bin" && {cp} "$0" "$3/bin/python"; exit $? ;;
            pip) echo "$*" >> "$FAKE_PIP_LOG"; exit "$FAKE_PIP_RC" ;;
        esac; exit 1 ;;
esac
echo "ran $*"
"""


@unittest.skipUnless(shutil.which("sh") and shutil.which("dirname"), "needs sh and dirname")
class LauncherTest(unittest.TestCase):
    """run-savegdrive.sh, run against a fake interpreter so no venv or pip is involved."""

    PACKAGES = "google-api-python-client google-auth google-auth-oauthlib send2trash textual"

    def run_launcher(self, names, version_ok=True, venv=False, stamp=None, stamp_age_days=0,
                     imports_ok=True, pip_rc=0):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        shutil.copy(Path(__file__).resolve().parent.parent / "run-savegdrive.sh", root)
        (root / "savegdrive.py").write_text("")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for tool in ("dirname", "find"):
            os.symlink(shutil.which(tool), bin_dir / tool)
        fake = FAKE_PYTHON.format(mkdir=shutil.which("mkdir"), cp=shutil.which("cp"))
        for name in names:
            (bin_dir / name).write_text(fake)
            (bin_dir / name).chmod(0o755)
        if venv:
            (root / ".venv" / "bin").mkdir(parents=True)
            (root / ".venv" / "bin" / "python").write_text(fake)
            (root / ".venv" / "bin" / "python").chmod(0o755)
        if stamp is not None:
            stamp_path = root / ".venv" / ".savegdrive-deps"
            stamp_path.write_text(stamp + "\n")
            when = time.time() - stamp_age_days * 86400
            os.utime(stamp_path, (when, when))
        env = {
            "PATH": str(bin_dir),
            "FAKE_PY_OK": "1" if version_ok else "0",
            "FAKE_PY_VERSION": "3.12.0" if version_ok else "3.10.22",
            "FAKE_IMPORTS_OK": "1" if imports_ok else "0",
            "FAKE_PIP_LOG": str(root / "pip.log"),
            "FAKE_PIP_RC": str(pip_rc),
        }
        result = subprocess.run(
            [shutil.which("sh"), str(root / "run-savegdrive.sh"), "--help"],
            env=env, capture_output=True, text=True, timeout=30,
        )
        return result, root

    def test_launcher_uses_python3_when_python_is_missing(self):
        result, root = self.run_launcher(["python3"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root / ".venv" / "bin" / "python").exists())
        self.assertIn("savegdrive.py --help", result.stdout)

    def test_launcher_falls_back_to_python(self):
        result, root = self.run_launcher(["python"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root / ".venv" / "bin" / "python").exists())

    def test_launcher_refuses_python_older_than_3_11(self):
        result, root = self.run_launcher(["python3"], version_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("3.11 or newer", result.stderr)
        self.assertIn("3.10.22", result.stderr)
        self.assertFalse((root / ".venv").exists())

    def pip_calls(self, root):
        log = root / "pip.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_launcher_upgrades_an_existing_venv_without_stamp(self):
        # A .venv from before the stamp (e.g. 1.1.0) whose imports all work.
        result, root = self.run_launcher(["python3"], venv=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.pip_calls(root),
            ["-m pip install -U --upgrade-strategy eager " + self.PACKAGES],
        )
        self.assertEqual((root / ".venv" / ".savegdrive-deps").read_text(), self.PACKAGES + "\n")
        self.assertIn("savegdrive.py --help", result.stdout)

    def test_launcher_upgrades_on_old_or_different_stamp_only(self):
        result, root = self.run_launcher(["python3"], venv=True, stamp=self.PACKAGES, stamp_age_days=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pip_calls(root), [])
        for stamp, age in ((self.PACKAGES, 31), ("google-api-python-client textual", 1)):
            with self.subTest(stamp=stamp, age=age):
                result, root = self.run_launcher(["python3"], venv=True, stamp=stamp, stamp_age_days=age)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(self.pip_calls(root)), 1)
                self.assertEqual((root / ".venv" / ".savegdrive-deps").read_text(), self.PACKAGES + "\n")

    def test_launcher_keeps_working_venv_when_upgrade_fails(self):
        result, root = self.run_launcher(["python3"], venv=True, pip_rc=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not update", result.stderr)
        self.assertIn("savegdrive.py --help", result.stdout)
        self.assertFalse((root / ".venv" / ".savegdrive-deps").exists())

    def test_launcher_fails_when_install_fails_and_imports_are_missing(self):
        result, root = self.run_launcher(["python3"], imports_ok=False, pip_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("savegdrive.py --help", result.stdout)
        self.assertFalse((root / ".venv" / ".savegdrive-deps").exists())


class GitignoreTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_fresh_clone_never_stages_secrets_or_local_environment(self):
        gitignore = Path(savegdrive.__file__).resolve().parent / ".gitignore"
        self.assertTrue(gitignore.is_file())
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                   "-c", "core.excludesFile=/dev/null", "-C", str(repo)]
            subprocess.run([*git, "init", "-q"], check=True)
            (repo / ".git" / "info" / "exclude").write_text("")  # only the versioned .gitignore counts
            shutil.copyfile(gitignore, repo / ".gitignore")
            for name in ("credentials.json", "token.json", "token.json.tmp", ".venv/pyvenv.cfg", "__pycache__/x.pyc"):
                (repo / name).parent.mkdir(exist_ok=True)
                (repo / name).write_text("FAKE")
            subprocess.run([*git, "add", "."], check=True)
            staged = subprocess.run([*git, "diff", "--cached", "--name-only"], check=True,
                                    capture_output=True, text=True).stdout.split()
        self.assertEqual(staged, [".gitignore"])


if __name__ == "__main__":
    unittest.main()

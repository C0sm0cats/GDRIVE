import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import savegdrive  # noqa: E402

from googleapiclient.errors import HttpError  # noqa: E402
from rich.console import Console  # noqa: E402
from textual.widgets import Input, Static, Tree  # noqa: E402
from textual.widgets._toast import Toast  # noqa: E402

FOLDER = savegdrive.FOLDER_MIME_TYPE
DOC = "application/vnd.google-apps.document"
FORM = "application/vnd.google-apps.form"


def http_error(status):
    response = mock.Mock(status=status, reason="error")
    return HttpError(response, b"{}")


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
            changes = self.drive.change_log[int(pageToken):]
            return {"changes": changes, "newStartPageToken": str(len(self.drive.change_log))}

        return FakeRequest(run)


class FakeDrives:
    def __init__(self, drive):
        self.drive = drive

    def list(self, **kwargs):
        return FakeRequest(lambda: {"drives": self.drive.shared_drives})


class FakeDrive:
    """In-memory Drive: items are dicts with id, name, mimeType, parent and optional content."""

    def __init__(self, items, page_size=1000, shared_drives=()):
        self.items = {item["id"]: dict(item) for item in items}
        self.page_size = page_size
        self.batches = []
        self.failures = {}  # folder id -> errors raised by its next listings
        self.change_log = []
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
                {"fileId": item_id, "removed": False, "file": dict(self.public(item), parents=[item["parent"]])}
            )

    def new_batch_http_request(self, callback):
        return FakeBatch(self, callback)

    def public(self, item):
        fields = ("id", "name", "mimeType", "size", "modifiedTime", "md5Checksum", "shortcutDetails", "capabilities")
        return {key: item[key] for key in fields if key in item}

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
        want_trashed = q.endswith("trashed = true")

        def run():
            failures = self.failures.get(folder_id)
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
                    if item.get("parent") == folder_id and self.is_trashed(item) == want_trashed
                ]
            start = int(pageToken or 0)
            page = children[start:start + self.page_size]
            response = {"files": page}
            if start + self.page_size < len(children):
                response["nextPageToken"] = str(start + self.page_size)
            return response

        return FakeRequest(run)

    def get(self, fileId, **kwargs):
        def run():
            if fileId == "root":
                return {"id": "root"}
            if fileId not in self.items:
                raise http_error(404)
            return self.public(self.items[fileId])

        return FakeRequest(run)

    def update(self, fileId, body, **kwargs):
        def run():
            if fileId not in self.items:
                raise http_error(404)
            self.items[fileId]["trashed"] = body["trashed"]
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
    return service.public(item)


def fake_create_folder(service, name, parent_id):
    folder_id = f"dir{len(service.items)}"
    service.items[folder_id] = {"id": folder_id, "name": name, "mimeType": FOLDER, "parent": parent_id}
    return {"id": folder_id, "name": name, "mimeType": FOLDER}


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


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "GDrive"
        savegdrive.initialize_managed_destination(self.root)
        self.trash = Path(self.temp.name) / "system trash"
        self.trash.mkdir()

        def fake_trash_local(path):
            os.replace(path, self.trash / f"{len(list(self.trash.iterdir()))}-{path.name}")

        for name, fake in [("fetch_media", fake_fetch), ("upload_file", fake_upload),
                           ("create_remote_folder", fake_create_folder), ("trash_local", fake_trash_local)]:
            patcher = mock.patch.object(savegdrive, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        self.drive = demo_drive()

    def preview(self):
        """The plan the preview shows, and the state it was computed against."""
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
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

    def test_folder_deleted_on_drive_goes_to_your_trash(self):
        self.run_sync()
        for key in ["photos", "p"]:
            del self.drive.items[key]
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos/beach.jpg"], "TRASH_LOCAL")
        self.assertFalse((self.root / "Photos").exists())  # left empty: removed

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

    def test_folder_removed_here_by_the_sync_is_not_trashed_on_drive(self):
        self.drive.items["club"] = {"id": "club", "name": "Club", "mimeType": FOLDER, "parent": "root"}
        self.drive.items["club_photos"] = {"id": "club_photos", "name": "Photos 2026", "mimeType": FOLDER,
                                           "parent": "club"}
        self.run_sync()
        self.drive.items["club"]["trashed"] = True  # x in the tree, or a colleague
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Club/Photos 2026"], "TRASH_LOCAL")
        self.assertFalse((self.root / "Club").exists())
        self.drive.items["club"]["trashed"] = False  # r in the Trash view
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Club"], "NEW")
        self.assertFalse(self.drive.items["club"].get("trashed"))
        self.assertTrue((self.root / "Club/Photos 2026").is_dir())

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
                mock.patch.object(savegdrive, "console", Console(file=io.StringIO())), \
                mock.patch.object(sys.stdin, "isatty", lambda: isatty), \
                mock.patch.object(sys.stdout, "isatty", lambda: isatty):
            savegdrive.main()

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
        (self.root / "notes.txt").unlink()
        self.assertIsNone(marks()["notes.txt"])

    def test_again(self):
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        by_path = {node["display_path"]: node for node in nodes}
        selections = []
        for path in ("Photos", "Docs/a.txt"):
            savegdrive.add_selected_item(selections, by_path[path]["item"], by_path[path]["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        savegdrive.remember_selection(state, selections, scopes)
        savegdrive.save_state(self.root, state)

        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        del self.drive.items["photos"]
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            again, again_scopes = savegdrive.last_selection(self.drive, savegdrive.load_state(self.root), {})
        self.assertEqual([entry["item"]["name"] for entry in again], ["a.txt"])
        self.assertEqual(again[0]["item"]["md5Checksum"], self.drive.items["a"]["md5Checksum"])
        self.assertEqual(again_scopes, [Path("Photos")])  # gone from Drive: still scanned locally
        self.assertIsNone(savegdrive.last_selection(self.drive, {"files": {}}, {}))

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
                with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
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

        replayed, new_token = savegdrive.apply_changes(self.drive, savegdrive.load_snapshot(self.root))
        fresh = {}
        savegdrive.prefetch_folders(self.drive, ["root"], fresh)
        self.assertEqual(replayed, fresh)
        self.assertEqual(new_token, "5")
        self.assertIsNone(savegdrive.apply_changes(self.drive, {"token": "expired", "folders": {}}))

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
        _, results = self.run_sync()
        self.assertEqual(results["FOLDER_LOCAL_ONLY"], 0)

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

    def test_header_text(self):
        home = Path.home()
        self.assertEqual(
            savegdrive.header_text("you@gmail.com", home / "GDrive", "preview"),
            "preview · you@gmail.com · syncs with ~/GDrive",
        )
        self.assertEqual(savegdrive.header_text(None, Path("/data/x")), "syncs with /data/x")

    def test_destination_option(self):
        target = Path(self.temp.name) / "Once"
        self.assertEqual(savegdrive.resolve_destination(str(target)), target)
        self.assertTrue(savegdrive.has_valid_state(target))
        with self.assertRaises(OSError):
            savegdrive.resolve_destination(str(Path(self.temp.name) / "missing" / "x"))

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

    def test_existing_identical_file_is_adopted(self):
        (self.root / "notes.txt").write_bytes(b"notes")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["notes.txt"], "UNCHANGED")

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

        entry = {"destination": self.root / "x.txt", "item": self.drive.items["a"]}
        with mock.patch.object(savegdrive, "fetch_media", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                savegdrive.download_file_atomically(self.drive, entry)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [savegdrive.STATE_FILE_NAME])

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
            item = savegdrive.upload_file(service, path, name="x.bin", parent_id="docs", on_bytes=sent.append)
        self.assertEqual(item["id"], "new")
        self.assertEqual(sum(sent), 10)
        body = service.files.return_value.create.call_args.kwargs["body"]
        self.assertEqual(body, {"name": "x.bin", "parents": ["docs"]})

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

    def make_app(self, drive=None, views=None):
        drive = drive or demo_drive()
        cache = {}
        views = views or [savegdrive.DriveView("My Drive", "root")]
        savegdrive.load_view(drive, views[0], cache)
        return savegdrive.DriveSelectorApp(views, self.root, savegdrive.load_state(self.root), drive, None, cache)

    def selected(self, app):
        return [savegdrive.selected_path(entry).as_posix() for entry in app.selections]

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

    async def test_columns_are_aligned(self):
        app = self.make_app()
        async with app.run_test(size=(120, 20)) as pilot:
            app.action_expand_all()
            await pilot.pause()
            tree = app.query_one(Tree)
            lines = ["".join(segment.text for segment in tree.render_line(y)) for y in range(9)]
        columns = {line.index(" 5 B") for line in lines if " 5 B" in line}
        self.assertEqual(len(columns), 1, lines)

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
                self.assertIsInstance(app.screen, savegdrive.DestinationSetupScreen)
                app.screen.query_one("#parent-path", Input).value = temp
                app.screen.query_one("#folder-name", Input).value = "Session"
                await pilot.press("ctrl+s")
                await pilot.pause()
                self.assertEqual(app.destination, Path(temp).resolve() / "Session")
                self.assertTrue(savegdrive.has_valid_state(app.destination))
                self.assertIn("Session", app.sub_title)
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
        drive = demo_drive()
        views = savegdrive.drive_views(drive, "root")
        app = self.make_app(drive, views)
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
            await pilot.press("T", *"empty", "enter")
            await self.wait(app, pilot)
            self.assertNotIn("a", drive.items)
            self.assertEqual(tree_names(), [])
            await pilot.press("r")  # only in the Trash view; here: nothing to restore, no question
            await pilot.press("tab")  # back to My Drive
            await self.wait(app, pilot)
            await pilot.press("r")
            self.assertNotIsInstance(app.screen, savegdrive.ConfirmScreen)
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
            await pilot.press("end", "x")  # notes.txt, read only: no question
            self.assertNotIsInstance(app.screen, savegdrive.ConfirmScreen)
            await pilot.press("home", "down", "space", "down", "space", "down", "space", "x")
            self.assertIsInstance(app.screen, savegdrive.ConfirmScreen)
            self.assertIn("2 items shared with you read only stay", str(app.screen.message))
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
    -c) case "$2" in *version_info*) [ "$FAKE_PY_OK" = 1 ]; exit $? ;; esac; exit 0 ;;
    -m) {mkdir} -p "$3/bin" && {cp} "$0" "$3/bin/python"; exit $? ;;
esac
echo "ran $*"
"""


@unittest.skipUnless(shutil.which("sh") and shutil.which("dirname"), "needs sh and dirname")
class LauncherTest(unittest.TestCase):
    """run-savegdrive.sh, run against a fake interpreter so no venv or pip is involved."""

    def run_launcher(self, names, version_ok=True):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        shutil.copy(Path(__file__).resolve().parent.parent / "run-savegdrive.sh", root)
        (root / "savegdrive.py").write_text("")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        os.symlink(shutil.which("dirname"), bin_dir / "dirname")
        fake = FAKE_PYTHON.format(mkdir=shutil.which("mkdir"), cp=shutil.which("cp"))
        for name in names:
            (bin_dir / name).write_text(fake)
            (bin_dir / name).chmod(0o755)
        env = {
            "PATH": str(bin_dir),
            "FAKE_PY_OK": "1" if version_ok else "0",
            "FAKE_PY_VERSION": "3.12.0" if version_ok else "3.10.22",
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


if __name__ == "__main__":
    unittest.main()

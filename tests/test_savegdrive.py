import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import savegdrive  # noqa: E402

from googleapiclient.errors import HttpError  # noqa: E402
from rich.console import Console  # noqa: E402
from textual.widgets import Input, Tree  # noqa: E402

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

    def run_sync(self, jobs=1):
        cache = {}
        nodes, browsed = savegdrive.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                savegdrive.add_selected_item(selections, node["item"], node["relative_parent"])
        scopes = savegdrive.build_local_scopes(selections, browsed)
        state = savegdrive.load_state(self.root)
        plan = savegdrive.build_plan(self.drive, selections, scopes, self.root, state, cache)
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        with mock.patch.object(savegdrive, "console", Console(file=io.StringIO())):
            results = savegdrive.apply_plan(self.drive, plan, self.root, state, jobs, lambda: self.drive)
        return statuses, results

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

    def test_folder_deleted_on_drive_goes_to_your_trash(self):
        self.run_sync()
        for key in ["photos", "p"]:
            del self.drive.items[key]
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Photos/beach.jpg"], "TRASH_LOCAL")
        self.assertFalse((self.root / "Photos").exists())  # left empty: removed

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

    def test_existing_identical_file_is_adopted(self):
        (self.root / "notes.txt").write_bytes(b"notes")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["notes.txt"], "UNCHANGED")

    def test_interrupted_download_leaves_no_part_file(self):
        def interrupted(service, item, file_handle, stop=None):
            file_handle.write(b"partial")
            raise KeyboardInterrupt

        entry = {"destination": self.root / "x.txt", "item": self.drive.items["a"]}
        with mock.patch.object(savegdrive, "fetch_media", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                savegdrive.download_file_atomically(self.drive, entry)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [savegdrive.STATE_FILE_NAME])

    def test_destination_must_be_empty_or_managed(self):
        other = Path(self.temp.name) / "Other"
        other.mkdir()
        (other / "file").write_text("x")
        with self.assertRaises(ValueError):
            savegdrive.initialize_managed_destination(other)
        self.assertEqual(savegdrive.initialize_managed_destination(self.root), self.root)


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
                    mock.patch.object(savegdrive, "sign_in", return_value=new) as sign_in:
                self.assertIs(savegdrive.authenticate(), new)
            self.assertIn("full Drive access", sign_in.call_args[0][0])


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


if __name__ == "__main__":
    unittest.main()

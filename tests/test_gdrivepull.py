import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gdrivepull  # noqa: E402

from googleapiclient.errors import HttpError  # noqa: E402
from rich.console import Console  # noqa: E402
from textual.widgets import Input, Tree  # noqa: E402

FOLDER = gdrivepull.FOLDER_MIME_TYPE
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
        fields = ("id", "name", "mimeType", "size", "modifiedTime", "md5Checksum", "shortcutDetails")
        return {key: item[key] for key in fields if key in item}

    def is_trashed(self, item):
        while item is not None:
            if item.get("trashed"):
                return True
            item = self.items.get(item.get("parent"))
        return False

    def list(self, q, pageToken=None, **kwargs):
        if q.startswith("sharedWithMe"):
            folder_id = gdrivepull.SHARED_WITH_ME
        elif q == "trashed = true":
            folder_id = gdrivepull.TRASH
        else:
            folder_id = q.split("'")[1]
        want_trashed = q.endswith("trashed = true")

        def run():
            failures = self.failures.get(folder_id)
            if failures:
                raise failures.pop(0)
            if folder_id == gdrivepull.SHARED_WITH_ME:
                children = [self.public(item) for item in self.items.values() if item.get("shared")]
            elif folder_id == gdrivepull.TRASH:
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
    file_handle.write(service.items[gdrivepull.effective_id(item)]["content"])


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
        self.assertEqual(gdrivepull.local_name({"name": "a/b", "mimeType": "text/plain"}), "a_b")
        self.assertEqual(gdrivepull.local_name({"name": "..", "mimeType": "text/plain"}), "_unnamed_")
        self.assertEqual(gdrivepull.local_name({"name": "Plan", "mimeType": DOC}), "Plan.docx")
        self.assertEqual(gdrivepull.local_name({"name": "Plan.DOCX", "mimeType": DOC}), "Plan.DOCX")

    def test_human_size(self):
        self.assertEqual(gdrivepull.human_size(999), "999 B")
        self.assertEqual(gdrivepull.human_size(2_400_000), "2.4 MB")
        self.assertEqual(gdrivepull.human_size(340_000_000), "340 MB")

    def test_shortcut_follows_target(self):
        shortcut = {"id": "s", "name": "Link", "mimeType": gdrivepull.SHORTCUT_MIME_TYPE,
                    "shortcutDetails": {"targetId": "t", "targetMimeType": FOLDER}}
        self.assertTrue(gdrivepull.is_folder(shortcut))
        self.assertEqual(gdrivepull.effective_id(shortcut), "t")
        self.assertIn("/folders/t", gdrivepull.drive_url(shortcut))

    def test_unsupported(self):
        self.assertTrue(gdrivepull.is_unsupported({"mimeType": FORM}))
        self.assertFalse(gdrivepull.is_unsupported({"mimeType": DOC}))
        self.assertFalse(gdrivepull.is_unsupported({"mimeType": FOLDER}))


class ListingTest(unittest.TestCase):
    def test_batches_pages_and_sorts(self):
        items = [{"id": f"f{i}", "name": f"Folder {i}", "mimeType": FOLDER, "parent": "root"} for i in range(60)]
        items += [file_item(f"x{i}", f"z{i}.txt", "f0", b"x") for i in range(5)]
        items.append({"id": "sub", "name": "A sub", "mimeType": FOLDER, "parent": "f0"})
        drive = FakeDrive(items, page_size=2)
        cache = {}
        gdrivepull.list_folders(drive, [f"f{i}" for i in range(60)], cache)
        self.assertEqual(len(cache), 60)
        self.assertEqual(len(cache["f0"]), 6)
        self.assertEqual(cache["f0"][0]["id"], "sub")  # folders first
        self.assertEqual(drive.batches[0], 50)  # one call per folder and page, batched
        self.assertEqual(sum(drive.batches), 62)

    def test_retries_rate_limits(self):
        drive = demo_drive()
        drive.failures["docs"] = [http_error(429)]
        with mock.patch.object(gdrivepull.time, "sleep"):
            cache = {}
            gdrivepull.list_folders(drive, ["docs"], cache)
        self.assertEqual(len(cache["docs"]), 4)

    def test_raises_other_errors(self):
        drive = demo_drive()
        drive.failures["docs"] = [http_error(404)]
        with self.assertRaises(HttpError):
            gdrivepull.list_folders(drive, ["docs"], {})

    def test_tree_order_and_totals(self):
        nodes, browsed = gdrivepull.collect_drive_tree(demo_drive(), "root", {})
        paths = [node["display_path"] for node in nodes]
        self.assertEqual(paths[:3], ["Docs", "Photos", "notes.txt"])
        self.assertIn("Docs/Report", paths)
        for node in nodes:
            if node["parent"] is not None:
                self.assertLess(node["parent"], node["index"])
        self.assertEqual(len(browsed[Path()]), 3)
        totals = gdrivepull.folder_totals(nodes)
        self.assertEqual(totals[0], [4, 10])  # Docs: a, b, Report, Survey; 5 + 5 known bytes

    def test_shortcut_cycle_stops(self):
        drive = FakeDrive([
            {"id": "loop", "name": "Loop", "mimeType": FOLDER, "parent": "root"},
            {"id": "back", "name": "Back", "mimeType": gdrivepull.SHORTCUT_MIME_TYPE, "parent": "loop",
             "shortcutDetails": {"targetId": "loop", "targetMimeType": FOLDER}},
        ])
        nodes, _ = gdrivepull.collect_drive_tree(drive, "root", {})
        self.assertEqual([node["display_path"] for node in nodes], ["Loop", "Loop/Back"])


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.folder = {"name": "Docs", "mimeType": FOLDER}
        self.child = {"name": "a.txt", "mimeType": "text/plain"}

    def test_parent_replaces_children(self):
        selections = []
        gdrivepull.add_selected_item(selections, self.child, Path("Docs"))
        gdrivepull.add_selected_item(selections, self.folder, Path())
        self.assertEqual([gdrivepull.selected_path(entry) for entry in selections], [Path("Docs")])
        self.assertFalse(gdrivepull.add_selected_item(selections, self.child, Path("Docs")))
        self.assertEqual(gdrivepull.path_selection_state(Path("Docs/a.txt"), selections), "*")

    def test_scopes(self):
        root_items = [self.folder, {"name": "n.txt", "mimeType": "text/plain"}]
        selections = []
        gdrivepull.add_selected_item(selections, self.folder, Path())
        self.assertEqual(gdrivepull.build_local_scopes(selections, {Path(): root_items}), [Path("Docs")])
        gdrivepull.add_selected_item(selections, root_items[1], Path())
        self.assertEqual(gdrivepull.build_local_scopes(selections, {Path(): root_items}), [Path()])


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "GDrive"
        gdrivepull.initialize_managed_destination(self.root)
        patcher = mock.patch.object(gdrivepull, "fetch_media", fake_fetch)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        self.drive = demo_drive()

    def run_sync(self, jobs=1):
        cache = {}
        nodes, browsed = gdrivepull.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                gdrivepull.add_selected_item(selections, node["item"], node["relative_parent"])
        scopes = gdrivepull.build_local_scopes(selections, browsed)
        state = gdrivepull.load_state(self.root)
        plan = gdrivepull.build_plan(self.drive, selections, scopes, self.root, state, cache)
        statuses = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        with mock.patch.object(gdrivepull, "console", Console(file=io.StringIO())):
            results = gdrivepull.apply_plan(self.drive, plan, self.root, state, jobs, lambda: self.drive)
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

    def test_update_conflict_removed_and_local_only(self):
        self.run_sync()
        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        (self.root / "Docs/b.txt").write_bytes(b"edited locally")
        del self.drive.items["p"]
        (self.root / "Photos/mine.txt").write_bytes(b"local")

        statuses, results = self.run_sync()
        self.assertEqual(statuses["Docs/a.txt"], "UPDATE")
        self.assertEqual(statuses["Docs/b.txt"], "CONFLICT")
        self.assertEqual(statuses["Photos/beach.jpg"], "REMOVED_REMOTE")
        self.assertEqual(statuses["Photos/mine.txt"], "LOCAL_ONLY")
        self.assertEqual((self.root / "Docs/a.txt").read_bytes(), b"alpha v2")
        self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"edited locally")
        self.assertTrue((self.root / "Photos/mine.txt").exists())
        recovered = list((self.root / gdrivepull.RECOVERY_DIR_NAME).rglob("beach.jpg"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(results["FILE_REMOVED_REMOTE"], 1)

    def test_parallel_downloads(self):
        for i in range(20):
            self.drive.items[f"m{i}"] = file_item(f"m{i}", f"m{i}.txt", "photos", f"file {i}".encode())
        statuses, results = self.run_sync(jobs=4)
        self.assertEqual(results["FILE_NEW"], 25)
        self.assertEqual((self.root / "Photos/m7.txt").read_bytes(), b"file 7")
        state = gdrivepull.load_state(self.root)
        self.assertEqual(len(state["files"]), 25)

    def test_marks(self):
        self.run_sync()
        nodes, _ = gdrivepull.collect_drive_tree(self.drive, "root", {})
        by_path = {node["display_path"]: node["index"] for node in nodes}

        def marks():
            state = gdrivepull.load_state(self.root)
            found = gdrivepull.local_marks(nodes, self.root, state)
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
        nodes, browsed = gdrivepull.collect_drive_tree(self.drive, "root", cache)
        by_path = {node["display_path"]: node for node in nodes}
        selections = []
        for path in ("Photos", "Docs/a.txt"):
            gdrivepull.add_selected_item(selections, by_path[path]["item"], by_path[path]["relative_parent"])
        scopes = gdrivepull.build_local_scopes(selections, browsed)
        state = gdrivepull.load_state(self.root)
        gdrivepull.remember_selection(state, selections, scopes)
        gdrivepull.save_state(self.root, state)

        self.drive.items["a"].update(file_item("a", "a.txt", "docs", b"alpha v2"))
        del self.drive.items["photos"]
        with mock.patch.object(gdrivepull, "console", Console(file=io.StringIO())):
            again, again_scopes = gdrivepull.last_selection(self.drive, gdrivepull.load_state(self.root), {})
        self.assertEqual([entry["item"]["name"] for entry in again], ["a.txt"])
        self.assertEqual(again[0]["item"]["md5Checksum"], self.drive.items["a"]["md5Checksum"])
        self.assertEqual(again_scopes, [Path("Photos")])  # gone from Drive: still scanned locally
        self.assertIsNone(gdrivepull.last_selection(self.drive, {"files": {}}, {}))

    def test_again_with_whole_view_takes_new_items(self):
        state = {"last_selection": {"roots": [{"id": "root", "base": "."}], "items": [], "scopes": ["."]}}
        self.drive.items["new"] = file_item("new", "new.txt", "root", b"new")
        selections, scopes = gdrivepull.last_selection(self.drive, state, {})
        self.assertEqual(
            sorted(gdrivepull.selected_path(entry).as_posix() for entry in selections),
            ["Docs", "Photos", "new.txt", "notes.txt"],
        )
        self.assertEqual(scopes, [Path()])

    def test_changes_replay_matches_a_fresh_listing(self):
        cache = {}
        token = gdrivepull.start_page_token(self.drive)
        gdrivepull.prefetch_folders(self.drive, ["root"], cache)
        gdrivepull.save_snapshot(self.root, token, cache, ["root"])

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

        replayed, new_token = gdrivepull.apply_changes(self.drive, gdrivepull.load_snapshot(self.root))
        fresh = {}
        gdrivepull.prefetch_folders(self.drive, ["root"], fresh)
        self.assertEqual(replayed, fresh)
        self.assertEqual(new_token, "5")
        self.assertIsNone(gdrivepull.apply_changes(self.drive, {"token": "expired", "folders": {}}))

    def test_shared_views_get_their_own_folders(self):
        self.drive.items["s"] = file_item("s", "from Ann.pdf", "nobody", b"pdf", shared=True)
        self.drive.items["t"] = file_item("t", "plan.txt", "team", b"plan")
        self.drive.shared_drives = [{"id": "team", "name": "Team"}]
        views = gdrivepull.drive_views(self.drive, "root")
        self.assertEqual([view.name for view in views], ["My Drive", "Shared with me", "Team", "Trash"])
        cache = {}
        selections = []
        for view in views:
            gdrivepull.load_view(self.drive, view, cache)
            for entry in view.children_of.get(None, []):
                gdrivepull.add_selected_item(selections, entry["item"], entry["relative_parent"])
        browsed = {}
        for view in views:
            browsed.update(view.browsed)
        scopes = gdrivepull.build_local_scopes(selections, browsed, [view.base for view in views])
        self.assertEqual(scopes, [Path(), Path("Shared with me"), Path("Shared drives/Team")])
        state = gdrivepull.load_state(self.root)
        plan = gdrivepull.build_plan(self.drive, selections, scopes, self.root, state, cache)
        paths = {entry["relative_path"].as_posix(): entry["status"] for entry in plan}
        self.assertEqual(paths["Shared with me/from Ann.pdf"], "NEW")
        self.assertEqual(paths["Shared drives/Team/plan.txt"], "NEW")
        with mock.patch.object(gdrivepull, "console", Console(file=io.StringIO())):
            gdrivepull.apply_plan(self.drive, plan, self.root, state)
        # A later My Drive-only run does not report the view folders as local only.
        _, results = self.run_sync()
        self.assertEqual(results["FOLDER_LOCAL_ONLY"], 0)

    def test_space_line(self):
        cache = {}
        nodes, browsed = gdrivepull.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                gdrivepull.add_selected_item(selections, node["item"], node["relative_parent"])
        plan = gdrivepull.build_plan(
            self.drive, selections, gdrivepull.build_local_scopes(selections, browsed),
            self.root, gdrivepull.load_state(self.root), cache,
        )
        self.assertEqual(gdrivepull.download_size(plan), (19, 1))  # a, b, beach, notes; Report has no size
        with mock.patch.object(gdrivepull.shutil, "disk_usage", return_value=mock.Mock(free=2_000_000)):
            text, fits = gdrivepull.space_line(plan, self.root)
        self.assertEqual(text, "19 B to download + 1 exported file of unknown size · 2.0 MB free")
        self.assertTrue(fits)
        with mock.patch.object(gdrivepull.shutil, "disk_usage", return_value=mock.Mock(free=10)):
            self.assertFalse(gdrivepull.space_line(plan, self.root)[1])
        self.assertEqual(gdrivepull.space_line([], self.root), (None, True))

    def test_header_text(self):
        home = Path.home()
        self.assertEqual(
            gdrivepull.header_text("you@gmail.com", home / "GDrive", "preview"),
            "preview · you@gmail.com · downloads to ~/GDrive",
        )
        self.assertEqual(gdrivepull.header_text(None, Path("/data/x")), "downloads to /data/x")

    def test_destination_option(self):
        target = Path(self.temp.name) / "Once"
        self.assertEqual(gdrivepull.resolve_destination(str(target)), target)
        self.assertTrue(gdrivepull.has_valid_state(target))
        with self.assertRaises(OSError):
            gdrivepull.resolve_destination(str(Path(self.temp.name) / "missing" / "x"))

    def test_keep_both(self):
        self.run_sync()
        (self.root / "Docs/b.txt").write_bytes(b"edited locally")
        self.drive.items["d"] = file_item("d", "a.txt", "docs", b"same name")  # two Drive files, one path
        cache = {}
        nodes, browsed = gdrivepull.collect_drive_tree(self.drive, "root", cache)
        selections = []
        for node in nodes:
            if node["parent"] is None:
                gdrivepull.add_selected_item(selections, node["item"], node["relative_parent"])
        state = gdrivepull.load_state(self.root)
        plan = gdrivepull.build_plan(
            self.drive, selections, gdrivepull.build_local_scopes(selections, browsed), self.root, state, cache
        )
        conflicts = [entry for entry in plan if entry["status"] == "CONFLICT"]
        self.assertEqual(len(conflicts), 3)  # b edited, and both a.txt
        gdrivepull.set_keep_both(plan, plan, True)
        names = sorted(entry["destination"].name for entry in plan if entry["status"] == "KEEP_BOTH")
        self.assertEqual(names, ["a (Drive 2).txt", "a (Drive).txt", "b (Drive).txt"])
        gdrivepull.set_keep_both(plan, plan, False)
        self.assertEqual(len([entry for entry in plan if entry["status"] == "CONFLICT"]), 3)
        self.assertEqual(sorted(entry["destination"].name for entry in conflicts), ["a.txt", "a.txt", "b.txt"])
        gdrivepull.set_keep_both(plan, plan, True)

        with mock.patch.object(gdrivepull, "console", Console(file=io.StringIO())):
            results = gdrivepull.apply_plan(self.drive, plan, self.root, state)
        self.assertEqual(results["FILE_KEEP_BOTH"], 3)
        self.assertEqual((self.root / "Docs/b.txt").read_bytes(), b"edited locally")
        self.assertEqual((self.root / "Docs/b (Drive).txt").read_bytes(), b"bravo")
        # Copies are not tracked: next time they are local only, never moved to recovery.
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["Docs/b (Drive).txt"], "LOCAL_ONLY")
        self.assertEqual(gdrivepull.drive_copy_path(Path("/x/.env"), set()), Path("/x/.env (Drive)"))

    def test_recovery_list_and_empty(self):
        self.run_sync()
        del self.drive.items["p"]
        self.run_sync()
        runs = gdrivepull.recovery_runs(self.root)
        self.assertEqual(len(runs), 1)
        self.assertEqual([path.name for path in runs[0]["files"]], ["beach.jpg"])
        self.assertIn("1 file removed from Drive set aside", gdrivepull.recovery_note(self.root))
        self.assertIn("Removed from Drive", gdrivepull.recovery_note(self.root, interactive=True))
        old = self.root / gdrivepull.RECOVERY_DIR_NAME / "20200101-000000"
        old.mkdir()
        (old / "old.txt").write_text("old")

        output = io.StringIO()
        with mock.patch.object(gdrivepull, "console", Console(file=output)):
            gdrivepull.show_recovery(self.root)
            gdrivepull.empty_recovery(self.root, older_than=30, assume_yes=True)
        self.assertIn("2020-01-01 00:00", output.getvalue())
        self.assertFalse(old.exists())
        self.assertEqual(len(gdrivepull.recovery_runs(self.root)), 1)  # the recent run stays

        with mock.patch.object(gdrivepull, "console", Console(file=io.StringIO())) as quiet:
            quiet.input = mock.Mock(return_value="no")
            gdrivepull.empty_recovery(self.root)
            self.assertEqual(len(gdrivepull.recovery_runs(self.root)), 1)
            quiet.input = mock.Mock(return_value="empty")
            gdrivepull.empty_recovery(self.root)
        self.assertFalse((self.root / gdrivepull.RECOVERY_DIR_NAME).exists())
        self.assertIsNone(gdrivepull.recovery_note(self.root))

    def test_existing_identical_file_is_adopted(self):
        (self.root / "notes.txt").write_bytes(b"notes")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["notes.txt"], "UNCHANGED")

    def test_interrupted_download_leaves_no_part_file(self):
        def interrupted(service, item, file_handle, stop=None):
            file_handle.write(b"partial")
            raise KeyboardInterrupt

        entry = {"destination": self.root / "x.txt", "item": self.drive.items["a"]}
        with mock.patch.object(gdrivepull, "fetch_media", interrupted):
            with self.assertRaises(KeyboardInterrupt):
                gdrivepull.download_file_atomically(self.drive, entry)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [gdrivepull.STATE_FILE_NAME])

    def test_destination_must_be_empty_or_managed(self):
        other = Path(self.temp.name) / "Other"
        other.mkdir()
        (other / "file").write_text("x")
        with self.assertRaises(ValueError):
            gdrivepull.initialize_managed_destination(other)
        self.assertEqual(gdrivepull.initialize_managed_destination(self.root), self.root)


class SignInTest(unittest.TestCase):
    def test_read_only_token_asks_for_full_access(self):
        with tempfile.TemporaryDirectory() as temp:
            token = Path(temp) / "token.json"
            token.write_text(
                '{"token": "t", "refresh_token": "r", "client_id": "c", "client_secret": "s", '
                '"scopes": ["https://www.googleapis.com/auth/drive.readonly"], "expiry": "2999-01-01T00:00:00Z"}'
            )
            new = mock.Mock(to_json=lambda: "{}")
            with mock.patch.object(gdrivepull, "TOKEN_PATH", token), \
                    mock.patch.object(gdrivepull, "sign_in", return_value=new) as sign_in:
                self.assertIs(gdrivepull.authenticate(), new)
            self.assertIn("full Drive access", sign_in.call_args[0][0])


class KeyListTest(unittest.TestCase):
    NAMES = {"question_mark": "?", "slash": "/", "escape": "esc", "B": "shift+b", "T": "shift+t", "X": "shift+x",
             "up": "↑", "down": "↓", "left": "←", "right": "→"}

    def listed(self, context):
        return {
            token
            for _, entries in gdrivepull.KEYS[context][1]
            for keys, _, description in entries
            for token in keys.split() + [description]
        }

    def test_every_binding_is_listed(self):
        screens = {
            ("tree", "trash", "removed"): [gdrivepull.DriveSelectorApp, gdrivepull.DriveTree],
            "confirm": [gdrivepull.ConfirmScreen],
            "preview": [gdrivepull.PreviewScreen],
            "preview-again": [gdrivepull.StandalonePreviewScreen],
            "folder": [gdrivepull.DestinationSetupScreen],
            "help": [gdrivepull.HelpScreen],
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
        lines = "\n".join(gdrivepull.help_lines())
        for title, _ in gdrivepull.KEYS.values():
            self.assertIn(title.lower(), lines)


class SelectorTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(gdrivepull.TRASHED_FOLDERS.clear)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = gdrivepull.initialize_managed_destination(Path(self.temp.name) / "GDrive")

    def make_app(self, drive=None, views=None):
        drive = drive or demo_drive()
        cache = {}
        views = views or [gdrivepull.DriveView("My Drive", "root")]
        gdrivepull.load_view(drive, views[0], cache)
        return gdrivepull.DriveSelectorApp(views, self.root, gdrivepull.load_state(self.root), drive, None, cache)

    def selected(self, app):
        return [gdrivepull.selected_path(entry).as_posix() for entry in app.selections]

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
            self.assertIsInstance(app.screen, gdrivepull.PreviewScreen)
            self.assertIn("to download", str(app.screen.query_one("#preview-summary").render()))
            table = app.screen.query_one("DataTable")
            self.assertEqual(table.row_count, 5)  # Docs/, a, b, Report, and Survey skipped
            await pilot.press("tab", "tab")  # everything, then each status: new
            self.assertEqual(table.row_count, 4)
            await pilot.press("escape")  # back to the tree, selection kept
            self.assertNotIsInstance(app.screen, gdrivepull.PreviewScreen)
            self.assertEqual(self.selected(app), ["Docs"])
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("b", "a", "c", "z")  # no conflict here; tree keys do nothing behind the preview
            self.assertEqual(self.selected(app), ["Docs"])
            self.assertEqual(app.sort, "name")
            await pilot.press("y")
        result = app.return_value
        self.assertEqual([gdrivepull.selected_path(entry).as_posix() for entry in result["selections"]], ["Docs"])
        self.assertEqual(result["destination"], self.root)
        self.assertEqual(len(result["plan"]), 5)

    async def test_views_load_on_tab(self):
        drive = demo_drive()
        drive.items["t"] = file_item("t", "plan.txt", "team", b"plan")
        drive.shared_drives = [{"id": "team", "name": "Team"}]
        views = gdrivepull.drive_views(drive, "root")
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
                self.assertIsInstance(app.screen, gdrivepull.DestinationSetupScreen)
                app.screen.query_one("#parent-path", Input).value = temp
                app.screen.query_one("#folder-name", Input).value = "Session"
                await pilot.press("ctrl+s")
                await pilot.pause()
                self.assertEqual(app.destination, Path(temp).resolve() / "Session")
                self.assertTrue(gdrivepull.has_valid_state(app.destination))
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
        views = [gdrivepull.DriveView("My Drive", "root")]
        app = self.make_app(drive, views)
        async with app.run_test() as pilot:
            await pilot.press("z", "down", "space", "d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("y")
        result = app.return_value
        with mock.patch.object(gdrivepull, "fetch_media", fake_fetch), \
                mock.patch.object(gdrivepull, "console", Console(file=io.StringIO())):
            args = mock.Mock(jobs=1)
            results = gdrivepull.run_downloads(
                args, drive, lambda: drive, result["plan"], result["selections"], result["scopes"], [],
                self.root, result["state"], app.cache, "0",
            )
        notice = gdrivepull.result_notice(result["plan"], results, self.root)
        self.assertIn("3 downloaded", notice.plain)

        # The next round: same view and sort, the result on top, marks from the download, nothing selected.
        again = gdrivepull.DriveSelectorApp(
            views, self.root, gdrivepull.load_state(self.root), drive, None, app.cache,
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
        views = gdrivepull.drive_views(drive, "root")
        app = self.make_app(drive, views)
        tree_names = lambda: [node.data["item"]["name"] for node in app.query_one(Tree).root.children]  # noqa: E731
        async with app.run_test() as pilot:
            await pilot.press("down", "down", "x")  # Photos, moved to the Drive trash after y
            self.assertIsInstance(app.screen, gdrivepull.ConfirmScreen)
            await pilot.press("y")
            await self.wait(app, pilot)
            self.assertTrue(drive.items["photos"]["trashed"])
            self.assertEqual(tree_names(), ["Docs", "notes.txt"])

            await pilot.press("shift+tab", "shift+tab")  # Trash, just before Removed from Drive
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
            self.assertIsInstance(app.screen, gdrivepull.ConfirmScreen)
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
            await pilot.press("tab", "tab")  # past Removed from Drive, to My Drive
            await self.wait(app, pilot)
            await pilot.press("r")
            self.assertNotIsInstance(app.screen, gdrivepull.ConfirmScreen)
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

    async def test_removed_from_drive_view(self):
        run = self.root / gdrivepull.RECOVERY_DIR_NAME / "20260101-120000"
        (run / "Photos").mkdir(parents=True)
        (run / "Photos/beach.jpg").write_bytes(b"jpeg")
        (run / "notes.txt").write_bytes(b"old notes")
        (run / "keep.txt").write_bytes(b"keep")
        (self.root / "notes.txt").write_bytes(b"current notes")
        app = self.make_app()
        async with app.run_test() as pilot:
            self.assertIn("Removed from Drive (3)", str(app.query_one("#views").render()))
            await pilot.press("shift+tab")  # the last view
            self.assertEqual(app.view.root_id, gdrivepull.REMOVED)
            self.assertEqual(app.query_one("#keys").context, "removed")
            self.assertFalse(app.query_one(Tree).display)
            names = sorted(relative.as_posix() for _, relative, _ in app.removed_rows)
            self.assertEqual(names, ["Photos/beach.jpg", "keep.txt", "notes.txt"])
            await pilot.press("space", "a", "slash", "z")  # tree keys do nothing here
            self.assertEqual(app.selections, [])
            self.assertFalse(app.query_one(Input).display)

            def go(name):
                rows = [relative.as_posix() for _, relative, _ in app.removed_rows]
                app.query_one("#removed-table").move_cursor(row=rows.index(name))

            go("notes.txt")
            await pilot.press("r")  # taken in the download folder: stays set aside
            self.assertEqual((self.root / "notes.txt").read_bytes(), b"current notes")
            self.assertEqual(len(app.removed_rows), 3)
            go("Photos/beach.jpg")
            await pilot.press("r")
            self.assertEqual((self.root / "Photos/beach.jpg").read_bytes(), b"jpeg")
            self.assertFalse((run / "Photos").exists())  # empty folders go too
            self.assertIn("Removed from Drive (2)", str(app.query_one("#views").render()))
            go("keep.txt")
            await pilot.press("x", "y")
            await pilot.pause()
            self.assertFalse((run / "keep.txt").exists())
            await pilot.press("X", *"empty", "enter")
            await pilot.pause()
            self.assertFalse((self.root / gdrivepull.RECOVERY_DIR_NAME).exists())
            self.assertIn("Nothing set aside", str(app.query_one("#removed-summary").render()))
            await pilot.press("tab")  # back to My Drive, with the tree
            self.assertTrue(app.query_one(Tree).display)
            self.assertEqual(app.query_one("#keys").context, "tree")
            await pilot.press("q")

    async def test_help_and_expand_all(self):
        app = self.make_app()
        async with app.run_test() as pilot:
            await pilot.press("question_mark")
            self.assertIsInstance(app.screen, gdrivepull.HelpScreen)
            await pilot.press("escape")
            self.assertNotIsInstance(app.screen, gdrivepull.HelpScreen)
            await pilot.press("e")
            tree = app.query_one(Tree)
            self.assertTrue(all(node.is_expanded for node in tree.root.children if node.allow_expand))
            await pilot.press("q")


if __name__ == "__main__":
    unittest.main()

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


class FakeDrive:
    """In-memory Drive: items are dicts with id, name, mimeType, parent and optional content."""

    def __init__(self, items, page_size=1000):
        self.items = {item["id"]: dict(item) for item in items}
        self.page_size = page_size
        self.batches = []
        self.failures = {}  # folder id -> errors raised by its next listings

    def files(self):
        return self

    def new_batch_http_request(self, callback):
        return FakeBatch(self, callback)

    def public(self, item):
        fields = ("id", "name", "mimeType", "size", "modifiedTime", "md5Checksum", "shortcutDetails")
        return {key: item[key] for key in fields if key in item}

    def list(self, q, pageToken=None, **kwargs):
        folder_id = q.split("'")[1]

        def run():
            failures = self.failures.get(folder_id)
            if failures:
                raise failures.pop(0)
            children = [self.public(item) for item in self.items.values() if item.get("parent") == folder_id]
            start = int(pageToken or 0)
            page = children[start:start + self.page_size]
            response = {"files": page}
            if start + self.page_size < len(children):
                response["nextPageToken"] = str(start + self.page_size)
            return response

        return FakeRequest(run)

    def get(self, fileId, **kwargs):
        return FakeRequest(lambda: self.public(self.items[fileId]))

    def get_media(self, fileId, **kwargs):
        return self.items[fileId]["content"]

    def export_media(self, fileId, mimeType):
        return self.items[fileId]["content"]


def fake_fetch(service, item, file_handle):
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

    def run_sync(self):
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
            results = gdrivepull.apply_plan(self.drive, plan, self.root, state)
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

    def test_existing_identical_file_is_adopted(self):
        (self.root / "notes.txt").write_bytes(b"notes")
        statuses, _ = self.run_sync()
        self.assertEqual(statuses["notes.txt"], "UNCHANGED")

    def test_interrupted_download_leaves_no_part_file(self):
        def interrupted(service, item, file_handle):
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


class SelectorTest(unittest.IsolatedAsyncioTestCase):
    def make_app(self):
        nodes, _ = gdrivepull.collect_drive_tree(demo_drive(), "root", {})
        return gdrivepull.DriveSelectorApp(nodes, "My Drive", Path("/tmp/GDrive"))

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
        self.assertEqual([gdrivepull.selected_path(entry).as_posix() for entry in app.return_value], ["Docs"])

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
        self.assertEqual(app.return_value, [])

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

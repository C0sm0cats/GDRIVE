"""Render the Drive tree with demo files into docs/screenshot.svg (the README screenshot).

Run from the repository root:  .venv/bin/python docs/make_screenshot.py
It runs the real selector headless (Textual's test driver) and exports its screen as SVG.
"""
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import gdrivepull  # noqa: E402

COLUMNS, ROWS = 110, 21
OUT = os.path.join(ROOT, "docs", "screenshot.svg")
FOLDER = gdrivepull.FOLDER_MIME_TYPE


def demo_nodes():
    now = datetime.now(timezone.utc)
    tree = []  # (parent index, name, mime type, size, days ago)

    def add(parent, name, mime_type=FOLDER, size=None, days=0):
        tree.append((parent, name, mime_type, size, days))
        return len(tree) - 1

    documents = add(None, "Documents")
    add(None, "Photos")
    projects = add(None, "Projects")
    add(None, "notes.txt", "text/plain", 4_200, 0)
    taxes = add(documents, "Taxes")
    add(documents, "Budget 2026", "application/vnd.google-apps.spreadsheet", None, 3)
    add(documents, "Lease agreement.pdf", "application/pdf", 1_830_000, 140)
    add(documents, "Meeting notes", "application/vnd.google-apps.document", None, 1)
    add(documents, "Signup form", "application/vnd.google-apps.form", None, 30)
    add(taxes, "2025 return.pdf", "application/pdf", 640_000, 160)
    add(taxes, "Receipts.zip", "application/zip", 48_500_000, 170)
    add(projects, "roadmap.md", "text/markdown", 12_000, 6)

    nodes = []
    for index, (parent, name, mime_type, size, days) in enumerate(tree):
        item = {"id": f"id{index}", "name": name, "mimeType": mime_type,
                "modifiedTime": (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")}
        if size:
            item["size"] = str(size)
        relative_parent = Path() if parent is None else (
            nodes[parent]["relative_parent"] / gdrivepull.local_name(nodes[parent]["item"]))
        display = name if parent is None else f"{nodes[parent]['display_path']}/{name}"
        nodes.append({"index": index, "parent": parent, "item": item,
                      "relative_parent": relative_parent, "display_path": display})
    # Photos: many files, for the folder totals.
    photos = 1
    for number in range(1, 7):
        index = len(nodes)
        nodes.append({"index": index, "parent": photos, "relative_parent": Path("Photos"),
                      "display_path": f"Photos/IMG_{number:04}.jpg",
                      "item": {"id": f"id{index}", "name": f"IMG_{number:04}.jpg", "mimeType": "image/jpeg",
                               "size": str(3_100_000 + number * 170_000), "modifiedTime": "2026-08-14T10:00:00Z"}})
    return nodes


def plain_svg(svg):
    """Turn Textual's SVG into one without <style>, web fonts or clip paths, with a fixed size.

    Some Markdown viewers draw nothing for the original (no width / height, CSS classes, remote fonts).
    """
    style = re.search(r"<style>(.*?)</style>", svg, re.S).group(1)
    rules = {}
    for name, body in re.findall(r"\.(terminal-[\w-]+)\s*\{([^}]*)\}", style):
        attributes = []
        for declaration in filter(None, (part.strip() for part in body.split(";"))):
            key, value = (part.strip() for part in declaration.split(":", 1))
            if key == "font-family" and name.endswith("-matrix"):
                value = "'Fira Code', 'JetBrains Mono', 'DejaVu Sans Mono', Menlo, Consolas, monospace"
            elif key == "font-size":
                value = value.removesuffix("px")
            elif key not in {"fill", "font-weight", "font-family"}:
                continue
            attributes.append(f'{key}="{value}"')
        rules[name] = " ".join(attributes)

    svg = re.sub(r"<style>.*?</style>", "", svg, flags=re.S)
    svg = re.sub(r"<defs>.*?</defs>", "", svg, flags=re.S)
    svg = re.sub(r'\s*clip-path="[^"]*"', "", svg)
    svg = re.sub(r'class="([^"]*)"', lambda match: rules.get(match.group(1), ""), svg)
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S)
    width, height = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups()
    return svg.replace("<svg ", f'<svg width="{width}" height="{height}" ', 1)


async def render():
    app = gdrivepull.DriveSelectorApp(demo_nodes(), "My Drive", Path.home() / "GDrive")
    async with app.run_test(size=(COLUMNS, ROWS)) as pilot:
        # Documents and Taxes open, Photos and a document selected, cursor on the lease.
        by_name = {entry["item"]["name"]: entry for entry in app.nodes}
        for name in ("Documents", "Taxes"):
            app.expand(app.tree_nodes[by_name[name]["index"]])
        for name in ("Photos", "Meeting notes"):
            app.add_entry(by_name[name])
        app.refresh_node_labels()
        await pilot.pause()
        app.query_one("#drive-tree").move_cursor(app.tree_nodes[by_name["Lease agreement.pdf"]["index"]])
        await pilot.pause()
        return app.export_screenshot(title="gdrivepull")


if __name__ == "__main__":
    svg = asyncio.run(render())
    svg = plain_svg(svg)
    with open(OUT, "w") as file:
        file.write(svg)
    print(f"wrote {os.path.relpath(OUT, ROOT)}")

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Static,
    Tree,
)


APP_NAME = "GDrive Pull"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
SCRIPT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = SCRIPT_DIR / "settings.json"
STATE_FILE_NAME = ".gdrivepull-managed-state.json"
RECOVERY_DIR_NAME = ".gdrivepull-recovery"
STATE_VERSION = 1
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"

# Google-native files must be exported because they have no downloadable binary.
EXPORT_FORMATS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.script": (
        "application/vnd.google-apps.script+json",
        ".json",
    ),
}


def authenticate():
    token_path = SCRIPT_DIR / "token.json"
    credentials_path = SCRIPT_DIR / "credentials.json"
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def read_configured_destination():
    if not SETTINGS_PATH.exists():
        return None
    try:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        download_path = settings.get("download_path")
        if not isinstance(download_path, str) or not download_path:
            raise ValueError("missing download_path")
        destination = Path(download_path)
        if not destination.is_absolute():
            raise ValueError("download_path must be absolute")
        return destination
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[WARNING] Ignoring invalid settings file: {error}")
        return None


def write_configured_destination(destination):
    temporary_path = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(
            {"download_path": str(destination)},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, SETTINGS_PATH)


def has_valid_state(destination):
    state_path = destination / STATE_FILE_NAME
    if not state_path.is_file():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return (
            state.get("version") == STATE_VERSION
            and isinstance(state.get("files"), dict)
        )
    except (OSError, json.JSONDecodeError):
        return False


def list_children(service, folder_id):
    items = []
    page_token = None

    while True:
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                spaces="drive",
                fields=(
                    "nextPageToken,"
                    "files(id,name,mimeType,size,modifiedTime,md5Checksum,"
                    "shortcutDetails)"
                ),
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        items.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return sorted(
        items,
        key=lambda item: (
            effective_mime_type(item) != FOLDER_MIME_TYPE,
            item["name"].casefold(),
        ),
    )


def effective_mime_type(item):
    if item["mimeType"] == SHORTCUT_MIME_TYPE:
        return item.get("shortcutDetails", {}).get(
            "targetMimeType", SHORTCUT_MIME_TYPE
        )
    return item["mimeType"]


def effective_id(item):
    if item["mimeType"] == SHORTCUT_MIME_TYPE:
        return item.get("shortcutDetails", {}).get("targetId", item["id"])
    return item["id"]


def resolve_file_shortcut(service, item):
    if item["mimeType"] != SHORTCUT_MIME_TYPE:
        return item
    if effective_mime_type(item) == FOLDER_MIME_TYPE:
        return item

    target = (
        service.files()
        .get(
            fileId=effective_id(item),
            fields="id,mimeType,size,modifiedTime,md5Checksum",
            supportsAllDrives=True,
        )
        .execute()
    )
    target["name"] = item["name"]
    return target


def item_kind(item):
    mime_type = effective_mime_type(item)
    if mime_type == FOLDER_MIME_TYPE:
        return "FOLDER"
    return "FILE"


def display_size(item):
    size = item.get("size")
    if not size:
        return "-"

    value = float(size)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return "-"


def selected_path(entry):
    return entry["relative_parent"] / local_name(entry["item"])


def path_selection_state(path, selections):
    for entry in selections:
        entry_path = selected_path(entry)
        if entry_path == path:
            return "x"
        if (
            effective_mime_type(entry["item"]) == FOLDER_MIME_TYPE
            and entry_path in path.parents
        ):
            return "*"
    return " "


def add_selected_item(selections, item, relative_parent):
    candidate = {
        "item": item,
        "relative_parent": relative_parent,
    }
    candidate_path = selected_path(candidate)

    for entry in selections:
        entry_path = selected_path(entry)
        if entry_path == candidate_path:
            return False
        if (
            effective_mime_type(entry["item"]) == FOLDER_MIME_TYPE
            and entry_path in candidate_path.parents
        ):
            return False

    if effective_mime_type(item) == FOLDER_MIME_TYPE:
        selections[:] = [
            entry
            for entry in selections
            if candidate_path not in selected_path(entry).parents
        ]
    selections.append(candidate)
    selections.sort(
        key=lambda entry: (
            effective_mime_type(entry["item"]) != FOLDER_MIME_TYPE,
            selected_path(entry).as_posix().casefold(),
        )
    )
    return True


def compact_scopes(scopes):
    compacted = []
    for scope in sorted(set(scopes), key=lambda path: (len(path.parts), path.as_posix())):
        if any(
            parent == Path()
            or parent == scope
            or parent in scope.parents
            for parent in compacted
        ):
            continue
        compacted.append(scope)
    return compacted


def build_local_scopes(selections, browsed_directories):
    scopes = [
        selected_path(entry)
        for entry in selections
        if effective_mime_type(entry["item"]) == FOLDER_MIME_TYPE
    ]

    root_items = browsed_directories.get(Path(), [])
    if root_items and all(
            path_selection_state(
                Path(local_name(item)), selections
            )
            in {"x", "*"}
            for item in root_items
    ):
        scopes.append(Path())

    return compact_scopes(scopes)


def initialize_managed_destination(destination):
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("The final destination is not a directory.")
        if has_valid_state(destination):
            return destination
        if any(destination.iterdir()):
            raise ValueError(
                "This destination already contains files but has no "
                f"{STATE_FILE_NAME}. Choose a different managed folder that "
                "is empty or does not exist yet."
            )
    else:
        destination.mkdir(parents=False)

    save_state(
        destination,
        {"version": STATE_VERSION, "files": {}},
    )
    return destination


class DestinationSetupApp(App):
    TITLE = APP_NAME
    SUB_TITLE = "Destination setup"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: #07111f;
        color: #dbeafe;
    }

    Header {
        background: #0f2742;
        color: #f8fafc;
    }

    #setup-body {
        height: 1fr;
        padding: 1 2;
    }

    #directory-tree {
        width: 1fr;
        height: 1fr;
        border: round #38bdf8;
        background: #081525;
    }

    #setup-form {
        width: 1fr;
        height: 1fr;
        margin-left: 2;
        padding: 1 2;
        border: round #22c55e;
        background: #0b1b2e;
    }

    .field-title {
        height: 2;
        margin-top: 1;
        color: #93c5fd;
        text-style: bold;
    }

    Input {
        margin-bottom: 1;
        border: round #475569;
    }

    Input:focus {
        border: round #38bdf8;
    }

    #final-path {
        min-height: 4;
        margin-top: 1;
        padding: 1;
        background: #0f2742;
        color: #e0f2fe;
    }

    #setup-status {
        min-height: 4;
        margin-top: 1;
        color: #fca5a5;
    }

    #setup-actions {
        height: 3;
        margin-top: 1;
    }

    Button {
        margin-right: 1;
    }

    Footer {
        background: #07111f;
        color: #bae6fd;
    }
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "Save"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, initial_destination):
        super().__init__()
        self.initial_destination = initial_destination

    def compose(self) -> ComposeResult:
        initial_parent = self.initial_destination.parent
        if not initial_parent.is_dir():
            initial_parent = Path.home()

        yield Header(show_clock=True)
        with Horizontal(id="setup-body"):
            yield DirectoryTree(
                initial_parent,
                id="directory-tree",
            )
            with Vertical(id="setup-form"):
                yield Static(
                    "Choose a parent directory, then name the managed folder.",
                )
                yield Static("PARENT DIRECTORY", classes="field-title")
                yield Input(
                    value=str(initial_parent),
                    id="parent-path",
                )
                yield Static("MANAGED FOLDER NAME", classes="field-title")
                yield Input(
                    value=self.initial_destination.name or "GDrive",
                    id="folder-name",
                )
                yield Static(id="final-path")
                yield Static(id="setup-status")
                with Horizontal(id="setup-actions"):
                    yield Button(
                        "Create / Use",
                        id="save-destination",
                        variant="success",
                    )
                    yield Button(
                        "Cancel",
                        id="cancel-setup",
                        variant="default",
                    )
        yield Footer()

    def on_mount(self):
        self.update_final_path()

    @on(DirectoryTree.DirectorySelected)
    def directory_selected(self, event):
        self.query_one("#parent-path", Input).value = str(
            event.path.resolve()
        )
        self.update_final_path()

    @on(Input.Changed)
    def input_changed(self):
        self.update_final_path()

    @on(Button.Pressed)
    def button_pressed(self, event):
        if event.button.id == "save-destination":
            self.action_save()
        elif event.button.id == "cancel-setup":
            self.action_cancel()

    def destination_from_inputs(self):
        parent_text = self.query_one("#parent-path", Input).value.strip()
        folder_name = self.query_one("#folder-name", Input).value.strip()
        if not parent_text:
            raise ValueError("Choose a parent directory.")

        parent = Path(parent_text).expanduser()
        if not parent.is_absolute():
            raise ValueError(
                "The parent directory must be an absolute path or start with ~."
            )
        parent = parent.resolve(strict=True)
        if not parent.is_dir():
            raise ValueError("The parent path is not a directory.")

        if (
            not folder_name
            or folder_name in {".", ".."}
            or Path(folder_name).name != folder_name
        ):
            raise ValueError("Enter a valid single folder name.")
        return parent / folder_name

    def update_final_path(self):
        preview = self.query_one("#final-path", Static)
        status = self.query_one("#setup-status", Static)
        status.update("")
        try:
            destination = self.destination_from_inputs()
            preview.update(
                Text.assemble(
                    ("FINAL DESTINATION\n", "bold bright_cyan"),
                    (str(destination), "bright_white"),
                )
            )
        except (OSError, ValueError) as error:
            preview.update(
                Text(str(error), style="bold bright_red")
            )

    def action_save(self):
        status = self.query_one("#setup-status", Static)
        try:
            destination = self.destination_from_inputs()
            destination = initialize_managed_destination(destination)
            write_configured_destination(destination)
        except (OSError, ValueError) as error:
            status.update(
                Text(str(error), style="bold bright_red")
            )
            return
        self.exit(destination)

    def action_cancel(self):
        self.exit(None)


def configure_destination(force_configuration=False):
    configured = read_configured_destination()
    if (
        configured is not None
        and not force_configuration
        and has_valid_state(configured)
    ):
        return configured

    if configured is not None and not has_valid_state(configured):
        print(
            "[WARNING] The configured destination is missing a valid "
            f"{STATE_FILE_NAME}."
        )
    initial_destination = configured or (Path.home() / "GDrive")
    return DestinationSetupApp(initial_destination).run()


def collect_drive_tree(
    service,
    folder_id,
    relative_parent,
    display_parts,
    nodes,
    browsed_directories,
    ancestor_folder_ids=frozenset(),
):
    items = list_children(service, folder_id)
    browsed_directories[relative_parent] = items

    for item in items:
        node = {
            "item": item,
            "relative_parent": relative_parent,
            "display_path": "/".join(display_parts + [item["name"]]),
        }
        nodes.append(node)

        if effective_mime_type(item) != FOLDER_MIME_TYPE:
            continue
        child_folder_id = effective_id(item)
        if child_folder_id in ancestor_folder_ids:
            continue
        collect_drive_tree(
            service,
            child_folder_id,
            relative_parent / local_name(item),
            display_parts + [item["name"]],
            nodes,
            browsed_directories,
            ancestor_folder_ids | {child_folder_id},
        )


class DriveSelectorApp(App):
    TITLE = APP_NAME
    SUB_TITLE = "Select files and folders"
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    Screen {
        background: #07111f;
        color: #dbeafe;
    }

    Header {
        background: #0f2742;
        color: #f8fafc;
    }

    #instructions {
        height: 3;
        padding: 1 2;
        background: #0b1b2e;
        color: #93c5fd;
    }

    #drive-tree {
        height: 1fr;
        margin: 1 2;
        padding: 0 1;
        border: round #38bdf8;
        background: #081525;
    }

    #selection-summary {
        height: 3;
        padding: 1 2;
        background: #0f2742;
        color: #e0f2fe;
    }

    Footer {
        background: #07111f;
        color: #bae6fd;
    }

    Tree > .tree--cursor {
        background: #164e63;
        color: #ffffff;
        text-style: bold;
    }

    Tree > .tree--guides {
        color: #334155;
    }
    """

    BINDINGS = [
        Binding("space", "toggle_current", "Select", priority=True),
        Binding("enter", "activate_current", "Expand", priority=True),
        Binding("a", "select_all", "Select all"),
        Binding("c", "clear_selection", "Clear"),
        Binding("d", "confirm", "Download"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, nodes):
        super().__init__()
        self.nodes = nodes
        self.selections = []
        self.tree_nodes = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            Text.assemble(
                ("FOLDERS", "bold bright_cyan"),
                (" are cyan   "),
                ("FILES", "bold bright_green"),
                (" are green   |   SPACE select   |   ENTER expand/collapse"),
            ),
            id="instructions",
        )
        yield Tree(
            Text("Google Drive", style="bold bright_blue"),
            id="drive-tree",
        )
        yield Static(id="selection-summary")
        yield Footer()

    def on_mount(self):
        tree = self.query_one("#drive-tree", Tree)
        parent_nodes = {Path(): tree.root}

        for entry in self.nodes:
            relative_parent = entry["relative_parent"]
            parent_node = parent_nodes.get(relative_parent, tree.root)
            is_folder = (
                effective_mime_type(entry["item"]) == FOLDER_MIME_TYPE
            )
            tree_node = parent_node.add(
                self.node_label(entry),
                data=entry,
                expand=False,
                allow_expand=is_folder,
            )
            self.tree_nodes.append(tree_node)
            if is_folder:
                parent_nodes[selected_path(entry)] = tree_node

        tree.root.expand()
        tree.focus()
        self.update_selection_summary()

    def node_label(self, entry):
        state = path_selection_state(
            selected_path(entry), self.selections
        )
        checkbox = {"x": "☑", "*": "◩"}.get(state, "☐")
        label = Text()
        label.append(
            f"{checkbox} ",
            style="bold bright_green" if state != " " else "bright_black",
        )
        if effective_mime_type(entry["item"]) == FOLDER_MIME_TYPE:
            label.append("FOLDER  ", style="bold bright_cyan")
            label.append(entry["item"]["name"], style="cyan")
        else:
            label.append("FILE    ", style="bold bright_green")
            label.append(entry["item"]["name"], style="bright_white")
            size = display_size(entry["item"])
            if size != "-":
                label.append(f"   {size}", style="dim")
        return label

    def refresh_node_labels(self):
        for tree_node in self.tree_nodes:
            tree_node.set_label(self.node_label(tree_node.data))
        self.update_selection_summary()

    def update_selection_summary(self):
        folders = sum(
            effective_mime_type(entry["item"]) == FOLDER_MIME_TYPE
            for entry in self.selections
        )
        files = len(self.selections) - folders
        summary = self.query_one("#selection-summary", Static)
        summary.update(
            f"Selected: {len(self.selections)}  |  "
            f"{folders} folder{'s' if folders != 1 else ''}  |  "
            f"{files} file{'s' if files != 1 else ''}"
        )

    def current_entry(self):
        tree = self.query_one("#drive-tree", Tree)
        node = tree.cursor_node
        return None if node is None else node.data

    def toggle_entry(self, entry):
        if entry is None:
            return
        target_path = selected_path(entry)
        exact_index = next(
            (
                index
                for index, selected in enumerate(self.selections)
                if selected_path(selected) == target_path
            ),
            None,
        )
        if exact_index is not None:
            self.selections.pop(exact_index)
        elif path_selection_state(target_path, self.selections) == "*":
            self.notify(
                "This item is included by a selected parent folder.",
                severity="warning",
            )
            return
        else:
            add_selected_item(
                self.selections,
                entry["item"],
                entry["relative_parent"],
            )
        self.refresh_node_labels()

    def action_toggle_current(self):
        self.toggle_entry(self.current_entry())

    def action_activate_current(self):
        tree = self.query_one("#drive-tree", Tree)
        node = tree.cursor_node
        if node is None:
            return
        if node.data is None:
            node.toggle()
        elif effective_mime_type(node.data["item"]) == FOLDER_MIME_TYPE:
            node.toggle()

    def action_select_all(self):
        self.selections.clear()
        for entry in self.nodes:
            add_selected_item(
                self.selections,
                entry["item"],
                entry["relative_parent"],
            )
        self.refresh_node_labels()

    def action_clear_selection(self):
        self.selections.clear()
        self.refresh_node_labels()

    def action_confirm(self):
        if not self.selections:
            self.notify("Select at least one item.", severity="warning")
            return
        self.exit(list(self.selections))

    def action_cancel(self):
        self.exit([])


def browse_and_select(service, root_folder_id):
    nodes = []
    browsed_directories = {}
    print("[INFO] Loading the Drive tree...")
    collect_drive_tree(
        service,
        root_folder_id,
        Path(),
        [],
        nodes,
        browsed_directories,
    )
    if not nodes:
        print("[INFO] No files or folders are visible at this location.")
        return [], []

    selections = DriveSelectorApp(nodes).run()
    if not selections:
        return [], []
    return selections, build_local_scopes(
        selections, browsed_directories
    )


def safe_name(name):
    name = name.replace("/", "_").replace("\0", "")
    return name if name not in {"", ".", ".."} else "_unnamed_"


def export_name(name, suffix):
    if name.casefold().endswith(suffix.casefold()):
        return name
    return name + suffix


def local_name(item):
    name = safe_name(item["name"])
    mime_type = effective_mime_type(item)
    if mime_type in EXPORT_FORMATS:
        name = export_name(name, EXPORT_FORMATS[mime_type][1])
    return name


def file_hash(path, algorithm="sha256"):
    digest = hashlib.new(algorithm)
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remote_signature(item):
    if item.get("md5Checksum"):
        return f"md5:{item['md5Checksum']}"
    modified_time = item.get("modifiedTime", "")
    size = item.get("size", "")
    return f"metadata:{modified_time}:{size}:{effective_mime_type(item)}"


def state_key(item, relative_path):
    return f"{effective_id(item)}::{relative_path.as_posix()}"


def load_state(destination_root):
    state_path = destination_root / STATE_FILE_NAME
    if not state_path.exists():
        return {"version": STATE_VERSION, "files": {}}

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("version") != STATE_VERSION or not isinstance(
            state.get("files"), dict
        ):
            raise ValueError("unsupported state format")
        return state
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"[WARNING] Ignoring invalid state file {state_path}: {error}")
        return {"version": STATE_VERSION, "files": {}}


def save_state(destination_root, state):
    state_path = destination_root / STATE_FILE_NAME
    temporary_path = destination_root / f"{STATE_FILE_NAME}.tmp"
    state["version"] = STATE_VERSION
    temporary_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary_path, state_path)


class HashSink:
    def __init__(self):
        self.digest = hashlib.sha256()

    def write(self, data):
        self.digest.update(data)
        return len(data)

    def hexdigest(self):
        return self.digest.hexdigest()


def remote_export_hash(service, item):
    sink = HashSink()
    downloader = MediaIoBaseDownload(sink, download_request(service, item))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return sink.hexdigest()


def classify_file(service, item, relative_path, destination, state):
    if not destination.exists():
        return "NEW", None
    if not destination.is_file():
        return "CONFLICT", None

    current_sha256 = file_hash(destination)
    stored = state["files"].get(state_key(item, relative_path))
    if stored is None:
        stored = state["files"].get(effective_id(item))
    signature = remote_signature(item)

    if stored and stored.get("path") == relative_path.as_posix():
        if current_sha256 != stored.get("local_sha256"):
            return "CONFLICT", current_sha256
        if signature == stored.get("remote_signature"):
            return "UNCHANGED", current_sha256
        return "UPDATE", current_sha256

    remote_md5 = item.get("md5Checksum")
    if remote_md5 and file_hash(destination, "md5") == remote_md5:
        return "UNCHANGED", current_sha256

    if effective_mime_type(item) in EXPORT_FORMATS:
        try:
            if remote_export_hash(service, item) == current_sha256:
                return "UNCHANGED", current_sha256
        except HttpError as error:
            print(
                f"[WARNING] Could not compare {relative_path.as_posix()}: "
                f"{error}"
            )
    return "CONFLICT", current_sha256


def collect_plan(
    service,
    item,
    relative_parent,
    destination_root,
    state,
    plan,
    ancestor_folder_ids=frozenset(),
):
    mime_type = effective_mime_type(item)
    relative_path = relative_parent / local_name(item)
    destination = destination_root / relative_path

    if mime_type == FOLDER_MIME_TYPE:
        folder_id = effective_id(item)
        status = "UNCHANGED" if destination.is_dir() else "NEW"
        if relative_path in {
            Path(STATE_FILE_NAME),
            Path(RECOVERY_DIR_NAME),
        }:
            status = "CONFLICT"
        elif folder_id in ancestor_folder_ids:
            status = "SKIPPED"
        elif destination.exists() and not destination.is_dir():
            status = "CONFLICT"
        plan.append(
            {
                "status": status,
                "kind": "FOLDER",
                "item": item,
                "relative_path": relative_path,
                "destination": destination,
            }
        )
        if status in {"CONFLICT", "SKIPPED"}:
            return
        for child in list_children(service, effective_id(item)):
            collect_plan(
                service,
                child,
                relative_path,
                destination_root,
                state,
                plan,
                ancestor_folder_ids | {folder_id},
            )
        return

    item = resolve_file_shortcut(service, item)
    mime_type = effective_mime_type(item)
    relative_path = relative_parent / local_name(item)
    destination = destination_root / relative_path

    if relative_path in {
        Path(STATE_FILE_NAME),
        Path(RECOVERY_DIR_NAME),
    }:
        status = "CONFLICT"
        current_sha256 = None
    elif mime_type.startswith("application/vnd.google-apps.") and mime_type not in EXPORT_FORMATS:
        status = "SKIPPED"
        current_sha256 = None
    else:
        status, current_sha256 = classify_file(
            service, item, relative_path, destination, state
        )

    plan.append(
        {
            "status": status,
            "kind": "FILE",
            "item": item,
            "relative_path": relative_path,
            "destination": destination,
            "local_sha256": current_sha256,
        }
    )


def mark_duplicate_targets(plan):
    file_targets = Counter(
        entry["destination"]
        for entry in plan
        if entry["kind"] == "FILE" and entry["status"] != "SKIPPED"
    )
    for entry in plan:
        if entry["kind"] == "FILE" and file_targets[entry["destination"]] > 1:
            entry["status"] = "CONFLICT"


def path_is_in_scope(relative_path, scopes):
    return any(
        scope == Path()
        or relative_path == scope
        or scope in relative_path.parents
        for scope in scopes
    )


def tracked_files_by_path(state):
    tracked = {}
    for key, record in state["files"].items():
        record_path = record.get("path")
        if record_path:
            tracked[Path(record_path)] = (key, record)
    return tracked


def add_local_only_entries(plan, destination_root, state, scopes):
    remote_files = {
        entry["relative_path"]
        for entry in plan
        if entry["kind"] == "FILE" and entry.get("item") is not None
    }
    remote_directories = {
        entry["relative_path"]
        for entry in plan
        if entry["kind"] == "FOLDER" and entry.get("item") is not None
    }
    tracked = tracked_files_by_path(state)
    planned_local_paths = {
        entry["relative_path"]
        for entry in plan
        if entry.get("item") is None
    }

    def has_tracked_descendant(relative_directory):
        return any(
            relative_directory in tracked_path.parents
            for tracked_path in tracked
        )

    def add_local_entry(relative_path, kind):
        if relative_path in planned_local_paths:
            return
        destination = destination_root / relative_path
        tracked_entry = tracked.get(relative_path) if kind == "FILE" else None

        if tracked_entry:
            tracked_key, record = tracked_entry
            current_sha256 = file_hash(destination)
            if current_sha256 == record.get("local_sha256"):
                status = "REMOVED_REMOTE"
            else:
                status = "CONFLICT"
        else:
            tracked_key = None
            status = "LOCAL_ONLY"

        plan.append(
            {
                "status": status,
                "kind": kind,
                "item": None,
                "relative_path": relative_path,
                "destination": destination,
                "state_key": tracked_key,
            }
        )
        planned_local_paths.add(relative_path)

    def scan_directory(relative_directory):
        directory = destination_root / relative_directory
        if not directory.is_dir():
            return

        for child in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
            relative_path = child.relative_to(destination_root)
            if relative_path.parts[0] in {
                STATE_FILE_NAME,
                f"{STATE_FILE_NAME}.tmp",
                RECOVERY_DIR_NAME,
            }:
                continue
            if child.name.endswith(".gdrivepull.part"):
                continue

            if child.is_dir():
                if relative_path in remote_directories:
                    scan_directory(relative_path)
                elif has_tracked_descendant(relative_path):
                    scan_directory(relative_path)
                else:
                    add_local_entry(relative_path, "FOLDER")
            elif child.is_file() and relative_path not in remote_files:
                add_local_entry(relative_path, "FILE")

    for scope in scopes:
        destination = destination_root / scope
        if scope == Path():
            scan_directory(scope)
        elif destination.is_dir():
            scan_directory(scope)
        elif destination.is_file() and scope not in remote_files:
            add_local_entry(scope, "FILE")


def shorten(text, width):
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def item_count_text(file_count, folder_count):
    parts = []
    if file_count:
        parts.append(f"{file_count} file{'s' if file_count != 1 else ''}")
    if folder_count:
        parts.append(
            f"{folder_count} folder{'s' if folder_count != 1 else ''}"
        )
    return ", ".join(parts)


def print_vertical_summary(title, rows, no_changes=False):
    visible_rows = [
        (label, item_count_text(file_count, folder_count))
        for label, file_count, folder_count in rows
        if file_count or folder_count
    ]
    label_width = max(
        [len(label) for label, _ in visible_rows] + [len("Status")]
    )

    print(f"\n{title}")
    print("-" * (label_width + 24))
    if no_changes:
        print("No changes required")
    for label, value in visible_rows:
        print(f"{label:<{label_width}}  {value}")


def print_plan(plan, destination_root):
    remote_width = min(
        68,
        max(20, max(len(entry["relative_path"].as_posix()) for entry in plan)),
    )
    header = f"{'STATUS':<14}  {'TYPE':<6}  {'ITEM':<{remote_width}}  LOCAL TARGET"

    print("\nDownload preview\n")
    print(header)
    print("-" * len(header))
    for entry in plan:
        relative_path = entry["relative_path"].as_posix()
        print(
            f"{entry['status']:<14}  {entry['kind']:<6}  "
            f"{shorten(relative_path, remote_width):<{remote_width}}  "
            f"{entry['destination']}"
        )

    counts = Counter(
        (entry["kind"], entry["status"]) for entry in plan
    )
    actionable = sum(
        counts[kind, status]
        for kind in ("FILE", "FOLDER")
        for status in ("NEW", "UPDATE", "CONFLICT", "REMOVED_REMOTE")
    )
    print_vertical_summary(
        "SUMMARY",
        [
            ("New", counts["FILE", "NEW"], counts["FOLDER", "NEW"]),
            ("Updated", counts["FILE", "UPDATE"], 0),
            (
                "Unchanged",
                counts["FILE", "UNCHANGED"],
                counts["FOLDER", "UNCHANGED"],
            ),
            (
                "Conflicts",
                counts["FILE", "CONFLICT"],
                counts["FOLDER", "CONFLICT"],
            ),
            ("Removed remotely", counts["FILE", "REMOVED_REMOTE"], 0),
            (
                "Local only",
                counts["FILE", "LOCAL_ONLY"],
                counts["FOLDER", "LOCAL_ONLY"],
            ),
            ("Unsupported", counts["FILE", "SKIPPED"], 0),
            ("Skipped", 0, counts["FOLDER", "SKIPPED"]),
        ],
        no_changes=actionable == 0,
    )
    if counts["FILE", "CONFLICT"] or counts["FOLDER", "CONFLICT"]:
        print("Conflicts will be preserved and skipped.")
    if counts["FILE", "LOCAL_ONLY"] or counts["FOLDER", "LOCAL_ONLY"]:
        print("Local-only items will be preserved and skipped.")
    if counts["FILE", "REMOVED_REMOTE"]:
        print(
            "Items removed from Drive will be moved to the local recovery "
            "directory after confirmation."
        )
    print(f"Destination: {destination_root}")


def download_request(service, item):
    mime_type = effective_mime_type(item)
    if mime_type in EXPORT_FORMATS:
        export_mime_type, _ = EXPORT_FORMATS[mime_type]
        return service.files().export_media(
            fileId=effective_id(item), mimeType=export_mime_type
        )
    return service.files().get_media(
        fileId=effective_id(item), supportsAllDrives=True
    )


def set_remote_mtime(destination, item):
    modified_time = item.get("modifiedTime")
    if not modified_time:
        return
    timestamp = datetime.fromisoformat(
        modified_time.replace("Z", "+00:00")
    ).timestamp()
    os.utime(destination, (timestamp, timestamp))


def download_file_atomically(service, entry):
    destination = entry["destination"]
    temporary = destination.with_name(
        f".{destination.name}.gdrivepull.part"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        request = download_request(service, entry["item"])
        with temporary.open("wb") as file_handle:
            downloader = MediaIoBaseDownload(file_handle, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        set_remote_mtime(temporary, entry["item"])
        os.replace(temporary, destination)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def state_record(entry, local_sha256):
    item = entry["item"]
    return {
        "remote_id": effective_id(item),
        "path": entry["relative_path"].as_posix(),
        "name": item["name"],
        "mime_type": effective_mime_type(item),
        "modified_time": item.get("modifiedTime"),
        "remote_signature": remote_signature(item),
        "local_sha256": local_sha256,
    }


def unused_recovery_target(recovery_root, relative_path):
    candidate = recovery_root / relative_path
    if not candidate.exists():
        return candidate

    counter = 2
    while True:
        candidate = (
            recovery_root
            / relative_path.parent
            / f"{relative_path.name}.{counter}"
        )
        if not candidate.exists():
            return candidate
        counter += 1


def apply_plan(service, plan, destination_root, state):
    results = Counter()
    recovery_root = (
        destination_root
        / RECOVERY_DIR_NAME
        / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    recovery_announced = False

    for entry in plan:
        status = entry["status"]
        if entry.get("item") is None:
            if status == "REMOVED_REMOTE":
                try:
                    recovery_target = unused_recovery_target(
                        recovery_root, entry["relative_path"]
                    )
                    recovery_target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(entry["destination"], recovery_target)
                    if not recovery_announced:
                        print(f"[RECOVERY] {recovery_root}")
                        recovery_announced = True
                    print(
                        f"[REMOVED_REMOTE] {entry['destination']} -> "
                        f"{recovery_target}"
                    )
                    if entry.get("state_key"):
                        state["files"].pop(entry["state_key"], None)
                    results["FILE_REMOVED_REMOTE"] += 1
                except OSError as error:
                    results[f"{entry['kind']}_ERROR"] += 1
                    print(
                        f"[ERROR] Could not move {entry['destination']} to "
                        f"recovery: {error}",
                        file=sys.stderr,
                    )
            else:
                results[f"{entry['kind']}_{status}"] += 1
            continue

        if entry["kind"] == "FOLDER":
            if status == "NEW":
                entry["destination"].mkdir(parents=True, exist_ok=True)
            results[f"FOLDER_{status}"] += 1
            continue

        item_state_key = state_key(entry["item"], entry["relative_path"])
        if status == "UNCHANGED":
            local_sha256 = entry.get("local_sha256") or file_hash(
                entry["destination"]
            )
            state["files"][item_state_key] = state_record(entry, local_sha256)
            results["FILE_UNCHANGED"] += 1
            continue
        if status in {"CONFLICT", "SKIPPED"}:
            results[f"FILE_{status}"] += 1
            continue

        try:
            print(f"[{status}] {entry['destination']}")
            download_file_atomically(service, entry)
            local_sha256 = file_hash(entry["destination"])
            state["files"][item_state_key] = state_record(entry, local_sha256)
            results[f"FILE_{status}"] += 1
        except (HttpError, OSError) as error:
            results["FILE_ERROR"] += 1
            print(
                f"[ERROR] {entry['relative_path'].as_posix()}: {error}",
                file=sys.stderr,
            )

    save_state(destination_root, state)
    return results


def print_results(results):
    changed = (
        results["FILE_NEW"]
        + results["FILE_UPDATE"]
        + results["FILE_REMOVED_REMOTE"]
        + results["FOLDER_NEW"]
    )
    print_vertical_summary(
        "RESULT",
        [
            ("Downloaded", results["FILE_NEW"], 0),
            ("Updated", results["FILE_UPDATE"], 0),
            (
                "Unchanged",
                results["FILE_UNCHANGED"],
                results["FOLDER_UNCHANGED"],
            ),
            (
                "Conflicts skipped",
                results["FILE_CONFLICT"],
                results["FOLDER_CONFLICT"],
            ),
            ("Moved to recovery", results["FILE_REMOVED_REMOTE"], 0),
            (
                "Local-only preserved",
                results["FILE_LOCAL_ONLY"],
                results["FOLDER_LOCAL_ONLY"],
            ),
            ("Created", 0, results["FOLDER_NEW"]),
            ("Unsupported", results["FILE_SKIPPED"], 0),
            ("Skipped", 0, results["FOLDER_SKIPPED"]),
            (
                "Errors",
                results["FILE_ERROR"],
                results["FOLDER_ERROR"],
            ),
        ],
        no_changes=changed == 0
        and results["FILE_ERROR"] == 0
        and results["FOLDER_ERROR"] == 0,
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "List files and folders at the Google Drive root, preview changes, "
            "then download the selection safely."
        )
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="Choose or change the managed local destination.",
    )
    parser.add_argument(
        "--folder-id",
        default="root",
        help="Drive folder to browse (default: My Drive root)",
    )
    args = parser.parse_args()

    try:
        destination_root = configure_destination(args.configure)
        if destination_root is None:
            print("[INFO] Destination setup cancelled.")
            return

        service = build(
            "drive", "v3", credentials=authenticate(), cache_discovery=False
        )
        selections, local_scopes = browse_and_select(
            service, args.folder_id
        )
        if not selections:
            print("[INFO] No items selected.")
            return

        state = load_state(destination_root)
        plan = []
        for selection in selections:
            collect_plan(
                service,
                selection["item"],
                selection["relative_parent"],
                destination_root,
                state,
                plan,
            )
        mark_duplicate_targets(plan)
        add_local_only_entries(
            plan, destination_root, state, local_scopes
        )
        print_plan(plan, destination_root)

        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("[INFO] Download cancelled. No files were changed.")
            return

        results = apply_plan(service, plan, destination_root, state)
        print_results(results)
    except (HttpError, OSError) as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
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
DOWNLOAD_PATH = "~/GDrive"
TOKEN_PATH = SCRIPT_DIR / "token.json"
CREDENTIALS_PATH = SCRIPT_DIR / "credentials.json"
STATE_FILE_NAME = ".gdrivepull-managed-state.json"
RECOVERY_DIR_NAME = ".gdrivepull-recovery"
REMOTE_CACHE_NAME = ".gdrivepull-remote-cache.json"
RESERVED_NAMES = {
    STATE_FILE_NAME,
    f"{STATE_FILE_NAME}.tmp",
    RECOVERY_DIR_NAME,
    REMOTE_CACHE_NAME,
    f"{REMOTE_CACHE_NAME}.tmp",
}
RESERVED_PATHS = {Path(name) for name in RESERVED_NAMES}
SHARED_WITH_ME = "sharedWithMe"  # pseudo folder id: the items others shared with you
SHARED_WITH_ME_DIR = "Shared with me"
SHARED_DRIVES_DIR = "Shared drives"
VIEW_DIRS = {SHARED_WITH_ME_DIR, SHARED_DRIVES_DIR}
STATE_VERSION = 1
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
LIST_FIELDS = (
    "nextPageToken,"
    "files(id,name,mimeType,size,modifiedTime,md5Checksum,shortcutDetails,webViewLink,driveId)"
)
API_BATCH_SIZE = 50  # Drive accepts 100 calls per batch, but throttles large ones
API_RETRIES = 5

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

console = Console(highlight=False)
VERBOSE = False


def debug(message):
    if VERBOSE:
        console.print(f"[dim]  {escape(message)}[/]")


def warn(message):
    console.print(f"[yellow]![/] {escape(message)}")


def error_text(error):
    if isinstance(error, HttpError):
        return f"{error.status_code} {error.reason}"
    return str(error)


# --- Sign-in ---------------------------------------------------------------

SIGN_IN_SUCCESS_PAGE = "gdrivepull: signed in. You can close this tab."


def oauth_flow():
    if not CREDENTIALS_PATH.exists():
        console.print(
            f"[red]✗[/] Missing Google OAuth client file: {escape(str(CREDENTIALS_PATH))}\n"
            "  [dim]Download it from Google Cloud Console → APIs & Services → Credentials "
            "(OAuth client ID, Desktop app) and save it there as credentials.json.[/]"
        )
        sys.exit(1)
    return InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)


def open_quietly(url):
    """Open url in the default browser without letting the browser write to our terminal.

    Falls back to Python's webbrowser (which honors $BROWSER) when no system opener is usable.
    """
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if sys.platform != "win32" and not os.environ.get("BROWSER") and shutil.which(opener):
        try:
            subprocess.Popen(
                [opener, url],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except OSError:
            pass
    return webbrowser.open(url)


def local_expiry(creds):
    if creds.expiry is None:
        return "unknown"
    return creds.expiry.replace(tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def sign_in(reason):
    """Browser OAuth sign-in with compact output, erased once signed in (kept with --verbose)."""
    flow = oauth_flow()

    class QuietBrowser(webbrowser.BaseBrowser):
        # run_local_server hands us the sign-in URL here, so the prompt and opening are ours.
        def open(self, url, new=0, autoraise=True):
            console.print(f"[yellow]![/] {reason}")
            console.print(f"  [dim]Didn't open?[/] [link={url}]Open the sign-in page[/link]")
            if VERBOSE:
                # Soft wrap keeps the URL one logical line, so terminals still detect it as a link.
                console.print(f"  {url}", style="dim", markup=False, soft_wrap=True)
            return open_quietly(url)

    webbrowser.register("gdrivepull", None, QuietBrowser("gdrivepull"))
    with console.status("[dim]Waiting for sign-in…[/]"):
        creds = flow.run_local_server(
            port=0,
            browser="gdrivepull",
            authorization_prompt_message="",
            success_message=SIGN_IN_SUCCESS_PAGE,
        )
    if not VERBOSE and console.is_terminal:
        console.file.write("\x1b[2F\x1b[J")  # erase the two prompt lines
    debug(f"Token expires {local_expiry(creds)}")
    return creds


def authenticate():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                debug(f"Token refreshed, expires {local_expiry(creds)}")
            except Exception as error:
                debug(f"Could not refresh token: {error}")
                if "invalid_grant" in str(error):
                    reason = "Google Drive session expired, sign in again in your browser."
                else:
                    reason = "Could not refresh the Google Drive session, sign in again in your browser."
                creds = sign_in(reason)
        else:
            creds = sign_in("Sign in to Google Drive in your browser.")
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return creds


# --- Settings and managed destination --------------------------------------

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


# --- Drive listing ---------------------------------------------------------

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


def is_folder(item):
    return effective_mime_type(item) == FOLDER_MIME_TYPE


def is_unsupported(item):
    mime_type = effective_mime_type(item)
    return (
        mime_type.startswith("application/vnd.google-apps.")
        and mime_type != FOLDER_MIME_TYPE
        and mime_type not in EXPORT_FORMATS
    )


def sort_items(items):
    return sorted(
        items,
        key=lambda item: (not is_folder(item), item["name"].casefold()),
    )


def is_retryable(error):
    if not isinstance(error, HttpError):
        return False
    if error.status_code in {429, 500, 502, 503, 504}:
        return True
    return error.status_code == 403 and "ratelimit" in str(error).casefold()


FOLDER_DRIVES = {}  # folder id -> shared drive id: shared drive folders are listed within their drive


def list_request(service, folder_id, page_token=None):
    options = {}
    if folder_id == SHARED_WITH_ME:
        query = "sharedWithMe and trashed = false"
    else:
        query = f"'{folder_id}' in parents and trashed = false"
        if folder_id in FOLDER_DRIVES:
            options = {"corpora": "drive", "driveId": FOLDER_DRIVES[folder_id]}
    return service.files().list(
        q=query,
        spaces="drive",
        fields=LIST_FIELDS,
        pageSize=1000,
        pageToken=page_token,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        **options,
    )


def note_drives(items):
    for item in items:
        if item.get("driveId") and is_folder(item):
            FOLDER_DRIVES[effective_id(item)] = item["driveId"]


def list_folders(service, folder_ids, cache):
    """Store the sorted children of every folder in cache, listing them in batched API calls."""
    pending = [(folder_id, None) for folder_id in dict.fromkeys(folder_ids) if folder_id not in cache]
    found = {folder_id: [] for folder_id, _ in pending}
    attempts = Counter()

    while pending:
        chunk, pending = pending[:API_BATCH_SIZE], pending[API_BATCH_SIZE:]
        retry, errors = [], []

        def callback(request_id, response, exception, chunk=chunk):
            folder_id, page_token = chunk[int(request_id)]
            if exception is not None:
                if is_retryable(exception) and attempts[folder_id] < API_RETRIES:
                    attempts[folder_id] += 1
                    retry.append((folder_id, page_token))
                else:
                    errors.append(exception)
                return
            found[folder_id].extend(response.get("files", []))
            if response.get("nextPageToken"):
                pending.append((folder_id, response["nextPageToken"]))

        batch = service.new_batch_http_request(callback=callback)
        for index, (folder_id, page_token) in enumerate(chunk):
            batch.add(list_request(service, folder_id, page_token), request_id=str(index))
        batch.execute()
        debug(f"Listed {len(chunk)} folder page(s)")
        if errors:
            raise errors[0]
        if retry:
            delay = min(2 ** max(attempts[folder_id] for folder_id, _ in retry), 30)
            debug(f"Drive rate limit, retrying {len(retry)} listing(s) in {delay}s")
            time.sleep(delay)
            pending = retry + pending

    for folder_id, items in found.items():
        note_drives(items)
        cache[folder_id] = sort_items(items)


def list_children(service, folder_id, cache):
    list_folders(service, [folder_id], cache)
    return cache[folder_id]


def prefetch_folders(service, folder_ids, cache):
    """List every folder below folder_ids, one batched call per level, so planning needs no listing."""
    seen = set()
    level = list(folder_ids)
    while level:
        seen.update(level)
        list_folders(service, level, cache)
        level = list(dict.fromkeys(
            effective_id(item)
            for folder_id in level
            for item in cache[folder_id]
            if is_folder(item) and effective_id(item) not in seen
        ))


def reachable_folders(cache, folder_ids):
    seen = set()
    stack = [folder_id for folder_id in folder_ids if folder_id in cache]
    while stack:
        folder_id = stack.pop()
        if folder_id in seen:
            continue
        seen.add(folder_id)
        stack.extend(
            effective_id(item) for item in cache[folder_id]
            if is_folder(item) and effective_id(item) in cache
        )
    return seen


# --- Remote snapshot: --again replays Drive changes instead of listing every folder -------------

CHANGE_FIELDS = (
    "nextPageToken,newStartPageToken,changes(fileId,removed,"
    "file(id,name,mimeType,size,modifiedTime,md5Checksum,shortcutDetails,webViewLink,driveId,parents,trashed))"
)


def start_page_token(service):
    return (
        service.changes()
        .getStartPageToken(supportsAllDrives=True)
        .execute(num_retries=API_RETRIES)["startPageToken"]
    )


def save_snapshot(destination_root, token, cache, folder_ids):
    folders = reachable_folders(cache, folder_ids)
    snapshot = {
        "version": 1,
        "token": token,
        "folders": {folder_id: cache[folder_id] for folder_id in folders},
        "drives": {folder_id: FOLDER_DRIVES[folder_id] for folder_id in folders if folder_id in FOLDER_DRIVES},
    }
    path = destination_root / REMOTE_CACHE_NAME
    temporary_path = destination_root / f"{REMOTE_CACHE_NAME}.tmp"
    temporary_path.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary_path, path)


def load_snapshot(destination_root):
    try:
        snapshot = json.loads((destination_root / REMOTE_CACHE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if snapshot.get("version") != 1 or not snapshot.get("token") or not isinstance(snapshot.get("folders"), dict):
        return None
    return snapshot


def apply_changes(service, snapshot):
    """The snapshot's folder listings updated with the Drive changes since it was saved, and the new token.

    None when the token is no longer valid: the caller lists the folders again.
    """
    cache = {folder_id: list(items) for folder_id, items in snapshot["folders"].items()}
    FOLDER_DRIVES.update(snapshot.get("drives", {}))
    where = {}  # item id -> folders listing it
    for folder_id, items in cache.items():
        for item in items:
            where.setdefault(item["id"], set()).add(folder_id)

    page_token, changed, count = snapshot["token"], set(), 0
    while True:
        try:
            response = (
                service.changes()
                .list(
                    pageToken=page_token,
                    fields=CHANGE_FIELDS,
                    pageSize=1000,
                    spaces="drive",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                )
                .execute(num_retries=API_RETRIES)
            )
        except HttpError as error:
            if error.status_code in {400, 404, 410}:
                debug(f"Saved Drive changes token rejected ({error.status_code}), listing folders again")
                return None
            raise
        for change in response.get("changes", []):
            count += 1
            file_id = change["fileId"]
            for folder_id in where.pop(file_id, ()):
                cache[folder_id] = [item for item in cache[folder_id] if item["id"] != file_id]
                changed.add(folder_id)
            item = change.get("file")
            if change.get("removed") or not item or item.get("trashed"):
                continue
            parents = item.pop("parents", [])
            item.pop("trashed", None)
            note_drives([item])
            for folder_id in parents:
                if folder_id in cache:
                    cache[folder_id].append(item)
                    where.setdefault(file_id, set()).add(folder_id)
                    changed.add(folder_id)
        if "newStartPageToken" in response:
            break
        page_token = response["nextPageToken"]

    for folder_id in changed:
        cache[folder_id] = sort_items(cache[folder_id])
    debug(f"Applied {plural(count, 'Drive change')} to {plural(len(cache), 'saved folder')}")
    return cache, response["newStartPageToken"]


def resolve_file_shortcut(service, item):
    if item["mimeType"] != SHORTCUT_MIME_TYPE:
        return item
    if is_folder(item):
        return item

    target = (
        service.files()
        .get(
            fileId=effective_id(item),
            fields="id,mimeType,size,modifiedTime,md5Checksum,webViewLink",
            supportsAllDrives=True,
        )
        .execute(num_retries=API_RETRIES)
    )
    target["name"] = item["name"]
    return target


def folder_name(service, folder_id):
    if folder_id == "root":
        return "My Drive"
    folder = (
        service.files()
        .get(fileId=folder_id, fields="name", supportsAllDrives=True)
        .execute(num_retries=API_RETRIES)
    )
    return folder["name"]


def collect_drive_tree(service, root_folder_id, cache, on_progress=lambda folders, items: None, base=Path()):
    """Load the whole tree below root_folder_id, one batched listing per level.

    Returns the nodes (parents always before their children) and the root items by local path.
    """
    nodes = []
    browsed_directories = {}
    level = [(root_folder_id, None, base, [], frozenset({root_folder_id}))]

    while level:
        list_folders(service, [folder_id for folder_id, *_ in level], cache)
        next_level = []
        for folder_id, parent, relative_parent, display_parts, ancestors in level:
            items = cache[folder_id]
            browsed_directories.setdefault(relative_parent, items)
            for item in items:
                index = len(nodes)
                nodes.append(
                    {
                        "index": index,
                        "parent": parent,
                        "item": item,
                        "relative_parent": relative_parent,
                        "display_path": "/".join(display_parts + [item["name"]]),
                    }
                )
                child_folder_id = effective_id(item)
                if is_folder(item) and child_folder_id not in ancestors:
                    next_level.append(
                        (
                            child_folder_id,
                            index,
                            relative_parent / local_name(item),
                            display_parts + [item["name"]],
                            ancestors | {child_folder_id},
                        )
                    )
        on_progress(len(cache), len(nodes))
        level = next_level

    return nodes, browsed_directories


def folder_totals(nodes):
    """Files and known bytes below every folder node, by node index."""
    totals = {node["index"]: [0, 0] for node in nodes if is_folder(node["item"])}
    for node in reversed(nodes):
        own = totals.get(node["index"])
        if own is None:
            own = [1, int(node["item"].get("size") or 0)]
        parent = node["parent"]
        if parent is not None:
            totals[parent][0] += own[0]
            totals[parent][1] += own[1]
    return totals


# --- Selection -------------------------------------------------------------

def human_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            if unit == "B":
                return f"{value:.0f} B"
            return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"
        value /= 1000
    return "-"


def display_size(item):
    size = item.get("size")
    return human_size(size) if size else "-"


def display_date(item):
    modified_time = item.get("modifiedTime")
    if not modified_time:
        return ""
    moment = datetime.fromisoformat(modified_time.replace("Z", "+00:00")).astimezone()
    if moment.date() == datetime.now().astimezone().date():
        return moment.strftime("%H:%M")
    return moment.strftime("%Y-%m-%d")


def short_path(path):
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


def selected_path(entry):
    return entry["relative_parent"] / local_name(entry["item"])


def path_selection_state(path, selections):
    for entry in selections:
        entry_path = selected_path(entry)
        if entry_path == path:
            return "x"
        if is_folder(entry["item"]) and entry_path in path.parents:
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
        if is_folder(entry["item"]) and entry_path in candidate_path.parents:
            return False

    if is_folder(item):
        selections[:] = [
            entry
            for entry in selections
            if candidate_path not in selected_path(entry).parents
        ]
    selections.append(candidate)
    selections.sort(
        key=lambda entry: (
            not is_folder(entry["item"]),
            selected_path(entry).as_posix().casefold(),
        )
    )
    return True


def scope_covers(parent, scope):
    if parent == Path():
        # My Drive's root does not cover the Shared with me / Shared drives folders.
        return not scope.parts or scope.parts[0] not in VIEW_DIRS
    return parent == scope or parent in scope.parents


def compact_scopes(scopes):
    compacted = []
    for scope in sorted(set(scopes), key=lambda path: (len(path.parts), path.as_posix())):
        if any(scope_covers(parent, scope) for parent in compacted):
            continue
        compacted.append(scope)
    return compacted


def fully_selected(root_items, base, selections):
    return bool(root_items) and all(
        path_selection_state(base / local_name(item), selections) in {"x", "*"}
        for item in root_items
    )


def build_local_scopes(selections, browsed_directories, bases=(Path(),)):
    scopes = [
        selected_path(entry)
        for entry in selections
        if is_folder(entry["item"])
    ]
    for base in bases:
        if fully_selected(browsed_directories.get(base, []), base, selections):
            scopes.append(base)
    return compact_scopes(scopes)


def matches_filter(entry, terms):
    haystack = entry["display_path"].casefold()
    return all(term in haystack for term in terms)


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


# --- Destination setup -----------------------------------------------------

SHARED_CSS = """
Screen {
    background: #07111f;
    color: #dbeafe;
}

Header {
    background: #0f2742;
    color: #f8fafc;
}

Footer {
    background: #07111f;
    color: #bae6fd;
}
"""


class DestinationSetupScreen(Screen):
    """Pick the download folder for this session (f in the tree)."""

    SUB_TITLE = "Download folder for this session"

    DEFAULT_CSS = """
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

        yield Header()
        with Horizontal(id="setup-body"):
            yield DirectoryTree(
                initial_parent,
                id="directory-tree",
            )
            with Vertical(id="setup-form"):
                yield Static(
                    "Pick a parent directory on the left (or type it), then name the managed folder.",
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
        self.query_one("#directory-tree").focus()

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
        except (OSError, ValueError) as error:
            status.update(
                Text(str(error), style="bold bright_red")
            )
            return
        self.dismiss(destination)

    def action_cancel(self):
        self.dismiss(None)


# --- Drive selector --------------------------------------------------------

KEY_HELP = [
    ("↑ ↓", "move"),
    ("tab", "next view: My Drive, Shared with me, each shared drive (shift+tab: previous)"),
    ("← →", "collapse / expand a folder, or go to the parent folder / first child"),
    ("enter", "expand or collapse a folder"),
    ("space", "select / unselect (a selected folder includes everything below it)"),
    ("a", "select / unselect all (only the matching items while a filter is active)"),
    ("c", "unselect everything, including items hidden by the filter"),
    ("e", "expand or collapse everything below the cursor"),
    ("/", "filter by name or path: enter keeps the filter, esc clears it"),
    ("o", "open the item in Google Drive"),
    ("f", "change the download folder for this session"),
    ("d", "preview: y downloads, v shows other statuses, esc comes back to the tree"),
    ("?", "this help"),
    ("q esc", "quit"),
]
KEY_HELP_NOTE = "Nothing changes on disk before the preview is confirmed."
MARKS = {
    "synced": ("✓", "green", "downloaded and up to date"),
    "changed": ("↻", "bright_cyan", "changed on Drive, or new files in the folder"),
    "edited": ("✎", "yellow", "changed locally (kept as a conflict)"),
}


def help_lines():
    lines = [f"  {key:<8}{description}" for key, description in KEY_HELP]
    lines += ["", "marks (from the last downloads):"]
    lines += [f"  {symbol:<8}{description}" for symbol, _, description in MARKS.values()]
    return lines + ["", "  " + KEY_HELP_NOTE]


def mtime_matches(path_stat, modified_time):
    if not modified_time:
        return False
    timestamp = datetime.fromisoformat(modified_time.replace("Z", "+00:00")).timestamp()
    return abs(path_stat.st_mtime - timestamp) < 2


NAME_COLUMN_MAX = 64  # cells from the tree's left edge to the end of the names


def label_start(node, children_of):
    """Cells before a node's label: tree guides, plus the expand arrow of folders with children."""
    depth = len(node["relative_parent"].parts) + 1
    return depth * 4 + (2 if node["index"] in children_of else 0)


def name_column(nodes):
    children_of = {node["parent"] for node in nodes}
    widest = 0
    for node in nodes:
        prefix = 4 + (2 if node["item"]["mimeType"] == SHORTCUT_MIME_TYPE else 0)  # checkbox, mark
        widest = max(widest, label_start(node, children_of) + prefix + Text(node["item"]["name"]).cell_len)
    return min(widest, NAME_COLUMN_MAX)


def local_marks(nodes, destination, state):
    """Mark files already downloaded (from the managed state, without hashing) and sum them up per folder."""
    if destination is None or state is None:
        return {}
    tracked = tracked_files_by_path(state)
    marks = {}
    counts = {}  # folder index -> Counter of its files' marks ("" = not downloaded yet)
    for node in reversed(nodes):
        item = node["item"]
        index = node["index"]
        if is_folder(item):
            folder = counts.get(index, Counter())
            if folder["edited"]:
                marks[index] = "edited"
            elif folder["changed"] or (folder["synced"] and folder[""]):
                marks[index] = "changed"
            elif folder["synced"]:
                marks[index] = "synced"
            own = folder
        else:
            own = Counter()
            if is_unsupported(item) or item["mimeType"] == SHORTCUT_MIME_TYPE:
                pass  # no local copy, or no metadata to compare before the preview resolves it
            else:
                mark = ""
                relative_path = selected_path(node)
                record = tracked.get(relative_path, (None, {}))[1]
                if record.get("remote_id") == effective_id(item):
                    try:
                        path_stat = (destination / relative_path).stat()
                    except OSError:
                        path_stat = None
                    if path_stat is not None:
                        if remote_signature(item) != record.get("remote_signature"):
                            mark = "changed"
                        elif not mtime_matches(path_stat, record.get("modified_time")):
                            mark = "edited"
                        else:
                            mark = "synced"
                if mark:
                    marks[index] = mark
                own[mark] += 1
        if node["parent"] is not None:
            counts.setdefault(node["parent"], Counter()).update(own)
    return marks


def drive_url(item):
    if item.get("webViewLink"):
        return item["webViewLink"]
    if is_folder(item):
        return f"https://drive.google.com/drive/folders/{effective_id(item)}"
    return f"https://drive.google.com/file/d/{effective_id(item)}/view"


class HelpScreen(ModalScreen):
    CSS = """
    HelpScreen {
        align: center middle;
    }

    #help {
        width: auto;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round #38bdf8;
        background: #0b1b2e;
    }
    """

    BINDINGS = [Binding("escape,q,question_mark,enter", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        text = Text()
        text.append("Keys\n\n", style="bold bright_cyan")
        for key, description in KEY_HELP:
            text.append(f"{key:<8}", style="bold bright_white")
            text.append(f"{description}\n")
        text.append("\nMarks", style="bold bright_cyan")
        text.append(" (from the last downloads)\n\n", style="dim")
        for symbol, style, description in MARKS.values():
            text.append(f"{symbol:<8}", style=f"bold {style}")
            text.append(f"{description}\n")
        text.append(f"\n{KEY_HELP_NOTE}", style="dim")
        yield Static(text, id="help")

    def on_click(self):
        self.dismiss()


class DriveTree(Tree):
    # These replace Tree's own space / enter and go to the app, so the selection stays there.
    BINDINGS = [
        Binding("space", "app.toggle_current", "Select"),
        Binding("enter", "app.activate_current", "Expand", key_display="enter"),
        Binding("left", "app.collapse_or_parent", "Collapse", show=False),
        Binding("right", "app.expand_or_child", "Expand", show=False),
        Binding("e", "app.expand_all", "Expand all", show=False),
    ]


class DriveView:
    """One root shown in the tree: My Drive, Shared with me, or a shared drive."""

    def __init__(self, name, root_id, base=Path()):
        self.name = name
        self.root_id = root_id
        self.base = base
        self.nodes = None  # loaded on first display
        self.browsed = {}
        self.children_of = {}
        self.totals = {}
        self.marks = {}
        self.name_column = 0
        self.expanded = set()
        self.error = None

    def load(self, nodes, browsed):
        self.nodes = nodes
        self.browsed = browsed
        self.children_of = {}
        for node in nodes:
            self.children_of.setdefault(node["parent"], []).append(node)
        self.totals = folder_totals(nodes)
        self.name_column = name_column(nodes)


def drive_views(service, folder_id):
    """My Drive, Shared with me and every shared drive, or only the folder given with --folder-id."""
    if folder_id != "root":
        return [DriveView(folder_name(service, folder_id), folder_id)]
    root_id = (
        service.files().get(fileId="root", fields="id").execute(num_retries=API_RETRIES)["id"]
    )
    views = [DriveView("My Drive", root_id), DriveView(SHARED_WITH_ME_DIR, SHARED_WITH_ME, Path(SHARED_WITH_ME_DIR))]
    page_token = None
    while True:
        response = (
            service.drives()
            .list(pageSize=100, pageToken=page_token, fields="nextPageToken,drives(id,name)")
            .execute(num_retries=API_RETRIES)
        )
        for drive in response.get("drives", []):
            FOLDER_DRIVES[drive["id"]] = drive["id"]
            views.append(DriveView(drive["name"], drive["id"], Path(SHARED_DRIVES_DIR) / safe_name(drive["name"])))
        page_token = response.get("nextPageToken")
        if not page_token:
            return views


def load_view(service, view, cache, on_progress=lambda folders, items: None):
    nodes, browsed = collect_drive_tree(service, view.root_id, cache, on_progress, view.base)
    view.load(nodes, browsed)


class DriveSelectorApp(App):
    TITLE = APP_NAME
    ENABLE_COMMAND_PALETTE = False

    CSS = SHARED_CSS + """
    #views {
        height: 1;
        margin: 1 2 0 2;
    }

    #drive-tree {
        height: 1fr;
        margin: 1 2 0 2;
        padding: 0 1;
        border: round #38bdf8;
        background: #081525;
    }

    #filter {
        margin: 0 2;
        border: round #f59e0b;
    }

    #selection-summary {
        height: 1;
        margin: 0 2;
        padding: 0 1;
        color: #e0f2fe;
    }

    Tree > .tree--cursor {
        background: #164e63;
        color: #ffffff;
    }

    Tree > .tree--guides {
        color: #334155;
    }
    """

    BINDINGS = [
        Binding("tab", "next_view", "View", priority=True),
        Binding("shift+tab", "previous_view", "Previous view", show=False, priority=True),
        Binding("a", "select_all", "Select all"),
        Binding("c", "clear_selection", "Unselect all"),
        Binding("slash", "start_filter", "Filter"),
        Binding("o", "open_in_drive", "Open in Drive", show=False),
        Binding("f", "change_destination", "Folder"),
        Binding("d", "confirm", "Download"),
        Binding("question_mark", "help", "Help"),
        Binding("escape", "cancel", "Quit"),
        Binding("q", "quit_selector", "Quit", show=False),
    ]

    def __init__(self, views, destination=None, state=None, service=None, make_service=None, cache=None,
                 skip_preview=False):
        super().__init__()
        self.views = views
        self.view = views[0]
        self.destination = destination
        self.state = state
        self.service = service
        self.make_service = make_service or (lambda: service)
        self.cache = {} if cache is None else cache
        self.skip_preview = skip_preview
        for view in views:
            if view.nodes is not None:
                view.marks = local_marks(view.nodes, destination, state)
        self.selections = []
        self.tree_nodes = {}
        self.populated = set()
        self.filter_terms = []
        self.filter_ancestors = set()
        self.filter_visible = None
        self.busy = False

    # The current view's data.

    @property
    def nodes(self):
        return self.view.nodes or []

    @property
    def children_of(self):
        return self.view.children_of

    @property
    def totals(self):
        return self.view.totals

    @property
    def marks(self):
        return self.view.marks

    @property
    def expanded(self):
        return self.view.expanded

    @property
    def name_column(self):
        return self.view.name_column

    def compose(self) -> ComposeResult:
        yield Header(icon="")
        views = Static(id="views")
        views.display = len(self.views) > 1
        yield views
        yield DriveTree(Text(self.view.name, style="bold bright_blue"), id="drive-tree")
        filter_input = Input(placeholder="filter by name or path", id="filter")
        filter_input.display = False
        yield filter_input
        yield Static(id="selection-summary")
        yield Footer()

    def on_mount(self):
        self.update_sub_title()
        tree = self.query_one("#drive-tree", Tree)
        tree.focus()
        self.show_view(self.view)

    # Views.

    def show_view(self, view):
        self.view = view
        if self.filter_terms or self.query_one("#filter", Input).display:
            self.clear_filter(rebuild=False)
        tree = self.query_one("#drive-tree", Tree)
        if view.nodes is None:
            tree.clear()
            tree.root.set_label(Text.assemble((view.name, "bold bright_blue"), ("  loading…", "dim")))
            if view.error is None:
                self.run_worker(lambda: self.load_in_background(view), thread=True, group="views")
            else:
                tree.root.set_label(Text.assemble((view.name, "bold bright_blue"), (f"  {view.error}", "red")))
        else:
            tree.root.set_label(Text(view.name, style="bold bright_blue"))
            self.rebuild()
            if not view.nodes:
                tree.root.set_label(Text.assemble((view.name, "bold bright_blue"), ("  empty", "dim")))
        self.update_views()
        self.update_selection_summary()

    def load_in_background(self, view):
        try:
            load_view(self.make_service(), view, self.cache)
            view.marks = local_marks(view.nodes, self.destination, self.state)
        except (HttpError, OSError) as error:
            view.error = error_text(error)
        self.call_from_thread(self.view_loaded, view)

    def view_loaded(self, view):
        if view is self.view:
            self.show_view(view)

    def update_views(self):
        text = Text()
        for view in self.views:
            selected = sum(
                1 for entry in self.selections
                if entry.get("view") is view
            )
            label = f" {view.name}" + (f" ({selected})" if selected else "") + " "
            if view is self.view:
                text.append(label, style="bold #07111f on #38bdf8")
            else:
                text.append(label, style="#93c5fd")
            text.append(" ")
        self.query_one("#views", Static).update(text)

    def switch_view(self, step):
        if len(self.views) < 2 or self.busy:
            return
        index = (self.views.index(self.view) + step) % len(self.views)
        self.show_view(self.views[index])

    def action_next_view(self):
        self.switch_view(1)

    def action_previous_view(self):
        self.switch_view(-1)

    # Tree building: children are added when their folder is first expanded.

    def populate(self, tree_node):
        key = None if tree_node.data is None else tree_node.data["index"]
        if key in self.populated:
            return
        self.populated.add(key)
        for entry in self.children_of.get(key, []):
            if self.filter_visible is not None and entry["index"] not in self.filter_visible:
                continue
            child = tree_node.add(
                self.node_label(entry),
                data=entry,
                expand=False,
                allow_expand=entry["index"] in self.children_of,
            )
            self.tree_nodes[entry["index"]] = child

    def expand(self, tree_node):
        self.populate(tree_node)
        tree_node.expand()

    @on(Tree.NodeExpanded)
    def node_expanded(self, event):
        self.populate(event.node)
        if not self.filter_terms and event.node.data is not None:
            self.expanded.add(event.node.data["index"])

    @on(Tree.NodeCollapsed)
    def node_collapsed(self, event):
        if not self.filter_terms and event.node.data is not None:
            self.expanded.discard(event.node.data["index"])

    def rebuild(self):
        tree = self.query_one("#drive-tree", Tree)
        current = self.current_entry()
        tree.clear()
        self.tree_nodes = {}
        self.populated = set()
        self.populate(tree.root)
        tree.root.expand()
        to_expand = self.filter_ancestors if self.filter_terms else self.expanded
        for entry in self.nodes:  # parents come first, so each one is already in the tree
            index = entry["index"]
            if index in to_expand and index in self.tree_nodes:
                self.expand(self.tree_nodes[index])
        target = None
        if current is not None and current in self.nodes:
            target = self.tree_nodes.get(current["index"])
        if target is not None:
            self.call_after_refresh(tree.move_cursor, target)
        self.update_selection_summary()

    def apply_filter(self, text):
        self.filter_terms = text.casefold().split()
        if not self.filter_terms:
            self.filter_visible = None
            self.filter_ancestors = set()
        else:
            matched = {entry["index"] for entry in self.nodes if matches_filter(entry, self.filter_terms)}
            ancestors = set()
            visible = set(matched)
            by_index = self.nodes
            for index in matched:
                parent = by_index[index]["parent"]
                while parent is not None and parent not in ancestors:
                    ancestors.add(parent)
                    parent = by_index[parent]["parent"]
            visible |= ancestors
            # Everything below a matching folder stays reachable.
            for entry in self.nodes:
                if entry["parent"] in visible and entry["parent"] in matched:
                    visible.add(entry["index"])
                    if entry["index"] in self.children_of:
                        matched.add(entry["index"])
            self.filter_visible = visible
            self.filter_ancestors = ancestors
        if self.view.nodes is not None:
            self.rebuild()

    # Labels and summary.

    def update_sub_title(self):
        self.sub_title = "" if self.destination is None else f"downloads to {short_path(self.destination)}"

    def node_label(self, entry):
        item = entry["item"]
        state = path_selection_state(selected_path(entry), self.selections)
        label = Text()
        label.append(
            {"x": "☑ ", "*": "◩ "}.get(state, "☐ "),
            style="bold bright_green" if state != " " else "bright_black",
        )
        mark = self.marks.get(entry["index"])
        if mark:
            symbol, style, _ = MARKS[mark]
            label.append(f"{symbol} ", style=style)
        else:
            label.append("  ")
        if item["mimeType"] == SHORTCUT_MIME_TYPE:
            label.append("↪ ", style="bright_magenta")

        folder = is_folder(item)
        unsupported = not folder and is_unsupported(item)
        name_style = "bold cyan" if folder else "bright_black" if unsupported else "bright_white"
        width = self.name_column - label_start(entry, self.children_of) - label.cell_len
        name = Text(item["name"], style=name_style)
        name.truncate(max(width, 4), overflow="ellipsis", pad=True)
        label.append_text(name)

        mime_type = effective_mime_type(item)
        if folder:
            files, size = self.totals.get(entry["index"], (0, 0))
            size_text, date_text, info = human_size(size) if size else "", "", plural(files, "file")
        else:
            size_text = display_size(item) if item.get("size") else ""
            date_text = display_date(item)
            if mime_type in EXPORT_FORMATS:
                info = f"→ {EXPORT_FORMATS[mime_type][1]}"
            elif unsupported:
                info = "not downloadable"
            else:
                info = ""
        label.append(f"  {size_text:>7}  {date_text:<10}  ", style="dim")
        label.append(info, style="dim")
        label.rstrip()
        return label

    def refresh_node_labels(self):
        for tree_node in self.tree_nodes.values():
            tree_node.set_label(self.node_label(tree_node.data))
        self.update_selection_summary()
        self.update_views()

    def update_selection_summary(self, status=None):
        text = Text()
        if status is not None:
            text.append(status, style="dim")
            self.query_one("#selection-summary", Static).update(text)
            return
        folders = sum(is_folder(entry["item"]) for entry in self.selections)
        files = len(self.selections) - folders
        total_files = sum(entry["totals"][0] for entry in self.selections)
        total_bytes = sum(entry["totals"][1] for entry in self.selections)
        if self.selections:
            text.append(f"{len(self.selections)} selected", style="bold bright_green")
            parts = []
            if folders:
                parts.append(plural(folders, "folder"))
            if files:
                parts.append(plural(files, "file"))
            text.append(f" ({', '.join(parts)})")
            text.append(f" · {plural(total_files, 'file')} to check", style="dim")
            if total_bytes:
                text.append(f" · {human_size(total_bytes)}", style="dim")
        else:
            text.append("Nothing selected", style="bright_black")
        if self.filter_terms:
            text.append("   filter: ", style="dim")
            text.append(" ".join(self.filter_terms), style="bold #f59e0b")
        self.query_one("#selection-summary", Static).update(text)

    # Actions.

    def current_node(self):
        return self.query_one("#drive-tree", Tree).cursor_node

    def current_entry(self):
        node = self.current_node()
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
            self.add_entry(entry)
        self.refresh_node_labels()

    def add_entry(self, entry):
        if add_selected_item(self.selections, entry["item"], entry["relative_parent"]):
            # Keep the view and totals, for the tab counts and the summary.
            if is_folder(entry["item"]):
                totals = tuple(self.totals.get(entry["index"], (0, 0)))
            else:
                totals = (1, int(entry["item"].get("size") or 0))
            for selected in self.selections:
                if selected["item"] is entry["item"]:
                    selected["view"] = self.view
                    selected["totals"] = totals

    def action_toggle_current(self):
        self.toggle_entry(self.current_entry())

    def action_activate_current(self):
        node = self.current_node()
        if node is None or not node.allow_expand:
            return
        if node.is_expanded:
            node.collapse()
        else:
            self.expand(node)

    def action_collapse_or_parent(self):
        tree = self.query_one("#drive-tree", Tree)
        node = tree.cursor_node
        if node is None:
            return
        if node.allow_expand and node.is_expanded:
            node.collapse()
        elif node.parent is not None:
            tree.move_cursor(node.parent)

    def action_expand_or_child(self):
        tree = self.query_one("#drive-tree", Tree)
        node = tree.cursor_node
        if node is None or not node.allow_expand:
            return
        if not node.is_expanded:
            self.expand(node)
        elif node.children:
            tree.move_cursor(node.children[0])

    def action_expand_all(self):
        node = self.current_node()
        if node is None:
            return
        if node.data is not None and not node.allow_expand:
            node = node.parent
        if node.is_expanded and node.data is not None and all(
            not child.allow_expand or child.is_expanded for child in node.children
        ):
            node.collapse_all()
            return
        stack = [node]
        while stack:
            current = stack.pop()
            if current.allow_expand or current.data is None:
                self.expand(current)
                stack.extend(current.children)

    def action_select_all(self):
        if self.view.nodes is None:
            return
        if self.filter_terms:
            targets = [
                entry for entry in self.nodes
                if entry["index"] in self.filter_visible and matches_filter(entry, self.filter_terms)
            ]
        else:
            targets = self.children_of.get(None, [])
        if all(path_selection_state(selected_path(entry), self.selections) != " " for entry in targets):
            # Everything is already selected: unselect it.
            paths = {selected_path(entry) for entry in targets}
            self.selections[:] = [
                entry for entry in self.selections
                if selected_path(entry) not in paths
                and not any(path in selected_path(entry).parents for path in paths)
            ]
        else:
            for entry in targets:
                self.add_entry(entry)
        self.refresh_node_labels()

    def action_clear_selection(self):
        self.selections.clear()
        self.refresh_node_labels()

    def action_start_filter(self):
        filter_input = self.query_one("#filter", Input)
        filter_input.display = True
        filter_input.focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event):
        self.apply_filter(event.value)

    @on(Input.Submitted, "#filter")
    def filter_submitted(self, event):
        if not event.value.strip():
            event.input.display = False
        self.query_one("#drive-tree", Tree).focus()

    def clear_filter(self, rebuild=True):
        filter_input = self.query_one("#filter", Input)
        filter_input.display = False
        self.query_one("#drive-tree", Tree).focus()
        if not rebuild:
            with filter_input.prevent(Input.Changed):
                filter_input.value = ""
            self.filter_terms = []
            self.filter_visible = None
            self.filter_ancestors = set()
        elif filter_input.value:
            filter_input.value = ""  # Input.Changed rebuilds the tree

    def action_open_in_drive(self):
        entry = self.current_entry()
        if entry is None:
            return
        if open_quietly(drive_url(entry["item"])):
            self.notify(f"Opened {entry['item']['name']} in your browser.")
        else:
            self.notify("Could not open a browser.", severity="error")

    def action_change_destination(self):
        if self.busy:
            return
        initial = self.destination or Path(DOWNLOAD_PATH).expanduser()

        def changed(destination):
            if destination is None or destination == self.destination:
                return
            self.destination = destination
            self.state = load_state(destination)
            for view in self.views:
                if view.nodes is not None:
                    view.marks = local_marks(view.nodes, destination, self.state)
            self.update_sub_title()
            self.refresh_node_labels()
            self.notify(f"Downloading to {destination} for this session.")

        self.push_screen(DestinationSetupScreen(initial), changed)

    def action_help(self):
        self.push_screen(HelpScreen())

    # Download: compare with the destination, then preview.

    def local_scopes(self):
        browsed = {}
        for view in self.views:
            browsed.update(view.browsed)
        return build_local_scopes(self.selections, browsed, [view.base for view in self.views if view.nodes])

    def action_confirm(self):
        if self.busy:
            return
        if not self.selections:
            self.notify("Select at least one item.", severity="warning")
            return
        self.busy = True
        self.update_selection_summary("Comparing with local files…")
        selections, scopes, destination = list(self.selections), self.local_scopes(), self.destination
        self.run_worker(lambda: self.plan_in_background(selections, scopes, destination), thread=True)

    def plan_in_background(self, selections, scopes, destination):
        checked = 0

        def on_item():
            nonlocal checked
            checked += 1
            if checked % 25 == 0:
                self.call_from_thread(self.update_selection_summary, f"Comparing with local files… {checked}")

        try:
            state = load_state(destination)
            plan = build_plan(self.make_service(), selections, scopes, destination, state, self.cache, on_item)
        except (HttpError, OSError) as error:
            self.call_from_thread(self.plan_failed, error_text(error))
            return
        result = {"plan": plan, "selections": selections, "scopes": scopes,
                  "destination": destination, "state": state}
        self.call_from_thread(self.plan_ready, result)

    def plan_failed(self, message):
        self.busy = False
        self.update_selection_summary()
        self.notify(f"Could not compare: {message}", severity="error")

    def plan_ready(self, result):
        self.busy = False
        self.update_selection_summary()
        if self.skip_preview:
            self.exit(result)
            return

        def answered(confirmed):
            if confirmed:
                self.exit(result)

        self.push_screen(PreviewScreen(result["plan"], result["destination"]), answered)

    def action_cancel(self):
        if self.query_one("#filter", Input).display:
            self.clear_filter()
            return
        self.exit(None)

    def action_quit_selector(self):
        self.exit(None)


ITEM_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum,shortcutDetails,webViewLink,trashed,driveId"


def remember_selection(state, selections, scopes, roots=()):
    """Store the selection for --again; roots are views selected entirely, so new items there come too."""
    state["last_selection"] = {
        "roots": [{"id": root_id, "base": base.as_posix()} for root_id, base in roots],
        "items": [
            {
                "id": entry["item"]["id"],
                "name": entry["item"]["name"],
                "relative_parent": entry["relative_parent"].as_posix(),
                "path": selected_path(entry).as_posix(),
            }
            for entry in selections
        ],
        "scopes": [scope.as_posix() for scope in scopes],
    }


def last_selection(service, state, cache):
    """The previous run's selection with fresh Drive metadata; items gone from Drive stay as local scopes."""
    saved = state.get("last_selection")
    if not saved or not (saved.get("items") or saved.get("roots")):
        return None
    selections = []
    scopes = [Path(scope) for scope in saved.get("scopes", [])]
    for root in saved.get("roots", []):
        if root["id"] == SHARED_WITH_ME:
            cache.pop(SHARED_WITH_ME, None)  # a search, not a folder: Drive changes do not update it
        for item in list_children(service, root["id"], cache):
            add_selected_item(selections, item, Path(root["base"]))
    for saved_item in saved["items"]:
        relative_parent = Path(saved_item["relative_parent"])
        if path_selection_state(Path(saved_item["path"]), selections) != " ":
            continue  # already there through a whole view
        try:
            item = (
                service.files()
                .get(fileId=saved_item["id"], fields=ITEM_FIELDS, supportsAllDrives=True)
                .execute(num_retries=API_RETRIES)
            )
        except HttpError as error:
            if error.status_code != 404:
                raise
            item = None
        if item is None or item.get("trashed"):
            warn(f"{saved_item['path']} is no longer on Drive")
            scopes.append(Path(saved_item["path"]))
            continue
        note_drives([item])
        add_selected_item(selections, item, relative_parent)
    return selections, compact_scopes(scopes)


# --- Planning --------------------------------------------------------------

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
        warn(f"Ignoring invalid state file {state_path}: {error}")
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


class Stopped(Exception):
    """Raised in download threads after Ctrl+C."""


def fetch_media(service, item, file_handle, stop=None):
    downloader = MediaIoBaseDownload(
        file_handle, download_request(service, item), chunksize=8 * 1024 * 1024
    )
    done = False
    while not done:
        if stop is not None and stop.is_set():
            raise Stopped()
        _, done = downloader.next_chunk(num_retries=API_RETRIES)


class CountingWriter:
    def __init__(self, file_handle, on_bytes):
        self.file_handle = file_handle
        self.on_bytes = on_bytes

    def write(self, data):
        written = self.file_handle.write(data)
        self.on_bytes(len(data))
        return written


def remote_export_hash(service, item):
    sink = HashSink()
    fetch_media(service, item, sink)
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
            warn(f"Could not compare {relative_path.as_posix()}: {error_text(error)}")
    return "CONFLICT", current_sha256


def collect_plan(
    service,
    item,
    relative_parent,
    destination_root,
    state,
    plan,
    cache,
    ancestor_folder_ids=frozenset(),
):
    mime_type = effective_mime_type(item)
    relative_path = relative_parent / local_name(item)
    destination = destination_root / relative_path

    if mime_type == FOLDER_MIME_TYPE:
        folder_id = effective_id(item)
        status = "UNCHANGED" if destination.is_dir() else "NEW"
        if relative_path in RESERVED_PATHS:
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
        for child in list_children(service, folder_id, cache):
            collect_plan(
                service,
                child,
                relative_path,
                destination_root,
                state,
                plan,
                cache,
                ancestor_folder_ids | {folder_id},
            )
        return

    item = resolve_file_shortcut(service, item)
    relative_path = relative_parent / local_name(item)
    destination = destination_root / relative_path

    if relative_path in RESERVED_PATHS:
        status = "CONFLICT"
        current_sha256 = None
    elif is_unsupported(item):
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
    tracked_directories = {
        parent for tracked_path in tracked for parent in tracked_path.parents
    }
    planned_local_paths = {
        entry["relative_path"]
        for entry in plan
        if entry.get("item") is None
    }

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
            if relative_path.parts[0] in RESERVED_NAMES:
                continue
            if (
                relative_directory == Path()
                and child.name in VIEW_DIRS
                and relative_path not in remote_directories
            ):
                continue  # the Shared with me / Shared drives views have their own scopes
            if child.name.endswith(".gdrivepull.part"):
                continue

            if child.is_dir():
                if relative_path in remote_directories or relative_path in tracked_directories:
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


def build_plan(service, selections, scopes, destination_root, state, cache, on_item=lambda: None):
    prefetch_folders(
        service,
        [effective_id(entry["item"]) for entry in selections if is_folder(entry["item"])],
        cache,
    )
    plan = []
    for selection in selections:
        before = len(plan)
        collect_plan(
            service,
            selection["item"],
            selection["relative_parent"],
            destination_root,
            state,
            plan,
            cache,
        )
        for _ in range(len(plan) - before):
            on_item()
    mark_duplicate_targets(plan)
    add_local_only_entries(plan, destination_root, state, scopes)
    return plan


# --- Preview and download --------------------------------------------------

STATUS_STYLES = {
    "NEW": ("green", "new"),
    "UPDATE": ("cyan", "update"),
    "UNCHANGED": ("bright_black", "unchanged"),
    "CONFLICT": ("yellow", "conflict"),
    "REMOVED_REMOTE": ("magenta", "removed from Drive"),
    "LOCAL_ONLY": ("blue", "local only"),
    "SKIPPED": ("bright_black", "skipped"),
}
ACTIONS = {"NEW", "UPDATE", "REMOVED_REMOTE"}


def plan_summary(plan):
    counts = Counter(entry["status"] for entry in plan)
    parts = []
    for status, (style, label) in STATUS_STYLES.items():
        if counts[status]:
            parts.append(f"[{style}]{counts[status]} {label}[/]")
    return " · ".join(parts)


def has_actions(plan):
    return any(entry["status"] in ACTIONS for entry in plan)


def print_plan(plan, destination_root):
    shown = [entry for entry in plan if VERBOSE or entry["status"] != "UNCHANGED"]
    hidden = len(plan) - len(shown)

    if shown:
        table = Table(box=None, show_header=True, header_style="bold", pad_edge=False)
        table.add_column("STATUS", no_wrap=True)
        table.add_column("ITEM", overflow="fold")
        table.add_column("SIZE", justify="right", no_wrap=True, style="dim")
        for entry in shown:
            style, label = STATUS_STYLES[entry["status"]]
            path = escape(entry["relative_path"].as_posix())
            if entry["kind"] == "FOLDER":
                path = f"[cyan]{path}/[/]"
            item = entry.get("item")
            size = display_size(item) if item is not None and entry["kind"] == "FILE" else ""
            table.add_row(f"[{style}]{label}[/]", path, size if size != "-" else "")
        console.print()
        console.print(table)

    console.print()
    console.print(f"[bold]Preview[/]  {plan_summary(plan)}")
    if hidden:
        console.print(f"[dim]{plural(hidden, 'unchanged item')} not listed (--verbose lists them).[/]")
    for note in plan_notes(plan):
        console.print(f"[dim]{note}[/]")
    console.print(f"[dim]Destination:[/] {escape(str(destination_root))}")


def plan_notes(plan):
    statuses = {entry["status"] for entry in plan}
    notes = []
    if "CONFLICT" in statuses:
        notes.append("Conflicts are preserved and skipped.")
    if "LOCAL_ONLY" in statuses:
        notes.append("Local-only items are preserved and skipped.")
    if "REMOVED_REMOTE" in statuses:
        notes.append(f"Files removed from Drive are moved to {RECOVERY_DIR_NAME}/.")
    return notes


PREVIEW_VIEWS = [
    ("Changes", lambda entry: entry["status"] != "UNCHANGED"),
    ("Everything", lambda entry: True),
] + [
    (label.capitalize(), lambda entry, status=status: entry["status"] == status)
    for status, (_, label) in STATUS_STYLES.items()
]


class PreviewScreen(Screen):
    """The plan before anything changes: y downloads, esc goes back (or cancels)."""

    SUB_TITLE = "Preview"

    DEFAULT_CSS = """
    #preview-summary {
        height: auto;
        margin: 1 2 0 2;
    }

    #preview-table {
        height: 1fr;
        margin: 1 2 0 2;
        border: round #38bdf8;
        background: #081525;
    }

    #preview-table > .datatable--cursor {
        background: #164e63;
        color: #ffffff;
    }

    #preview-table > .datatable--header {
        background: #0f2742;
        color: #93c5fd;
    }

    #preview-notes {
        height: auto;
        margin: 0 2;
        padding: 0 1;
        color: #94a3b8;
    }
    """

    BINDINGS = [
        Binding("y", "confirm", "Download"),
        Binding("v", "next_filter", "Show"),
        Binding("escape,n", "back", "Back"),
    ]

    def __init__(self, plan, destination):
        super().__init__()
        self.plan = plan
        self.destination = destination
        self.filters = [
            (name, keep) for name, keep in PREVIEW_VIEWS
            if any(keep(entry) for entry in plan)
        ] or PREVIEW_VIEWS[:1]
        self.filter_index = 0

    def compose(self) -> ComposeResult:
        yield Header(icon="")
        yield Static(id="preview-summary")
        yield DataTable(id="preview-table", cursor_type="row", zebra_stripes=False)
        yield Static(id="preview-notes")
        yield Footer()

    def on_mount(self):
        self.sub_title = f"preview · downloads to {short_path(self.destination)}"
        table = self.query_one(DataTable)
        table.add_column("STATUS", key="status")
        table.add_column("ITEM", key="item")
        table.add_column("SIZE", key="size")
        notes = Text()
        for note in plan_notes(self.plan):
            notes.append(note + "\n")
        if not has_actions(self.plan):
            notes.append("Everything is up to date: y saves the state and quits.", style="bold green")
        notes.rstrip()
        self.query_one("#preview-notes", Static).update(notes)
        self.fill()
        table.focus()

    def fill(self):
        name, keep = self.filters[self.filter_index]
        rows = [entry for entry in self.plan if keep(entry)]
        summary = Text.from_markup(f"[bold]Preview[/]  {plan_summary(self.plan)}")
        summary.append("\nShowing: ", style="dim")
        summary.append(name, style="bold #f59e0b")
        summary.append(f" ({plural(len(rows), 'item')})", style="dim")
        if len(self.filters) > 1:
            summary.append("   v: next", style="dim")
        self.query_one("#preview-summary", Static).update(summary)

        table = self.query_one(DataTable)
        table.clear()
        for entry in rows:
            style, label = STATUS_STYLES[entry["status"]]
            path = entry["relative_path"].as_posix()
            item = entry.get("item")
            size = display_size(item) if item is not None and entry["kind"] == "FILE" else ""
            table.add_row(
                Text(label, style=style),
                Text(path + "/", style="cyan") if entry["kind"] == "FOLDER" else Text(path),
                Text(size if size != "-" else "", style="dim", justify="right"),
            )

    def action_next_filter(self):
        self.filter_index = (self.filter_index + 1) % len(self.filters)
        self.fill()

    def action_confirm(self):
        self.dismiss(True)

    def action_back(self):
        self.dismiss(False)


class StandalonePreviewScreen(PreviewScreen):
    BINDINGS = [Binding("escape,n,q", "back", "Cancel")]


class PreviewApp(App):
    """The preview on its own, for --again."""

    TITLE = APP_NAME
    ENABLE_COMMAND_PALETTE = False
    CSS = SHARED_CSS

    def __init__(self, plan, destination):
        super().__init__()
        self.plan = plan
        self.destination = destination

    def on_mount(self):
        self.push_screen(StandalonePreviewScreen(self.plan, self.destination), self.exit)


def set_remote_mtime(destination, item):
    modified_time = item.get("modifiedTime")
    if not modified_time:
        return
    timestamp = datetime.fromisoformat(
        modified_time.replace("Z", "+00:00")
    ).timestamp()
    os.utime(destination, (timestamp, timestamp))


def download_file_atomically(service, entry, stop=None, on_bytes=lambda count: None):
    destination = entry["destination"]
    temporary = destination.with_name(
        f".{destination.name}.gdrivepull.part"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        with temporary.open("wb") as file_handle:
            fetch_media(service, entry["item"], CountingWriter(file_handle, on_bytes), stop)
        set_remote_mtime(temporary, entry["item"])
        os.replace(temporary, destination)
    except BaseException:
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


def apply_plan(service, plan, destination_root, state, jobs=1, make_service=None):
    """Apply the confirmed plan; downloads run in jobs threads, each with its own Drive client."""
    results = Counter()
    recovery_root = (
        destination_root
        / RECOVERY_DIR_NAME
        / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    downloads = []
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[transfer]}[/]"),
        console=console,
        transient=True,
    )

    def report(mark, text):
        progress.console.print(f"{mark} {escape(text)}")

    for entry in plan:
        status = entry["status"]
        relative = entry["relative_path"].as_posix()
        if entry.get("item") is None:
            if status == "REMOVED_REMOTE":
                try:
                    recovery_target = unused_recovery_target(
                        recovery_root, entry["relative_path"]
                    )
                    recovery_target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(entry["destination"], recovery_target)
                    report(
                        "[magenta]→[/]",
                        f"{relative} moved to {recovery_target.relative_to(destination_root)}",
                    )
                    if entry.get("state_key"):
                        state["files"].pop(entry["state_key"], None)
                    results["FILE_REMOVED_REMOTE"] += 1
                except OSError as error:
                    results[f"{entry['kind']}_ERROR"] += 1
                    report("[red]✗[/]", f"{relative}: could not move to recovery: {error}")
            else:
                results[f"{entry['kind']}_{status}"] += 1
            continue

        if entry["kind"] == "FOLDER":
            if status == "NEW":
                try:
                    entry["destination"].mkdir(parents=True, exist_ok=True)
                except OSError as error:
                    results["FOLDER_ERROR"] += 1
                    report("[red]✗[/]", f"{relative}/: {error}")
                    continue
            results[f"FOLDER_{status}"] += 1
            continue

        if status == "UNCHANGED":
            local_sha256 = entry.get("local_sha256") or file_hash(entry["destination"])
            state["files"][state_key(entry["item"], entry["relative_path"])] = state_record(entry, local_sha256)
            try:
                set_remote_mtime(entry["destination"], entry["item"])  # same content: lets the tree mark it ✓
            except OSError:
                pass
            results["FILE_UNCHANGED"] += 1
        elif status in {"CONFLICT", "SKIPPED"}:
            results[f"FILE_{status}"] += 1
        else:
            downloads.append(entry)

    if not downloads:
        save_state(destination_root, state)
        return results

    stop = threading.Event()
    lock = threading.Lock()
    clients = threading.local()
    started = time.monotonic()
    transferred = 0
    task = progress.add_task("Downloading", total=len(downloads), transfer="")

    def on_bytes(count):
        nonlocal transferred
        with lock:
            transferred += count
            done = transferred
        speed = done / max(time.monotonic() - started, 0.001)
        progress.update(task, transfer=f"{human_size(done)} · {human_size(speed)}/s")

    def download(entry):
        if make_service is None:
            client = service
        else:
            if not hasattr(clients, "service"):
                clients.service = make_service()
            client = clients.service
        download_file_atomically(client, entry, stop, on_bytes)
        return file_hash(entry["destination"])

    executor = ThreadPoolExecutor(max_workers=max(1, jobs))
    try:
        with progress:
            futures = {executor.submit(download, entry): entry for entry in downloads}
            for future in as_completed(futures):
                entry = futures[future]
                relative = entry["relative_path"].as_posix()
                try:
                    local_sha256 = future.result()
                except (HttpError, OSError) as error:
                    results["FILE_ERROR"] += 1
                    report("[red]✗[/]", f"{relative}: {error_text(error)}")
                else:
                    state["files"][state_key(entry["item"], entry["relative_path"])] = state_record(
                        entry, local_sha256
                    )
                    results[f"FILE_{entry['status']}"] += 1
                    report("[green]✓[/]" if entry["status"] == "NEW" else "[cyan]↻[/]", relative)
                progress.advance(task)
    except BaseException:
        stop.set()  # running downloads stop at their next chunk and remove their partial file
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        save_state(destination_root, state)
    return results


RESULT_LABELS = [
    ("FILE_NEW", "green", "downloaded", "downloaded"),
    ("FILE_UPDATE", "cyan", "updated", "updated"),
    ("UNCHANGED", "bright_black", "unchanged", "unchanged"),
    ("FOLDER_NEW", "green", "folder created", "folders created"),
    ("FILE_REMOVED_REMOTE", "magenta", "moved to recovery", "moved to recovery"),
    ("CONFLICT", "yellow", "conflict kept", "conflicts kept"),
    ("LOCAL_ONLY", "blue", "local-only item kept", "local-only items kept"),
    ("SKIPPED", "bright_black", "skipped", "skipped"),
    ("ERROR", "red", "error", "errors"),
]


def results_summary(results):
    parts = []
    for key, style, singular, plural_label in RESULT_LABELS:
        if key.startswith(("FILE_", "FOLDER_")):
            count = results[key]
        else:
            count = results[f"FILE_{key}"] + results[f"FOLDER_{key}"]
        if count:
            parts.append(f"[{style}]{count} {singular if count == 1 else plural_label}[/]")
    return " · ".join(parts) or "[dim]nothing to do[/]"


def print_results(results):
    console.print(f"[bold]Done[/]  {results_summary(results)}")


# --- Main ------------------------------------------------------------------

def resolve_destination(path_text):
    """The download folder: new (created), empty, or already managed by GDrive Pull."""
    destination = Path(path_text).expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    destination = destination.parent.resolve() / destination.name
    return initialize_managed_destination(destination)


def main():
    parser = argparse.ArgumentParser(
        prog="gdrivepull",
        description=(
            "Download files and folders from Google Drive into a managed local folder.\n"
            "Pick them in a terminal tree, review the preview, then confirm: local changes\n"
            "are never overwritten and nothing is deleted (files removed from Drive go to\n"
            f"{RECOVERY_DIR_NAME}/). Drive access is read-only."
        ),
        epilog="keys (in the tree):\n" + "\n".join(help_lines()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--download-path",
        metavar="PATH",
        default=DOWNLOAD_PATH,
        help="Download folder (default: %(default)s); new, empty or already managed by GDrive Pull.",
    )
    parser.add_argument(
        "--folder-id",
        default="root",
        metavar="ID",
        help="Drive folder to browse (default: My Drive root).",
    )
    parser.add_argument(
        "--again",
        action="store_true",
        help="Download the previous selection again, without the tree (with --yes: no prompt at all).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply the preview without asking for confirmation.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        choices=range(1, 17),
        metavar="N",
        help="Files downloaded at the same time, 1 to 16 (default: 4).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show sign-in, token and API details, and list unchanged items in the preview.",
    )
    args = parser.parse_args()
    global VERBOSE
    VERBOSE = args.verbose

    try:
        try:
            destination_root = resolve_destination(args.download_path)
        except (OSError, ValueError) as error:
            console.print(
                f"[red]✗[/] {escape(args.download_path)}: {escape(str(error))}\n"
                "  [dim]Choose another folder with --download-path PATH.[/]"
            )
            raise SystemExit(1)

        creds = authenticate()

        def make_service():
            return build("drive", "v3", credentials=creds, cache_discovery=False)

        service = make_service()
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

        if args.again:
            state = load_state(destination_root)
            if not state.get("last_selection"):
                console.print(
                    f"[red]✗[/] No previous selection in {escape(str(destination_root))}: "
                    "run once without --again."
                )
                raise SystemExit(1)
            with console.status("[dim]Checking Drive changes…[/]"):
                snapshot = load_snapshot(destination_root)
                replayed = apply_changes(service, snapshot) if snapshot else None
                if replayed:
                    cache, token = replayed
                else:
                    cache, token = {}, start_page_token(service)
                selections, local_scopes = last_selection(service, state, cache)
            names = ", ".join(escape(entry["item"]["name"]) for entry in selections[:5])
            more = f" and {len(selections) - 5} more" if len(selections) > 5 else ""
            console.print(f"[bold]Again[/]  {names}{more} [dim]→ {escape(str(destination_root))}[/]")
            roots = [(root["id"], Path(root["base"])) for root in state["last_selection"].get("roots", [])]
            with console.status("[dim]Comparing with local files…[/]") as status:
                checked = 0

                def on_item():
                    nonlocal checked
                    checked += 1
                    if checked % 25 == 0:
                        status.update(f"[dim]Comparing with local files… {checked}[/]")

                plan = build_plan(service, selections, local_scopes, destination_root, state, cache, on_item)
            if args.yes or not interactive:
                print_plan(plan, destination_root)
                if not args.yes and has_actions(plan):
                    answer = console.input("\nProceed? [y/N] ").strip().lower()
                    if answer not in {"y", "yes"}:
                        console.print("[dim]Cancelled. No files were changed.[/]")
                        return
            elif not PreviewApp(plan, destination_root).run():
                console.print("[dim]Cancelled. No files were changed.[/]")
                return
        else:
            token = start_page_token(service)
            cache = {}
            with console.status("[dim]Loading the Drive tree…[/]") as status:
                views = drive_views(service, args.folder_id)

                def on_progress(folders, items):
                    status.update(
                        f"[dim]Loading the Drive tree… {plural(folders, 'folder')}, {plural(items, 'item')}[/]"
                    )

                load_view(service, views[0], cache, on_progress)
            debug(f"Loaded {plural(len(views[0].nodes), 'item')} in {plural(len(cache), 'folder')}")
            result = DriveSelectorApp(
                views, destination_root, load_state(destination_root), service, make_service, cache,
                skip_preview=args.yes,
            ).run()
            if not result:
                console.print("[dim]Nothing downloaded.[/]")
                return
            plan, selections, local_scopes = result["plan"], result["selections"], result["scopes"]
            destination_root, state = result["destination"], result["state"]
            roots = [
                (view.root_id, view.base) for view in views
                if view.nodes and fully_selected(view.browsed.get(view.base, []), view.base, selections)
            ]
            if args.yes:
                print_plan(plan, destination_root)

        remember_selection(state, selections, local_scopes, roots)
        results = apply_plan(service, plan, destination_root, state, args.jobs, make_service)
        save_snapshot(
            destination_root, token, cache,
            [root_id for root_id, _ in roots]
            + [effective_id(entry["item"]) for entry in selections if is_folder(entry["item"])],
        )
        if not has_actions(plan):
            console.print("[green]✓[/] Everything is up to date.")
            return
        print_results(results)
        if results["FILE_ERROR"] or results["FOLDER_ERROR"]:
            raise SystemExit(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        raise SystemExit(130)
    except (HttpError, OSError) as error:
        console.print(f"[red]✗[/] {escape(error_text(error))}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

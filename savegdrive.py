import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import textwrap
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
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from rich.console import Console, Group
from send2trash import send2trash
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Header,
    Input,
    Static,
    Tree,
)


APP_NAME = "SaveGDrive"
# Full Drive access, like GMAIL: downloads only read, but the Trash view can restore, delete and empty,
# and x moves items to the Drive trash. Tokens from the read-only versions ask for a new sign-in.
SCOPES = ["https://www.googleapis.com/auth/drive"]
SCRIPT_DIR = Path(__file__).resolve().parent
DOWNLOAD_PATH = "~/GDrive"
TOKEN_PATH = SCRIPT_DIR / "token.json"
CREDENTIALS_PATH = SCRIPT_DIR / "credentials.json"
STATE_FILE_NAME = ".savegdrive-state.json"
REMOTE_CACHE_NAME = ".savegdrive-cache.json"
PART_SUFFIX = ".savegdrive.part"
RESERVED_NAMES = {
    STATE_FILE_NAME,
    f"{STATE_FILE_NAME}.tmp",
    REMOTE_CACHE_NAME,
    f"{REMOTE_CACHE_NAME}.tmp",
}
RESERVED_PATHS = {Path(name) for name in RESERVED_NAMES}
SHARED_WITH_ME = "sharedWithMe"  # pseudo folder id: the items others shared with you
SHARED_WITH_ME_DIR = "Shared with me"
SHARED_DRIVES_DIR = "Shared drives"
TRASH = "trash"  # pseudo folder id: the items you moved to the Drive trash
TRASH_DIR = "Trash"
VIEW_DIRS = {SHARED_WITH_ME_DIR, SHARED_DRIVES_DIR, TRASH_DIR}
STATE_VERSION = 1
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
LIST_FIELDS = (
    "nextPageToken,"
    "files(id,name,mimeType,size,modifiedTime,md5Checksum,shortcutDetails,webViewLink,driveId,explicitlyTrashed,"
    "capabilities(canEdit,canAddChildren,canTrash))"
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

SIGN_IN_SUCCESS_PAGE = "savegdrive: signed in. You can close this tab."


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

    webbrowser.register("savegdrive", None, QuietBrowser("savegdrive"))
    with console.status("[dim]Waiting for sign-in…[/]"):
        creds = flow.run_local_server(
            port=0,
            browser="savegdrive",
            authorization_prompt_message="",
            success_message=SIGN_IN_SUCCESS_PAGE,
        )
    if not VERBOSE and console.is_terminal:
        console.file.write("\x1b[2F\x1b[J")  # erase the two prompt lines
    debug(f"Token expires {local_expiry(creds)}")
    return creds


def signed_in_account(service):
    try:
        about = service.about().get(fields="user(emailAddress)").execute(num_retries=API_RETRIES)
    except HttpError as error:
        debug(f"Could not read the signed-in account: {error_text(error)}")
        return None
    return about.get("user", {}).get("emailAddress")


def authenticate():
    creds = None
    if TOKEN_PATH.exists():
        # Without scopes given, the credentials keep the ones the token was granted.
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        if not creds.has_scopes(SCOPES):
            # A token from the read-only versions: Google must ask again for the wider access.
            debug("Saved token lacks the full Drive scope")
            creds = sign_in("SaveGDrive now syncs both ways: allow full Drive access in your browser.")
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            return creds

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


TRASHED_FOLDERS = set()  # folders listed from the Drive trash: their children are trashed too


def list_request(service, folder_id, page_token=None):
    options = {}
    if folder_id == SHARED_WITH_ME:
        query = "sharedWithMe and trashed = false"
    elif folder_id == TRASH:
        query = "trashed = true"
    elif folder_id in TRASHED_FOLDERS:
        query = f"'{folder_id}' in parents and trashed = true"
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
        if folder_id == TRASH:
            # Only what you trashed yourself; what was inside a trashed folder shows below it.
            items = [item for item in items if item.get("explicitlyTrashed", True)]
        if folder_id == TRASH or folder_id in TRASHED_FOLDERS:
            TRASHED_FOLDERS.update(effective_id(item) for item in items if is_folder(item))
        for item in items:
            item.pop("explicitlyTrashed", None)
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
    "file(id,name,mimeType,size,modifiedTime,md5Checksum,shortcutDetails,webViewLink,driveId,parents,trashed,"
    "capabilities(canEdit,canAddChildren,canTrash)))"
)


def start_page_token(service):
    return (
        service.changes()
        .getStartPageToken(supportsAllDrives=True)
        .execute(num_retries=API_RETRIES)["startPageToken"]
    )


def save_snapshot(destination_root, token, cache, folder_ids):
    folders = reachable_folders(cache, folder_ids) - TRASHED_FOLDERS - {TRASH}
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
            fields="id,mimeType,size,modifiedTime,md5Checksum,webViewLink,capabilities(canEdit,canAddChildren,canTrash)",
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


def folder_newest(nodes):
    """The newest modification time below every folder node (ISO text, sortable), by node index."""
    newest = {}
    for node in reversed(nodes):
        own = newest.get(node["index"], "") if is_folder(node["item"]) else node["item"].get("modifiedTime", "")
        parent = node["parent"]
        if parent is not None and own > newest.get(parent, ""):
            newest[parent] = own
    return newest


SORTS = {
    "name": "name",
    "size": "size, biggest first",
    "date": "date, newest first",
}


def sort_key(sort, totals, newest):
    """Folders stay first; inside each group, by name, size or date (the two last ones descending)."""
    def size(node):
        if is_folder(node["item"]):
            return totals.get(node["index"], (0, 0))[1]
        return int(node["item"].get("size") or 0)

    def date(node):
        if is_folder(node["item"]):
            return newest.get(node["index"], "")
        return node["item"].get("modifiedTime", "")

    def key(node):
        name = node["item"]["name"].casefold()
        if sort == "size":
            return (not is_folder(node["item"]), -size(node), name)
        if sort == "date":
            # Descending text: invert each character, so newest sorts first; undated items last.
            when = date(node)
            return (not is_folder(node["item"]), not when, [-ord(char) for char in when], name)
        return (not is_folder(node["item"]), name)

    return key


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


def header_text(account, destination, prefix=None):
    parts = [part for part in (prefix, account) if part]
    if destination is not None:
        parts.append(f"syncs with {short_path(destination)}")
    return " · ".join(parts)


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

"""


class DestinationSetupScreen(Screen):
    """Pick the local folder for this session (f in the tree)."""

    SUB_TITLE = "Local folder for this session"

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
        min-height: 1;
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

        yield Header(icon="")
        with Horizontal(id="setup-body"):
            yield DirectoryTree(
                initial_parent,
                id="directory-tree",
            )
            with VerticalScroll(id="setup-form"):
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
        yield KeyBar("folder")

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

# Every key, by screen: (group, [(keys, short label for the key bar, description for ? and --help)]).
# The key bars, the ? help and --help are all built from this table.
TREE_MOVE = ("Move", [
    ("↑ ↓", "move", "move"),
    ("← →", "fold / unfold", "collapse / expand a folder, or go to the parent folder / first child"),
    ("enter", "fold / unfold", "collapse / expand a folder"),
    ("e", "unfold all", "expand the whole view; again: collapse it all (shift+space: below the cursor)"),
    ("pgup pgdn", "scroll", "scroll a page (home / end: top / bottom)"),
])
TREE_VIEW = ("View", [
    ("tab", "next view", "next view: My Drive, Shared with me, each shared drive, then Trash"),
    ("shift+tab", "previous", "previous view"),
    ("/", "filter", "filter by name or path"),
    ("z", "by size", "sort by size, biggest first; again: by name"),
    ("m", "by date", "sort by date, newest first; again: by name"),
])
TREE_SELECT = ("Select", [
    ("space", "select", "select / unselect (a selected folder includes everything below it)"),
    ("a", "all", "select all, or unselect all when everything is selected (with a filter: the matches)"),
    ("c", "none", "unselect everything, including items hidden by the filter or in other views"),
])
TREE_GO = ("Go", [
    ("d", "preview", "compare the selection with the local folder and open the preview"),
    ("s", "sync last", "review the sync of your last selection, checked at start (the line above the tree)"),
    ("f", "folder", "change the local folder for this session"),
    ("o", "open in Drive", "open the item under the cursor in Google Drive"),
    ("?", "help", "show the key help"),
    ("q esc", "quit", "quit (esc first closes the filter); after a sync, the tree comes back"),
])

# Every key, by screen: (title, [(group, [(keys, short label for the key bar, description for ? and --help)])]).
# The key bars, the ? help, --help and the README's key tables are all built from this table.
KEYS = {
    "tree": ("In the tree", [
        TREE_MOVE, TREE_VIEW, TREE_SELECT,
        ("Drive", [
            ("x", "to Drive trash", "move the selection (or the item under the cursor) to the Drive trash"),
        ]),
        TREE_GO,
    ]),
    "trash": ("In the Trash view", [
        TREE_MOVE, TREE_VIEW, TREE_SELECT,
        ("Drive", [
            ("r", "restore", "restore the selection (or the item under the cursor) on Drive"),
            ("x", "delete forever", "delete the selection (or the item under the cursor) forever, after typing delete"),
            ("shift+t", "empty trash", "empty the whole Drive trash, after typing empty"),
        ]),
        TREE_GO,
    ]),
    "filter": ("While typing a filter", [
        ("Filter", [
            ("enter", "keep", "keep the filter and go back to the tree"),
            ("esc", "clear", "clear the filter"),
        ]),
    ]),
    "preview": ("In the preview", [
        ("Move", [
            ("↑ ↓", "move", "move (pgup / pgdn, home / end: by page, to the top / bottom)"),
            ("tab", "next filter", "next filter tab: Changes, Everything, then one tab per status"),
            ("shift+tab", "previous filter", "previous filter tab"),
        ]),
        ("Conflicts", [
            ("b", "keep both", "keep both versions of the conflict under the cursor: yours as 'name (local).ext'"),
            ("shift+b", "keep both: all", "keep both for every conflict; again: undo"),
        ]),
        ("Go", [
            ("y", "sync", "sync: apply the preview"),
            ("esc n", "back", "back to the tree, selection kept"),
        ]),
    ]),
    "preview-again": ("In the preview of --again", [
        ("Move", [
            ("↑ ↓", "move", "move (pgup / pgdn, home / end: by page, to the top / bottom)"),
            ("tab", "next filter", "next filter tab"),
            ("shift+tab", "previous filter", "previous filter tab"),
        ]),
        ("Conflicts", [
            ("b", "keep both", "keep both versions of the conflict under the cursor: yours as 'name (local).ext'"),
            ("shift+b", "keep both: all", "keep both for every conflict; again: undo"),
        ]),
        ("Go", [
            ("y", "sync", "sync: apply the preview"),
            ("esc n q", "cancel", "cancel: nothing changes"),
        ]),
    ]),
    "folder": ("In the folder screen (f)", [
        ("Folder", [
            ("↑ ↓ enter", "pick", "pick a parent directory in the tree (space: unfold it)"),
            ("tab", "next field", "next field: parent directory, folder name, buttons"),
            ("ctrl+s", "apply", "apply: use this folder for the session"),
            ("esc", "cancel", "cancel"),
        ]),
    ]),
    "confirm": ("In a confirmation", [
        ("Confirm", [
            ("y", "yes", "yes"),
            ("n esc", "no", "no: nothing changes"),
        ]),
    ]),
    "confirm-word": ("In a confirmation that asks for a word (delete, empty)", [
        ("Confirm", [
            ("enter", "confirm", "confirm, once the word is typed"),
            ("esc", "cancel", "cancel: nothing changes"),
        ]),
    ]),
    "help": ("In this help", [
        ("Help", [
            ("↑ ↓", "scroll", "scroll"),
            ("esc ? q", "close", "close (enter or a click too)"),
        ]),
    ]),
}
KEY_HELP_NOTE = (
    "Nothing changes, here or on Drive, before you confirm: y in the preview, or the question of x, r and "
    "shift+t. The mouse works too: click to move, click a folder's arrow to unfold it."
)
MARKS = {
    "synced": ("✓", "green", "synced and the same on both sides"),
    "changed": ("↻", "bright_cyan", "changed on Drive since the last sync, or new files in the folder"),
    "edited": ("✎", "yellow", "changed here since the last sync: sent to Drive at the next one"),
}


def help_lines():
    """The ? help as plain lines, for --help."""
    lines = []
    for title, groups in KEYS.values():
        lines.append(f"{title.lower()}:")
        for _, keys in groups:
            lines += [f"  {key:<11}{description}" for key, _, description in keys]
        lines.append("")
    lines.append("marks (since the last sync):")
    lines += [f"  {symbol:<11}{description}" for symbol, _, description in MARKS.values()]
    return lines + [""] + ["  " + line for line in textwrap.wrap(KEY_HELP_NOTE, 76)]


def help_text():
    def table():
        keys = Table(box=None, show_header=False, pad_edge=False, padding=(0, 2, 0, 2))
        keys.add_column(style="bold #f59e0b", no_wrap=True, min_width=9)
        keys.add_column(overflow="fold")
        return keys

    parts = []
    for title, groups in KEYS.values():
        parts.append(Text(title, style="bold bright_cyan"))
        keys = table()
        for _, entries in groups:
            for key, _, description in entries:
                keys.add_row(key, description)
        parts += [keys, Text()]
    parts.append(Text.assemble(("Marks", "bold bright_cyan"), (" (since the last sync)", "dim")))
    marks = table()
    for symbol, style, description in MARKS.values():
        marks.add_row(Text(symbol, style=f"bold {style}"), description)
    parts += [marks, Text(), Text(KEY_HELP_NOTE, style="dim")]
    return Group(*parts)


class KeyBar(Widget):
    """Every key of a screen, by group, on as many lines as the width needs."""

    DEFAULT_CSS = """
    KeyBar {
        height: auto;
        padding: 0 1;
        background: #07111f;
    }
    """

    def __init__(self, context, **kwargs):
        super().__init__(**kwargs)
        self.context = context

    def set_context(self, context):
        if context != self.context:
            self.context = context
            self.refresh(layout=True)

    def lines(self, width):
        groups = KEYS[self.context][1]
        label_width = max(len(name) for name, _ in groups) + 2
        lines = []
        for name, keys in groups:
            line = Text(f"{name:<{label_width}}", style="dim")
            for key, label, _ in keys:
                pair = Text.assemble((key, "bold #f59e0b"), " ", (label, "#bae6fd"))
                if line.cell_len > label_width and line.cell_len + 3 + pair.cell_len > width:
                    lines.append(line)
                    line = Text(" " * label_width)
                elif line.cell_len > label_width:
                    line.append("   ")
                line.append_text(pair)
            lines.append(line)
        return lines

    def get_content_height(self, container, viewport, width):
        return len(self.lines(width))

    def render(self):
        return Text("\n").join(self.lines(self.content_size.width or 200))


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
        width: 90%;
        height: auto;
        max-height: 95%;
        padding: 1 2;
        border: round #38bdf8;
        background: #0b1b2e;
    }

    #help Static {
        width: 100%;
    }
    """

    BINDINGS = [Binding("escape,q,question_mark,enter", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield Static(help_text())

    def on_mount(self):
        self.query_one("#help").focus()

    def on_click(self):
        self.dismiss()


class ConfirmScreen(ModalScreen):
    """A yes / no question, or one that needs a word typed (delete, empty) for what cannot be undone."""

    CSS = """
    ConfirmScreen {
        align: center middle;
    }

    #confirm {
        width: 80%;
        max-width: 90;
        height: auto;
        padding: 1 2;
        border: round #f59e0b;
        background: #0b1b2e;
    }

    #confirm Input {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("y", "answer(True)", "Yes", show=False),
        Binding("n,escape", "answer(False)", "No", show=False),
    ]

    def __init__(self, message, word=None):
        super().__init__()
        self.message = message
        self.word = word

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(self.message)
            if self.word:
                yield Static(Text.assemble("Type ", (self.word, "bold #f59e0b"), " then enter, or esc to cancel."))
                yield Input(id="confirm-word")
            else:
                yield Static(Text.assemble(("y", "bold #f59e0b"), " yes   ", ("n esc", "bold #f59e0b"), " no"))
            yield KeyBar("confirm-word" if self.word else "confirm")

    def on_mount(self):
        if self.word:
            self.query_one(Input).focus()

    def check_action(self, action, parameters):
        if action == "answer" and parameters == (True,) and self.word:
            return False  # y would land in the input: the word is the answer
        return True

    @on(Input.Submitted)
    def submitted(self, event):
        if event.value.strip() == self.word:
            self.dismiss(True)
        else:
            self.notify(f"Type {self.word} to confirm, or esc to cancel.", severity="warning")

    def action_answer(self, answer):
        self.dismiss(answer)


def prune_empty_dirs(directory, stop):
    """Remove empty directories from directory up to stop, stop included."""
    while directory.is_dir() and not any(directory.iterdir()):
        directory.rmdir()
        if directory == stop:
            break
        directory = directory.parent


class DriveTree(Tree):
    # These replace Tree's own space / enter and go to the app, so the selection stays there.
    BINDINGS = [
        Binding("space", "app.toggle_current", "Select"),
        Binding("enter", "app.activate_current", "Expand", key_display="enter"),
        Binding("left", "app.collapse_or_parent", "Collapse", show=False),
        Binding("right", "app.expand_or_child", "Expand", show=False),
        Binding("e", "app.unfold_view", "Unfold all", show=False),
        Binding("shift+space", "app.expand_all", "Expand below", show=False),
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
        self.newest = folder_newest(nodes)
        self.name_column = name_column(nodes)
        self.sorted_by = "name"

    def sort(self, sort):
        if sort == self.sorted_by or self.nodes is None:
            return
        key = sort_key(sort, self.totals, self.newest)
        for children in self.children_of.values():
            children.sort(key=key)
        self.sorted_by = sort


def drive_views(service, folder_id):
    """My Drive, Shared with me and every shared drive, or only the folder given with --folder-id."""
    if folder_id != "root":
        return [DriveView(folder_name(service, folder_id), folder_id)]
    root_id = (
        service.files().get(fileId="root", fields="id").execute(num_retries=API_RETRIES)["id"]
    )
    views = [DriveView("My Drive", root_id), DriveView(SHARED_WITH_ME_DIR, SHARED_WITH_ME, Path(SHARED_WITH_ME_DIR))]
    shared_drives = []
    page_token = None
    while True:
        response = (
            service.drives()
            .list(pageSize=100, pageToken=page_token, fields="nextPageToken,drives(id,name)")
            .execute(num_retries=API_RETRIES)
        )
        for drive in response.get("drives", []):
            FOLDER_DRIVES[drive["id"]] = drive["id"]
            shared_drives.append(
                DriveView(drive["name"], drive["id"], Path(SHARED_DRIVES_DIR) / safe_name(drive["name"]))
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            return views + shared_drives + [DriveView(TRASH_DIR, TRASH, Path(TRASH_DIR))]


def view_roots(views):
    """Each view's local base and the Drive folder new local files go to (None: Shared with me, Trash)."""
    return {
        view.base: (None if view.root_id in {SHARED_WITH_ME, TRASH} else view.root_id)
        for view in views
    }


def load_view(service, view, cache, on_progress=lambda folders, items: None):
    nodes, browsed = collect_drive_tree(service, view.root_id, cache, on_progress, view.base)
    view.load(nodes, browsed)


# --- Drive trash: the only changes made on Drive, all from the tree after a confirmation -------------

def run_on_items(service, ids, make_request):
    """Run one Drive call per id in batches; returns the ids that failed, with their error."""
    failed = {}
    for start in range(0, len(ids), API_BATCH_SIZE):
        chunk = ids[start:start + API_BATCH_SIZE]

        def callback(request_id, response, exception, chunk=chunk):
            if exception is not None:
                failed[chunk[int(request_id)]] = error_text(exception)

        batch = service.new_batch_http_request(callback=callback)
        for index, item_id in enumerate(chunk):
            batch.add(make_request(item_id), request_id=str(index))
        batch.execute()
    return failed


def trash_items(service, ids):
    return run_on_items(
        service, ids,
        lambda item_id: service.files().update(fileId=item_id, body={"trashed": True}, supportsAllDrives=True),
    )


def restore_items(service, ids):
    return run_on_items(
        service, ids,
        lambda item_id: service.files().update(fileId=item_id, body={"trashed": False}, supportsAllDrives=True),
    )


def delete_items(service, ids):
    return run_on_items(
        service, ids, lambda item_id: service.files().delete(fileId=item_id, supportsAllDrives=True)
    )


def empty_drive_trash(service):
    service.files().emptyTrash().execute(num_retries=API_RETRIES)


class DriveSelectorApp(App):
    TITLE = APP_NAME
    ENABLE_COMMAND_PALETTE = False

    CSS = SHARED_CSS + """
    #notice {
        height: auto;
        margin: 1 2 0 2;
    }

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
        Binding("z", "sort('size')", "Sort by size", show=False),
        Binding("m", "sort('date')", "Sort by date", show=False),
        Binding("f", "change_destination", "Folder"),
        Binding("x", "trash", "Trash", show=False),
        Binding("r", "restore", "Restore", show=False),
        Binding("T", "empty_trash", "Empty trash", show=False, key_display="shift+t"),
        Binding("d", "confirm", "Preview"),
        Binding("s", "review_last", "Sync last", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("escape", "cancel", "Quit"),
        Binding("q", "quit_selector", "Quit", show=False),
    ]

    def __init__(self, views, destination=None, state=None, service=None, make_service=None, cache=None,
                 skip_preview=False, account=None, keep_both=False, view=None, sort="name", notice=None):
        super().__init__()
        self.keep_both = keep_both
        self.notice = notice
        self.last_sync = None  # the plan of the last selection, checked in the background at start
        self.last_sync_text = None
        self.last_sync_round = 0
        self.account = account
        self.views = views
        self.view = view if view in views else views[0]
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
        self.sort = sort

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
        notice = Static(id="notice")
        notice.display = False
        yield notice
        yield DriveTree(Text(self.view.name, style="bold bright_blue"), id="drive-tree")
        filter_input = Input(placeholder="filter by name or path", id="filter")
        filter_input.display = False
        yield filter_input
        yield Static(id="selection-summary")
        yield KeyBar("tree", id="keys")

    def on_mount(self):
        self.update_sub_title()
        tree = self.query_one("#drive-tree", Tree)
        tree.focus()
        self.show_view(self.view)
        self.check_last_sync()

    # The last selection, checked at start: what changed on either side since the last sync.

    def check_last_sync(self):
        self.last_sync = None
        state = self.state or {}
        if self.service is None or self.destination is None or not state.get("last_selection"):
            self.last_sync_text = None
            self.update_notice()
            return
        self.last_sync_text = Text("Checking your last selection against Drive…", style="dim")
        self.update_notice()
        self.last_sync_round += 1
        destination, round_ = self.destination, self.last_sync_round
        self.run_worker(lambda: self.last_sync_in_background(destination, round_), thread=True)

    def last_sync_in_background(self, destination, round_):
        try:
            service = self.make_service()
            state = load_state(destination)
            selections, scopes = last_selection(service, state, self.cache)
            roots = [(root["id"], Path(root["base"])) for root in state["last_selection"].get("roots", [])]
            upload_roots = {base: (None if root_id in {SHARED_WITH_ME, TRASH} else root_id)
                            for root_id, base in roots}
            plan = build_plan(service, selections, scopes, destination, state, self.cache, roots=upload_roots)
            if self.keep_both:
                set_keep_both(plan, plan, True)
            result = {"plan": plan, "selections": selections, "scopes": scopes, "destination": destination,
                      "state": state, "roots": roots, "round": round_}
        except Exception as error:  # a worker error would end the app: report it instead
            self.call_from_thread(self.last_sync_failed, round_, error_text(error))
            return
        self.call_from_thread(self.last_sync_ready, result)

    def last_sync_ready(self, result):
        if result["round"] != self.last_sync_round:
            return  # the folder or Drive changed meanwhile: a newer check is on its way
        self.last_sync = result
        self.last_sync_text = last_sync_summary(result["plan"])
        self.update_notice()

    def last_sync_failed(self, round_, message):
        if round_ == self.last_sync_round:
            self.last_sync_text = Text(f"Could not check your last selection: {message}", style="red")
            self.update_notice()

    def update_notice(self):
        if not self.is_mounted:
            return
        lines = [text for text in (self.notice, self.last_sync_text) if text]
        notice = self.query_one("#notice", Static)
        notice.display = bool(lines)
        notice.update(Text("\n").join(Text(line) if isinstance(line, str) else line for line in lines))

    def action_review_last(self):
        if self.busy:
            return
        if self.last_sync is None:
            self.notify("Your last selection is still being checked, or there is none yet.", severity="warning")
            return
        self.plan_ready(self.last_sync)

    # Views.

    def show_view(self, view):
        self.view = view
        if self.filter_terms or self.query_one("#filter", Input).display:
            self.clear_filter(rebuild=False)
        tree = self.query_one("#drive-tree", Tree)
        tree.focus()
        if view.nodes is None:
            tree.clear()
            tree.root.set_label(Text.assemble((view.name, "bold bright_blue"), ("  loading…", "dim")))
            if view.error is None:
                self.run_worker(lambda: self.load_in_background(view), thread=True, group="views")
            else:
                tree.root.set_label(Text.assemble((view.name, "bold bright_blue"), (f"  {view.error}", "red")))
        else:
            tree.root.set_label(Text(view.name, style="bold bright_blue"))
            view.sort(self.sort)
            self.rebuild()
            if not view.nodes:
                tree.root.set_label(Text.assemble((view.name, "bold bright_blue"), ("  empty", "dim")))
        self.update_views()
        self.update_selection_summary()
        self.query_one("#keys", KeyBar).set_context("trash" if view.root_id == TRASH else "tree")

    def load_in_background(self, view):
        try:
            load_view(self.make_service(), view, self.cache)
            view.marks = local_marks(view.nodes, self.destination, self.state)
        except Exception as error:  # a worker error would end the app: show it on the view instead
            view.nodes = None
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

    TREE_ACTIONS = {
        "next_view", "previous_view", "select_all", "clear_selection", "start_filter", "open_in_drive",
        "sort", "change_destination", "confirm", "cancel", "quit_selector", "trash", "restore",
        "empty_trash", "review_last",
    }

    def check_action(self, action, parameters):
        # While another screen is open (preview, help, folder, a confirmation), the tree's keys
        # must not act behind it (Tab, for one, belongs to the preview's filter tabs).
        if action in self.TREE_ACTIONS and len(self.screen_stack) > 1:
            return False
        if action in {"restore", "empty_trash"} and self.view.root_id != TRASH:
            return False
        return True

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
        self.sub_title = header_text(self.account, self.destination)

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
        if self.sort != "name":
            text.append("   sorted by ", style="dim")
            text.append(SORTS[self.sort], style="bold #f59e0b")
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
        self.query_one("#keys", KeyBar).set_context("filter")

    @on(Input.Blurred, "#filter")
    def filter_blurred(self):
        self.query_one("#keys", KeyBar).set_context("trash" if self.view.root_id == TRASH else "tree")

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
            self.check_last_sync()
            self.notify(f"Syncing with {destination} for this session.")

        self.push_screen(DestinationSetupScreen(initial), changed)

    def action_unfold_view(self):
        """e unfolds the whole view; when it is all unfolded, folds it back."""
        tree = self.query_one("#drive-tree", Tree)
        if self.view.nodes is None:
            return
        folders = [index for index in self.children_of if index is not None]
        if folders and all(
            index in self.tree_nodes and self.tree_nodes[index].is_expanded for index in folders
        ):
            for node in tree.root.children:
                node.collapse_all()
            return
        stack = [tree.root]
        while stack:
            current = stack.pop()
            self.expand(current)
            stack.extend(child for child in current.children if child.allow_expand)

    def drive_targets(self):
        """The current view's selection, or the item under the cursor."""
        selected = [entry for entry in self.selections if entry.get("view") is self.view]
        if selected:
            return [entry["item"] for entry in selected]
        entry = self.current_entry()
        return [] if entry is None else [entry["item"]]

    def describe(self, items):
        if len(items) == 1:
            return f"“{items[0]['name']}”"
        folders = sum(is_folder(item) for item in items)
        parts = []
        if folders:
            parts.append(plural(folders, "folder"))
        if len(items) - folders:
            parts.append(plural(len(items) - folders, "file"))
        return " and ".join(parts)

    def drive_change(self, question, word, work, done):
        """Ask, run work(service) in the background, then reload the tree from Drive."""
        def answered(yes):
            if not yes:
                return
            self.busy = True
            self.update_selection_summary("Updating Google Drive…")
            self.run_worker(lambda: self.change_in_background(work, done), thread=True)

        self.push_screen(ConfirmScreen(question, word), answered)

    def change_in_background(self, work, done):
        try:
            failed = work(self.make_service()) or {}
        except (HttpError, OSError) as error:
            failed = {"": error_text(error)}
        self.call_from_thread(self.drive_changed, done, failed)

    def drive_changed(self, done, failed):
        self.busy = False
        if failed:
            first = next(iter(failed.values()))
            self.notify(f"{plural(len(failed), 'item')} failed: {first}", severity="error")
        else:
            self.notify(done)
        # Drive changed: reload every view from scratch, starting with this one; the selection goes.
        self.cache.clear()
        TRASHED_FOLDERS.clear()  # a restored folder lists its children as usual again
        self.selections.clear()
        for view in self.views:
            view.nodes, view.error, view.expanded, view.sorted_by = None, None, set(), "name"
        self.show_view(self.view)
        self.check_last_sync()

    def action_trash(self):
        if self.busy:
            return
        items = self.drive_targets()
        if not items:
            return
        if self.view.root_id != TRASH:
            allowed = [item for item in items if can(item, "canTrash")]
            if not allowed:
                self.notify(
                    f"You cannot move {self.describe(items)} to the Drive trash: shared with you read only.",
                    severity="warning",
                )
                return
            skipped = len(items) - len(allowed)
            items = allowed
        ids = [item["id"] for item in items]
        what = self.describe(items)
        if self.view.root_id == TRASH:
            self.drive_change(
                f"Delete {what} forever from Google Drive? This cannot be undone.", "delete",
                lambda service: delete_items(service, ids), f"Deleted {what} forever.",
            )
        else:
            note = f" ({plural(skipped, 'item')} shared with you read only stay.)" if skipped else ""
            self.drive_change(
                f"Move {what} to the Google Drive trash? You can restore it from the Trash view.{note}", None,
                lambda service: trash_items(service, ids), f"Moved {what} to the Drive trash.",
            )

    def action_restore(self):
        if self.busy:
            return
        items = self.drive_targets()
        if not items:
            return
        ids = [item["id"] for item in items]
        what = self.describe(items)
        self.drive_change(
            f"Restore {what} on Google Drive, to where it was?", None,
            lambda service: restore_items(service, ids), f"Restored {what}.",
        )

    def action_empty_trash(self):
        if self.busy:
            return
        count = len(self.children_of.get(None, [])) if self.view.nodes else 0
        self.drive_change(
            f"Empty the Google Drive trash ({plural(count, 'item')} at its top)? Everything in it is deleted "
            "forever.", "empty",
            lambda service: empty_drive_trash(service), "Emptied the Drive trash.",
        )

    def action_sort(self, sort):
        """z / m sort by size / date; pressing the same key again goes back to names."""
        self.sort = "name" if self.sort == sort else sort
        if self.view.nodes is not None:
            self.view.sort(self.sort)
            self.rebuild()
        self.update_selection_summary()

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
            plan = build_plan(
                self.make_service(), selections, scopes, destination, state, self.cache, on_item, view_roots(self.views)
            )
            if self.keep_both:
                set_keep_both(plan, plan, True)
        except Exception as error:  # a worker error would end the app: report it instead
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

        self.push_screen(
            PreviewScreen(result["plan"], result["destination"], self.account, result["state"]), answered
        )

    def action_cancel(self):
        if self.query_one("#filter", Input).display:
            self.clear_filter()
            return
        self.exit(None)

    def action_quit_selector(self):
        self.exit(None)


ITEM_FIELDS = (
    "id,name,mimeType,size,modifiedTime,md5Checksum,shortcutDetails,webViewLink,trashed,driveId,"
    "capabilities(canEdit,canAddChildren,canTrash)"
)


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
        if root["id"] in {SHARED_WITH_ME, TRASH}:
            cache.pop(root["id"], None)  # a search, not a folder: Drive changes do not update it
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
        from_trash = Path(saved_item["path"]).parts[:1] == (TRASH_DIR,)
        if item is not None and item.get("trashed") and from_trash:
            if is_folder(item):
                TRASHED_FOLDERS.add(effective_id(item))  # picked in the Trash view: list it from there
        elif item is None or item.get("trashed"):
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
        state.setdefault("folders", {})  # synced folders, by local path: their Drive id
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


def is_readonly_path(relative_path):
    """Items here only come down: the Drive trash, and the top of Shared with me (no folder to send to)."""
    parts = relative_path.parts
    return bool(parts) and (parts[0] == TRASH_DIR or (parts[0] == SHARED_WITH_ME_DIR and len(parts) == 2))


def can(item, capability):
    """Does Drive let you do this to the item (canEdit, canAddChildren, canTrash)? Unknown: yes."""
    return item.get("capabilities", {}).get(capability, True)


def sends_back(item):
    """Can a local change of this Drive file go back up? Not for exports (Docs...), shortcuts, read-only files."""
    return (
        item["mimeType"] != SHORTCUT_MIME_TYPE
        and effective_mime_type(item) not in EXPORT_FORMATS
        and can(item, "canEdit")
    )


def tracked_record(state, item, relative_path):
    stored = state["files"].get(state_key(item, relative_path))
    if stored is None:
        stored = state["files"].get(effective_id(item))
    if stored and stored.get("path") == relative_path.as_posix():
        return stored
    return None


def classify_file(service, item, relative_path, destination, state):
    """Status of a Drive file against its local copy and the last sync, and the local SHA-256 if any."""
    record = tracked_record(state, item, relative_path)
    signature = remote_signature(item)
    readonly = is_readonly_path(relative_path) or item.get("readonly")

    if not destination.exists():
        if record and signature == record.get("remote_signature") and not readonly and can(item, "canTrash"):
            return "TRASH_REMOTE", None  # synced before, deleted here
        return "NEW", None
    if not destination.is_file():
        return "CONFLICT", None

    current_sha256 = local_hash(destination, record)
    if record:
        local_changed = current_sha256 != record.get("local_sha256")
        remote_changed = signature != record.get("remote_signature")
        if not local_changed:
            return ("UPDATE" if remote_changed else "UNCHANGED"), current_sha256
        if remote_changed or readonly or not sends_back(item):
            return "CONFLICT", current_sha256
        return "UPLOAD", current_sha256

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


def tracked_below(state, relative_path):
    """Records of the files synced below relative_path."""
    prefix = relative_path.as_posix() + "/"
    return [record for record in state["files"].values() if record.get("path", "").startswith(prefix)]


def remote_subtree_unchanged(service, folder_id, relative_path, state, cache, seen=frozenset()):
    """True when every Drive file below the folder was synced and has not changed since."""
    for child in list_children(service, folder_id, cache):
        child_path = relative_path / local_name(child)
        if is_folder(child):
            child_id = effective_id(child)
            if child_id in seen or not remote_subtree_unchanged(
                service, child_id, child_path, state, cache, seen | {child_id}
            ):
                return False
            continue
        if is_unsupported(child):
            continue
        record = tracked_record(state, child, child_path)
        if not record or record.get("remote_signature") != remote_signature(child):
            return False
    return True


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
        elif (
            status == "NEW"
            and item["mimeType"] != SHORTCUT_MIME_TYPE
            and not is_readonly_path(relative_path)
            and can(item, "canTrash")
            and (tracked_below(state, relative_path) or relative_path.as_posix() in state.get("folders", {}))
            and remote_subtree_unchanged(service, folder_id, relative_path, state, cache)
        ):
            status = "TRASH_REMOTE"  # synced before, the whole folder was deleted here
        plan.append(
            {
                "status": status,
                "kind": "FOLDER",
                "item": item,
                "relative_path": relative_path,
                "destination": destination,
            }
        )
        if status in {"CONFLICT", "SKIPPED", "TRASH_REMOTE"}:
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

    shortcut = item["mimeType"] == SHORTCUT_MIME_TYPE
    item = resolve_file_shortcut(service, item)
    if shortcut:
        item = dict(item, readonly=True)  # the target may live anywhere: only downloaded
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
            entry["duplicate"] = True  # two Drive files, one local name: rename one on Drive


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


def add_local_entries(plan, destination_root, state, scopes, remote_folders):
    """Local files and folders Drive does not have: new here (sent to Drive), or deleted on Drive (to your trash).

    remote_folders maps local folder paths to their Drive folder id (None: nothing can be sent there).
    """
    remote_files = {
        entry["relative_path"]
        for entry in plan
        if entry["kind"] == "FILE" and entry.get("item") is not None
    }
    remote_directories = {
        entry["relative_path"]
        for entry in plan
        if entry["kind"] == "FOLDER" and entry.get("item") is not None and entry["status"] != "TRASH_REMOTE"
    }
    trashed_remote = [
        entry["relative_path"] for entry in plan if entry["kind"] == "FOLDER" and entry["status"] == "TRASH_REMOTE"
    ]
    tracked = tracked_files_by_path(state)
    synced_folders = {Path(path) for path in state.get("folders", {})}
    tracked_directories = {
        parent for tracked_path in tracked for parent in tracked_path.parents
    } | synced_folders
    planned_local_paths = {
        entry["relative_path"]
        for entry in plan
        if entry.get("item") is None
    }
    new_folders = set()

    def can_send(relative_path):
        parent = relative_path.parent
        if is_readonly_path(relative_path):
            return False
        return parent in new_folders or remote_folders.get(parent) is not None

    def add_local_entry(relative_path, kind):
        if relative_path in planned_local_paths:
            return
        destination = destination_root / relative_path
        tracked_entry = tracked.get(relative_path) if kind == "FILE" else None
        tracked_key, current_sha256 = None, None

        if kind == "FOLDER" and relative_path in synced_folders:
            status = "TRASH_LOCAL"  # an empty folder synced before, deleted on Drive
        elif tracked_entry:
            tracked_key, record = tracked_entry
            current_sha256 = local_hash(destination, record)
            if current_sha256 == record.get("local_sha256"):
                status = "TRASH_LOCAL"  # synced before, deleted on Drive
            elif can_send(relative_path):
                status = "UPLOAD_NEW"  # deleted on Drive but changed here: the change wins
            else:
                status = "CONFLICT"
        elif can_send(relative_path):
            status = "UPLOAD_NEW"
            if kind == "FOLDER":
                new_folders.add(relative_path)
        else:
            status = "LOCAL_ONLY"

        plan.append(
            {
                "status": status,
                "kind": kind,
                "item": None,
                "relative_path": relative_path,
                "destination": destination,
                "state_key": tracked_key,
                "local_sha256": current_sha256,
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
                continue  # the Shared with me / Shared drives / Trash views have their own scopes
            if child.name.endswith(PART_SUFFIX) or child.is_symlink():
                continue
            if any(relative_path == trashed or trashed in relative_path.parents for trashed in trashed_remote):
                continue  # deleted here; Drive's copy goes to the Drive trash

            if child.is_dir():
                if relative_path in remote_directories:
                    scan_directory(relative_path)
                elif relative_path in tracked_directories:
                    scan_directory(relative_path)
                    if relative_path in synced_folders and not any(child.rglob("*")):
                        add_local_entry(relative_path, "FOLDER")  # synced, empty, gone from Drive
                else:
                    add_local_entry(relative_path, "FOLDER")
                    if relative_path in new_folders:
                        scan_directory(relative_path)  # its files go up too
            elif child.is_file() and relative_path not in remote_files:
                add_local_entry(relative_path, "FILE")

    for scope in scopes:
        destination = destination_root / scope
        if scope == Path() or destination.is_dir():
            scan_directory(scope)
        elif destination.is_file() and scope not in remote_files:
            add_local_entry(scope, "FILE")


def build_plan(service, selections, scopes, destination_root, state, cache, on_item=lambda: None, roots=None):
    """The sync plan of the selection; roots maps view base paths to their Drive folder id (None: read only)."""
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
    remote_folders = dict(roots if roots is not None else {Path(): "root"})
    for entry in plan:
        if entry["kind"] == "FOLDER" and entry.get("item") is not None and entry["status"] != "TRASH_REMOTE":
            # None: a folder you may not add to (shared read only): new files there stay local
            remote_folders[entry["relative_path"]] = (
                effective_id(entry["item"]) if can(entry["item"], "canAddChildren") else None
            )
    add_local_entries(plan, destination_root, state, scopes, remote_folders)
    for entry in plan:
        parent_id = remote_folders.get(entry["relative_path"].parent)
        if entry["status"] == "UPLOAD_NEW":
            entry["parent_id"] = parent_id
        elif entry["kind"] == "FILE" and entry.get("item") is not None:
            # Could a renamed copy of yours (keep both) go up next to it?
            entry["copy_goes_up"] = parent_id is not None and not is_readonly_path(entry["relative_path"])
    return plan


# --- Preview and download --------------------------------------------------

# status: (color, label in the preview, tab name). The arrows say which way it goes: ↓ to your disk, ↑ to Drive.
STATUS_STYLES = {
    "NEW": ("green", "↓ new", "↓ New"),
    "UPDATE": ("cyan", "↓ changed", "↓ Changed"),
    "UPLOAD_NEW": ("bright_green", "↑ new", "↑ New"),
    "UPLOAD": ("bright_cyan", "↑ changed", "↑ Changed"),
    "TRASH_REMOTE": ("magenta", "↑ to Drive trash", "Deleted here"),
    "TRASH_LOCAL": ("bright_magenta", "↓ to your trash", "Deleted on Drive"),
    "UNCHANGED": ("bright_black", "unchanged", "Unchanged"),
    "CONFLICT": ("yellow", "conflict", "Conflict"),
    "KEEP_BOTH": ("bright_yellow", "keep both", "Keep both"),
    "LOCAL_ONLY": ("blue", "local only", "Local only"),
    "SKIPPED": ("bright_black", "skipped", "Skipped"),
}
ACTIONS = {"NEW", "UPDATE", "UPLOAD_NEW", "UPLOAD", "TRASH_REMOTE", "TRASH_LOCAL", "KEEP_BOTH"}
DOWNLOADS = {"NEW", "UPDATE", "KEEP_BOTH"}
UPLOADS = {"UPLOAD_NEW", "UPLOAD"}
DELETIONS = {"TRASH_REMOTE", "TRASH_LOCAL"}
MASS_DELETION_FILES = 20  # deleting more than this, or more than a quarter of the synced files,
MASS_DELETION_SHARE = 0.25  # needs a typed confirmation: an unplugged disk or an emptied folder looks the same


def can_keep_both(entry):
    """A file changed on both sides (or an edited export) whose local copy can step aside."""
    return (
        entry["kind"] == "FILE"
        and entry.get("item") is not None
        and entry["status"] in {"CONFLICT", "KEEP_BOTH"}
        and not entry.get("duplicate")
        and entry["destination"].is_file()
    )


def local_copy_path(destination, taken):
    """`name (local).ext` next to destination, or `(local 2)`... when that one is taken."""
    stem, suffix = destination.stem, destination.suffix
    if destination.name.startswith(".") and destination.suffix == destination.name:
        stem, suffix = destination.name, ""
    counter = 1
    while True:
        label = "local" if counter == 1 else f"local {counter}"
        candidate = destination.with_name(f"{stem} ({label}){suffix}")
        if candidate not in taken and not candidate.exists():
            return candidate
        counter += 1


def set_keep_both(plan, entries, keep):
    """Keep both versions of conflicts (keep), or leave them as conflicts again.

    Your version is renamed 'name (local).ext' (a new local file, sent to Drive at the next sync) and the Drive
    version is downloaded in its place: both versions end up on both sides, and the conflict is gone.
    """
    taken = {entry["local_copy"] for entry in plan if entry["status"] == "KEEP_BOTH"}
    for entry in entries:
        if not can_keep_both(entry):
            continue
        if keep and entry["status"] == "CONFLICT":
            entry["local_copy"] = local_copy_path(entry["destination"], taken)
            taken.add(entry["local_copy"])
            entry["status"] = "KEEP_BOTH"
        elif not keep and entry["status"] == "KEEP_BOTH":
            taken.discard(entry.pop("local_copy"))
            entry["status"] = "CONFLICT"


def plan_summary(plan):
    counts = Counter(entry["status"] for entry in plan)
    parts = []
    for status, (style, label, _) in STATUS_STYLES.items():
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
            style, label, _ = STATUS_STYLES[entry["status"]]
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
    space, fits = space_line(plan, destination_root)
    if space:
        console.print(f"[dim]{space}[/]" if fits else f"[bold red]! {space}: not enough disk space[/]")
    for note in plan_notes(plan):
        console.print(f"[dim]{note}[/]")
    console.print(f"[dim]Destination:[/] {escape(str(destination_root))}")


def download_size(plan):
    """Bytes the plan downloads, and how many of its files have no size before export (Google Docs...)."""
    total, unknown = 0, 0
    for entry in plan:
        if entry["kind"] == "FILE" and entry["status"] in DOWNLOADS and entry.get("item") is not None:
            if entry["item"].get("size"):
                total += int(entry["item"]["size"])
            else:
                unknown += 1
    return total, unknown


def free_space(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def upload_size(plan, destination_root):
    return sum(
        (destination_root / entry["relative_path"]).stat().st_size
        for entry in plan
        if entry["kind"] == "FILE" and entry["status"] in UPLOADS
    )


def space_line(plan, destination_root):
    """("12 MB to download · 40 GB free · 3 MB to send", fits): fits is False when the download cannot fit."""
    total, unknown = download_size(plan)
    sent = upload_size(plan, destination_root)
    parts = []
    fits = True
    if total or unknown:
        text = f"{human_size(total)} to download"
        if unknown:
            text += f" + {plural(unknown, 'exported file')} of unknown size"
        free = free_space(destination_root)
        if free is not None:
            text += f" · {human_size(free)} free"
            fits = total <= free
        parts.append(text)
    if sent:
        parts.append(f"{human_size(sent)} to send to Drive")
    return (" · ".join(parts) or None), fits


def deletion_count(plan, state):
    """Files the plan deletes (a folder counts the synced files below it), and the synced files in play."""
    count = 0
    for entry in plan:
        if entry["status"] not in DELETIONS:
            continue
        if entry["kind"] == "FOLDER":
            count += max(1, len(tracked_below(state, entry["relative_path"])))
        else:
            count += 1
    synced = len(state["files"])
    return count, synced


def needs_deletion_check(plan, state):
    count, synced = deletion_count(plan, state)
    return count > MASS_DELETION_FILES or (count > 2 and synced and count > synced * MASS_DELETION_SHARE)


def plan_notes(plan):
    statuses = {entry["status"] for entry in plan}
    notes = []
    if "CONFLICT" in statuses:
        notes.append(
            "Conflicts are left as they are (b: keep both versions of this one, shift+b: of all). "
            "A file you cannot edit on Drive (a Google Doc export, a file shared read only) and changed here is a "
            "conflict too: it never goes back to Drive."
        )
    if "KEEP_BOTH" in statuses:
        notes.append(
            "Keep both: your version is renamed 'name (local).ext' and sent to Drive at the next sync; "
            "Drive's version takes its place. Delete the one you do not want, the sync follows."
        )
    if any(entry["status"] == "KEEP_BOTH" and not entry.get("copy_goes_up", True) for entry in plan):
        notes.append("Some kept versions are in folders read only on Drive: they stay on your disk only.")
    if any(entry.get("duplicate") for entry in plan):
        notes.append("Two Drive files with the same name in a folder: rename one on Drive (o opens it).")
    if "TRASH_REMOTE" in statuses:
        notes.append("Deleted here since the last sync: Drive's copies go to the Drive trash (kept 30 days).")
    if "TRASH_LOCAL" in statuses:
        notes.append("Deleted on Drive since the last sync: your copies go to your system trash.")
    if "LOCAL_ONLY" in statuses:
        notes.append(
            "Local only: Drive has nowhere to put them (top of Shared with me, Trash, or a folder shared with you "
            "read only); left as they are."
        )
    return notes


PREVIEW_VIEWS = [
    ("Changes", lambda entry: entry["status"] != "UNCHANGED"),
    ("Everything", lambda entry: True),
] + [
    (label, lambda entry, status=status: entry["status"] == status)
    for status, (_, _, label) in STATUS_STYLES.items()
]


class PreviewScreen(Screen):
    """The plan before anything changes: y syncs, esc goes back (or cancels)."""

    SUB_TITLE = "Preview"

    DEFAULT_CSS = """
    #preview-summary {
        height: auto;
        margin: 1 2 0 2;
    }

    #preview-filters {
        height: 1;
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

    KEY_CONTEXT = "preview"

    BINDINGS = [
        Binding("y", "confirm", "Sync"),
        Binding("tab", "next_filter", "Filter", priority=True),
        Binding("shift+tab", "previous_filter", "Previous filter", show=False, priority=True),
        Binding("b", "keep_both", "Keep both"),
        Binding("B", "keep_both_all", "Keep both: all", show=False, key_display="shift+b"),
        Binding("escape,n", "back", "Back"),
    ]

    def __init__(self, plan, destination, account=None, state=None):
        super().__init__()
        self.plan = plan
        self.destination = destination
        self.account = account
        self.state = state
        self.filters = [
            (name, keep) for name, keep in PREVIEW_VIEWS
            if any(keep(entry) for entry in plan)
        ] or PREVIEW_VIEWS[:1]
        self.filter_index = 0

    def compose(self) -> ComposeResult:
        yield Header(icon="")
        yield Static(id="preview-summary")
        yield Static(id="preview-filters")
        yield DataTable(id="preview-table", cursor_type="row", zebra_stripes=False)
        yield Static(id="preview-notes")
        yield KeyBar(self.KEY_CONTEXT)

    def on_mount(self):
        self.sub_title = header_text(self.account, self.destination, "preview")
        table = self.query_one(DataTable)
        table.add_column("STATUS", key="status")
        table.add_column("ITEM", key="item")
        table.add_column("SIZE", key="size")
        self.rows = []
        self.update_notes()
        self.fill()
        table.focus()

    def update_notes(self):
        notes = Text()
        for note in plan_notes(self.plan):
            notes.append(note + "\n")
        if not has_actions(self.plan):
            notes.append("Everything is up to date: y saves the state and quits.", style="bold green")
        notes.rstrip()
        self.query_one("#preview-notes", Static).update(notes)

    def fill(self):
        name, keep = self.filters[self.filter_index]
        rows = [entry for entry in self.plan if keep(entry)]
        summary = Text.from_markup(f"[bold]Preview[/]  {plan_summary(self.plan)}")
        space, fits = space_line(self.plan, self.destination)
        if space and fits:
            summary.append(f"\n{space}", style="dim")
        elif space:
            summary.append(f"\n! {space}: not enough disk space", style="bold red")
        self.query_one("#preview-summary", Static).update(summary)

        tabs = Text()
        for index, (tab_name, tab_keep) in enumerate(self.filters):
            label = f" {tab_name} ({sum(1 for entry in self.plan if tab_keep(entry))}) "
            if index == self.filter_index:
                tabs.append(label, style="bold #07111f on #f59e0b")
            else:
                tabs.append(label, style="#93c5fd")
            tabs.append(" ")
        if len(self.filters) > 1:
            tabs.append(" tab", style="bold #f59e0b")
            tabs.append(" next · ", style="dim")
            tabs.append("shift+tab", style="bold #f59e0b")
            tabs.append(" previous", style="dim")
        self.query_one("#preview-filters", Static).update(tabs)

        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        self.rows = rows
        for entry in rows:
            style, label, _ = STATUS_STYLES[entry["status"]]
            path = entry["relative_path"].as_posix()
            item = entry.get("item")
            size = display_size(item) if item is not None and entry["kind"] == "FILE" else ""
            if entry["status"] == "KEEP_BOTH":
                path += f"  (yours → {entry['local_copy'].name})"
            elif entry.get("duplicate"):
                path += "  (same name on Drive)"
            table.add_row(
                Text(label, style=style),
                Text(path + "/", style="cyan") if entry["kind"] == "FOLDER" else Text(path),
                Text(size if size != "-" else "", style="dim", justify="right"),
            )
        if rows:
            table.move_cursor(row=min(row, len(rows) - 1))

    def action_next_filter(self):
        self.filter_index = (self.filter_index + 1) % len(self.filters)
        self.fill()

    def action_previous_filter(self):
        self.filter_index = (self.filter_index - 1) % len(self.filters)
        self.fill()

    def refresh_plan(self):
        current = self.filters[self.filter_index][0]
        self.filters = [
            (name, keep) for name, keep in PREVIEW_VIEWS
            if any(keep(entry) for entry in self.plan) or name == current
        ]
        self.filter_index = next(i for i, (name, _) in enumerate(self.filters) if name == current)
        self.update_notes()
        self.fill()

    def action_keep_both(self):
        table = self.query_one(DataTable)
        if not self.rows:
            return
        entry = self.rows[table.cursor_row]
        if not can_keep_both(entry):
            self.notify("Keep both applies to a file changed on both sides.", severity="warning")
            return
        keep = entry["status"] == "CONFLICT"
        set_keep_both(self.plan, [entry], keep)
        if keep and not entry.get("copy_goes_up", True):
            self.notify(
                f"{entry['local_copy'].name} cannot go up: this folder is read only on Drive. "
                "It stays on your disk only.",
                severity="warning",
            )
        self.refresh_plan()

    def action_keep_both_all(self):
        conflicts = [entry for entry in self.plan if can_keep_both(entry)]
        if not conflicts:
            self.notify("No file conflicts from Drive.", severity="warning")
            return
        keep = any(entry["status"] == "CONFLICT" for entry in conflicts)
        set_keep_both(self.plan, conflicts, keep)
        self.refresh_plan()

    def action_confirm(self):
        if self.state is not None and needs_deletion_check(self.plan, self.state):
            count, _ = deletion_count(self.plan, self.state)

            def answered(yes):
                if yes:
                    self.dismiss(True)

            self.app.push_screen(
                ConfirmScreen(
                    f"This sync deletes {plural(count, 'file')} (to the Drive trash or your system trash). "
                    "That is a lot: check the Deleted here / Deleted on Drive tabs first.",
                    word="delete",
                ),
                answered,
            )
            return
        self.dismiss(True)

    def action_back(self):
        self.dismiss(False)


class StandalonePreviewScreen(PreviewScreen):
    KEY_CONTEXT = "preview-again"
    BINDINGS = [Binding("escape,n,q", "back", "Cancel")]


class PreviewApp(App):
    """The preview on its own, for --again."""

    TITLE = APP_NAME
    ENABLE_COMMAND_PALETTE = False
    CSS = SHARED_CSS

    def __init__(self, plan, destination, account=None, state=None):
        super().__init__()
        self.plan = plan
        self.destination = destination
        self.account = account
        self.state = state

    def on_mount(self):
        self.push_screen(
            StandalonePreviewScreen(self.plan, self.destination, self.account, self.state), self.exit
        )


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
        f".{destination.name}{PART_SUFFIX}"
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


def local_hash(path, record):
    """SHA-256 of a local file; reuses the last sync's when its size and date have not changed (like rsync)."""
    if record:
        stat = path.stat()
        if record.get("local_size") == stat.st_size and record.get("local_mtime_ns") == stat.st_mtime_ns:
            return record["local_sha256"]
    return file_hash(path)


def state_record(entry, local_sha256):
    item = entry["item"]
    try:
        stat = entry["destination"].stat()
        local = {"local_size": stat.st_size, "local_mtime_ns": stat.st_mtime_ns}
    except OSError:
        local = {}
    return local | {
        "remote_id": effective_id(item),
        "path": entry["relative_path"].as_posix(),
        "name": item["name"],
        "mime_type": effective_mime_type(item),
        "modified_time": item.get("modifiedTime"),
        "remote_signature": remote_signature(item),
        "local_sha256": local_sha256,
    }


UPLOAD_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum,webViewLink,capabilities(canEdit,canAddChildren,canTrash)"


def upload_file(service, path, file_id=None, name=None, parent_id=None, stop=None, on_bytes=lambda count: None):
    """Send a local file to Drive: a new version of file_id, or a new file in parent_id. Returns its metadata."""
    media = MediaFileUpload(
        str(path),
        mimetype=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        chunksize=8 * 1024 * 1024,
        resumable=True,
    )
    if file_id:
        request = service.files().update(
            fileId=file_id, media_body=media, fields=UPLOAD_FIELDS, supportsAllDrives=True
        )
    else:
        request = service.files().create(
            body={"name": name, "parents": [parent_id]}, media_body=media, fields=UPLOAD_FIELDS,
            supportsAllDrives=True,
        )
    sent = 0
    response = None
    while response is None:
        if stop is not None and stop.is_set():
            raise Stopped()
        status, response = request.next_chunk(num_retries=API_RETRIES)
        if status is not None:
            on_bytes(status.resumable_progress - sent)
            sent = status.resumable_progress
    on_bytes(max(0, path.stat().st_size - sent))
    return response


def create_remote_folder(service, name, parent_id):
    return (
        service.files()
        .create(
            body={"name": name, "parents": [parent_id], "mimeType": FOLDER_MIME_TYPE},
            fields="id,name,mimeType,webViewLink",
            supportsAllDrives=True,
        )
        .execute(num_retries=API_RETRIES)
    )


def trash_local(path):
    """Move a local file or folder to the system trash (the file manager's), never delete it."""
    send2trash(str(path))


def forget_below(state, relative_path):
    """Drop the records of relative_path and of everything below it, files and folders."""
    prefix = relative_path.as_posix()
    for key in [
        key for key, record in state["files"].items()
        if record.get("path") == prefix or record.get("path", "").startswith(prefix + "/")
    ]:
        del state["files"][key]
    folders = state.setdefault("folders", {})
    for path in [path for path in folders if path == prefix or path.startswith(prefix + "/")]:
        del folders[path]


def apply_plan(service, plan, destination_root, state, jobs=1, make_service=None):
    """Apply the confirmed plan. Transfers run in jobs threads, each with its own Drive client."""
    results = Counter()
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

    def record(entry, item, local_sha256):
        state["files"][state_key(item, entry["relative_path"])] = state_record(dict(entry, item=item), local_sha256)

    # 1. Folders: created here, then on Drive (parents first).
    folder_ids = {}
    for entry in plan:
        if entry["kind"] == "FOLDER" and entry.get("item") is not None and entry["status"] != "TRASH_REMOTE":
            folder_ids[entry["relative_path"]] = effective_id(entry["item"])
    for entry in plan:
        if entry["kind"] != "FOLDER":
            continue
        relative = entry["relative_path"].as_posix()
        status = entry["status"]
        if status == "NEW":
            try:
                entry["destination"].mkdir(parents=True, exist_ok=True)
                results["FOLDER_NEW"] += 1
                state.setdefault("folders", {})[relative] = effective_id(entry["item"])
            except OSError as error:
                results["FOLDER_ERROR"] += 1
                report("[red]✗[/]", f"{relative}/: {error}")
        elif status in {"UNCHANGED", "CONFLICT", "SKIPPED", "LOCAL_ONLY"}:
            results[f"FOLDER_{status}"] += 1
            if status == "UNCHANGED" and entry.get("item") is not None:
                state.setdefault("folders", {})[relative] = effective_id(entry["item"])
    for entry in sorted(
        (entry for entry in plan if entry["kind"] == "FOLDER" and entry["status"] == "UPLOAD_NEW"),
        key=lambda entry: len(entry["relative_path"].parts),
    ):
        relative = entry["relative_path"]
        parent_id = folder_ids.get(relative.parent, entry.get("parent_id"))
        try:
            if parent_id is None:
                raise OSError("its parent folder could not be created on Drive")
            folder = create_remote_folder(service, relative.name, parent_id)
            folder_ids[relative] = folder["id"]
            state.setdefault("folders", {})[relative.as_posix()] = folder["id"]
            results["FOLDER_UPLOAD_NEW"] += 1
            report("[bright_green]↑[/]", f"{relative.as_posix()}/")
        except (HttpError, OSError) as error:
            results["FOLDER_ERROR"] += 1
            report("[red]✗[/]", f"{relative.as_posix()}/: {error_text(error)}")

    # 2. Files that need no transfer.
    transfers = []
    for entry in plan:
        if entry["kind"] != "FILE":
            continue
        status = entry["status"]
        if status == "UNCHANGED":
            local_sha256 = entry.get("local_sha256") or file_hash(entry["destination"])
            try:
                set_remote_mtime(entry["destination"], entry["item"])  # same content: lets the tree mark it ✓
            except OSError:
                pass
            record(entry, entry["item"], local_sha256)  # after the date: the next run reuses this hash
            results["FILE_UNCHANGED"] += 1
        elif status in {"CONFLICT", "SKIPPED", "LOCAL_ONLY"}:
            results[f"FILE_{status}"] += 1
        elif status in DOWNLOADS or status in UPLOADS:
            transfers.append(entry)

    # 3. Downloads and uploads.
    stop = threading.Event()
    lock = threading.Lock()
    clients = threading.local()
    started = time.monotonic()
    transferred = 0
    task = progress.add_task("Syncing", total=len(transfers), transfer="")

    def on_bytes(count):
        nonlocal transferred
        with lock:
            transferred += count
            done = transferred
        speed = done / max(time.monotonic() - started, 0.001)
        progress.update(task, transfer=f"{human_size(done)} · {human_size(speed)}/s")

    def client():
        if make_service is None:
            return service
        if not hasattr(clients, "service"):
            clients.service = make_service()
        return clients.service

    def transfer(entry):
        if entry["status"] == "KEEP_BOTH":
            os.replace(entry["destination"], entry["local_copy"])  # your version steps aside
            try:
                download_file_atomically(client(), entry, stop, on_bytes)
            except BaseException:
                os.replace(entry["local_copy"], entry["destination"])
                raise
            return entry["item"], file_hash(entry["destination"])
        if entry["status"] in DOWNLOADS:
            download_file_atomically(client(), entry, stop, on_bytes)
            return entry["item"], file_hash(entry["destination"])
        local_sha256 = file_hash(entry["destination"])
        if entry["status"] == "UPLOAD":
            item = upload_file(client(), entry["destination"], file_id=effective_id(entry["item"]),
                               stop=stop, on_bytes=on_bytes)
        else:
            parent_id = folder_ids.get(entry["relative_path"].parent, entry.get("parent_id"))
            if parent_id is None:
                raise OSError("its folder could not be created on Drive")
            item = upload_file(client(), entry["destination"], name=entry["relative_path"].name,
                               parent_id=parent_id, stop=stop, on_bytes=on_bytes)
        try:
            set_remote_mtime(entry["destination"], item)  # same date on both sides: the tree marks it ✓
        except OSError:
            pass
        return item, local_sha256

    executor = ThreadPoolExecutor(max_workers=max(1, jobs))
    try:
        with progress:
            futures = {executor.submit(transfer, entry): entry for entry in transfers}
            for future in as_completed(futures):
                entry = futures[future]
                relative = entry["relative_path"].as_posix()
                status = entry["status"]
                try:
                    item, local_sha256 = future.result()
                except (HttpError, OSError) as error:
                    results["FILE_ERROR"] += 1
                    report("[red]✗[/]", f"{relative}: {error_text(error)}")
                else:
                    results[f"FILE_{status}"] += 1
                    if entry.get("state_key"):
                        state["files"].pop(entry["state_key"], None)
                    record(entry, item, local_sha256)
                    if status == "KEEP_BOTH":
                        report("[bright_yellow]⧉[/]", f"{relative} ↓ · yours kept as {entry['local_copy'].name}")
                    else:
                        mark = {"NEW": "[green]↓[/]", "UPDATE": "[cyan]↓[/]", "UPLOAD_NEW": "[bright_green]↑[/]",
                                "UPLOAD": "[bright_cyan]↑[/]"}[status]
                        report(mark, relative)
                progress.advance(task)

        # 4. Deleted here: Drive's copies go to the Drive trash.
        trashed = [entry for entry in plan if entry["status"] == "TRASH_REMOTE"]
        if trashed:
            failed = trash_items(service, [entry["item"]["id"] for entry in trashed])
            for entry in trashed:
                relative = entry["relative_path"].as_posix() + ("/" if entry["kind"] == "FOLDER" else "")
                error = failed.get(entry["item"]["id"])
                if error:
                    results[f"{entry['kind']}_ERROR"] += 1
                    report("[red]✗[/]", f"{relative}: {error}")
                else:
                    results[f"{entry['kind']}_TRASH_REMOTE"] += 1
                    forget_below(state, entry["relative_path"])
                    report("[magenta]✗[/]", f"{relative} → Drive trash")

        # 5. Deleted on Drive: your copies go to your system trash; folders left empty by it go too.
        emptied = set()
        for entry in plan:
            if entry["status"] != "TRASH_LOCAL":
                continue
            relative = entry["relative_path"]
            try:
                trash_local(entry["destination"])
            except OSError as error:
                results[f"{entry['kind']}_ERROR"] += 1
                report("[red]✗[/]", f"{relative.as_posix()}: {error}")
                continue
            results[f"{entry['kind']}_TRASH_LOCAL"] += 1
            forget_below(state, relative)
            emptied.add(relative.parent)
            report("[bright_magenta]✗[/]", f"{relative.as_posix()} → your trash")
        remote_folders = {path for path in folder_ids}
        for folder in sorted(emptied, key=lambda path: len(path.parts), reverse=True):
            while folder != Path() and folder not in remote_folders:
                directory = destination_root / folder
                if not directory.is_dir() or any(directory.iterdir()):
                    break
                directory.rmdir()
                folder = folder.parent
    except BaseException:
        stop.set()  # running transfers stop at their next chunk; downloads remove their partial file
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        save_state(destination_root, state)
    return results


# (counters summed, color, singular, plural)
RESULT_LABELS = [
    (("FILE_NEW",), "green", "downloaded", "downloaded"),
    (("FILE_UPDATE",), "cyan", "updated from Drive", "updated from Drive"),
    (("FILE_UPLOAD_NEW", "FILE_UPLOAD"), "bright_cyan", "sent to Drive", "sent to Drive"),
    (("FILE_UNCHANGED", "FOLDER_UNCHANGED"), "bright_black", "unchanged", "unchanged"),
    (("FOLDER_NEW",), "green", "folder created here", "folders created here"),
    (("FOLDER_UPLOAD_NEW",), "bright_cyan", "folder created on Drive", "folders created on Drive"),
    (("FILE_TRASH_REMOTE", "FOLDER_TRASH_REMOTE"), "magenta", "moved to the Drive trash", "moved to the Drive trash"),
    (("FILE_TRASH_LOCAL", "FOLDER_TRASH_LOCAL"), "bright_magenta", "moved to your trash", "moved to your trash"),
    (("FILE_KEEP_BOTH",), "bright_yellow", "conflict kept both ways", "conflicts kept both ways"),
    (("FILE_CONFLICT", "FOLDER_CONFLICT"), "yellow", "conflict kept", "conflicts kept"),
    (("FILE_LOCAL_ONLY", "FOLDER_LOCAL_ONLY"), "blue", "local-only item kept", "local-only items kept"),
    (("FILE_SKIPPED", "FOLDER_SKIPPED"), "bright_black", "skipped", "skipped"),
    (("FILE_ERROR", "FOLDER_ERROR"), "red", "error", "errors"),
]


def results_summary(results):
    parts = []
    for keys, style, singular, plural_label in RESULT_LABELS:
        count = sum(results[key] for key in keys)
        if count:
            parts.append(f"[{style}]{count} {singular if count == 1 else plural_label}[/]")
    return " · ".join(parts) or "[dim]nothing to do[/]"


def print_results(results):
    console.print(f"[bold]Done[/]  {results_summary(results)}")


# --- Main ------------------------------------------------------------------

def run_sync(args, service, make_service, plan, selections, scopes, roots, destination_root, state, cache, token):
    """Apply a confirmed plan, save the selection for --again and the Drive snapshot, print the result."""
    remember_selection(state, selections, scopes, roots)
    results = apply_plan(service, plan, destination_root, state, args.jobs, make_service)
    save_snapshot(
        destination_root, token, cache,
        [root_id for root_id, _ in roots]
        + [effective_id(entry["item"]) for entry in selections if is_folder(entry["item"])],
    )
    if not has_actions(plan):
        console.print("[green]✓[/] Everything is in sync.")
    else:
        print_results(results)
    return results


def confirm_in_terminal(args, plan, state):
    """The terminal preview's answer: always yes with --yes, unless it deletes too much."""
    if needs_deletion_check(plan, state):
        count, _ = deletion_count(plan, state)
        if args.yes and not args.allow_deletions:
            console.print(
                f"[red]✗[/] This sync deletes {plural(count, 'file')}: refused with --yes. Check it in the "
                "preview, or add --allow-deletions."
            )
            raise SystemExit(1)
        if not args.yes:
            console.print(f"[yellow]![/] This sync deletes {plural(count, 'file')}.")
            if console.input("  Type [bold]delete[/] to go on: ").strip() != "delete":
                console.print("[dim]Cancelled. Nothing was changed.[/]")
                return False
            return True
    if args.yes or not has_actions(plan):
        return True
    if console.input("\nProceed? [y/N] ").strip().lower() not in {"y", "yes"}:
        console.print("[dim]Cancelled. Nothing was changed.[/]")
        return False
    return True


def changes_drive(plan):
    return any(entry["status"] in UPLOADS | {"TRASH_REMOTE"} for entry in plan)


def last_sync_summary(plan):
    """One line for the start: how far the last selection is from being in sync."""
    # Files only for transfers (a new folder comes with its files); deletions count a whole folder once.
    counts = Counter(
        entry["status"] for entry in plan
        if entry["kind"] == "FILE" or entry["status"] in DELETIONS | {"CONFLICT"}
    )
    text = Text()
    if not has_actions(plan) and not counts["CONFLICT"]:
        text.append("✓ ", style="green")
        text.append("Your last selection is in sync.", style="dim")
        return text
    parts = [
        (counts["NEW"] + counts["UPDATE"], "↓ to download", "green"),
        (counts["UPLOAD_NEW"] + counts["UPLOAD"], "↑ to send", "bright_cyan"),
        (counts["TRASH_REMOTE"], "deleted here", "magenta"),
        (counts["TRASH_LOCAL"], "deleted on Drive", "bright_magenta"),
        (counts["CONFLICT"], "conflict" if counts["CONFLICT"] == 1 else "conflicts", "yellow"),
    ]
    text.append("Since the last sync: ", style="bold")
    text.append_text(Text(" · ").join(Text(f"{count} {label}", style=style) for count, label, style in parts if count))
    text.append("   ")
    text.append("s", style="bold #f59e0b")
    text.append(" to review and sync", style="dim")
    return text


def result_notice(plan, results):
    """The last sync's result, shown above the tree when it comes back."""
    text = Text()
    if has_actions(plan):
        text.append("Done  ", style="bold")
        text.append_text(Text.from_markup(results_summary(results)))
    else:
        text.append("✓ ", style="green")
        text.append("Everything was in sync.")
    return text


def resolve_destination(path_text):
    """The local folder: new (created), empty, or already managed by SaveGDrive."""
    destination = Path(path_text).expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    destination = destination.parent.resolve() / destination.name
    return initialize_managed_destination(destination)


def main():
    parser = argparse.ArgumentParser(
        prog="savegdrive",
        description=(
            "Sync Google Drive folders and files with a local folder, both ways. Pick them in a\n"
            "terminal tree, review the preview, then confirm. A file changed on both sides is a\n"
            "conflict, never overwritten, and nothing is deleted for good: what you delete on one\n"
            "side goes to the trash of the other (the Drive trash, or your system trash). The tree\n"
            "also moves items to the Drive trash; the Trash view restores, deletes or empties it."
        ),
        epilog="keys:\n" + "\n".join(help_lines()),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--download-path",
        metavar="PATH",
        default=DOWNLOAD_PATH,
        help="Local folder (default: %(default)s); new, empty or already managed by SaveGDrive.",
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
        help="Sync the previous selection again, without the tree (with --yes: no prompt at all, e.g. from cron).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply the preview without asking for confirmation, then exit after the sync.",
    )
    parser.add_argument(
        "--keep-both",
        action="store_true",
        help="Keep both versions of conflicts: yours renamed 'name (local).ext', Drive's in its place.",
    )
    parser.add_argument(
        "--allow-deletions",
        action="store_true",
        help=f"With --yes: also apply a sync that deletes more than {MASS_DELETION_FILES} files or a quarter "
        "of the synced ones (refused otherwise).",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        choices=range(1, 17),
        metavar="N",
        help="Files sent or downloaded at the same time, 1 to 16 (default: 4).",
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
        account = signed_in_account(service)

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
            who = f"{escape(account)} " if account else ""
            console.print(f"[bold]Again[/]  {names}{more} [dim]{who}→ {escape(str(destination_root))}[/]")
            roots = [(root["id"], Path(root["base"])) for root in state["last_selection"].get("roots", [])]
            upload_roots = {
                base: (None if root_id in {SHARED_WITH_ME, TRASH} else root_id) for root_id, base in roots
            }
            with console.status("[dim]Comparing with local files…[/]") as status:
                checked = 0

                def on_item():
                    nonlocal checked
                    checked += 1
                    if checked % 25 == 0:
                        status.update(f"[dim]Comparing with local files… {checked}[/]")

                plan = build_plan(
                    service, selections, local_scopes, destination_root, state, cache, on_item, upload_roots
                )
            if args.keep_both:
                set_keep_both(plan, plan, True)
            if args.yes or not interactive:
                print_plan(plan, destination_root)
                if not confirm_in_terminal(args, plan, state):
                    return
            elif not PreviewApp(plan, destination_root, account, state).run():
                console.print("[dim]Cancelled. Nothing was changed.[/]")
                return
            results = run_sync(args, service, make_service, plan, selections, local_scopes, roots,
                               destination_root, state, cache, token)
            if results["FILE_ERROR"] or results["FOLDER_ERROR"]:
                raise SystemExit(1)
            return

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

        # Like GMAIL, the tree comes back after each sync, with its result on top, until q / esc.
        view, sort, notice, failed = views[0], "name", None, False
        while True:
            app = DriveSelectorApp(
                views, destination_root, load_state(destination_root), service, make_service, cache,
                skip_preview=args.yes, account=account, keep_both=args.keep_both,
                view=view, sort=sort, notice=notice,
            )
            result = app.run()
            view, sort, destination_root = app.view, app.sort, app.destination
            if not result:
                break
            plan, selections, local_scopes = result["plan"], result["selections"], result["scopes"]
            destination_root, state = result["destination"], result["state"]
            roots = result.get("roots")  # from s: the last selection's own
            if roots is None:
                roots = [
                    (view_.root_id, view_.base) for view_ in views
                    if view_.nodes and fully_selected(view_.browsed.get(view_.base, []), view_.base, selections)
                ]
            if args.yes:
                print_plan(plan, destination_root)
                if not confirm_in_terminal(args, plan, state):
                    break
            results = run_sync(args, service, make_service, plan, selections, local_scopes, roots,
                               destination_root, state, cache, token)
            failed = failed or bool(results["FILE_ERROR"] or results["FOLDER_ERROR"])
            if args.yes:
                break
            notice = result_notice(plan, results)
            if changes_drive(plan):
                # Drive changed under the loaded tree: list it again before the next round.
                token = start_page_token(service)
                cache.clear()
                TRASHED_FOLDERS.clear()
                for view_ in views:
                    view_.nodes, view_.error, view_.sorted_by = None, None, "name"
                load_view(service, view, cache)
        if failed:
            raise SystemExit(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        raise SystemExit(130)
    except (HttpError, OSError) as error:
        console.print(f"[red]✗[/] {escape(error_text(error))}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

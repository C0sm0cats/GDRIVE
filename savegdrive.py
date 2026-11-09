import argparse
import contextlib
import fcntl
import hashlib
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import sys
import threading
import textwrap
import time
import unicodedata
import webbrowser
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import httplib2
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from rich.console import Console, Group
from send2trash import send2trash
from rich.markup import escape
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
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
FILTER_DELAY = 0.25  # seconds without typing before the filter rebuilds the tree
# GDrive Pull (before 2.0) kept the same state format under these names.
LEGACY_STATE_FILE_NAME = ".gdrivepull-managed-state.json"
LEGACY_REMOTE_CACHE_NAME = ".gdrivepull-remote-cache.json"
RESERVED_NAMES = {
    STATE_FILE_NAME,
    f"{STATE_FILE_NAME}.tmp",
    REMOTE_CACHE_NAME,
    f"{REMOTE_CACHE_NAME}.tmp",
    LEGACY_STATE_FILE_NAME,
    f"{LEGACY_STATE_FILE_NAME}.tmp",
    LEGACY_REMOTE_CACHE_NAME,
    f"{LEGACY_REMOTE_CACHE_NAME}.tmp",
}
RESERVED_PATHS = {Path(name) for name in RESERVED_NAMES}
# The system trash of a disk whose root is the destination (freedesktop, macOS, Windows): never sent to Drive.
DISK_TRASH_NAMES = {".Trash", ".Trashes", "$RECYCLE.BIN"}


def is_disk_trash(name):
    return name in DISK_TRASH_NAMES or name.startswith(".Trash-")
SHARED_WITH_ME = "sharedWithMe"  # pseudo folder id: the items others shared with you
SHARED_WITH_ME_DIR = "Shared with me"
SHARED_DRIVES_DIR = "Shared drives"
TRASH = "trash"  # pseudo folder id: the items you moved to the Drive trash
TRASH_DIR = "Trash"
VIEW_DIRS = {SHARED_WITH_ME_DIR, SHARED_DRIVES_DIR, TRASH_DIR}
# A Drive item at the top named like a read only view would share its local folder: what you change
# there would only come down. It is a conflict until renamed on Drive.
VIEW_NAME_PATHS = {Path(SHARED_WITH_ME_DIR), Path(TRASH_DIR)}
STATE_VERSION = 1
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
LIST_FIELDS = (
    "nextPageToken,"
    "files(id,name,mimeType,size,modifiedTime,md5Checksum,shortcutDetails,webViewLink,driveId,parents,"
    "explicitlyTrashed,capabilities(canEdit,canAddChildren,canTrash))"
)
API_BATCH_SIZE = 50  # Drive accepts 100 calls per batch, but throttles large ones
API_RETRIES = 5
# Drive nests folders 100 deep; only chains of folder shortcuts go deeper: past this they are skipped
MAX_FOLDER_DEPTH = 200

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

# What one Drive call can raise when it fails: an API error, a local or network error (OSError),
# httplib2 without network (ServerNotFoundError...), or a sign-in that cannot be renewed (RefreshError...).
DRIVE_CALL_ERRORS = (HttpError, OSError, httplib2.HttpLib2Error, GoogleAuthError)
VERBOSE = False
# The --folder-id of this run (None with --again, which replays the folder's own roots). A managed folder
# mirrors one Drive root: synced again from another one, all its files would look deleted on Drive.
DRIVE_ROOT = None


def printable(text):
    """text with its control characters (ESC, line breaks...) shown as \\xNN rather than sent to the terminal:
    Drive names are chosen by whoever shares a file. Rich and Textual only drop BEL, BS, VT, FF and CR.
    A byte of a local name that is not UTF-8 (a lone surrogate, which no terminal can encode) shows as \\xNN too."""
    if text.isprintable():
        return text
    return "".join(
        f"\\x{ord(char) - 0xDC00:02x}" if "\udc80" <= char <= "\udcff"
        else f"\\u{ord(char):04x}" if unicodedata.category(char) == "Cs"
        else f"\\x{ord(char):02x}" if char != "\t" and unicodedata.category(char) == "Cc"
        else char
        for char in text
    )


def is_utf8_name(name):
    """False for a local name whose bytes are not UTF-8 (Latin-1 from an old zip...): Drive cannot take it."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def debug(message):
    if VERBOSE:
        console.print(f"[dim]  {escape(printable(message))}[/]")


def warn(message):
    console.print(f"[yellow]![/] {escape(printable(message))}")


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


def sign_in_or_exit(reason, needed):
    """sign_in, or exit when no one is at a terminal to finish it (cron, --again --yes).

    Without a terminal, the browser sign-in would wait forever for a redirect that never comes.
    """
    if not sys.stdin.isatty():
        console.print(
            f"[red]✗[/] {needed}\n"
            "  [dim]Run savegdrive once in a terminal to sign in, then this run works again.[/]"
        )
        raise SystemExit(1)
    return sign_in(reason)


def save_token(creds, path=None):
    """Write the token readable by its owner only, and atomically: a failed write keeps the old one."""
    path = path or TOKEN_PATH
    temporary = path.with_name(f"{path.name}.tmp")
    text = creds.to_json()
    try:
        temporary.unlink(missing_ok=True)  # O_CREAT keeps the mode of a file already there
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def authenticate():
    creds = None
    if TOKEN_PATH.exists():
        try:
            # Without scopes given, the credentials keep the ones the token was granted.
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        except ValueError as error:
            # Empty or damaged (a write cut short): the session is lost, sign in again.
            debug(f"Could not read {TOKEN_PATH}: {error}")
            creds = sign_in_or_exit(
                f"Could not read the saved Google Drive session ({TOKEN_PATH.name}), sign in again in your browser.",
                f"Could not read the saved Google Drive session in {escape(str(TOKEN_PATH))}.",
            )
            save_token(creds)
            return creds
        if not creds.has_scopes(SCOPES):
            # A token from the read-only versions: Google must ask again for the wider access.
            debug("Saved token lacks the full Drive scope")
            creds = sign_in_or_exit(
                "SaveGDrive now syncs both ways: allow full Drive access in your browser.",
                "SaveGDrive now syncs both ways and needs full Drive access.",
            )
            save_token(creds)
            return creds

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                debug(f"Token refreshed, expires {local_expiry(creds)}")
            except TransportError as error:
                # Network down (laptop just out of sleep, DNS): the session itself is still good.
                debug(f"Could not refresh token: {error}")
                console.print(
                    "[red]✗[/] Could not reach Google to refresh the Drive session: "
                    f"{escape(str(error))}\n  [dim]Check the network connection and try again.[/]"
                )
                raise SystemExit(1) from error
            except Exception as error:
                debug(f"Could not refresh token: {error}")
                if "invalid_grant" in str(error):
                    creds = sign_in_or_exit(
                        "Google Drive session expired, sign in again in your browser.",
                        "Google Drive session expired.",
                    )
                else:
                    creds = sign_in_or_exit(
                        "Could not refresh the Google Drive session, sign in again in your browser.",
                        f"Could not refresh the Google Drive session: {escape(str(error))}",
                    )
        else:
            creds = sign_in_or_exit("Sign in to Google Drive in your browser.", "Not signed in to Google Drive.")
        save_token(creds)

    return creds


# --- Settings and managed destination --------------------------------------

def has_valid_state(destination, name=STATE_FILE_NAME):
    state_path = destination / name
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
            trashed = {effective_id(item) for item in items if is_folder(item)}
            for stale in trashed - TRASHED_FOLDERS:
                cache.pop(stale, None)  # listed before it was trashed: list its trashed content instead
            TRASHED_FOLDERS.update(trashed)
        for item in items:
            item.pop("explicitlyTrashed", None)
        cache[folder_id] = sort_items(items)


def get_items(service, ids, fields):
    """The metadata of every id in batched API calls: id -> item, or the HttpError Drive gave for it."""
    pending = list(dict.fromkeys(ids))
    found = {}
    attempts = Counter()

    while pending:
        chunk, pending = pending[:API_BATCH_SIZE], pending[API_BATCH_SIZE:]
        retry = []

        def callback(request_id, response, exception, chunk=chunk):
            item_id = chunk[int(request_id)]
            if exception is None:
                found[item_id] = response
            elif is_retryable(exception) and attempts[item_id] < API_RETRIES:
                attempts[item_id] += 1
                retry.append(item_id)
            else:
                found[item_id] = exception

        batch = service.new_batch_http_request(callback=callback)
        for index, item_id in enumerate(chunk):
            batch.add(
                service.files().get(fileId=item_id, fields=fields, supportsAllDrives=True), request_id=str(index)
            )
        batch.execute()
        debug(f"Got {plural(len(chunk), 'item')}")
        if retry:
            delay = min(2 ** max(attempts[item_id] for item_id in retry), 30)
            debug(f"Drive rate limit, retrying {len(retry)} call(s) in {delay}s")
            time.sleep(delay)
            pending = retry + pending
    return found


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
            file_id = change.get("fileId")
            if not file_id:  # a shared drive's own change (changeType "drive"): no file to update
                continue
            for folder_id in where.pop(file_id, ()):
                cache[folder_id] = [item for item in cache[folder_id] if item["id"] != file_id]
                changed.add(folder_id)
            item = change.get("file")
            if change.get("removed") or not item or item.get("trashed"):
                continue
            parents = item.get("parents") or []  # kept: --again tells a moved item by its parent
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


def refresh_listings(service, cache, token):
    """Bring cache up to date with the Drive changes since token instead of listing every folder again.

    Shared with me and the Trash are searches, and trashed folders list trashed items: the changes feed does
    not update them, so their listings go and are listed again when needed. Returns the new token; when token
    is no longer valid, the whole cache goes.
    """
    searches = {SHARED_WITH_ME, TRASH} | TRASHED_FOLDERS
    folders = {folder_id: items for folder_id, items in cache.items() if folder_id not in searches}
    replayed = apply_changes(service, {"token": token, "folders": folders})
    TRASHED_FOLDERS.clear()  # a restored folder lists its children as usual again
    cache.clear()
    if replayed is None:
        return start_page_token(service)
    cache.update(replayed[0])
    return replayed[1]


SHORTCUT_TARGET_FIELDS = (
    "id,mimeType,size,modifiedTime,md5Checksum,webViewLink,capabilities(canEdit,canAddChildren,canTrash)"
)


def is_file_shortcut(item):
    return item["mimeType"] == SHORTCUT_MIME_TYPE and not is_folder(item)


def resolve_file_shortcut(service, item, targets=None):
    """The target of a file shortcut, named like it; targets: the ones get_items already fetched."""
    if not is_file_shortcut(item):
        return item

    target = (targets or {}).get(effective_id(item))
    if target is None:
        target = (
            service.files()
            .get(fileId=effective_id(item), fields=SHORTCUT_TARGET_FIELDS, supportsAllDrives=True)
            .execute(num_retries=API_RETRIES)
        )
    elif isinstance(target, Exception):
        raise target
    return dict(target, name=item["name"])


def drive_folder(service, folder_id):
    """The name of the --folder-id folder, and whether you may add files to it."""
    if folder_id == "root":
        return {"name": "My Drive"}
    return (
        service.files()
        .get(fileId=folder_id, fields="name,capabilities(canAddChildren)", supportsAllDrives=True)
        .execute(num_retries=API_RETRIES)
    )


def collect_drive_tree(service, root_folder_id, cache, on_progress=lambda folders, items: None, base=Path()):
    """Load the whole tree below root_folder_id, one batched listing per level.

    Returns the nodes (parents always before their children) and the root items by local path.
    """
    nodes = []
    browsed_directories = {}
    level = [(root_folder_id, None, base, [], frozenset({root_folder_id}))]
    visited = {root_folder_id}  # a folder reached twice (two shortcuts to it) is listed and opened once

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
                if (
                    is_folder(item)
                    and child_folder_id not in ancestors
                    and child_folder_id not in visited
                    and len((relative_parent / local_name(item)).parts) < MAX_FOLDER_DEPTH  # a shortcut chain
                ):
                    visited.add(child_folder_id)
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
    # The folder comes before the account: a narrow header cuts the account, not
    # where the sync writes and deletes.
    parts = [prefix] if prefix else []
    if destination is not None:
        parts.append(f"syncs with {short_path(destination)}")
    if account:
        parts.append(account)
    return " · ".join(parts)


def short_path(path):
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)


def plural(count, word):
    return f"{count} {word}{'' if count == 1 else 's'}"


SELECTION_BOXES = {"x": "☑ ", "*": "◩ "}  # selection state -> checkbox; anything else is "☐ "


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


def selection_index(selections):
    """The selected paths (with their Drive ids) and the selected folders' paths, for indexed_selection_state."""
    index = ({}, set())
    for entry in selections:
        index_selection(index, entry)
    return index


def indexed_selection_state(path, index):
    """path_selection_state against a selection_index: O(depth) instead of O(selection)."""
    paths, folders = index
    if path in paths:
        return "x"
    if not folders.isdisjoint(path.parents):
        return "*"
    return " "


def index_selection(index, entry):
    path = selected_path(entry)
    index[0].setdefault(path, set()).add(entry["item"].get("id"))
    if is_folder(entry["item"]):
        index[1].add(path)


def already_selected(path, item_id, index):
    """Is this Drive item in the selection, itself or through a folder? Another item with the same
    local path is not: both are kept, so the plan sees two Drive items on one local path (a conflict)."""
    state = indexed_selection_state(path, index)
    return state == "*" or (state == "x" and item_id in index[0][path])


def add_selected_items(selections, candidates):
    """Add candidates ({"item", "relative_parent", …}) in order, like add_selected_item on each,
    but indexing the selection once and sorting once. Returns the candidates that were kept."""
    index = selection_index(selections)
    added, new_folders = [], set()
    for candidate in candidates:
        path = selected_path(candidate)
        if already_selected(path, candidate["item"].get("id"), index):
            continue
        index_selection(index, candidate)
        if is_folder(candidate["item"]):
            new_folders.add(path)
        added.append(candidate)
    if not added:
        return []
    selections.extend(added)
    if new_folders:
        # What an added folder covers goes, including candidates added before it.
        selections[:] = [
            entry for entry in selections
            if new_folders.isdisjoint(selected_path(entry).parents)
        ]
    selections.sort(
        key=lambda entry: (
            not is_folder(entry["item"]),
            selected_path(entry).as_posix().casefold(),
        )
    )
    kept = {id(entry) for entry in selections}
    return [candidate for candidate in added if id(candidate) in kept]


def add_selected_item(selections, item, relative_parent):
    return bool(add_selected_items(selections, [{"item": item, "relative_parent": relative_parent}]))


def scope_covers(parent, scope):
    if parent == Path():
        # My Drive's root does not cover the Shared with me / Shared drives folders.
        return not scope.parts or scope.parts[0] not in VIEW_DIRS
    return parent == scope or parent in scope.parents


def compact_scopes(scopes):
    compacted, kept = [], set()
    for scope in sorted(set(scopes), key=lambda path: (len(path.parts), path.as_posix())):
        # Same as any(scope_covers(parent, scope) for parent in compacted), in O(depth).
        if Path() in kept and scope_covers(Path(), scope):
            continue
        if any(parent in kept for parent in scope.parents if parent != Path()):
            continue
        compacted.append(scope)
        kept.add(scope)
    return compacted


def fully_selected(root_items, base, selections):
    if not root_items:
        return False
    index = selection_index(selections)  # once: path_selection_state per item rescanned the selection
    return all(
        indexed_selection_state(base / local_name(item), index) in {"x", "*"}
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


def migrate_legacy_state(destination):
    """Adopt a GDrive Pull folder: rename its state, drop its cache (rebuilt on the next listing)."""
    if has_valid_state(destination) or not has_valid_state(destination, LEGACY_STATE_FILE_NAME):
        return
    os.replace(destination / LEGACY_STATE_FILE_NAME, destination / STATE_FILE_NAME)
    for name in (
        f"{LEGACY_STATE_FILE_NAME}.tmp",
        LEGACY_REMOTE_CACHE_NAME,
        f"{LEGACY_REMOTE_CACHE_NAME}.tmp",
    ):
        try:
            (destination / name).unlink()
        except FileNotFoundError:
            pass


def drive_root_name(drive_root):
    return "My Drive" if drive_root == "root" else f"the Drive folder {drive_root}"


def claim_drive_root(destination, drive_root):
    """Record the Drive root a managed folder mirrors, and refuse any other one."""
    if drive_root is None:
        return
    state = json.loads((destination / STATE_FILE_NAME).read_text(encoding="utf-8"))
    recorded = state.get("drive_root")
    if recorded is None:
        if state["files"] or state.get("folders"):
            # Synced before the root was recorded: My Drive (the default), unless this folder was synced whole.
            roots = (state.get("last_selection") or {}).get("roots", [])
            whole = any(root.get("id") == drive_root and root.get("base") == "." for root in roots)
            recorded = drive_root if whole else "root"
        else:
            recorded = drive_root
        if recorded == drive_root:
            state["drive_root"] = drive_root
            save_state(destination, state)
    if recorded != drive_root:
        raise ValueError(
            f"This folder syncs {drive_root_name(recorded)}, not {drive_root_name(drive_root)}: "
            "each one needs its own folder."
        )


FOLDER_LOCK = {}  # the managed folder this run syncs, locked until it exits: {"path": Path, "fd": int}


def lock_folder(destination):
    """Lock the managed folder (flock on the folder itself), so two runs never sync it at once.

    Returns the new lock's descriptor, or None when this run already holds that folder."""
    if FOLDER_LOCK.get("path") == destination:
        return None
    fd = os.open(destination, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise ValueError("Another SaveGDrive run is using this folder: wait for it to finish.") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def holds_state(directory):
    """A SaveGDrive folder (or a GDrive Pull one): its own state file is there."""
    return any((directory / name).is_file() for name in (STATE_FILE_NAME, LEGACY_STATE_FILE_NAME))


def initialize_managed_destination(destination):
    outer = next((parent for parent in destination.parents if holds_state(parent)), None)
    if outer is not None:
        raise ValueError(
            f"This folder is inside {outer}, already synced by SaveGDrive: its next sync would send "
            "this folder to Drive. Choose a folder outside it."
        )
    if destination.exists():
        if not destination.is_dir():
            raise ValueError("The final destination is not a directory.")
    else:
        # Only the folder itself is created: a missing parent may be an unmounted disk.
        if not destination.parent.is_dir():
            raise ValueError(
                f"The parent folder {destination.parent} does not exist: create it first."
            )
        destination.mkdir(parents=False)
    fd = lock_folder(destination)
    try:
        migrate_legacy_state(destination)
        if has_valid_state(destination):
            claim_drive_root(destination, DRIVE_ROOT)
        elif any(destination.iterdir()):
            raise ValueError(
                "This destination already contains files but has no "
                f"{STATE_FILE_NAME}. Choose a different managed folder that "
                "is empty or does not exist yet."
            )
        else:
            state = {"version": STATE_VERSION, "files": {}}
            if DRIVE_ROOT is not None:
                state["drive_root"] = DRIVE_ROOT
            save_state(destination, state)
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    if fd is not None:
        # Keep one lock: the previous folder's (before f) is released.
        previous = FOLDER_LOCK.get("fd")
        FOLDER_LOCK.update(path=destination, fd=fd)
        if previous is not None:
            os.close(previous)
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
        height: 1;
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
        min-height: 2;
        margin-top: 1;
        padding: 0 1;
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
            # On a short terminal the refusal and the button sit below the form's fold.
            self.call_after_refresh(
                self.query_one("#setup-actions").scroll_visible, animate=False
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
    "edited": ("✎", "yellow", "changed or new here since the last sync: sent to Drive at the next one"),
    "deleted": ("✗", "magenta", "deleted here since the last sync: goes to the Drive trash at the next one"),
    "conflict": ("!", "red", "changed here, but it cannot go back to Drive (export, read only) or changed on both "
                 "sides: a conflict, never sent"),
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


NAME_COLUMN_MIN = 24  # cells from the tree's left edge to the end of the names, however narrow the tree


def label_start(node, children_of, base=Path()):
    """Cells before a node's label: tree guides, plus the expand arrow of folders with children.
    The view's base (Shared with me, Shared drives/<name>, Trash) is not tree depth."""
    depth = len(node["relative_parent"].relative_to(base).parts) + 1
    return depth * 4 + (2 if node["index"] in children_of else 0)


def name_column(nodes, base=Path()):
    children_of = {node["parent"] for node in nodes}
    widest = 0
    for node in nodes:
        prefix = 4 + (2 if node["item"]["mimeType"] == SHORTCUT_MIME_TYPE else 0)  # checkbox, mark
        name = Text(printable(node["item"]["name"]))
        widest = max(widest, label_start(node, children_of, base) + prefix + name.cell_len)
    return widest


def label_columns(entry, totals):
    """Size, date and format (or file count) shown after a node's name."""
    item = entry["item"]
    if is_folder(item):
        files, size = totals.get(entry["index"], (0, 0))
        return human_size(size) if size else "", "", plural(files, "file")
    mime_type = effective_mime_type(item)
    size_text = display_size(item) if item.get("size") else ""
    if mime_type in EXPORT_FORMATS:
        info = f"→ {EXPORT_FORMATS[mime_type][1]}"
    elif is_unsupported(item):
        info = "not downloadable"
    else:
        info = ""
    return size_text, display_date(item), info


def label_tail(entry, totals):
    size_text, date_text, info = label_columns(entry, totals)
    return f"  {size_text:>7}  {date_text:<10}  {info}".rstrip()


def local_marks(nodes, destination, state):
    """Mark files already downloaded (from the managed state, without hashing) and sum them up per folder.

    A mark tells what the next sync does: down (changed), up (edited, deleted), or nothing but a conflict.
    """
    if destination is None or state is None:
        return {}
    tracked = tracked_files_by_path(state)
    synced_folders = {Path(path) for path in state.get("folders", {})}
    tracked_directories = {parent for tracked_path in tracked for parent in tracked_path.parents} | synced_folders
    download_only = set()  # below a folder shortcut: its content only comes down
    drive_names = {}  # folder index -> local names of its Drive children
    for node in nodes:
        item = node["item"]
        parent = node["parent"]
        if parent in download_only or (is_folder(item) and item["mimeType"] == SHORTCUT_MIME_TYPE):
            download_only.add(node["index"])
        if parent is not None:
            drive_names.setdefault(parent, set()).add(local_name(item))
    marks = {}
    counts = {}  # folder index -> Counter of its files' marks ("" = not downloaded yet)
    for node in reversed(nodes):
        item = node["item"]
        index = node["index"]
        relative_path = selected_path(node)
        if is_folder(item):
            folder = counts.get(index, Counter())
            if relative_path in tracked_directories:
                folder.update(local_only_marks(
                    destination / relative_path, relative_path, drive_names.get(index, set()), tracked,
                    tracked_directories,
                    can_receive=index not in download_only and not item.get("readonly")
                    and can(item, "canAddChildren"),
                ))
            if folder["conflict"]:
                marks[index] = "conflict"
            elif folder["deleted"] and not folder["synced"] and not (destination / relative_path).is_dir():
                marks[index] = "deleted"
            elif folder["edited"] or folder["deleted"]:
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
                record = tracked.get(relative_path, (None, {}))[1]
                if record.get("remote_id") == effective_id(item):
                    try:
                        path_stat = (destination / relative_path).stat()
                    except OSError:
                        path_stat = None
                    readonly = index in download_only or item.get("readonly") or is_readonly_path(relative_path)
                    remote_changed = remote_signature(item) != record.get("remote_signature")
                    if path_stat is None:
                        if (
                            not remote_changed
                            and not readonly
                            and can(item, "canTrash")
                            and effective_mime_type(item) not in EXPORT_FORMATS
                        ):
                            mark = "deleted"  # synced, then deleted here: goes to the Drive trash
                        # else it comes down again: no mark, like a file not downloaded yet
                    else:
                        local_changed = not mtime_matches(path_stat, record.get("modified_time"))
                        # The top of Shared with me has no folder for new files, but a file you may edit goes back up
                        goes_up = (
                            index not in download_only
                            and not item.get("readonly")
                            and relative_path.parts[:1] != (TRASH_DIR,)
                            and sends_back(item)
                        )
                        if remote_changed:
                            mark = "conflict" if local_changed else "changed"
                        elif local_changed:
                            mark = "edited" if goes_up else "conflict"
                        else:
                            mark = "synced"
                if mark:
                    marks[index] = mark
                own[mark] += 1
        if node["parent"] is not None:
            counts.setdefault(node["parent"], Counter()).update(own)
    return marks


def local_only_marks(directory, relative_directory, drive_names, tracked, tracked_directories, can_receive):
    """Marks of what is in a synced local folder but not in its Drive folder: new here (goes up if the folder
    takes new items), or synced before and deleted on Drive (goes to your trash). One listing, no hashing."""
    found = Counter()
    try:
        children = list(os.scandir(directory))
    except OSError:
        return found
    folded = {unicodedata.normalize("NFC", name).casefold() for name in drive_names}
    for child in children:
        name = child.name
        if name in drive_names or name.endswith(PART_SUFFIX) or child.is_symlink():
            continue
        relative_path = relative_directory / name
        if relative_path in tracked or relative_path in tracked_directories:
            found["changed"] += 1  # deleted on Drive: goes to your trash, or a conflict if changed here
            continue
        if unicodedata.normalize("NFC", name).casefold() in folded:
            continue  # the Drive item under another spelling (case-insensitive disk), or a name clash
        try:
            if child.is_dir() and holds_state(Path(child.path)):
                continue  # another SaveGDrive folder, synced on its own
        except OSError:
            continue
        if can_receive and not is_readonly_path(relative_path):
            found["edited"] += 1  # new here: goes up at the next sync
    return found


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
            yield Static(self.message, markup=False)  # names may hold [brackets]
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

    def __init__(self, name, root_id, base=Path(), can_add=True):
        self.name = name
        self.root_id = root_id
        self.base = base
        self.can_add = can_add  # False: a shared drive or folder you only read, new files at its top stay here
        self.nodes = None  # loaded on first display
        self.browsed = {}
        self.children_of = {}
        self.totals = {}
        self.marks = {}
        self.name_column = 0  # widest label start + name: what the names need
        self.tail_width = 0  # widest size, date and format after the names
        self.expanded = set()  # local paths of the open folders: indexes change when the view is listed again
        self.error = None

    def load(self, nodes, browsed):
        self.nodes = nodes
        self.browsed = browsed
        self.children_of = {}
        for node in nodes:
            self.children_of.setdefault(node["parent"], []).append(node)
        self.totals = folder_totals(nodes)
        self.newest = folder_newest(nodes)
        self.name_column = name_column(nodes, self.base)
        self.tail_width = max((Text(label_tail(node, self.totals)).cell_len for node in nodes), default=0)
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
        folder = drive_folder(service, folder_id)
        return [DriveView(folder["name"], folder_id, can_add=can(folder, "canAddChildren"))]
    root_id = (
        service.files().get(fileId="root", fields="id").execute(num_retries=API_RETRIES)["id"]
    )
    views = [DriveView("My Drive", root_id), DriveView(SHARED_WITH_ME_DIR, SHARED_WITH_ME, Path(SHARED_WITH_ME_DIR))]
    shared_drives = []
    page_token = None
    while True:
        response = (
            service.drives()
            .list(pageSize=100, pageToken=page_token,
                  fields="nextPageToken,drives(id,name,capabilities(canAddChildren))")
            .execute(num_retries=API_RETRIES)
        )
        for drive in response.get("drives", []):
            FOLDER_DRIVES[drive["id"]] = drive["id"]
            shared_drives.append(drive)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return views + shared_drive_views(shared_drives) + [DriveView(TRASH_DIR, TRASH, Path(TRASH_DIR))]


def shared_drive_views(drives):
    """One view per shared drive, each with its own local folder. Drives whose names match
    (letter case aside) all get their id after the name: none keeps the plain name, so a drive
    someone else names like yours never receives what you put in your drive's folder."""
    names = Counter(safe_name(drive["name"]).casefold() for drive in drives)
    taken, views = set(), []
    for drive in drives:
        name = safe_name(drive["name"])
        if names[name.casefold()] > 1:
            name = safe_name(f"{name} ({drive['id']})")
        unique, number = name, 2
        while unique.casefold() in taken:
            unique, number = f"{name} ({number})", number + 1
        taken.add(unique.casefold())
        shown = drive["name"] if unique == safe_name(drive["name"]) else unique  # tells the tabs apart
        views.append(
            DriveView(shown, drive["id"], Path(SHARED_DRIVES_DIR) / unique, can(drive, "canAddChildren"))
        )
    return views


def upload_roots(roots):
    """Each view's local base and the Drive folder new local files go to, from (root id, base[, can add])
    (None: Shared with me, Trash, read only, or a base two roots share: which one would get the files?)."""
    parents = {}
    for root_id, base, *can_add in roots:
        parent = None if root_id in {SHARED_WITH_ME, TRASH} or can_add == [False] else root_id
        parents[base] = parent if parents.get(base, parent) == parent else None
    return parents


def view_roots(views):
    """Each view's local base and the Drive folder new local files go to (None: Shared with me, Trash, read only)."""
    return upload_roots((view.root_id, view.base, view.can_add) for view in views)


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
                 skip_preview=False, account=None, keep_both=False, view=None, sort="name", notice=None,
                 token=None):
        super().__init__()
        self.drive_token = token  # Drive changes token of self.cache: replayed before each plan
        self.cache_lock = threading.Lock()
        self.keep_both = keep_both
        self.notice = notice
        self.last_sync = None  # the plan of the last selection, checked in the background at start
        self.last_sync_text = None
        self.last_sync_round = 0
        self.last_sync_error = False  # the last check failed: s checks again
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
        self.tree_frame = None
        self.label_column = None  # the name column of the labels shown, to redo them when the width changes
        self.label_index = None  # selection_index of self.selections for node_label; None when stale
        self.tree_nodes = {}
        self.node_paths = {}  # index -> selected_path of its entry, for the tree nodes
        self.populated = set()
        self.filter_terms = []
        self.filter_ancestors = set()
        self.filter_visible = None
        self.filter_timer = None  # pending apply_filter while typing
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
        return self.fit_name_column(self.size.width)

    def fit_name_column(self, screen_width):
        """Names get what they need, up to the tree's width less the columns after them."""
        if self.tree_frame is None:  # margins, border, padding and scrollbar around the labels
            styles = self.query_one("#drive-tree", Tree).styles
            self.tree_frame = styles.margin.width + styles.gutter.width + styles.scrollbar_size_vertical
        width = screen_width - self.tree_frame
        return min(self.view.name_column, max(width - self.view.tail_width, NAME_COLUMN_MIN))

    def on_resize(self, event):  # runs before App's own handler: self.size is still the old one
        if self.view.nodes is None or self.label_column == self.fit_name_column(event.size.width):
            return
        self.label_column = self.fit_name_column(event.size.width)
        for tree_node in self.tree_nodes.values():
            tree_node.set_label(self.node_label(tree_node.data, self.label_column))

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
        self.last_sync_error = False
        state = self.state or {}
        if self.service is None or self.destination is None or not has_last_selection(state):
            self.last_sync_text = None
            self.update_notice()
            return
        self.last_sync_text = Text("Checking your last selection against Drive…", style="dim")
        self.update_notice()
        self.last_sync_round += 1
        destination, round_ = self.destination, self.last_sync_round
        self.run_worker(lambda: self.last_sync_in_background(destination, round_), thread=True)

    def refresh_cache(self, service):
        """Replay the Drive changes made since the tree was loaded: a plan never compares to stale listings."""
        if self.drive_token is None:
            return
        with self.cache_lock:
            # Shared with me and the Trash are searches, and trashed folders list trashed items: no replay.
            folders = {folder_id: items for folder_id, items in self.cache.items()
                       if folder_id not in {SHARED_WITH_ME, TRASH} and folder_id not in TRASHED_FOLDERS}
            replayed = apply_changes(service, {"token": self.drive_token, "folders": folders})
            if replayed is None:  # token too old: these folders are listed again by the plan
                self.drive_token = start_page_token(service)
                for folder_id in folders:
                    self.cache.pop(folder_id, None)
                return
            self.cache.update(replayed[0])
            self.drive_token = replayed[1]

    def fresh_selections(self, selections):
        """The selected items with their metadata from the refreshed listings, when they are listed there."""
        fresh = {}
        for folder_id, items in list(self.cache.items()):
            if folder_id not in {SHARED_WITH_ME, TRASH}:
                fresh.update((item["id"], item) for item in items)
        return [dict(entry, item=fresh.get(entry["item"]["id"], entry["item"])) for entry in selections]

    def last_sync_in_background(self, destination, round_):
        try:
            service = self.make_service()
            self.refresh_cache(service)
            state = load_state(destination)
            kept = []
            selections, scopes = last_selection(service, state, self.cache, kept)
            roots = saved_roots(state)
            plan = build_plan(service, selections, scopes, destination, state, self.cache, roots=upload_roots(roots))
            if self.keep_both:
                set_keep_both(plan, plan, True)
            result = {"plan": plan, "selections": selections, "scopes": scopes, "destination": destination,
                      "state": state, "roots": roots, "kept": kept, "round": round_}
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
            self.last_sync_error = True
            self.last_sync_text = Text(f"Could not check your last selection: {message} (press s to try again)",
                                       style="red")
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
        if self.last_sync is None and self.last_sync_error:
            self.notify("Checking your last selection again…")
            self.check_last_sync()
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
            self.node_paths[entry["index"]] = selected_path(entry)

    def expand(self, tree_node):
        self.populate(tree_node)
        tree_node.expand()

    @on(Tree.NodeExpanded, "#drive-tree")
    def node_expanded(self, event):
        self.populate(event.node)
        if not self.filter_terms and event.node.data is not None:
            self.expanded.add(selected_path(event.node.data))

    @on(Tree.NodeCollapsed, "#drive-tree")
    def node_collapsed(self, event):
        if not self.filter_terms and event.node.data is not None:
            self.expanded.discard(selected_path(event.node.data))

    def rebuild(self):
        tree = self.query_one("#drive-tree", Tree)
        current = self.current_entry()
        tree.clear()
        self.label_index = None
        self.tree_nodes = {}
        self.node_paths = {}
        self.populated = set()
        self.populate(tree.root)
        tree.root.expand()
        for entry in self.nodes:  # parents come first, so each one is already in the tree
            index = entry["index"]
            if self.filter_terms:
                wanted = index in self.filter_ancestors
            else:
                wanted = index in self.tree_nodes and self.node_paths[index] in self.expanded
            if wanted and index in self.tree_nodes:
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

    def node_label(self, entry, column=None):
        item = entry["item"]
        if self.label_index is None:
            self.label_index = selection_index(self.selections)
        state = indexed_selection_state(selected_path(entry), self.label_index)
        label = Text()
        label.append(
            SELECTION_BOXES.get(state, "☐ "),
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
        if column is None:
            column = self.label_column = self.name_column
        width = column - label_start(entry, self.children_of, self.view.base) - label.cell_len
        name = Text(printable(item["name"]), style=name_style)
        name.truncate(max(width, 4), overflow="ellipsis", pad=True)
        label.append_text(name)

        size_text, date_text, info = label_columns(entry, self.totals)
        label.append(f"  {size_text:>7}  {date_text:<10}  ", style="dim")
        label.append(info, style="dim")
        label.rstrip()
        return label

    def refresh_node_labels(self, selection_only=False):
        """Relabel the populated nodes. selection_only: only the selection changed, so relabel just
        the nodes whose checkbox changed (each set_label repaints, seconds on a large unfolded tree)."""
        self.label_index = None  # the selection changed
        if not selection_only:
            for tree_node in self.tree_nodes.values():
                tree_node.set_label(self.node_label(tree_node.data))
        else:
            self.label_index = paths, folders = selection_index(self.selections)
            # indexed_selection_state, top down: a node is covered when its tree parent is a selected
            # folder or covered itself (a child's relative_parent is its parent's path). Parents come
            # first in tree_nodes, as they are populated before their children.
            covers = {}  # index -> are its children covered by a selected folder
            for index, tree_node in self.tree_nodes.items():
                path = self.node_paths[index]
                parent = tree_node.data["parent"]
                covered = covers[parent] if parent in covers else not folders.isdisjoint(path.parents)
                covers[index] = covered or path in folders
                state = "x" if path in paths else "*" if covered else " "
                if tree_node.label.plain[:1] != SELECTION_BOXES.get(state, "☐ ")[0]:
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
        if any(selected_path(selected) == target_path for selected in self.selections):
            # Every Drive item on this local path goes, twins with the same name included.
            self.selections[:] = [
                selected for selected in self.selections if selected_path(selected) != target_path
            ]
        elif path_selection_state(target_path, self.selections) == "*":
            self.notify(
                "This item is included by a selected parent folder.",
                severity="warning",
            )
            return
        else:
            self.add_entry(entry)
        self.refresh_node_labels(selection_only=True)

    def selection_candidate(self, entry):
        # Keep the view and totals, for the tab counts and the summary.
        if is_folder(entry["item"]):
            totals = tuple(self.totals.get(entry["index"], (0, 0)))
        else:
            totals = (1, int(entry["item"].get("size") or 0))
        return {
            "item": entry["item"],
            "relative_parent": entry["relative_parent"],
            "parent_id": self.sendable_parent_id(entry),
            "view": self.view,
            "totals": totals,
        }

    def sendable_parent_id(self, entry):
        """The Drive folder the entry sits in, if new files may go there (None: read only, behind a shortcut):
        --again sends it back there when it is deleted on Drive but changed here."""
        if entry["parent"] is None:
            readonly = self.view.root_id in {SHARED_WITH_ME, TRASH} or not self.view.can_add
            return None if readonly else self.view.root_id
        parent = self.nodes[entry["parent"]]
        folder = parent["item"]
        if not can(folder, "canAddChildren") or folder.get("readonly"):
            return None
        while parent is not None:
            if parent["item"]["mimeType"] == SHORTCUT_MIME_TYPE:
                return None  # below a shortcut, only downloads
            parent = None if parent["parent"] is None else self.nodes[parent["parent"]]
        return effective_id(folder)

    def add_entry(self, entry):
        # Items with the same name in the same folder share one local path: take them all, so the
        # plan shows them as a conflict instead of syncing that path with whichever was checked.
        target_path = selected_path(entry)
        twins = [
            other for other in self.children_of.get(entry["parent"], [])
            if other is not entry and selected_path(other) == target_path
        ]
        add_selected_items(self.selections, [self.selection_candidate(other) for other in [entry, *twins]])

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
        index = selection_index(self.selections)
        if all(indexed_selection_state(selected_path(entry), index) != " " for entry in targets):
            # Everything is already selected: unselect it.
            paths = {selected_path(entry) for entry in targets}
            self.selections[:] = [
                entry for entry in self.selections
                if selected_path(entry) not in paths
                and paths.isdisjoint(selected_path(entry).parents)
            ]
        else:
            add_selected_items(self.selections, [self.selection_candidate(entry) for entry in targets])
        self.refresh_node_labels(selection_only=True)

    def action_clear_selection(self):
        self.selections.clear()
        self.refresh_node_labels(selection_only=True)

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
        # A rebuild walks the whole tree: wait until typing stops instead of redoing it per key.
        self.cancel_pending_filter()
        self.filter_timer = self.set_timer(FILTER_DELAY, self.apply_pending_filter)

    def cancel_pending_filter(self):
        if self.filter_timer is not None:
            self.filter_timer.stop()
            self.filter_timer = None
            return True
        return False

    def apply_pending_filter(self):
        self.filter_timer = None
        self.apply_filter(self.query_one("#filter", Input).value)

    @on(Input.Submitted, "#filter")
    def filter_submitted(self, event):
        if self.cancel_pending_filter():
            self.apply_filter(event.value)
        if not event.value.strip():
            event.input.display = False
        self.query_one("#drive-tree", Tree).focus()

    def clear_filter(self, rebuild=True):
        filter_input = self.query_one("#filter", Input)
        filter_input.display = False
        self.query_one("#drive-tree", Tree).focus()
        self.cancel_pending_filter()
        with filter_input.prevent(Input.Changed):
            filter_input.value = ""
        if not rebuild:
            self.filter_terms = []
            self.filter_visible = None
            self.filter_ancestors = set()
        elif self.filter_terms:
            self.apply_filter("")

    def action_open_in_drive(self):
        entry = self.current_entry()
        if entry is None:
            return
        if open_quietly(drive_url(entry["item"])):
            self.notify(f"Opened {printable(entry['item']['name'])} in your browser.", markup=False)
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
            self.notify(f"Syncing with {destination} for this session.", markup=False)

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
            return f"“{printable(items[0]['name'])}”"
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
        service = self.make_service()
        try:
            failed = work(service) or {}
        except (HttpError, OSError) as error:
            failed = {"": error_text(error)}
        replayed = False
        if self.drive_token is not None:
            # Replay what changed on Drive rather than listing the whole tree again.
            try:
                with self.cache_lock:
                    self.drive_token = refresh_listings(service, self.cache, self.drive_token)
                replayed = True
            except DRIVE_CALL_ERRORS as error:
                debug(f"Could not replay the Drive changes, listing again: {error_text(error)}")
        self.call_from_thread(self.drive_changed, done, failed, replayed)

    def drive_changed(self, done, failed, replayed=False):
        self.busy = False
        if failed:
            first = next(iter(failed.values()))
            self.notify(f"{plural(len(failed), 'item')} failed: {first}", severity="error", markup=False)
        else:
            self.notify(done, markup=False)
        # Drive changed: rebuild every view from the updated listings, starting with this one; the selection goes.
        if not replayed:
            self.cache.clear()
            TRASHED_FOLDERS.clear()  # a restored folder lists its children as usual again
        self.selections.clear()
        self.label_index = None
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
                    f"You cannot move {self.describe(items)} to the Drive trash: Drive does not let you "
                    "(read only, owned by someone else, or a shared drive role that cannot delete).",
                    severity="warning",
                    markup=False,
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
            note = f" ({plural(skipped, 'item')} you cannot trash on Drive stay.)" if skipped else ""
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
        if self.view.nodes is None:  # no count yet: never ask on a made-up "0 items"
            if self.view.error is not None:
                self.notify("The Trash could not be listed, so it is not emptied.", severity="warning")
            else:
                self.notify("The Trash view is still loading.", severity="warning")
            return
        count = len(self.children_of.get(None, []))
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
            service = self.make_service()
            self.refresh_cache(service)
            selections = self.fresh_selections(selections)
            state = load_state(destination)
            plan = build_plan(
                service, selections, scopes, destination, state, self.cache, on_item, view_roots(self.views)
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
    "id,name,mimeType,size,modifiedTime,md5Checksum,shortcutDetails,webViewLink,trashed,driveId,parents,"
    "capabilities(canEdit,canAddChildren,canTrash)"
)


def moved_on_drive(saved_item, item):
    """Whether item now sits in another Drive folder than when it was picked (known only when both are)."""
    before, now = saved_item.get("parents"), item.get("parents")
    return bool(before) and bool(now) and set(before) != set(now)


def remember_selection(state, selections, scopes, roots=(), kept=()):
    """Store the selection for --again; roots are views selected entirely, so new items there come too.

    kept are saved items now in the Drive trash, stored as they were: restored there, they come back.
    """
    state["last_selection"] = {
        "roots": [
            dict({"id": root_id, "base": base.as_posix()}, **({"can_add": False} if can_add == [False] else {}))
            for root_id, base, *can_add in roots
        ],
        "items": [
            {
                "id": entry["item"]["id"],
                "name": entry["item"]["name"],
                "relative_parent": entry["relative_parent"].as_posix(),
                "path": selected_path(entry).as_posix(),
                "parents": entry["item"].get("parents") or [],
                "parent_id": entry.get("parent_id"),
            }
            for entry in selections
        ] + [dict(saved_item) for saved_item in kept],
        "scopes": [scope.as_posix() for scope in scopes],
    }


def saved_roots(state):
    """The views --again and s sync entirely, as (root id, base, can add): a root saved before can_add can add."""
    return [
        (root["id"], Path(root["base"]), root.get("can_add", True))
        for root in state["last_selection"].get("roots", [])
    ]


def has_last_selection(state):
    """Whether --again has something to sync: once every saved item left Drive, the selection is empty."""
    saved = state.get("last_selection")
    return bool(saved and (saved.get("items") or saved.get("roots")))


def last_selection(service, state, cache, kept=None):
    """The previous run's selection with fresh Drive metadata; items gone from Drive stay as local scopes.

    Saved items now in the Drive trash are added to kept, when given, to stay in the saved selection.
    """
    if not has_last_selection(state):
        return None
    saved = state["last_selection"]
    selections = []
    scopes = [Path(scope) for scope in saved.get("scopes", [])]
    from_roots = []
    for root in saved.get("roots", []):
        if root["id"] in {SHARED_WITH_ME, TRASH}:
            cache.pop(root["id"], None)  # a search, not a folder: Drive changes do not update it
        from_roots.extend(
            {"item": item, "relative_parent": Path(root["base"])}
            for item in list_children(service, root["id"], cache)
        )
    add_selected_items(selections, from_roots)
    # Selected so far, kept up to date as items are picked, then added at once.
    index = selection_index(selections)
    fetched = get_items(
        service,
        [
            saved_item["id"] for saved_item in saved["items"]
            if not already_selected(Path(saved_item["path"]), saved_item["id"], index)
        ],
        ITEM_FIELDS,
    )
    picked = []
    for saved_item in saved["items"]:
        relative_parent = Path(saved_item["relative_parent"])
        if already_selected(Path(saved_item["path"]), saved_item["id"], index):
            continue  # already there through a whole view
        item = fetched[saved_item["id"]]
        if isinstance(item, Exception):
            if not isinstance(item, HttpError) or item.status_code != 404:
                raise item
            item = None
        from_trash = Path(saved_item["path"]).parts[:1] == (TRASH_DIR,)
        if item is not None and item.get("trashed") and from_trash:
            if is_folder(item):
                TRASHED_FOLDERS.add(effective_id(item))  # picked in the Trash view: list it from there
        elif item is None or item.get("trashed") or from_trash:
            # Picked in the Trash view and restored on Drive: gone from that view, its new place comes if selected.
            if item is not None and item.get("trashed") and kept is not None:
                kept.append(saved_item)  # in the Drive trash: it may be restored there
            warn(f"{saved_item['path']} is no longer " + ("in the Drive trash" if item is not None else "on Drive"))
            scopes.append(Path(saved_item["path"]))
            continue
        elif moved_on_drive(saved_item, item):
            # Like a deletion and a new item: the old copy goes, the new place comes if it is selected.
            warn(f"{saved_item['path']} was moved to another folder on Drive")
            scopes.append(Path(saved_item["path"]))
            continue
        note_drives([item])
        candidate = {"item": item, "relative_parent": relative_parent, "parent_id": saved_item.get("parent_id")}
        if not already_selected(selected_path(candidate), item.get("id"), index):
            index_selection(index, candidate)
            picked.append(candidate)
    add_selected_items(selections, picked)
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


def record_at_path(state, relative_path):
    """The last sync's record of whichever Drive file was at relative_path: (key, record), or (None, None)."""
    path = relative_path.as_posix()
    for key, stored in state["files"].items():
        if stored.get("path") == path:
            return key, stored
    return None, None


def classify_file(service, item, relative_path, destination, state):
    """Status of a Drive file against its local copy and the last sync, the local SHA-256 if any,
    and the state key of the record it replaces (another Drive file synced before at this path), if any."""
    record = tracked_record(state, item, relative_path)
    signature = remote_signature(item)
    readonly = is_readonly_path(relative_path) or item.get("readonly")

    if not destination.exists():
        if (
            record
            and signature == record.get("remote_signature")
            and not readonly
            and can(item, "canTrash")
            and effective_mime_type(item) not in EXPORT_FORMATS  # exports only come down: deleted here, it comes back
        ):
            return "TRASH_REMOTE", None, None  # synced before, deleted here
        return "NEW", None, None
    if not destination.is_file():
        return "CONFLICT", None, None

    replaced_key = replaced = None
    if record is None:
        # Another Drive file synced here before (deleted and sent again, or renamed over it)?
        replaced_key, replaced = record_at_path(state, relative_path)
    try:
        current_sha256 = local_hash(destination, record or replaced)
    except OSError:
        return "CONFLICT", None, None  # cannot read it here: left as it is on both sides (see the unreadable mark)
    if record:
        local_changed = current_sha256 != record.get("local_sha256")
        remote_changed = signature != record.get("remote_signature")
        if not local_changed:
            return ("UPDATE" if remote_changed else "UNCHANGED"), current_sha256, None
        if remote_changed and item.get("md5Checksum") and file_hash(destination, "md5") == item["md5Checksum"]:
            return "UNCHANGED", current_sha256, None  # same content both sides (a transfer whose state was not saved)
        # The top of Shared with me has no folder for new files, but a file you may edit goes back up.
        download_only = item.get("readonly") or relative_path.parts[:1] == (TRASH_DIR,)
        if remote_changed or download_only or not sends_back(item):
            return "CONFLICT", current_sha256, None
        return "UPLOAD", current_sha256, None

    remote_md5 = item.get("md5Checksum")
    if remote_md5 and file_hash(destination, "md5") == remote_md5:
        return "UNCHANGED", current_sha256, replaced_key

    if replaced and current_sha256 == replaced.get("local_sha256"):
        return "UPDATE", current_sha256, replaced_key  # only Drive changed: the new file comes down

    if effective_mime_type(item) in EXPORT_FORMATS:
        try:
            if remote_export_hash(service, item) == current_sha256:
                return "UNCHANGED", current_sha256, replaced_key
        except HttpError as error:
            warn(f"Could not compare {relative_path.as_posix()}: {error_text(error)}")
    return "CONFLICT", current_sha256, None


def tracked_below(state, relative_path):
    """Records of the files synced below relative_path."""
    prefix = relative_path.as_posix() + "/"
    return [record for record in state["files"].values() if record.get("path", "").startswith(prefix)]


def synced_here_before(state, relative_path, folder_id, tracked_folders):
    """This Drive folder was synced at this path: gone here, it was deleted here."""
    known_id = state.get("folders", {}).get(relative_path.as_posix())
    if known_id is not None:
        return known_id == folder_id  # another Drive folder by that name is new, not deleted here
    return relative_path.as_posix() in tracked_folders  # a state without folder ids: its files tell


def tracked_folder_paths(state):
    """Folders (posix paths) holding synced files: one pass, so a plan tests them without rereading the state."""
    folders = set()
    for record in state["files"].values():
        path = record.get("path", "")
        while "/" in path:
            path = path.rpartition("/")[0]
            if path in folders:
                break  # its parents are in too
            folders.add(path)
    return folders


def remote_subtree_unchanged(service, folder_id, relative_path, state, cache, seen=None):
    """True when every Drive file below the folder was synced and has not changed since."""
    if seen is None:
        seen = {folder_id}  # every folder checked once: crossed shortcuts would make it exponential
    for child in list_children(service, folder_id, cache):
        child_path = relative_path / local_name(child)
        if not can(child, "canTrash"):
            return False  # not yours to trash (a colleague's file...): deleted here, it comes back
        if is_folder(child):
            child_id = effective_id(child)
            if child_id in seen:
                return False  # a loop, or a folder also reached elsewhere: not safe to trash
            if len(child_path.parts) >= MAX_FOLDER_DEPTH:
                return False  # a shortcut chain too deep to check
            seen.add(child_id)
            if not remote_subtree_unchanged(service, child_id, child_path, state, cache, seen):
                return False
            continue
        if is_unsupported(child):
            return False  # never came down (a Google Form...): deleting the folder here did not delete it
        if effective_mime_type(child) in EXPORT_FORMATS:
            return False  # exports only come down: deleting the folder here does not trash the Google Doc
        record = tracked_record(state, child, child_path)
        if not record or record.get("remote_signature") != remote_signature(child):
            return False
    return True


def symlink_on_path(destination_root, relative_path):
    """A symbolic link at relative_path, or at one of its folders below destination_root: never followed."""
    path = destination_root
    for part in relative_path.parts:
        path = path / part
        if path.is_symlink():
            return True
    return False


def has_symlink(destination_root, relative_path, ancestor_folder_ids):
    """Symbolic links are never followed. Below the selection the walk stops at a link, so only the item itself
    needs a look; a selected item (no ancestors) checks every folder above it too."""
    if ancestor_folder_ids:
        return (destination_root / relative_path).is_symlink()
    return symlink_on_path(destination_root, relative_path)


def local_folder_has_content(path):
    """Anything in this local folder (unreadable counts as yes: it may hold your files)."""
    try:
        return any(path.iterdir())
    except OSError:
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
    tracked_folders=None,
    visited=None,
    parent_closed=False,
    targets=None,
):
    """parent_closed: the Drive parent takes no new items (shared with you read only).

    targets: the file shortcut targets build_plan fetched in batches, by id.
    """
    mime_type = effective_mime_type(item)
    relative_path = relative_parent / local_name(item)
    destination = destination_root / relative_path

    if tracked_folders is None:
        tracked_folders = tracked_folder_paths(state)  # build_plan computes it once for all
    if mime_type == FOLDER_MIME_TYPE:
        symlink = has_symlink(destination_root, relative_path, ancestor_folder_ids)
        folder_id = effective_id(item)
        if item["mimeType"] == SHORTCUT_MIME_TYPE:
            item = dict(item, readonly=True)  # the target may live anywhere: its content only comes down
        status = "UNCHANGED" if destination.is_dir() else "NEW"
        repeat = arrived = too_deep = False
        view_name = relative_path in VIEW_NAME_PATHS
        if relative_path in RESERVED_PATHS or symlink or view_name:
            status = "CONFLICT"
        elif folder_id in ancestor_folder_ids:
            status = "SKIPPED"
        elif len(relative_path.parts) >= MAX_FOLDER_DEPTH:
            status, too_deep = "SKIPPED", True  # a chain of folder shortcuts: it would never end
        elif visited is not None and folder_id in visited:
            status, repeat = "SKIPPED", True  # already planned through another path (two shortcuts to it)
        elif destination.exists() and not destination.is_dir():
            status = "CONFLICT"
        elif (
            status == "NEW"
            and not item.get("readonly")
            and not is_readonly_path(relative_path)
            and can(item, "canTrash")
            and synced_here_before(state, relative_path, folder_id, tracked_folders)
            and remote_subtree_unchanged(service, folder_id, relative_path, state, cache)
        ):
            status = "TRASH_REMOTE"  # synced before, the whole folder was deleted here
        elif (
            status == "UNCHANGED"
            and (parent_closed or is_readonly_path(relative_path))
            and state.get("folders", {}).get(relative_path.as_posix()) != folder_id
            and relative_path.as_posix() not in tracked_folders
            and local_folder_has_content(destination)
        ):
            # A Drive folder appeared over your local only folder (someone shared one by that name):
            # syncing would send your files into it. Rename one of the two.
            status = "CONFLICT"
            arrived = True
        # Deleted here, but Drive will not let you trash it: it comes down again.
        comes_back = (
            status == "NEW"
            and not can(item, "canTrash")
            and synced_here_before(state, relative_path, folder_id, tracked_folders)
        )
        plan.append(
            {
                "status": status,
                "kind": "FOLDER",
                "item": item,
                "relative_path": relative_path,
                "destination": destination,
                "repeat": repeat,
                "arrived": arrived,
                "too_deep": too_deep,
                "comes_back": comes_back,
                "symlink": symlink,
                "view_name": view_name,
            }
        )
        if visited is not None and status in {"NEW", "UNCHANGED", "TRASH_REMOTE"}:
            visited.add(folder_id)
        if status in {"CONFLICT", "SKIPPED", "TRASH_REMOTE"}:
            return
        for child in list_children(service, folder_id, cache):
            collect_plan(
                service,
                dict(child, readonly=True) if item.get("readonly") else child,
                relative_path,
                destination_root,
                state,
                plan,
                cache,
                ancestor_folder_ids | {folder_id},
                tracked_folders,
                visited,
                not can(item, "canAddChildren"),
                targets,
            )
        return

    shortcut = item["mimeType"] == SHORTCUT_MIME_TYPE or item.get("readonly")
    try:
        item = resolve_file_shortcut(service, item, targets)
    except HttpError as error:
        if error.status_code not in {403, 404}:
            raise
        # Target deleted or no longer shared: skip this one shortcut, not the whole sync.
        # Its path stays a Drive file, so a copy synced before is neither sent nor trashed.
        warn(f"{relative_path.as_posix()}: shortcut target not available, skipped")
        plan.append(
            {
                "status": "SKIPPED",
                "kind": "FILE",
                "item": dict(item, readonly=True),
                "relative_path": relative_path,
                "destination": destination,
                "local_sha256": None,
                "local_stat": local_stat(destination),
            }
        )
        return
    if shortcut:
        item = dict(item, readonly=True)  # the target may live anywhere: only downloaded
    relative_path = relative_parent / local_name(item)
    symlink = has_symlink(destination_root, relative_path, ancestor_folder_ids)
    plan.append(file_entry(service, item, relative_path, destination_root / relative_path, state, symlink))


def file_entry(service, item, relative_path, destination, state, symlink=None):
    """The plan entry of a Drive file synced at relative_path (symlink: a symbolic link there, never followed)."""
    if symlink is None:
        symlink = destination.is_symlink()
    planned_stat = local_stat(destination)  # before hashing: an edit made while hashing changes it
    replaced_key = None
    unreadable = False  # a file here it cannot read
    view_name = relative_path in VIEW_NAME_PATHS
    if relative_path in RESERVED_PATHS or symlink or view_name:
        status = "CONFLICT"
        current_sha256 = None
    elif is_unsupported(item):
        status = "SKIPPED"
        current_sha256 = None
    else:
        status, current_sha256, replaced_key = classify_file(
            service, item, relative_path, destination, state
        )
        unreadable = status == "CONFLICT" and current_sha256 is None and destination.is_file()

    return (
        {
            "status": status,
            "kind": "FILE",
            "item": item,
            "relative_path": relative_path,
            "destination": destination,
            "state_key": replaced_key,  # the replaced file's record, dropped once this one is synced
            "local_sha256": current_sha256,
            "local_stat": planned_stat,
            # Deleted here, but Drive will not let you trash it: it comes down again.
            "comes_back": status == "NEW"
            and not can(item, "canTrash")
            and tracked_record(state, item, relative_path) is not None,
            "symlink": symlink,
            "view_name": view_name,
            "unreadable": unreadable,
        }
    )


def same_name_key(path):
    """The name a case- or normalization-insensitive disk (macOS, exFAT...) sees: one file for all spellings."""
    return unicodedata.normalize("NFC", path.as_posix()).casefold()


def numbered_name(name, number):
    """`Scan (2).pdf` for Scan.pdf and 2 (`.env (2)` for a dot file)."""
    path = Path(name)
    stem, suffix = path.stem, path.suffix
    if name.startswith(".") and suffix == name:
        stem, suffix = name, ""
    return f"{stem} ({number}){suffix}"


def number_readonly_duplicates(service, plan, state):
    """Drive files sharing one local name that you may not rename on Drive (shared read only): the first keeps
    the name, the others come down as `name (2).ext`, `name (3).ext`... They only come down, like any read-only
    file. A file keeps the name it was synced under before, so later syncs find it again."""
    groups = {}
    for index, entry in enumerate(plan):
        if entry["status"] != "SKIPPED":
            groups.setdefault(same_name_key(entry["destination"]), []).append(index)
    taken = set(groups)
    synced_as = {}  # Drive id: the local paths it was synced under
    for record_key, record in state["files"].items():
        if record.get("path"):
            synced_as.setdefault(record_key.partition("::")[0], set()).add(record["path"])
    for indexes in list(groups.values()):
        entries = [plan[index] for index in indexes]
        if len(entries) < 2 or any(
            entry["kind"] != "FILE" or entry.get("item") is None or can(entry["item"], "canEdit")
            or entry["relative_path"] in RESERVED_PATHS
            for entry in entries
        ):
            continue  # a file you may rename on Drive, or a folder: a duplicate to rename there
        relative_path = entries[0]["relative_path"]
        names = [relative_path.name] + [numbered_name(relative_path.name, number) for number in range(2, len(entries) + 1)]
        indexes = sorted(indexes, key=lambda index: effective_id(plan[index]["item"]))
        chosen = {}
        for index in indexes:  # synced before under one of these names: it keeps it
            for name in names:
                path = (relative_path.parent / name).as_posix()
                if name not in chosen.values() and path in synced_as.get(effective_id(plan[index]["item"]), ()):
                    chosen[index] = name
                    break
        number = 1
        for index in indexes:
            if index in chosen:
                continue
            name = relative_path.name
            while name in chosen.values() or (
                name != relative_path.name and same_name_key(entries[0]["destination"].with_name(name)) in taken
            ):
                number += 1
                name = numbered_name(relative_path.name, number)
            chosen[index] = name
        for index, name in chosen.items():
            entry = plan[index]
            taken.add(same_name_key(entry["destination"].with_name(name)))
            if same_name_key(entry["destination"].with_name(name)) == same_name_key(entry["destination"]):
                continue  # keeps its own name
            plan[index] = file_entry(
                service, entry["item"], entry["relative_path"].with_name(name), entry["destination"].with_name(name), state
            )
            plan[index]["numbered"] = True


def mark_duplicate_targets(plan):
    """Two Drive items on one local path: rename one on Drive. Neither syncs, and for folders
    nothing below them either: which one would get your new files, which one's deletions count?

    Names that differ only by case or Unicode normalization count as one path: some disks
    (macOS, exFAT...) store them as one file. A folder and a file on one path count too: the
    disk cannot hold both."""
    targets = Counter(
        same_name_key(entry["destination"])
        for entry in plan
        if entry["status"] != "SKIPPED"
    )
    duplicate_folders = {
        same_name_key(entry["destination"])
        for entry in plan
        if entry["kind"] == "FOLDER" and entry["status"] != "SKIPPED"
        and targets[same_name_key(entry["destination"])] > 1
    }
    if duplicate_folders:
        plan[:] = [
            entry for entry in plan
            if not any(same_name_key(parent) in duplicate_folders for parent in entry["destination"].parents)
        ]
    names = {}
    for entry in plan:
        if entry["status"] != "SKIPPED" and targets[same_name_key(entry["destination"])] > 1:
            names.setdefault(same_name_key(entry["destination"]), []).append(entry)
    for entry in plan:
        if targets[same_name_key(entry["destination"])] > 1:
            entry["status"] = "CONFLICT"
            entry["duplicate"] = True
            entry["same_local_name"] = [
                source_name(other) for other in names.get(same_name_key(entry["destination"]), []) if other is not entry
            ]


def source_name(entry):
    """The name the user sees on Drive (or here, for a local only item)."""
    item = entry.get("item")
    return item["name"] if item is not None else f"{entry['relative_path'].name} (here)"


def duplicate_label(entry):
    """`(Drive: Report · same local name as Report.docx)`: which Drive items clash, under their Drive names."""
    others = ", ".join(entry.get("same_local_name") or [])
    return printable(f"(Drive: {source_name(entry)} · same local name as {others or 'another item'})")


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


def maybe_exists(path):
    """False only when path is surely not there: below a folder you cannot read, it may be."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


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
    trashed_remote = {
        entry["relative_path"] for entry in plan
        if entry["kind"] == "FOLDER"
        and (
            entry["status"] == "TRASH_REMOTE" or entry.get("duplicate") or entry.get("repeat") or entry.get("too_deep")
            or entry.get("symlink") or entry.get("view_name")
        )
    }  # deleted here, two Drive folders on this path, synced at another path, too deep or a symbolic link here:
    # nothing below goes anywhere

    def at_or_below_trashed(path):
        # One set lookup per parent: a scan of every trashed folder for each local path was quadratic
        return bool(trashed_remote) and (path in trashed_remote or not trashed_remote.isdisjoint(path.parents))

    def gone_from_both_sides(path, remote):
        return (
            path_is_in_scope(path, scopes)
            and path not in remote
            and not at_or_below_trashed(path)
            and not maybe_exists(destination_root / path)
        )

    # Gone from Drive and from here since the last sync: forget it (saved only if the sync is applied), so what
    # is restored or made again there later counts as new. Kept below a folder going to the Drive trash: counted.
    for key, record in list(state["files"].items()):
        if record.get("path") and gone_from_both_sides(Path(record["path"]), remote_files):
            del state["files"][key]
    for path_text in list(state.get("folders", {})):
        if gone_from_both_sides(Path(path_text), remote_directories):
            del state["folders"][path_text]
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
    unreadable = set()  # folders that cannot be listed here, and files that cannot be read
    # On a case-insensitive disk (macOS, exFAT...), Drive's "Report.txt" can be the local "report.txt"
    remote_by_key = {
        "FILE": {same_name_key(path): path for path in remote_files},
        "FOLDER": {same_name_key(path): path for path in remote_directories},
    }

    def drive_spelling(child, relative_path, kind):
        """The Drive path child is on disk under another spelling, or None."""
        remote = remote_by_key[kind].get(same_name_key(relative_path))
        if remote is None or remote == relative_path:
            return None
        try:
            return remote if os.path.samefile(child, destination_root / remote) else None
        except OSError:
            return None

    def can_send(relative_path):
        parent = relative_path.parent
        if is_readonly_path(relative_path):
            return False
        return parent in new_folders or remote_folders.get(parent) is not None

    def add_local_entry(relative_path, kind, symlink=False, not_utf8=False):
        if relative_path in planned_local_paths:
            return
        destination = destination_root / relative_path
        tracked_entry = tracked.get(relative_path) if kind == "FILE" else None
        tracked_key, current_sha256 = None, None

        planned_stat = None
        if symlink:
            status = "LOCAL_ONLY"  # never followed: neither the link nor what it points to is sent or trashed
        elif not_utf8:
            status = "LOCAL_ONLY"  # Drive names are UTF-8: neither it nor what is in it is sent or trashed
        elif kind == "FOLDER" and relative_path in new_folders:
            status = "UPLOAD_NEW"  # gone from Drive, but what is in it goes up: created again
        elif kind == "FOLDER" and relative_path in synced_folders:
            status = "TRASH_LOCAL"  # an empty folder synced before, deleted on Drive
        elif tracked_entry:
            tracked_key, record = tracked_entry
            planned_stat = local_stat(destination)  # before hashing: an edit made while hashing changes it
            try:
                current_sha256 = local_hash(destination, record)
            except OSError:
                current_sha256 = None
                unreadable.add(relative_path)
            if relative_path in unreadable:
                status = "CONFLICT"  # cannot read it here: neither sent nor trashed
            elif current_sha256 == record.get("local_sha256"):
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
                "local_stat": planned_stat,
                "symlink": symlink,
                "not_utf8": not_utf8,
            }
        )
        planned_local_paths.add(relative_path)

    def add_symlink_entry(child, relative_path):
        """A symbolic link Drive has no item for: listed as local only, never followed."""
        if (
            relative_path in remote_files
            or relative_path in remote_directories
            or drive_spelling(child, relative_path, "FILE") is not None
            or drive_spelling(child, relative_path, "FOLDER") is not None
        ):
            return  # planned with Drive's item: a conflict
        add_local_entry(relative_path, "FOLDER" if child.is_dir() else "FILE", symlink=True)

    def scan_gone_folder(relative_path, directory):
        """A folder synced before and gone from Drive. Something changed or new in it goes up (the change wins
        over the deletion): the folder is created again on Drive. Else what is in it follows the deletion."""
        start = len(plan)
        maybe_sent = can_send(relative_path)
        if maybe_sent:
            new_folders.add(relative_path)  # its content is planned as if it were created again
        scan_directory(relative_path, directory)
        if maybe_sent and any(entry["status"] == "UPLOAD_NEW" for entry in plan[start:]):
            add_local_entry(relative_path, "FOLDER")
            plan.insert(start, plan.pop())  # listed before its content
            return
        new_folders.discard(relative_path)  # nothing goes up: nothing in it was planned through can_send
        below = plan[start:]
        if below and all(entry["status"] == "TRASH_LOCAL" for entry in below) and holds_only(directory, below):
            # All of it follows the deletion: one folder to the system trash, not one call (and one name
            # search in the trash) per file. What it held is kept to check, right before, that nothing changed.
            del plan[start:]
            plan.append(
                {
                    "status": "TRASH_LOCAL",
                    "kind": "FOLDER",
                    "item": None,
                    "relative_path": relative_path,
                    "destination": directory,
                    "state_key": None,
                    "local_sha256": None,
                    "local_stat": None,
                    "contents": below,
                    "files_below": sum(entry.get("files_below", 1) for entry in below),
                }
            )
            planned_local_paths.add(relative_path)
            return
        if relative_path in synced_folders and not any(directory.rglob("*")):
            add_local_entry(relative_path, "FOLDER")  # synced, empty, gone from Drive

    def scan_directory(relative_directory, directory=None):
        """directory: where relative_directory is on disk, when the disk spells it differently."""
        if directory is None:
            directory = destination_root / relative_directory
        if not directory.is_dir():
            return
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
        except OSError:
            unreadable.add(relative_directory)  # nothing in it or below goes anywhere: see the end
            return

        for child in children:
            relative_path = relative_directory / child.name
            if relative_path.parts[0] in RESERVED_NAMES or is_disk_trash(relative_path.parts[0]):
                continue
            if (
                relative_directory == Path()
                and child.name in VIEW_DIRS
                and relative_path not in remote_directories
            ):
                continue  # the Shared with me / Shared drives / Trash views have their own scopes
            if child.is_symlink():
                if not at_or_below_trashed(relative_path):
                    add_symlink_entry(child, relative_path)
                continue
            if child.is_dir() and holds_state(child):
                continue  # another SaveGDrive folder, synced on its own
            if child.name.endswith(PART_SUFFIX):
                if child.is_file():
                    with contextlib.suppress(OSError):
                        child.unlink()  # left by a killed run (kill -9, power cut): ours, never complete
                continue
            if at_or_below_trashed(relative_path):
                continue  # deleted here; Drive's copy goes to the Drive trash
            if not is_utf8_name(child.name):
                if child.is_dir() or child.is_file():
                    add_local_entry(relative_path, "FOLDER" if child.is_dir() else "FILE", not_utf8=True)
                continue

            if child.is_dir():
                remote = drive_spelling(child, relative_path, "FOLDER")
                if relative_path in remote_directories:
                    scan_directory(relative_path, child)
                elif remote is not None:
                    scan_directory(remote, child)  # the same folder under Drive's spelling
                elif relative_path in tracked_directories:
                    scan_gone_folder(relative_path, child)
                else:
                    add_local_entry(relative_path, "FOLDER")
                    if relative_path in new_folders:
                        scan_directory(relative_path, child)  # its files go up too
            elif (
                child.is_file()
                and relative_path not in remote_files
                and drive_spelling(child, relative_path, "FILE") is None  # else Drive's file, under another spelling
            ):
                add_local_entry(relative_path, "FILE")

    for scope in scopes:
        destination = destination_root / scope
        if symlink_on_path(destination_root, scope):
            if destination.is_symlink() and not symlink_on_path(destination_root, scope.parent):
                if not at_or_below_trashed(scope):
                    add_symlink_entry(destination, scope)
            continue  # never followed
        if scope != Path() and destination.is_dir() and scope not in remote_folders:
            scan_gone_folder(scope, destination)  # picked before, gone from Drive (--again)
        elif scope == Path() or destination.is_dir():
            scan_directory(scope)
        elif destination.is_file() and scope not in remote_files:
            add_local_entry(scope, "FILE")

    # In or below a folder that cannot be listed here, files look deleted here (to the Drive trash) or missing
    # (to download into it): all left as they are on both sides, until it can be read.
    if unreadable:
        for entry in plan:
            path = entry["relative_path"]
            if path in unreadable or not unreadable.isdisjoint(path.parents):
                if entry["status"] != "SKIPPED":
                    entry["status"] = "CONFLICT"
                entry["unreadable"] = True
                entry.pop("contents", None)

    # An item picked alone and deleted here may have been renamed or moved next to it: the new name is outside
    # the selection, so it is not sent. Listed as local only, so the preview does not hide it.
    beside_picked = sorted({
        entry["relative_path"].parent
        for entry in plan
        if entry["status"] == "TRASH_REMOTE" and not path_is_in_scope(entry["relative_path"].parent, scopes)
    }, key=lambda path: path.as_posix())
    for parent in beside_picked:
        directory = destination_root / parent
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir(), key=lambda path: path.name.casefold()):
            relative_path = parent / child.name
            kind = "FOLDER" if child.is_dir() else "FILE"
            if (
                relative_path.parts[0] in RESERVED_NAMES
                or is_disk_trash(relative_path.parts[0])
                or (parent == Path() and child.name in VIEW_DIRS)
                or child.is_symlink()
                or child.name.endswith(PART_SUFFIX)
                or not (child.is_dir() or child.is_file())
                or (child.is_dir() and holds_state(child))
                or path_is_in_scope(relative_path, scopes)
                or relative_path in planned_local_paths
                or relative_path in remote_files
                or relative_path in remote_directories
                or relative_path in tracked
                or relative_path in tracked_directories
                or drive_spelling(child, relative_path, kind) is not None
            ):
                continue
            plan.append(
                {
                    "status": "LOCAL_ONLY",
                    "kind": kind,
                    "item": None,
                    "relative_path": relative_path,
                    "destination": child,
                    "state_key": None,
                    "local_sha256": None,
                    "local_stat": None,
                    "outside_selection": True,
                }
            )
            planned_local_paths.add(relative_path)


def moved_scopes(state, selections):
    """Where items of the last selection were, when they are picked again at another path (renamed or moved
    on Drive since): the old copy is scanned like an item deleted on Drive, to your trash or sent back if changed."""
    now = {}
    for entry in selections:
        now.setdefault(entry["item"].get("id"), set()).add(selected_path(entry))
    return [
        Path(saved_item["path"])
        for saved_item in (state.get("last_selection") or {}).get("items", [])
        if saved_item.get("id") in now and Path(saved_item["path"]) not in now[saved_item["id"]]
    ]


def build_plan(service, selections, scopes, destination_root, state, cache, on_item=lambda: None, roots=None):
    """The sync plan of the selection; roots maps view base paths to their Drive folder id (None: read only)."""
    folder_ids = [effective_id(entry["item"]) for entry in selections if is_folder(entry["item"])]
    prefetch_folders(service, folder_ids, cache)
    # Every file shortcut below the selection, resolved in batches, not one call each while planning.
    shortcuts = [entry["item"] for entry in selections] + [
        item for folder_id in reachable_folders(cache, folder_ids) for item in cache[folder_id]
    ]
    targets = get_items(
        service, [effective_id(item) for item in shortcuts if is_file_shortcut(item)], SHORTCUT_TARGET_FIELDS
    )
    plan = []
    tracked_folders = tracked_folder_paths(state)
    visited = set()  # Drive folders already planned: each one syncs at its first path only
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
            tracked_folders=tracked_folders,
            visited=visited,
            targets=targets,
        )
        for _ in range(len(plan) - before):
            on_item()
    number_readonly_duplicates(service, plan, state)
    mark_duplicate_targets(plan)
    remote_folders = dict(roots if roots is not None else {Path(): "root"})
    for entry in plan:
        if entry["kind"] == "FOLDER" and entry.get("item") is not None and entry["status"] != "TRASH_REMOTE":
            # None: a folder you may not add to (shared read only, behind a shortcut): new files there stay local
            remote_folders[entry["relative_path"]] = (
                effective_id(entry["item"])
                if can(entry["item"], "canAddChildren") and not entry["item"].get("readonly")
                and not entry.get("duplicate") and not entry.get("arrived")
                else None
            )
    for saved_item in (state.get("last_selection") or {}).get("items", []):
        if saved_item.get("parent_id"):
            # An item picked alone: if it is gone from Drive but changed here, it goes back to its folder.
            remote_folders.setdefault(Path(saved_item["relative_parent"]), saved_item["parent_id"])
    scopes = compact_scopes([*scopes, *moved_scopes(state, selections)])
    add_local_entries(plan, destination_root, state, scopes, remote_folders)
    # The Drive folder of a file picked alone, from the tree: its local folder is not scanned.
    picked_parents = {
        selection["relative_parent"]: selection["parent_id"]
        for selection in selections
        if not is_folder(selection["item"]) and selection.get("parent_id")
    }
    for entry in plan:
        parent = entry["relative_path"].parent
        parent_id = remote_folders.get(parent)
        if entry["status"] == "UPLOAD_NEW":
            entry["parent_id"] = parent_id
        elif entry["kind"] == "FILE" and entry.get("item") is not None:
            # Could a renamed copy of yours (keep both) go up next to it? At the next sync if its folder is
            # scanned; else (a file picked alone) only with --again, which then picks the copy too.
            sendable = (parent_id or picked_parents.get(parent)) is not None and not is_readonly_path(
                entry["relative_path"]
            )
            entry["copy_goes_up"] = sendable and path_is_in_scope(parent, scopes)
            entry["copy_on_again"] = sendable and not entry["copy_goes_up"]
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
UNREADABLE_LABEL = "(cannot read it here)"
UNREADABLE_NOTE = (
    "Some files or folders cannot be read here (their permissions: a Docker volume, a folder made with sudo...): "
    "they are left as they are on both sides until you make them readable or move them out."
)
MASS_DELETION_MIN = 2  # the quarter rule only counts above this: 1 or 2 deletions in a small selection pass


def can_keep_both(entry):
    """A file changed on both sides (or an edited export) whose local copy can step aside."""
    return (
        entry["kind"] == "FILE"
        and entry.get("item") is not None
        and entry["status"] in {"CONFLICT", "KEEP_BOTH"}
        and not entry.get("duplicate")
        and not entry.get("symlink")
        and not entry.get("view_name")
        and not entry.get("unreadable")
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
    taken |= {entry["destination"] for entry in plan}  # a file this plan brings down: never your copy's name
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


# Printed output lands on the terminal's own background, light or dark: words and numbers keep its default
# color (bold or dim at most), and only marks are colored, in bold when the color is too pale on white.
TERMINAL_WORD_STYLES = {"yellow": "bold", "bright_yellow": "bold", "bright_black": "dim", "red": "red"}
TERMINAL_MARK_STYLES = {"yellow": "bold", "bright_yellow": "bold", "bright_green": "bold", "bright_cyan": "bold"}


def terminal_text(style, text, count=None):
    """Markup for '[count] [arrow] words' printed on the terminal: the arrow keeps a readable color, the
    count and words none (bold or dim at most)."""
    mark, _, words = text.partition(" ") if text[:1] in "↓↑" else ("", "", text)
    word_style = TERMINAL_WORD_STYLES.get(style)

    def plain(part):
        return f"[{word_style}]{part}[/]" if word_style else part

    parts = [] if count is None else [plain(str(count))]
    if mark:
        parts.append(f"[{TERMINAL_MARK_STYLES.get(style, style)}]{mark}[/]")
    parts.append(plain(words))
    return " ".join(parts)


def plan_summary(plan, plain=False):
    """Status counts. plain: for the terminal's own background (print_plan), not the TUI's dark one."""
    counts = Counter(entry["status"] for entry in plan)
    parts = []
    for status, (style, label, _) in STATUS_STYLES.items():
        if counts[status]:
            if plain:
                parts.append(terminal_text(style, label, counts[status]))
            else:
                parts.append(f"[{style}]{counts[status]} {label}[/]")
    return " · ".join(parts)


def has_actions(plan):
    return any(entry["status"] in ACTIONS for entry in plan)


def in_sync(plan):
    """Nothing to do and no conflict left: both sides are the same."""
    return not has_actions(plan) and not any(entry["status"] == "CONFLICT" for entry in plan)


def entry_size(entry):
    """The size a preview row shows: what is sent for an upload (your file), else Drive's."""
    if entry["kind"] != "FILE":
        return ""
    if entry["status"] in UPLOADS:
        try:
            return human_size(entry["destination"].stat().st_size)
        except (KeyError, OSError):
            return ""  # gone since the plan was made: apply_plan reports it as an error
    item = entry.get("item")
    return display_size(item) if item is not None else ""


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
            path = escape(printable(entry["relative_path"].as_posix()))
            if entry["kind"] == "FOLDER":
                path = f"[bold]{path}/[/]"  # the color would be pale on a light terminal
            if entry.get("duplicate"):
                path += f"  [dim]{escape(duplicate_label(entry))}[/]"
            elif entry.get("unreadable"):
                path += f"  [dim]{UNREADABLE_LABEL}[/]"
            size = entry_size(entry)
            table.add_row(terminal_text(style, label), path, size if size != "-" else "")
        console.print()
        console.print(table)

    console.print()
    console.print(f"[bold]Preview[/]  {plan_summary(plan, plain=True)}")
    if hidden:
        console.print(f"[dim]{plural(hidden, 'unchanged item')} not listed (--verbose lists them).[/]")
    space, fits = space_line(plan, destination_root)
    if space:
        console.print(f"[dim]{space}[/]" if fits else f"[bold red]! {space}: not enough disk space[/]")
    for note in plan_notes(plan, terminal=True):
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
    total = 0
    for entry in plan:
        if entry["kind"] == "FILE" and entry["status"] in UPLOADS:
            try:
                total += (destination_root / entry["relative_path"]).stat().st_size
            except OSError:
                continue  # gone since the plan was made: apply_plan reports it as an error
    return total


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
        if "files_below" in entry:
            count += max(1, entry["files_below"])  # a whole folder deleted on Drive: its files, counted at the plan
        elif entry["kind"] == "FOLDER":
            count += max(1, len(tracked_below(state, entry["relative_path"])))
        else:
            count += 1
    in_play = {entry["relative_path"].as_posix() for entry in plan}
    synced = sum(  # an older selection's files, still in the state, are not in play
        1 for record in state["files"].values()
        if record.get("path") and (record["path"] in in_play
                                   or not in_play.isdisjoint(parent.as_posix() for parent in
                                                             PurePosixPath(record["path"]).parents))
    )
    return count, synced


def needs_deletion_check(plan, state):
    count, synced = deletion_count(plan, state)
    return count > MASS_DELETION_FILES or (count > MASS_DELETION_MIN and synced and count > synced * MASS_DELETION_SHARE)


def plan_notes(plan, terminal=False):
    """The notes under the preview. terminal: printed by print_plan, where no key of the tree is read."""
    statuses = {entry["status"] for entry in plan}
    notes = []
    opens = "(in the tree, o opens it)" if terminal else "(o opens it)"
    keep_both = "--keep-both: keep both versions" if terminal else "b: keep both versions of this one, shift+b: of all"
    if any(
        entry["status"] == "CONFLICT" and not entry.get("duplicate") and not entry.get("symlink")
        and not entry.get("view_name") and not entry.get("unreadable")
        for entry in plan
    ):
        notes.append(
            f"Conflicts are left as they are ({keep_both}). "
            "A file you cannot edit on Drive (a Google Doc export, a file shared read only) and changed here is a "
            "conflict too: it never goes back to Drive."
        )
    if "KEEP_BOTH" in statuses:
        notes.append(
            "Keep both: your version is renamed 'name (local).ext' and sent to Drive at the next sync; "
            "Drive's version takes its place. Delete the one you do not want, the sync follows."
        )
    if any(entry["status"] == "KEEP_BOTH" and entry.get("copy_on_again") for entry in plan):
        notes.append(
            "Some kept versions are next to files picked alone, in folders not selected: they go to Drive "
            "with --again, or when you select their folder."
        )
    if any(
        entry["status"] == "KEEP_BOTH" and not entry.get("copy_goes_up", True) and not entry.get("copy_on_again")
        for entry in plan
    ):
        notes.append("Some kept versions are in folders read only on Drive: they stay on your disk only.")
    if any(entry.get("unreadable") for entry in plan):
        notes.append(UNREADABLE_NOTE)
    if any(entry.get("duplicate") for entry in plan):
        notes.append(
            "Several Drive items get one local name (the same name, a Google Doc next to its .docx, "
            f"a / turned into _...): none of them syncs until you rename one on Drive {opens}."
        )
    if any(entry.get("numbered") for entry in plan):
        notes.append(
            "Drive files with the same name that you cannot rename (shared read only) come down as "
            "'name (2).ext', 'name (3).ext'..."
        )
    if any(entry.get("arrived") for entry in plan):
        notes.append(
            "A Drive folder appeared over a local only folder of yours (someone shared one by that name): "
            "nothing syncs in it, so your files stay off Drive. Rename one of the two."
        )
    if any(entry.get("repeat") for entry in plan):
        notes.append(
            "A Drive folder reached through two paths (shortcuts) syncs at its first path only: "
            "the others are skipped, and what is on your disk there is left as it is."
        )
    if any(entry.get("too_deep") for entry in plan):
        notes.append(
            f"Folders too deep (past {MAX_FOLDER_DEPTH} levels, through a chain of shortcuts) are skipped: "
            "what is on your disk there is left as it is."
        )
    if any(entry.get("comes_back") for entry in plan):
        notes.append(
            "Deleted here, but Drive does not let you delete them (read only, not yours, or your shared drive "
            "role): downloaded again. Ask their owner or a manager to delete them."
        )
    if any(entry.get("symlink") for entry in plan):
        notes.append(
            "Symbolic links are never followed: a link stays here only, and what it points to is not synced. "
            "A link with the name of a Drive item is a conflict: nothing syncs there until you replace the link."
        )
    if any(entry.get("not_utf8") for entry in plan):
        notes.append(
            "Names that are not UTF-8 (shown with \\xNN, often from an old zip) cannot go to Drive: they stay here "
            "only, with what is in them. Rename them to send them."
        )
    if any(entry.get("view_name") for entry in plan):
        names = " or ".join(f"'{name}'" for name in sorted(path.as_posix() for path in VIEW_NAME_PATHS))
        notes.append(
            f"A Drive item at the top named like a view ({names}) would share that view's local folder: "
            f"nothing syncs in it, and what is on your disk there is left as it is. Rename it on Drive {opens}."
        )
    if "TRASH_REMOTE" in statuses:
        notes.append("Deleted here since the last sync: Drive's copies go to the Drive trash (kept 30 days).")
    if "TRASH_LOCAL" in statuses:
        notes.append("Deleted on Drive since the last sync: your copies go to your system trash.")
    if any(entry.get("outside_selection") for entry in plan):
        notes.append(
            "Not in your selection, so not sent: new here next to an item deleted here (renamed or moved?). "
            "Select its parent folder in the tree, or the whole view, to send it."
        )
    if any(
        entry["status"] == "LOCAL_ONLY" and not entry.get("outside_selection") and not entry.get("symlink")
        and not entry.get("not_utf8")
        for entry in plan
    ):
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


PREVIEW_ITEM_MIN = 20  # cells of the ITEM column of the preview, however narrow the screen


def fit_path(path, suffix, width):
    """The path cut on the left (…/folder/file) so that path and suffix fit in width cells."""
    if Text(path + suffix).cell_len <= width:
        return path + suffix
    room = width - Text(suffix).cell_len - 1
    if room < 1:
        return path + suffix  # the table crops it
    kept = ""
    for char in reversed(path):
        if Text(char + kept).cell_len > room:
            break
        kept = char + kept
    return "…" + kept + suffix


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
        min-height: 6;
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
        max-height: 30%;
        overflow-y: auto;
        margin: 0 2;
        padding: 0 1;
        color: #94a3b8;
    }
    """

    KEY_CONTEXT = "preview"
    QUITS = False  # in the tree, y syncs and the tree comes back; --again quits

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
        self.rows = None

    def compose(self) -> ComposeResult:
        yield Header(icon="")
        yield Static(id="preview-summary")
        yield Static(id="preview-filters")
        yield DataTable(id="preview-table", cursor_type="row", zebra_stripes=False)
        yield Static(id="preview-notes")
        yield KeyBar(self.KEY_CONTEXT)

    def on_mount(self):
        self.sub_title = header_text(self.account, self.destination, "preview")
        self.rows = []
        self.update_notes()
        self.fill()
        self.query_one(DataTable).focus()

    def update_notes(self):
        notes = Text()
        for note in plan_notes(self.plan):
            notes.append(note + "\n")
        if not has_actions(self.plan):
            then = " and quits." if self.QUITS else "."
            if in_sync(self.plan):
                notes.append(f"Everything is up to date: y saves the state{then}", style="bold green")
            else:
                notes.append(f"Nothing to transfer but conflicts: y saves the state{then}", style="bold yellow")
        notes.rstrip()
        self.query_one("#preview-notes", Static).update(notes)

    def fill(self):
        name, keep = self.filters[self.filter_index]
        rows = [entry for entry in self.plan if keep(entry)]
        summary = Text.from_markup(f"[bold]Preview[/]  {plan_summary(self.plan)}")
        summary.append("\nDestination: ", style="dim")
        summary.append(short_path(self.destination))
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
        cells = []
        for entry in rows:
            style, label, _ = STATUS_STYLES[entry["status"]]
            path = printable(entry["relative_path"].as_posix()) + ("/" if entry["kind"] == "FOLDER" else "")
            size = entry_size(entry)
            suffix = ""
            if entry["status"] == "KEEP_BOTH":
                suffix = f"  (yours → {printable(entry['local_copy'].name)})"
            elif entry.get("duplicate"):
                suffix = f"  {duplicate_label(entry)}"
            elif entry.get("unreadable"):
                suffix = f"  {UNREADABLE_LABEL}"
            size_cell = Text(size if size != "-" else "", style="dim", justify="right")
            cells.append((Text(label, style=style), path, suffix, size_cell))
        # STATUS and SIZE keep their width; ITEM gets the rest, so SIZE stays in view.
        status_width = max([len("STATUS")] + [status.cell_len for status, *_ in cells])
        size_width = max([len("SIZE")] + [size.cell_len for *_, size in cells])
        styles = table.styles
        frame = styles.margin.width + styles.gutter.width + styles.scrollbar_size_vertical
        room = self.app.size.width - frame - 6 * table.cell_padding - status_width - size_width
        needed = max([len("ITEM")] + [Text(path + suffix).cell_len for _, path, suffix, _ in cells])
        item_width = max(min(needed, room), PREVIEW_ITEM_MIN)
        table.clear(columns=True)
        table.add_column("STATUS", key="status", width=status_width)
        table.add_column("ITEM", key="item", width=item_width)
        table.add_column(Text("SIZE", justify="right"), key="size", width=size_width)
        self.rows = rows
        for entry, (status, path, suffix, size) in zip(rows, cells):
            item = fit_path(path, suffix, item_width)
            table.add_row(status, Text(item, style="cyan") if entry["kind"] == "FOLDER" else Text(item), size)
        if rows:
            table.move_cursor(row=min(row, len(rows) - 1))

    def on_resize(self, event):
        if self.rows is not None:  # filled: fit ITEM to the new width
            self.fill()

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
        if entry.get("duplicate"):
            self.notify(
                f"{duplicate_label(entry)}: rename one on Drive (o opens it).", severity="warning", markup=False
            )
            return
        if entry.get("unreadable"):
            self.notify(UNREADABLE_NOTE, severity="warning", markup=False)
            return
        if not can_keep_both(entry):
            self.notify("Keep both applies to a file changed on both sides.", severity="warning")
            return
        keep = entry["status"] == "CONFLICT"
        set_keep_both(self.plan, [entry], keep)
        if keep and entry.get("copy_on_again"):
            self.notify(
                f"{printable(entry['local_copy'].name)} goes to Drive with --again, or when you select its folder: "
                "this folder is not in your selection.",
                severity="warning",
                markup=False,
            )
        elif keep and not entry.get("copy_goes_up", True):
            self.notify(
                f"{printable(entry['local_copy'].name)} cannot go up: this folder is read only on Drive. "
                "It stays on your disk only.",
                severity="warning",
                markup=False,
            )
        self.refresh_plan()

    def action_keep_both_all(self):
        conflicts = [entry for entry in self.plan if can_keep_both(entry)]
        if not conflicts:
            if any(entry.get("duplicate") for entry in self.plan):
                self.notify(
                    "Only Drive items sharing one local name here: rename one on Drive (o opens it).",
                    severity="warning",
                )
            else:
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
    QUITS = True
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


def download_file_atomically(service, entry, stop=None, on_bytes=lambda count: None, check=None):
    """Download to a temporary file, then put it in place; check() runs just before and may refuse (OSError)."""
    destination = entry["destination"]
    # A short name, whatever the length of the file's: ".<name>.savegdrive.part" would not fit for names of
    # 239 to 255 bytes (Errno 36). One per destination, in the same folder so os.replace stays atomic.
    name_hash = hashlib.sha256(os.fsencode(destination.name)).hexdigest()[:16]
    temporary = destination.with_name(f".{name_hash}{PART_SUFFIX}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        with temporary.open("wb") as file_handle:
            fetch_media(service, entry["item"], CountingWriter(file_handle, on_bytes), stop)
        set_remote_mtime(temporary, entry["item"])
        if check is not None:
            check()
        if entry["status"] == "UPDATE" and destination.is_file():
            shutil.copymode(destination, temporary)  # a new version, not a new file: keep its mode (x, 600...)
        os.replace(temporary, destination)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise


def local_stat(path):
    """(size, date in ns) of a local file, or None when there is none."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def unchanged_since_plan(entry):
    """True when the local file is still what the plan saw: still absent, or the same content."""
    destination = entry["destination"]
    if entry.get("local_sha256") is None:
        return not os.path.lexists(destination)
    now = local_stat(destination)
    if now is None:
        return False
    if now == entry.get("local_stat"):
        return True
    try:
        return file_hash(destination) == entry["local_sha256"]
    except OSError:
        return False


CHANGED_SINCE_PREVIEW = "changed here since the preview, left as is: compared again at the next sync"


def check_unchanged_since_plan(entry):
    if not unchanged_since_plan(entry):
        raise OSError(CHANGED_SINCE_PREVIEW)


CHANGED_ON_DRIVE_SINCE_PREVIEW = "changed on Drive since the preview, not trashed: compared again at the next sync"


def still_deleted_here(service, entry, state):
    """Right before the Drive trash: still gone here, and Drive's side still the one last synced (the plan may be old)."""
    if os.path.lexists(entry["destination"]):
        return False
    item = entry["item"]
    try:
        if entry["kind"] == "FOLDER":
            return remote_subtree_unchanged(service, effective_id(item), entry["relative_path"], state, {})
        now = service.files().get(fileId=item["id"], fields=ITEM_FIELDS, supportsAllDrives=True).execute(
            num_retries=API_RETRIES)
    except HttpError:
        return False
    return remote_signature(now) == remote_signature(item) and can(now, "canTrash")


def holds_only(directory, entries):
    """True when directory holds nothing but entries (their paths, or below a folder among them): no symlink,
    no file or empty folder the plan left out. Paths are compared on disk, below directory."""
    covered = {entry["destination"] for entry in entries}
    folders = {entry["destination"] for entry in entries if entry["kind"] == "FOLDER"}

    def unreadable(error):
        raise error

    try:
        for dirpath, dirnames, filenames in os.walk(directory, onerror=unreadable):
            here = Path(dirpath)
            if here != directory and here in folders:
                dirnames[:] = []
                continue
            if here != directory and not dirnames and not filenames:
                return False  # an empty folder the plan left out
            for name in filenames:
                if here / name not in covered:
                    return False
            for name in dirnames:
                if (here / name).is_symlink():
                    return False
    except OSError:
        return False
    return True


def still_deleted_on_drive_unchanged_here(entry):
    """Right before the system trash: the local file is still the one last synced, the folder still empty,
    or for a whole folder: what it held is unchanged, and nothing else is in it."""
    destination = entry["destination"]
    if "contents" in entry:
        if not destination.is_dir() or destination.is_symlink():
            return False
        return (
            all(still_deleted_on_drive_unchanged_here(inside) for inside in entry["contents"])
            and holds_only(destination, entry["contents"])
        )
    if entry["kind"] == "FOLDER":
        try:
            return destination.is_dir() and not destination.is_symlink() and not any(destination.rglob("*"))
        except OSError:
            return False
    return entry.get("local_sha256") is not None and unchanged_since_plan(entry)


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
    # Keep the file's own date on Drive (else Drive puts the upload time, which then comes back here).
    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="milliseconds")
    modified = modified.replace("+00:00", "Z")
    if file_id:
        request = service.files().update(
            fileId=file_id, body={"modifiedTime": modified}, media_body=media, fields=UPLOAD_FIELDS,
            supportsAllDrives=True,
        )
    else:
        request = service.files().create(
            body={"name": name, "parents": [parent_id], "modifiedTime": modified}, media_body=media,
            fields=UPLOAD_FIELDS, supportsAllDrives=True,
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


def remote_folder_request(service, name, parent_id):
    return service.files().create(
        body={"name": name, "parents": [parent_id], "mimeType": FOLDER_MIME_TYPE},
        fields="id,name,mimeType,webViewLink",
        supportsAllDrives=True,
    )


def create_remote_folders(service, folders):
    """Create every (name, parent id) of folders on Drive in batched calls; returns, for each, the folder or its error."""
    made = [None] * len(folders)
    pending = list(range(len(folders)))
    attempts = Counter()

    while pending:
        chunk, pending = pending[:API_BATCH_SIZE], pending[API_BATCH_SIZE:]
        retry = []

        def callback(request_id, response, exception, chunk=chunk):
            index = chunk[int(request_id)]
            if exception is None:
                made[index] = response
            elif is_retryable(exception) and attempts[index] < API_RETRIES:
                attempts[index] += 1
                retry.append(index)
            else:
                made[index] = exception

        batch = service.new_batch_http_request(callback=callback)
        for position, index in enumerate(chunk):
            batch.add(remote_folder_request(service, *folders[index]), request_id=str(position))
        try:
            batch.execute()
        except DRIVE_CALL_ERRORS as error:
            for index in chunk:
                if made[index] is None and index not in retry:
                    made[index] = error
        debug(f"Created {plural(len(chunk), 'folder')} on Drive")
        if retry:
            delay = min(2 ** max(attempts[index] for index in retry), 30)
            debug(f"Drive rate limit, retrying {len(retry)} folder creation(s) in {delay}s")
            time.sleep(delay)
            pending = retry + pending
    return made


def trash_local(path):
    """Move a local file or folder to the system trash (the file manager's), never delete it."""
    send2trash(str(path))


def forget_all_below(state, relative_paths):
    """Drop the records of each of relative_paths and of everything below them, files and folders, in one pass."""
    prefixes = {relative_path.as_posix() for relative_path in relative_paths}
    if not prefixes:
        return

    def gone(path):
        while path:
            if path in prefixes:
                return True
            path = path.rpartition("/")[0]
        return False

    for key in [key for key, record in state["files"].items() if gone(record.get("path", ""))]:
        del state["files"][key]
    folders = state.setdefault("folders", {})
    for path in [path for path in folders if gone(path)]:
        del folders[path]


STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)  # system shutdown, `timeout`, kill; the terminal closed


def interrupt_on_stop_signal(signal_number, frame):
    raise KeyboardInterrupt(signal.Signals(signal_number).name)


@contextlib.contextmanager
def stop_signals_interrupt():
    """SIGTERM and SIGHUP take the Ctrl+C path (partial files removed, finished transfers saved), not a silent exit."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {number: signal.signal(number, interrupt_on_stop_signal) for number in STOP_SIGNALS}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def record_synced(state, entry, item, local_sha256):
    """Save what this entry now is on both sides, under its new key, so the next run compares against it."""
    if entry.get("state_key"):
        state["files"].pop(entry["state_key"], None)
    key = state_key(item, entry["relative_path"])
    state["files"][key] = state_record(dict(entry, item=item), local_sha256)
    if entry.get("changed_meanwhile"):
        state["files"][key].pop("local_mtime_ns", None)  # saved again while sent: hash it again next time


def create_folders(service, plan, state, results, report):
    """Phase 1. Folders: created here, then on Drive (parents first). Returns the Drive id of each folder path."""
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
    levels = {}
    for entry in plan:
        if entry["kind"] == "FOLDER" and entry["status"] == "UPLOAD_NEW":
            levels.setdefault(len(entry["relative_path"].parts), []).append(entry)
    for depth in sorted(levels):
        # Parents first: every folder of this level has its parent on Drive by now, so one batch makes them all.
        outcomes = {}
        sent = []
        for entry in levels[depth]:
            parent_id = folder_ids.get(entry["relative_path"].parent, entry.get("parent_id"))
            if parent_id is None:
                outcomes[id(entry)] = OSError("its parent folder could not be created on Drive")
            else:
                sent.append((entry, parent_id))
        made = create_remote_folders(service, [(entry["relative_path"].name, parent_id) for entry, parent_id in sent])
        outcomes.update((id(entry), folder) for (entry, _), folder in zip(sent, made))
        parents = {id(entry): parent_id for entry, parent_id in sent}
        for entry in levels[depth]:
            relative = entry["relative_path"]
            folder = outcomes[id(entry)]
            if isinstance(folder, BaseException):
                results["FOLDER_ERROR"] += 1
                report("[red]✗[/]", f"{relative.as_posix()}/: {error_text(folder)}")
                continue
            folder_ids[relative] = folder["id"]
            state.setdefault("folders", {})[relative.as_posix()] = folder["id"]
            reselect_sent(state, relative, folder, parents[id(entry)])
            results["FOLDER_UPLOAD_NEW"] += 1
            report("[bold]↑[/]", f"{relative.as_posix()}/")
    return folder_ids


def settle_untransferred_files(plan, state, results, report):
    """Phase 2. Files that need no transfer are counted and recorded. Returns the files to transfer."""
    transfers = []
    for entry in plan:
        if entry["kind"] != "FILE":
            continue
        status = entry["status"]
        if status == "UNCHANGED":
            if entry.get("local_sha256") is not None and not unchanged_since_plan(entry):
                results["FILE_ERROR"] += 1  # its old date and hash would hide the edit for good
                report("[red]✗[/]", f"{entry['relative_path'].as_posix()}: {CHANGED_SINCE_PREVIEW}")
                continue
            local_sha256 = entry.get("local_sha256") or file_hash(entry["destination"])
            try:
                set_remote_mtime(entry["destination"], entry["item"])  # same content: lets the tree mark it ✓
            except OSError:
                pass
            record_synced(state, entry, entry["item"], local_sha256)  # after the date: the next run reuses this hash
            results["FILE_UNCHANGED"] += 1
        elif status in {"CONFLICT", "SKIPPED", "LOCAL_ONLY"}:
            results[f"FILE_{status}"] += 1
        elif status in DOWNLOADS or status in UPLOADS:
            transfers.append(entry)
    return transfers


def transfer_entry(client, entry, folder_ids, stop, on_bytes):
    """Download or upload one file with the Drive client client() gives. Returns (Drive item, local sha256)."""
    destination = entry["destination"]
    if entry["status"] == "KEEP_BOTH":
        check_unchanged_since_plan(entry)
        if os.path.lexists(entry["local_copy"]):
            raise OSError(f"{entry['local_copy'].name} appeared here since the preview")
        os.replace(destination, entry["local_copy"])  # your version steps aside

        def still_free():
            if os.path.lexists(destination):
                raise OSError(CHANGED_SINCE_PREVIEW)

        try:
            download_file_atomically(client(), entry, stop, on_bytes, still_free)
        except BaseException:
            if not os.path.lexists(destination):
                os.replace(entry["local_copy"], destination)
            raise
        return entry["item"], file_hash(destination)
    if entry["status"] in DOWNLOADS:
        download_file_atomically(client(), entry, stop, on_bytes, lambda: check_unchanged_since_plan(entry))
        return entry["item"], file_hash(destination)
    before = local_stat(destination)  # before hashing: an edit made from now on changes it
    local_sha256 = file_hash(destination)
    if entry["status"] == "UPLOAD":
        current = client().files().get(
            fileId=effective_id(entry["item"]), fields="id,mimeType,size,modifiedTime,md5Checksum",
            supportsAllDrives=True,
        ).execute(num_retries=API_RETRIES)
        if remote_signature(current) != remote_signature(entry["item"]):
            raise OSError("changed on Drive since the preview, not sent: compared again at the next sync")
        item = upload_file(client(), destination, file_id=effective_id(entry["item"]),
                           stop=stop, on_bytes=on_bytes)
    else:
        parent_id = folder_ids.get(entry["relative_path"].parent, entry.get("parent_id"))
        if parent_id is None:
            raise OSError("its folder could not be created on Drive")
        item = upload_file(client(), destination, name=entry["relative_path"].name,
                           parent_id=parent_id, stop=stop, on_bytes=on_bytes)
    if local_stat(destination) != before:
        entry["changed_meanwhile"] = True  # saved again while sent: keep its date, hash it again next time
        return item, local_sha256
    try:
        set_remote_mtime(destination, item)  # same date on both sides: the tree marks it ✓
    except OSError:
        pass
    return item, local_sha256


# ↑ in bold, not bright green or cyan: those are unreadable on a light terminal
TRANSFER_MARKS = {"NEW": "[green]↓[/]", "UPDATE": "[cyan]↓[/]", "UPLOAD_NEW": "[bold]↑[/]", "UPLOAD": "[bold]↑[/]"}


def run_transfers(service, make_service, transfers, folder_ids, state, results, report, progress, executor,
                  futures, handled, stop):
    """Phase 3. Downloads and uploads, in the executor's threads, each with its own Drive client.
    futures and handled are filled here and read by the caller, which records the transfers finished but
    not read yet when the run stops."""
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

    with progress:
        futures.update({executor.submit(transfer_entry, client, entry, folder_ids, stop, on_bytes): entry
                        for entry in transfers})
        for future in as_completed(futures):
            handled.add(future)
            entry = futures[future]
            relative = entry["relative_path"].as_posix()
            status = entry["status"]
            try:
                item, local_sha256 = future.result()
            except DRIVE_CALL_ERRORS as error:
                results["FILE_ERROR"] += 1
                report("[red]✗[/]", f"{relative}: {error_text(error)}")
            else:
                results[f"FILE_{status}"] += 1
                record_synced(state, entry, item, local_sha256)
                if status == "UPLOAD_NEW":
                    reselect_sent(state, entry["relative_path"], item,
                                  folder_ids.get(entry["relative_path"].parent, entry.get("parent_id")))
                if status == "KEEP_BOTH":
                    report("[bold]⧉[/]", f"{relative} ↓ · yours kept as {entry['local_copy'].name}")
                    if entry.get("copy_on_again"):
                        select_kept_copy(state, entry["relative_path"].parent / entry["local_copy"].name)
                else:
                    report(TRANSFER_MARKS[status], relative)
            progress.advance(task)


def trash_remote_entries(service, plan, state, results, report, forgotten):
    """Phase 4. Deleted here: Drive's copies go to the Drive trash. Their paths are added to forgotten."""
    trashed = []
    for entry in plan:
        if entry["status"] != "TRASH_REMOTE":
            continue
        if still_deleted_here(service, entry, state):
            trashed.append(entry)
            continue
        results[f"{entry['kind']}_ERROR"] += 1
        relative = entry["relative_path"].as_posix() + ("/" if entry["kind"] == "FOLDER" else "")
        changed_here = os.path.lexists(entry["destination"])
        report("[red]✗[/]", f"{relative}: {CHANGED_SINCE_PREVIEW if changed_here else CHANGED_ON_DRIVE_SINCE_PREVIEW}")
    if not trashed:
        return
    failed = trash_items(service, [entry["item"]["id"] for entry in trashed])
    for entry in trashed:
        relative = entry["relative_path"].as_posix() + ("/" if entry["kind"] == "FOLDER" else "")
        error = failed.get(entry["item"]["id"])
        if error:
            results[f"{entry['kind']}_ERROR"] += 1
            report("[red]✗[/]", f"{relative}: {error}")
        else:
            results[f"{entry['kind']}_TRASH_REMOTE"] += 1
            forgotten.append(entry["relative_path"])
            report("[magenta]✗[/]", f"{relative} → Drive trash")


def trash_local_entries(plan, results, report, forgotten):
    """Phase 5. Deleted on Drive: your copies go to your system trash. Their paths are added to forgotten.
    Returns the folders that held them, which may now be empty."""
    emptied = set()
    for entry in plan:
        if entry["status"] != "TRASH_LOCAL":
            continue
        relative = entry["relative_path"]
        if not still_deleted_on_drive_unchanged_here(entry):
            results[f"{entry['kind']}_ERROR"] += 1
            report("[red]✗[/]", f"{relative.as_posix()}: {CHANGED_SINCE_PREVIEW}")
            continue
        try:
            trash_local(entry["destination"])
        except OSError as error:
            results[f"{entry['kind']}_ERROR"] += 1
            report("[red]✗[/]", f"{relative.as_posix()}: {error}")
            continue
        results[f"{entry['kind']}_TRASH_LOCAL"] += 1
        forgotten.append(relative)
        emptied.add(relative.parent)
        report("[bright_magenta]✗[/]", f"{relative.as_posix()} → your trash")
    return emptied


def prune_emptied_dirs(destination_root, emptied, remote_folders, state):
    """Remove the folders left empty by phase 5, deepest first, then their parents left empty in turn.
    Stops at the destination root and at folders that still exist on Drive (remote_folders)."""
    for folder in sorted(emptied, key=lambda path: len(path.parts), reverse=True):
        while folder != Path() and folder not in remote_folders:
            directory = destination_root / folder
            if not directory.is_dir() or any(directory.iterdir()):
                break
            directory.rmdir()
            state.setdefault("folders", {}).pop(folder.as_posix(), None)  # removed by the sync, not by you
            folder = folder.parent


@stop_signals_interrupt()
def apply_plan(service, plan, destination_root, state, jobs=1, make_service=None):
    """Apply the confirmed plan, phase by phase. Transfers run in jobs threads, each with its own Drive client.
    Whatever happens, finished transfers and deletions are saved in the state."""
    results = Counter()
    results.failures = []  # "path: reason" of each error, shown above the tree when it comes back
    progress = Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),  # no color: MofNCompleteColumn's green is pale on white
        TextColumn("[dim]{task.fields[transfer]}[/]"),
        console=console,
        transient=True,
    )

    def report(mark, text):
        if mark == "[red]✗[/]":
            results.failures.append(printable(text))
        progress.console.print(f"{mark} {escape(printable(text))}")

    forgotten = []  # deleted paths, forgotten in one pass before the state is saved
    folder_ids = create_folders(service, plan, state, results, report)
    transfers = settle_untransferred_files(plan, state, results, report)
    stop = threading.Event()
    executor = ThreadPoolExecutor(max_workers=max(1, jobs))
    futures = {}
    handled = set()
    try:
        run_transfers(service, make_service, transfers, folder_ids, state, results, report, progress, executor,
                      futures, handled, stop)
        trash_remote_entries(service, plan, state, results, report, forgotten)
        emptied = trash_local_entries(plan, results, report, forgotten)
        prune_emptied_dirs(destination_root, emptied, set(folder_ids), state)
    except BaseException:
        stop.set()  # running transfers stop at their next chunk; downloads remove their partial file
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        for future, entry in futures.items():  # finished but not read yet when the run stopped (Ctrl+C...)
            if future not in handled and future.done() and not future.cancelled() and future.exception() is None:
                record_synced(state, entry, *future.result())
        forget_all_below(state, forgotten)
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


def results_summary(results, plain=False):
    """Result counts. plain: for the terminal's own background (print_results), not the TUI's dark one."""
    parts = []
    for keys, style, singular, plural_label in RESULT_LABELS:
        count = sum(results[key] for key in keys)
        if count:
            text = f"{count} {singular if count == 1 else plural_label}"
            parts.append(terminal_text(style, text) if plain else f"[{style}]{text}[/]")
    return " · ".join(parts) or "[dim]nothing to do[/]"


def print_results(results):
    console.print(f"[bold]Done[/]  {results_summary(results, plain=True)}")


# --- Main ------------------------------------------------------------------

def select_kept_copy(state, relative_path):
    """Your version kept next to a file picked alone, outside the scanned folders: added to the selection, so
    --again sends it (then picks it, through reselect_sent)."""
    saved = state.get("last_selection")
    if saved is not None and relative_path.as_posix() not in saved.setdefault("scopes", []):
        saved["scopes"].append(relative_path.as_posix())


def reselect_sent(state, relative_path, item, parent_id):
    """A picked item gone from Drive, sent again from here: picked again under its new id, so the next
    --again syncs it instead of taking it for deleted on Drive."""
    saved = state.get("last_selection")
    path = relative_path.as_posix()
    if not saved or path not in saved.get("scopes", []):
        return
    if any(saved_item["path"] == path and saved_item["id"] == item["id"] for saved_item in saved["items"]):
        return
    saved["items"].append(
        {
            "id": item["id"],
            "name": item.get("name", relative_path.name),
            "relative_parent": relative_path.parent.as_posix(),
            "path": path,
            "parents": [parent_id],
            "parent_id": parent_id,
        }
    )


def run_sync(args, service, make_service, plan, selections, scopes, roots, destination_root, state, cache, token,
             kept=()):
    """Apply a confirmed plan, save the selection for --again and the Drive snapshot, print the result."""
    remember_selection(state, selections, scopes, roots, kept)
    results = apply_plan(service, plan, destination_root, state, args.jobs, make_service)
    save_snapshot(
        destination_root, token, cache,
        [root[0] for root in roots]
        + [effective_id(entry["item"]) for entry in selections if is_folder(entry["item"])],
    )
    if in_sync(plan):
        console.print("[green]✓[/] Everything is in sync.")
    else:
        print_results(results)
    return results


def ask(question):
    """The terminal's answer, or "" when there is none: stdin at its end (cron, Ctrl+D) is a no."""
    try:
        return console.input(question)
    except EOFError:
        console.print("\n[dim]No answer: add --yes to run without a terminal.[/]")
        return ""


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
            if ask("  Type [bold]delete[/] to go on: ").strip() != "delete":
                console.print("[dim]Cancelled. Nothing was changed.[/]")
                return False
            return True
    if args.yes or not has_actions(plan):
        return True
    if ask("\nProceed? \\[y/N] ").strip().lower() not in {"y", "yes"}:
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
    if in_sync(plan):
        text.append("✓ ", style="green")
        text.append("Your last selection is in sync.", style="dim")
        return text
    # A new folder no file comes with (empty, or holding only what is already in sync) is work of its own.
    new_folders = [entry for entry in plan if entry["kind"] == "FOLDER" and entry["status"] in {"NEW", "UPLOAD_NEW"}]
    file_paths = {
        direction: [entry["relative_path"] for entry in plan if entry["kind"] == "FILE" and entry["status"] in statuses]
        for direction, statuses in (("NEW", {"NEW", "UPDATE"}), ("UPLOAD_NEW", UPLOADS))
    } if new_folders else {}
    folders_only = Counter(
        entry["status"] for entry in new_folders
        if not any(entry["relative_path"] in path.parents for path in file_paths[entry["status"]])
    )
    parts = [
        (counts["NEW"] + counts["UPDATE"], "↓ to download", "green"),
        (counts["UPLOAD_NEW"] + counts["UPLOAD"], "↑ to send", "bright_cyan"),
        (folders_only["NEW"], "folder to create here" if folders_only["NEW"] == 1 else "folders to create here",
         "green"),
        (folders_only["UPLOAD_NEW"],
         "folder to create on Drive" if folders_only["UPLOAD_NEW"] == 1 else "folders to create on Drive",
         "bright_cyan"),
        (counts["KEEP_BOTH"],
         "conflict to keep both ways" if counts["KEEP_BOTH"] == 1 else "conflicts to keep both ways",
         "bright_yellow"),
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


NOTICE_FAILURES = 3  # errors listed above the tree; all of them stay in the terminal


def result_notice(plan, results):
    """The last sync's result, shown above the tree when it comes back."""
    text = Text()
    if not in_sync(plan):
        text.append("Done  ", style="bold")
        text.append_text(Text.from_markup(results_summary(results)))
        failures = getattr(results, "failures", [])
        for failure in failures[:NOTICE_FAILURES]:
            text.append("\n✗ ", style="red")
            text.append(failure)
        if len(failures) > NOTICE_FAILURES:
            text.append(
                f"\n… and {len(failures) - NOTICE_FAILURES} more, listed in the terminal after you quit", style="dim"
            )
    else:
        text.append("✓ ", style="green")
        text.append("Everything was in sync.")
    return text


def destination_path(path_text):
    """The local folder's absolute path, nothing created."""
    destination = Path(path_text).expanduser()
    if not destination.is_absolute():
        destination = Path.cwd() / destination
    return destination.parent.resolve() / destination.name


def resolve_destination(path_text):
    """The local folder: new (created), empty, or already managed by SaveGDrive."""
    return initialize_managed_destination(destination_path(path_text))


def no_previous_selection(destination):
    console.print(
        f"[red]✗[/] No previous selection in {escape(str(destination))}. Pass the folder you synced with "
        "--download-path PATH, or run once without --again."
    )
    raise SystemExit(1)


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
    # Python 3.14 colors --help and usage errors in cyan and yellow, unreadable on a light terminal;
    # an attribute, since color=False raises TypeError before 3.14.
    parser.color = False
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
        help=f"With --yes: also apply a sync that deletes more than {MASS_DELETION_FILES} files, or more than "
        f"{MASS_DELETION_MIN} files and a quarter of the synced ones (refused otherwise).",
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
    global VERBOSE, DRIVE_ROOT
    VERBOSE = args.verbose
    DRIVE_ROOT = None if args.again else args.folder_id

    try:
        if args.again:
            # Checked before the folder is created and before signing in: a typo or a folder never synced.
            candidate = destination_path(args.download_path)
            if not holds_state(candidate) or (
                has_valid_state(candidate) and not has_last_selection(load_state(candidate))
            ):
                no_previous_selection(candidate)
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
            if not has_last_selection(state):
                no_previous_selection(destination_root)
            with console.status("[dim]Checking Drive changes…[/]"):
                snapshot = load_snapshot(destination_root)
                replayed = apply_changes(service, snapshot) if snapshot else None
                if replayed:
                    cache, token = replayed
                else:
                    cache, token = {}, start_page_token(service)
                kept = []
                selections, local_scopes = last_selection(service, state, cache, kept)
            names = ", ".join(escape(printable(entry["item"]["name"])) for entry in selections[:5])
            more = f" and {len(selections) - 5} more" if len(selections) > 5 else ""
            who = f"{escape(account)} " if account else ""
            console.print(f"[bold]Again[/]  {names}{more} [dim]{who}→ {escape(str(destination_root))}[/]")
            roots = saved_roots(state)
            again_roots = upload_roots(roots)
            with console.status("[dim]Comparing with local files…[/]") as status:
                checked = 0

                def on_item():
                    nonlocal checked
                    checked += 1
                    if checked % 25 == 0:
                        status.update(f"[dim]Comparing with local files… {checked}[/]")

                plan = build_plan(
                    service, selections, local_scopes, destination_root, state, cache, on_item, again_roots
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
                               destination_root, state, cache, token, kept)
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
                view=view, sort=sort, notice=notice, token=token,
            )
            result = app.run()
            view, sort, destination_root, token = app.view, app.sort, app.destination, app.drive_token
            if not result:
                break
            plan, selections, local_scopes = result["plan"], result["selections"], result["scopes"]
            destination_root, state = result["destination"], result["state"]
            roots = result.get("roots")  # from s: the last selection's own
            if roots is None:
                roots = [
                    (view_.root_id, view_.base, view_.can_add) for view_ in views
                    if view_.nodes and fully_selected(view_.browsed.get(view_.base, []), view_.base, selections)
                ]
            if args.yes:
                print_plan(plan, destination_root)
                if not confirm_in_terminal(args, plan, state):
                    break
            results = run_sync(args, service, make_service, plan, selections, local_scopes, roots,
                               destination_root, state, cache, token, result.get("kept", ()))
            failed = failed or bool(results["FILE_ERROR"] or results["FOLDER_ERROR"])
            if args.yes:
                break
            notice = result_notice(plan, results)
            if changes_drive(plan):
                # Drive changed under the loaded tree: replay its changes before the next round.
                with console.status("[dim]Checking Drive changes…[/]"):
                    token = refresh_listings(service, cache, token)
                    for view_ in views:
                        view_.nodes, view_.error, view_.sorted_by = None, None, "name"
                # The tree lists the view again itself: "loading…" on the view, then the tree or the
                # error there. A failure here would end the tool on a ✗ right after a sync that worked.
        if failed:
            raise SystemExit(1)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/]")
        raise SystemExit(130)
    except DRIVE_CALL_ERRORS as error:
        console.print(f"[red]✗[/] {escape(error_text(error))}")
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()

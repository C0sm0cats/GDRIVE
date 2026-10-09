# GDRIVE - Two-Way Google Drive Sync

SaveGDrive syncs Google Drive folders and files with a local folder, both ways,
from the terminal, on Linux and other POSIX environments. You pick what to sync
in a colored tree, review a preview of every change on both sides, then
confirm. A file changed on both sides is never overwritten, and nothing is
deleted for good: what you delete on one side goes to the trash of the other.

![SaveGDrive in the terminal: the Drive tree with folder totals, file sizes, dates and export formats, views as tabs, two selected items and every key, grouped under the tree](docs/screenshot.svg)

## Features

- Two-way sync of the folders and files you pick: new and changed files go
  down and up, deletions go to the Drive trash or your system trash
- Textual tree of your Drive in aligned columns: folders first, with their
  file count and size; files with their size, modification date and export
  format
- Marks since the last sync: ✓ same on both sides, ↻ changed on Drive, ✎
  changed here
- My Drive, Shared with me, every shared drive and the Drive trash as views,
  switched with `Tab`
- Multi-selection of files and folders at any depth, across views; a
  selected folder includes everything below it
- `z` / `m` sort by size or date, `/` filter by name or path, `o` opens the
  item in Google Drive, `f` changes the local folder for the session, `?`
  shows the key help
- Preview in the interface before any change, with a tab per kind of change,
  the amount to download and send, and a typed confirmation when a sync
  deletes many files
- At start, a line above the tree says what changed on either side since the
  last sync of your last selection; `s` reviews and syncs it
- After a sync, the tree comes back with the result on top
- `--again` syncs the previous selection without the tree, replaying only the
  Drive changes since the last run; with `--yes` it runs unattended, for
  example from cron
- Conflicts can be kept both ways: your version is renamed `name (local).ext`
  and Drive's takes its place, then both end up on both sides
- Atomic downloads through temporary `.part` files, removed if a download fails
  or is interrupted; resumable uploads
- Google Docs, Sheets, Slides, Drawings and Apps Script exported to local
  formats (download only)
- Drive trash management: `x` moves items to the Drive trash; the Trash view
  restores them (`r`), deletes them forever (`x`) or empties the trash
  (`shift+t`), always after a confirmation
- Fast loading: one batched API call per tree level, with retries on Drive
  rate limits
- Parallel transfers (4 at a time by default) with a progress bar, the amount
  sent and the speed, a line per file and a final summary
- Compact browser sign-in with a clickable link; the header shows the
  signed-in account and the local folder

## Repository contents

```text
GDRIVE/
├── LICENSE
├── README.md
├── savegdrive.py
├── run-savegdrive.sh
├── docs/
│   ├── make_screenshot.py
│   └── screenshot.svg
└── tests/
    └── test_savegdrive.py
```

Runtime files such as OAuth credentials, tokens, and the virtual
environment are local files and must never be committed.

## Requirements

- Python 3.10 or newer
- A POSIX shell
- Python virtual environment support
- A Google Cloud project with the Google Drive API enabled
- OAuth 2.0 Desktop application credentials

The runner creates `.venv` and installs the required Python packages
automatically:

- `google-api-python-client`
- `google-auth`
- `google-auth-oauthlib`
- `send2trash`
- `textual`

## Google API setup

1. Create or select a project in the
   [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the Google Drive API.
3. Configure the OAuth consent screen.
4. Create an OAuth client ID for a Desktop application.
5. Download the client file, rename it to `credentials.json`, and place it next
   to `savegdrive.py`.

For additional details, see the
[Google Drive API Python quickstart](https://developers.google.com/workspace/drive/api/quickstart/python).

Never commit `credentials.json` or `token.json`.

## Installation

```bash
git clone https://github.com/C0sm0cats/GDRIVE.git
cd GDRIVE
chmod +x run-savegdrive.sh
./run-savegdrive.sh --help
```

The first run of the runner creates `.venv` and installs the dependencies.

## First run

```bash
./run-savegdrive.sh
```

Files are synced with `~/GDrive` by default; use `--download-path PATH` for
another folder, or press `f` in the tree to change it for the session. The
folder is created if it does not exist. It must be:

- new;
- empty; or
- an existing SaveGDrive folder containing `.savegdrive-state.json`.

A non-empty folder without that state file is refused to prevent accidental
adoption or overwriting of unrelated data.

SaveGDrive then signs in to Google Drive with full Drive access (it sends files
and manages the Drive trash). Your browser opens the Google sign-in page; if it
does not, the terminal shows a clickable link. The session is kept in
`token.json` and renewed automatically.

## How the sync works

The files and folders you select are synced. Each run compares three things:
Drive, your local folder, and the state of the last sync, kept in
`.savegdrive-state.json`. This is how SaveGDrive knows which side changed,
even for changes made outside it (in your file manager, on the web, on your
phone):

| Since the last sync | In the preview | What happens |
| --- | --- | --- |
| New on Drive | `↓ new` | Downloaded |
| Changed on Drive | `↓ changed` | Downloaded, replacing your unchanged copy |
| New here | `↑ new` | Sent to Drive, in the same folder (folders are created) |
| Changed here | `↑ changed` | Sent to Drive as a new version of the file |
| Deleted here | `↑ to Drive trash` | Drive's copy goes to the Drive trash (kept 30 days) |
| Deleted on Drive | `↓ to your trash` | Your copy goes to your system trash |
| Changed on both sides | `conflict` | Nothing changes; `b` keeps both versions (see [Preview](#preview)) |
| Same on both sides | `unchanged` | Nothing to do |

Some rules keep it safe:

- **The change wins over a deletion**: deleted on one side but changed on the
  other, the file comes back on the side where it was deleted.
- **Google Docs, Sheets, Slides** are exported (`.docx`, `.xlsx`...) and only
  come down: a changed export is a conflict, never sent back. Shortcuts and the
  Trash view only come down too.
- **Never synced yet**: a file present on both sides with different content is
  a conflict; the same content is simply adopted. The first sync of a folder
  you already have never deletes anything.
- **Many deletions** (more than 20 files, or more than a quarter of the synced
  ones) need you to type `delete`; with `--yes` they are refused unless you add
  `--allow-deletions`. That is what an unplugged disk or an emptied folder
  looks like.
- **Your rights on Drive count**: a file shared with you read only is never
  sent back (a local change is a conflict) nor trashed (deleted here, it comes
  back); a new file in a folder you cannot add to stays local only, like new
  files at the top of Shared with me or in the Trash view.
- **Empty folders are synced too**: created, deleted or emptied on one side,
  the other side follows.

Renaming or moving a file on one side shows as a deletion and a new file: the
content arrives at its new place, the old one goes to the trash. On Drive, the
file then starts a new version history and loses its sharing.

Only files whose size or date changed since the last sync are read again to
compare them, so a sync of a large, mostly unchanged folder stays quick.

## Options

| Option | Effect |
| --- | --- |
| `--download-path PATH` | Local folder (default: `~/GDrive`); new, empty or already managed |
| `--folder-id ID` | Browse this Drive folder instead of the root of My Drive |
| `--again` | Sync the previous selection again, without the tree |
| `--yes` | Apply the preview without asking for confirmation, then exit after the sync |
| `--allow-deletions` | With `--yes`: also apply a sync that deletes many files (refused otherwise) |
| `--keep-both` | Keep both versions of conflicts: yours renamed `name (local).ext`, Drive's in its place |
| `--jobs N` | Files sent or downloaded at the same time, 1 to 16 (default: 4) |
| `--verbose` | Show sign-in, token and API details, and list unchanged items in the preview |

## Interactive selector

The tree supports keyboard and mouse navigation. All the keys are listed in
[Keys](#keys); press `?` for the same help, or run
`./run-savegdrive.sh --help`.

At start, SaveGDrive checks your last selection in the background and says
above the tree how far it is from being in sync, changes and deletions made
outside it included:

```text
Since the last sync: 2 ↓ to download · 1 deleted here · 1 conflict   s to review and sync
```

`s` opens the preview of that sync directly; nothing changes before `y`.

After a sync, the tree comes back with the result on top, the marks up to date
and nothing selected; the view, the sort and the open folders are kept. Pick
something else, or quit with `q` / `Esc`. With `--yes` or `--again`,
SaveGDrive syncs once and exits.

Folders show their number of files and their total size; files show their size,
their modification date (time only for today), and the local format of
Google-native files (`→ .docx`), aligned in columns. Items that cannot be
synced, such as Google Forms, are greyed out. Selecting a parent folder
includes its complete subtree and replaces the child selections. The line under
the tree counts the selected items, the files they contain and their size.

A mark before the name compares Drive and your folder with the last sync, read
from the state without hashing any file:

| Mark | Meaning |
| --- | --- |
| `✓` | Synced and the same on both sides (for a folder: every file in it) |
| `↻` | Changed on Drive since the last sync, or a folder with new files |
| `✎` | Changed here since the last sync: sent to Drive at the next one |

### Views

`Tab` switches between My Drive, Shared with me (the items others shared with
you), each shared drive you are a member of, and Trash (what you moved to the
Drive trash). A view is loaded the first time it is shown; the tabs show how
many items are selected in each. Selections are kept across views and synced
together:

```text
<local folder>/                     My Drive
<local folder>/Shared with me/      Shared with me
<local folder>/Shared drives/Team/  the shared drive "Team"
<local folder>/Trash/               Trash
```

With `--folder-id`, only that folder is shown.

Folders always come first. Sorted by size, a folder counts the size of
everything below it; sorted by date, its newest file. Items without a size or
date come last. The sort applies to every view and is shown under the tree.

The filter keeps the matching items, their parent folders, and everything
below a matching folder. Selections hidden by the filter are kept.

### Drive trash

Besides the sync, the tree changes Drive only on request, after a
confirmation:

| Where | Key | On Drive |
| --- | --- | --- |
| Any view but Trash | `x` | Move the selection, or the item under the cursor, to the Drive trash |
| Trash | `r` | Restore the selection, or the item under the cursor, where it was |
| Trash | `x` | Delete forever, after typing `delete` |
| Trash | `shift+t` | Empty the whole Drive trash, after typing `empty` |

After a change, the views are reloaded from Drive and the selection is cleared.
The Trash view shows what you trashed yourself; what was inside a trashed
folder shows below it.

## Keys

Each screen shows its keys at the bottom, grouped on several lines; `?` in the
tree and `./run-savegdrive.sh --help` list them all with these descriptions.

### In the tree

| Key | Action |
| --- | --- |
| `↑` `↓` | Move |
| `←` `→` | Collapse / expand a folder, or go to the parent folder / first child |
| `enter` | Collapse / expand a folder |
| `e` | Expand the whole view; again: collapse it all (shift+space: below the cursor) |
| `pgup` `pgdn` | Scroll a page (home / end: top / bottom) |
| `tab` | Next view: My Drive, Shared with me, each shared drive, then Trash |
| `shift+tab` | Previous view |
| `/` | Filter by name or path |
| `z` | Sort by size, biggest first; again: by name |
| `m` | Sort by date, newest first; again: by name |
| `space` | Select / unselect (a selected folder includes everything below it) |
| `a` | Select all, or unselect all when everything is selected (with a filter: the matches) |
| `c` | Unselect everything, including items hidden by the filter or in other views |
| `x` | Move the selection (or the item under the cursor) to the Drive trash |
| `d` | Compare the selection with the local folder and open the preview |
| `s` | Review the sync of your last selection, checked at start (the line above the tree) |
| `f` | Change the local folder for this session |
| `o` | Open the item under the cursor in Google Drive |
| `?` | Show the key help |
| `q` `esc` | Quit (esc first closes the filter); after a sync, the tree comes back |

### In the Trash view

| Key | Action |
| --- | --- |
| `↑` `↓` | Move |
| `←` `→` | Collapse / expand a folder, or go to the parent folder / first child |
| `enter` | Collapse / expand a folder |
| `e` | Expand the whole view; again: collapse it all (shift+space: below the cursor) |
| `pgup` `pgdn` | Scroll a page (home / end: top / bottom) |
| `tab` | Next view: My Drive, Shared with me, each shared drive, then Trash |
| `shift+tab` | Previous view |
| `/` | Filter by name or path |
| `z` | Sort by size, biggest first; again: by name |
| `m` | Sort by date, newest first; again: by name |
| `space` | Select / unselect (a selected folder includes everything below it) |
| `a` | Select all, or unselect all when everything is selected (with a filter: the matches) |
| `c` | Unselect everything, including items hidden by the filter or in other views |
| `r` | Restore the selection (or the item under the cursor) on Drive |
| `x` | Delete the selection (or the item under the cursor) forever, after typing delete |
| `shift+t` | Empty the whole Drive trash, after typing empty |
| `d` | Compare the selection with the local folder and open the preview |
| `s` | Review the sync of your last selection, checked at start (the line above the tree) |
| `f` | Change the local folder for this session |
| `o` | Open the item under the cursor in Google Drive |
| `?` | Show the key help |
| `q` `esc` | Quit (esc first closes the filter); after a sync, the tree comes back |

### While typing a filter

| Key | Action |
| --- | --- |
| `enter` | Keep the filter and go back to the tree |
| `esc` | Clear the filter |

### In the preview

| Key | Action |
| --- | --- |
| `↑` `↓` | Move (pgup / pgdn, home / end: by page, to the top / bottom) |
| `tab` | Next filter tab: Changes, Everything, then one tab per status |
| `shift+tab` | Previous filter tab |
| `b` | Keep both versions of the conflict under the cursor: yours as 'name (local).ext' |
| `shift+b` | Keep both for every conflict; again: undo |
| `y` | Sync: apply the preview |
| `esc` `n` | Back to the tree, selection kept |

### In the preview of --again

| Key | Action |
| --- | --- |
| `↑` `↓` | Move (pgup / pgdn, home / end: by page, to the top / bottom) |
| `tab` | Next filter tab |
| `shift+tab` | Previous filter tab |
| `b` | Keep both versions of the conflict under the cursor: yours as 'name (local).ext' |
| `shift+b` | Keep both for every conflict; again: undo |
| `y` | Sync: apply the preview |
| `esc` `n` `q` | Cancel: nothing changes |

### In the folder screen (f)

| Key | Action |
| --- | --- |
| `↑` `↓` `enter` | Pick a parent directory in the tree (space: unfold it) |
| `tab` | Next field: parent directory, folder name, buttons |
| `ctrl+s` | Apply: use this folder for the session |
| `esc` | Cancel |

### In a confirmation

| Key | Action |
| --- | --- |
| `y` | Yes |
| `n` `esc` | No: nothing changes |

### In a confirmation that asks for a word (delete, empty)

| Key | Action |
| --- | --- |
| `enter` | Confirm, once the word is typed |
| `esc` | Cancel: nothing changes |

### In this help

| Key | Action |
| --- | --- |
| `↑` `↓` | Scroll |
| `esc` `?` `q` | Close (enter or a click too) |

Nothing changes, here or on Drive, before you confirm: y in the preview, or the question of x, r and shift+t. The mouse works too: click to move, click a folder's arrow to unfold it.

## Preview

`d` compares the selection with the local folder and opens the preview: the
plan, a summary by kind of change, the amount to download (next to the free
disk space) and to send, and notes about conflicts and deletions. Nothing has
changed yet.

The tabs above the list show each kind of change with its count; `Tab` /
`Shift+Tab` switch between them. `y` syncs, `b` keeps both versions of a
conflict (`shift+b`: of all), `Esc` goes back to the tree.

When the known download size exceeds the free space, the line turns red. When
the sync deletes many files, `y` asks you to type `delete`. With `--yes`, the
preview is printed and applied without asking.

Keep both renames your version `name (local).ext` (or `(local 2)`... when
taken) and downloads Drive's version in its place: the conflict is gone, and
the next sync sends your version to Drive as a new file. Delete the one you do
not want on either side; the sync follows. Two Drive files with the same name
in a folder are conflicts too, but keep both cannot help there: rename one on
Drive (`o` in the tree opens it).

Transfers run in parallel (`--jobs`, 4 by default), each thread with its own
Drive connection. If a transfer fails, the file keeps its previous content and
the run ends with an error count. The state is saved even after `Ctrl+C`, so
the next run picks up where it stopped.

## Syncing the same selection again

Each confirmed run stores its selection in the state of the local folder. To
sync it again without the tree:

```bash
./run-savegdrive.sh --again          # preview, then confirm
./run-savegdrive.sh --again --yes    # no prompt, e.g. from cron
```

Each run also saves the folder listings it used and a Drive changes token in
`.savegdrive-cache.json`. `--again` replays the Drive changes since then
instead of listing every folder again, so it stays fast on a large Drive; if
the token has expired, the folders are listed again.

Selected items are looked up again on Drive, so renamed or updated files are
picked up. When a whole view was selected (every item at its top level), new
items in it are synced too.

## Google-native file exports

| Drive format | Local format |
| --- | --- |
| Google Docs | `.docx` |
| Google Sheets | `.xlsx` |
| Google Slides | `.pptx` |
| Google Drawings | `.pdf` |
| Google Apps Script | `.json` |

## Optional Drive folder

By default, the selector displays the root of My Drive. An explicit Drive folder
ID can be used as the remote root:

```bash
./run-savegdrive.sh --folder-id DRIVE_FOLDER_ID
```

The folder ID is the last part of the folder URL in Google Drive.

## Local files

These files and directories are used or created locally and must not be
published:

```text
.venv/
credentials.json
token.json
```

The local folder contains:

```text
.savegdrive-state.json    the last sync: what is synced, and the last selection
.savegdrive-cache.json    Drive listings and changes token, for a fast --again
```

Do not delete the state file: without it, SaveGDrive no longer knows what was
synced and treats every difference as a conflict. The cache can be deleted at
any time.

## Troubleshooting

- **Missing `credentials.json`**: SaveGDrive says where to put it; see
  [Google API setup](#google-api-setup).
- **Sign-in keeps failing**: remove `token.json` and run again.
- **A file stays in `conflict`**: it changed on both sides, or it existed on
  both sides before the first sync with different content. Use `b` to keep
  both, then keep the version you want.
- **A sync is refused with `--yes`**: it deletes many files. Check it in the
  preview, or add `--allow-deletions` if it is expected.
- **More details**: run with `--verbose`.

## Tests

```bash
.venv/bin/python -m unittest discover -s tests
```

The README screenshot is generated from the real selector with demo files:

```bash
.venv/bin/python docs/make_screenshot.py
```

## License

This project is licensed under the terms of the [LICENSE](LICENSE) file.

## Activity

![Repository activity](https://repobeats.axiom.co/api/embed/25976cb32f4e20b563591e534b83230e6b3d61f8.svg "Repobeats analytics image")

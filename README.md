# GDrive Pull

GDrive Pull is an interactive Google Drive downloader for Linux and other POSIX
environments. You pick files and folders in a colored terminal tree, review a
preview of every local change, then confirm. Local edits are never overwritten
and nothing local is deleted. The same tree also manages the Drive trash.

![GDrive Pull in the terminal: the Drive tree with folder totals, file sizes, dates and export formats, views as tabs, two selected items and every key, grouped under the tree](docs/screenshot.svg)

## Features

- Textual tree of your Drive in aligned columns: folders first, with their
  file count and size; files with their size, modification date and export
  format
- Marks from earlier downloads: ✓ up to date, ↻ changed on Drive, ✎ changed
  locally
- My Drive, Shared with me and every shared drive as views, switched with
  `Tab`
- Multi-selection of files and folders at any depth, across views; a
  selected folder includes everything below it
- `z` / `m` sort by size or date, `/` filter by name or path, `o` opens the item in Google Drive, `f` changes
  the download folder for the session, `?` shows the key help
- `--again` downloads the previous selection without the tree, replaying only
  the Drive changes since the last run; with `--yes` it runs unattended, for
  example from cron
- Fast loading: one batched API call per tree level, with retries on Drive
  rate limits; the plan reuses the loaded tree instead of listing it again
- Preview in the interface before any change: new, update, unchanged,
  conflict, removed from Drive, local only, skipped; `Esc` goes back to the
  tree to adjust the selection
- Atomic downloads through temporary `.part` files, removed if a download fails
  or is interrupted
- Google Docs, Sheets, Slides, Drawings and Apps Script exported to local
  formats
- Local change detection through checksums and a managed state file
- Conflicts can be kept both ways: the Drive version is saved next to your
  local copy as `name (Drive).ext`
- Files removed from Drive are set aside on your disk instead of being
  deleted; the Removed from Drive tab puts them back or deletes them
- Unknown local files and folders are preserved
- Drive shortcuts followed to their target files and folders
- Parallel downloads (4 at a time by default), with a progress bar, the
  amount downloaded and the speed, a ✓ / ✗ line per file and a final summary
- Drive trash management: `x` moves items to the Drive trash; the Trash view
  restores them (`r`), deletes them forever (`x`) or empties the trash
  (`shift+t`), always after a confirmation
- Compact browser sign-in with a clickable link; the header shows the
  signed-in account and the download folder

## Repository contents

```text
GDRIVE/
├── LICENSE
├── README.md
├── gdrivepull.py
├── run-gdrivepull.sh
├── docs/
│   ├── make_screenshot.py
│   └── screenshot.svg
└── tests/
    └── test_gdrivepull.py
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
- `textual`

## Google API setup

1. Create or select a project in the
   [Google Cloud Console](https://console.cloud.google.com/).
2. Enable the Google Drive API.
3. Configure the OAuth consent screen.
4. Create an OAuth client ID for a Desktop application.
5. Download the client file, rename it to `credentials.json`, and place it next
   to `gdrivepull.py`.

For additional details, see the
[Google Drive API Python quickstart](https://developers.google.com/workspace/drive/api/quickstart/python).

Never commit `credentials.json` or `token.json`.

## Installation

```bash
git clone https://github.com/C0sm0cats/GDRIVE.git
cd GDRIVE
chmod +x run-gdrivepull.sh
./run-gdrivepull.sh --help
```

The first run of the runner creates `.venv` and installs the dependencies.

## First run

```bash
./run-gdrivepull.sh
```

Files are downloaded into `~/GDrive` by default; use `--download-path PATH` for
another folder, or press `F` in the tree to change it for the session. The
folder is created if it does not exist. It must be:

- new;
- empty; or
- an existing GDrive Pull folder containing
  `.gdrivepull-managed-state.json`.

A non-empty folder without that state file is refused to prevent accidental
adoption or overwriting of unrelated data. The managed state stays inside the
download folder.

GDrive Pull then signs in to Google Drive. Your browser opens the Google
sign-in page; if it does not, the terminal shows a clickable link. The session
is kept in `token.json` and renewed automatically; when it has expired or been
revoked, the browser sign-in comes back.

## Options

| Option | Effect |
| --- | --- |
| `--download-path PATH` | Download folder (default: `~/GDrive`); new, empty or already managed |
| `--folder-id ID` | Browse this Drive folder instead of the root of My Drive |
| `--again` | Download the previous selection again, without the tree |
| `--yes` | Apply the preview without asking for confirmation, then exit after the download |
| `--keep-both` | Save the Drive version of conflicts next to the local file, as `name (Drive).ext` |
| `--removed` | List the files removed from Drive that downloads set aside, then exit (old name: `--recovery`) |
| `--empty-removed` | Delete the set-aside files for good, after typing `empty` (no prompt with `--yes`), then exit (old name: `--empty-recovery`) |
| `--older-than DAYS` | With `--empty-removed`: only delete files set aside more than `DAYS` days ago |
| `--jobs N` | Files downloaded at the same time, 1 to 16 (default: 4) |
| `--verbose` | Show sign-in, token and API details, and list unchanged items in the preview |

## Interactive selector

The tree supports keyboard and mouse navigation. Press `?` for the same key
help, or run `./run-gdrivepull.sh --help`.

All the keys are listed in [Keys](#keys).

After a download, the tree comes back with the result on top, the marks up to
date and nothing selected; the view, the sort and the open folders are kept.
Pick something else, or quit with `q` / `Esc`. With `--yes` or `--again`,
GDrive Pull downloads once and exits.

Folders show their number of files and their total size; files show their size,
their modification date (time only for today), and the local format of
Google-native files (`→ .docx`). These details are aligned in columns. Items that cannot be downloaded, such as Google
Forms, are greyed out. Selecting a parent folder includes its complete subtree
and replaces the child selections. The line under the tree counts the selected
items, the files they contain and their size.

A mark before the name shows what earlier downloads left in the destination,
read from the managed state without hashing any file:

| Mark | Meaning |
| --- | --- |
| `✓` | Downloaded and up to date (for a folder: every file in it) |
| `↻` | Changed on Drive since the download, or a folder with new files |
| `✎` | Changed locally since the download; the preview will keep it as a conflict |

Changing the folder with `F` updates the marks for the new destination.

### Views

`Tab` switches between My Drive, Shared with me (the items others shared with
you), each shared drive you are a member of, Trash (what you moved to the
Drive trash), and Removed from Drive (local files set aside, see below). A view is loaded the first time
it is shown; the tabs show how many items are selected in each. Selections are
kept across views and downloaded together:

```text
<download folder>/                     My Drive
<download folder>/Shared with me/      Shared with me
<download folder>/Shared drives/Team/  the shared drive "Team"
<download folder>/Trash/               Trash
```

### Drive trash

Google Drive changes only on request, always after a confirmation:

| Where | Key | On Drive |
| --- | --- | --- |
| Any view but Trash | `x` | Move the selection, or the item under the cursor, to the Drive trash |
| Trash | `r` | Restore the selection, or the item under the cursor, where it was |
| Trash | `x` | Delete forever, after typing `delete` |
| Trash | `shift+t` | Empty the whole Drive trash, after typing `empty` |

After a change, the views are reloaded from Drive and the selection is cleared.
The Trash view shows what you trashed yourself; what was inside a trashed
folder shows below it. Items can be downloaded from there too.

This needs full Drive access, like GMAIL needs full Gmail access: the first
run after an update from a read-only version asks you to sign in again and
allow it. Downloads themselves never change anything on Drive.

With `--folder-id`, only that folder is shown.

Folders always come first. Sorted by size, a folder counts the size of
everything below it; sorted by date, its newest file. Items without a size or
date come last. The sort applies to every view and is shown under the tree.

The filter keeps the matching items, their parent folders, and everything
below a matching folder. Selections hidden by the filter are kept.

## Keys

Each screen shows its keys at the bottom, grouped on several lines; `?` in the
tree and `./run-gdrivepull.sh --help` list them all with these descriptions.

### In the tree

| Key | Action |
| --- | --- |
| `↑` `↓` | Move |
| `←` `→` | Collapse / expand a folder, or go to the parent folder / first child |
| `enter` | Collapse / expand a folder |
| `e` | Expand the whole view; again: collapse it all (shift+space: below the cursor) |
| `pgup` `pgdn` | Scroll a page (home / end: top / bottom) |
| `tab` | Next view: My Drive, Shared with me, each shared drive, Trash, Removed from Drive |
| `shift+tab` | Previous view |
| `/` | Filter by name or path |
| `z` | Sort by size, biggest first; again: by name |
| `m` | Sort by date, newest first; again: by name |
| `space` | Select / unselect (a selected folder includes everything below it) |
| `a` | Select all, or unselect all when everything is selected (with a filter: the matches) |
| `c` | Unselect everything, including items hidden by the filter or in other views |
| `x` | Move the selection (or the item under the cursor) to the Drive trash |
| `d` | Compare with the download folder and open the preview |
| `f` | Change the download folder for this session |
| `o` | Open the item under the cursor in Google Drive |
| `?` | Show the key help |
| `q` `esc` | Quit (esc first closes the filter); after a download, the tree comes back |

### In the Trash view

| Key | Action |
| --- | --- |
| `↑` `↓` | Move |
| `←` `→` | Collapse / expand a folder, or go to the parent folder / first child |
| `enter` | Collapse / expand a folder |
| `e` | Expand the whole view; again: collapse it all (shift+space: below the cursor) |
| `pgup` `pgdn` | Scroll a page (home / end: top / bottom) |
| `tab` | Next view: My Drive, Shared with me, each shared drive, Trash, Removed from Drive |
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
| `d` | Compare with the download folder and open the preview |
| `f` | Change the download folder for this session |
| `o` | Open the item under the cursor in Google Drive |
| `?` | Show the key help |
| `q` `esc` | Quit (esc first closes the filter); after a download, the tree comes back |

### In Removed from Drive

| Key | Action |
| --- | --- |
| `↑` `↓` | Move (pgup / pgdn, home / end: by page, to the top / bottom) |
| `tab` | Next view |
| `shift+tab` | Previous view |
| `r` | Put the file back at its place in the download folder (never over another file) |
| `x` | Delete the file under the cursor from your disk, for good |
| `shift+x` | Delete every set-aside file, after typing empty |
| `o` | Open the folder holding the set-aside files in your file manager |
| `d` | Compare the selection from the other views and open the preview |
| `f` | Change the download folder for this session |
| `?` | Show the key help |
| `q` `esc` | Quit |

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
| `b` | Keep both versions of the conflict under the cursor: the Drive one as 'name (Drive).ext' |
| `shift+b` | Keep both for every conflict; again: undo |
| `y` | Download: apply the preview |
| `esc` `n` | Back to the tree, selection kept |

### In the preview of --again

| Key | Action |
| --- | --- |
| `↑` `↓` | Move (pgup / pgdn, home / end: by page, to the top / bottom) |
| `tab` | Next filter tab |
| `shift+tab` | Previous filter tab |
| `b` | Keep both versions of the conflict under the cursor |
| `shift+b` | Keep both for every conflict; again: undo |
| `y` | Download: apply the preview |
| `esc` `n` `q` | Cancel: nothing is downloaded |

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
| `y` | Yes (or type the word asked, then enter) |
| `n` `esc` | No: nothing changes |

### In this help

| Key | Action |
| --- | --- |
| `↑` `↓` | Scroll |
| `esc` `?` `q` | Close (enter or a click too) |

Nothing changes on disk before the preview is confirmed. The mouse works too: click to move, click a folder's arrow to unfold it.

## Preview and synchronization behavior

`D` compares the selection with the download folder and opens the preview:
the plan, a summary by status, the amount to download next to the free disk
space, and notes about conflicts and files removed from Drive. Nothing has
changed on disk yet.

Google Docs, Sheets and Slides have no size before export: they are counted
apart. When the known size exceeds the free space, the line turns red; you can
still go back and select less.

Keys: `Y` downloads, `Tab` / `Shift+Tab` switch the filter tabs (Changes, Everything,
then one tab per status), `B` keeps both versions of a conflict (`Shift+B`: all),
`Esc` goes back to the tree. All of them are in [Keys](#keys).

When nothing needs to change, `Y` only saves the state. With `--yes`, the
preview is printed and applied without asking.

| Status | Meaning | Action |
| --- | --- | --- |
| `new` | The item does not exist locally | Download or create |
| `update` | Drive changed and the local copy still matches the previous state | Replace atomically |
| `unchanged` | Drive and local content match | Skip |
| `conflict` | Local content changed or cannot be matched safely | Preserve and skip |
| `removed from Drive` | A downloaded, unchanged local file was removed from Drive | Set aside (see below) |
| `local only` | The local item is unknown to GDrive Pull | Preserve and skip |
| `keep both` | A conflict whose Drive version is saved next to the local file | Download as `name (Drive).ext` |
| `skipped` | The Drive format is unsupported, or a shortcut loops back to a parent folder | Skip |

GDrive Pull never permanently deletes local content on its own.

The Drive copies saved by keep both (`name (Drive).ext`, or `(Drive 2)`... when
taken) are not tracked: compare them with your file, keep the one you want, and
the next run sees the result. Two Drive files with the same name in a folder
are conflicts too; keep both saves each of them.

### Removed from Drive

When a file you downloaded is removed from Drive (from the web, your phone or
the `x` key), the next download of its folder does not delete your copy: it
sets it aside, so the download folder stays a mirror of Drive and nothing is
lost. A file you changed locally is never set aside: it stays as a conflict.

The **Removed from Drive** tab, the last one, lists these files with the date
they were set aside and their place in the download folder; the tab shows how
many there are. There:

| Key | Action |
| --- | --- |
| `r` | Put the file back at its place in the download folder, never over another file. GDrive Pull leaves it alone from then on: it shows as local only |
| `x` | Delete the file from your disk, for good, after `y` |
| `shift+x` | Delete every set-aside file, after typing `empty` |
| `o` | Open the folder that holds them in your file manager |

On disk they live in `.gdrivepull-recovery/<date>/` inside the download folder
(hidden, so it never mixes with your Drive content). This is not the Drive
trash: these files are only on your disk. From the command line:

```bash
./run-gdrivepull.sh --removed                                 # list them
./run-gdrivepull.sh --empty-removed                           # delete them all, after typing "empty"
./run-gdrivepull.sh --empty-removed --older-than 30 --yes     # e.g. from cron
```

These commands work on the download folder (`--download-path`) and need no
sign-in.

Files are downloaded in parallel (`--jobs`, 4 by default), each thread with its
own Drive connection. The progress bar shows the files done, the amount
downloaded and the speed, with one line per file. If a download fails, the
file keeps its previous content and the run ends with an error count. The state
is saved even after `Ctrl+C`, so the next run picks up where it stopped.

## Downloading the same selection again

Each confirmed run stores its selection in the managed state of the
destination. To download it again without the tree:

```bash
./run-gdrivepull.sh --again          # preview, then confirm
./run-gdrivepull.sh --again --yes    # no prompt, e.g. from cron
```

Each run also saves the folder listings it used and a Drive changes token in
`.gdrivepull-remote-cache.json`, in the download folder. `--again` replays the
Drive changes since then instead of listing every folder again, so it stays fast
on a large Drive; if the token has expired, the folders are listed again.

Selected items are looked up again on Drive, so renamed or updated files are
picked up. When a whole view was selected (every item at its top level), new
items in it are downloaded too. A selected item that is no longer on Drive is
reported, and its local files are handled like other files removed from Drive.

`--again` shows the same preview; with `--yes`, or without a terminal, it is
printed instead.

## Google-native file exports

Google-native files are exported as follows:

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
./run-gdrivepull.sh --folder-id DRIVE_FOLDER_ID
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

The download folder contains:

```text
.gdrivepull-managed-state.json
.gdrivepull-remote-cache.json
```

The state file also keeps the last selection, for `--again`.

The `.gdrivepull-recovery/` directory (Removed from Drive) is created only when tracked local
content removed from Drive needs to be preserved.

## Troubleshooting

- **Missing `credentials.json`**: GDrive Pull says where to put it; see
  [Google API setup](#google-api-setup).
- **Sign-in keeps failing**: remove `token.json` and run again.
- **A file stays in `conflict`**: it was changed locally or already existed
  with different content. Move or rename the local copy, then run again.
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

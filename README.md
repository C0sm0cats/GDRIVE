# GDrive Pull

GDrive Pull is an interactive, read-only Google Drive downloader for Linux and
other POSIX environments. It lets you select files and folders from a colored,
collapsible terminal tree, previews every local change, and protects existing
data from silent overwrites or deletions.

## Features

- Colored Textual interface with expandable folders and multi-selection
- Recursive downloads that preserve the complete Drive directory structure
- Selection of individual files, nested folders, or an entire Drive tree
- Explicit preview with `NEW`, `UPDATE`, `UNCHANGED`, `CONFLICT`,
  `REMOVED_REMOTE`, `LOCAL_ONLY`, and `SKIPPED` statuses
- Atomic file replacement through temporary `.part` files
- Google-native document export to local formats
- Drive shortcuts followed to their target files and folders
- Local change detection through checksums and a managed state file
- Recovery of tracked files removed from Drive instead of permanent deletion
- Preservation of unknown local files and folders
- Read-only Google Drive OAuth scope

## Repository contents

The published repository contains four files:

```text
GDRIVE/
├── LICENSE
├── README.md
├── gdrivepull.py
└── run-gdrivepull.sh
```

Runtime files such as OAuth credentials, tokens, settings, and the virtual
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
```

## First run

```bash
./run-gdrivepull.sh
```

The first run opens a destination setup interface. Choose:

- a parent directory;
- the name of the managed download folder.

The final managed folder must be:

- new;
- empty; or
- an existing GDrive Pull folder containing
  `.gdrivepull-managed-state.json`.

Press `Ctrl+S` to save the destination or `Esc` to cancel.

A non-empty folder without that state file is refused to prevent accidental
adoption or overwriting of unrelated data.

The selected absolute path is stored locally in `settings.json`, next to the
runner. The managed state remains inside the selected destination.

## Interactive selector

The Google Drive selector supports keyboard and mouse navigation.

| Key | Action |
| --- | --- |
| `Up` / `Down` | Move through the tree |
| `Left` / `Right` | Navigate folder levels |
| `Enter` | Expand or collapse a folder |
| `Space` | Select or unselect an item |
| `A` | Select all |
| `C` | Clear the selection |
| `D` | Continue to the download preview |
| `Esc` | Cancel |

Folders are displayed in cyan and files in green and white. Selecting a parent
folder includes its complete subtree and avoids duplicate child selections.

## Preview and synchronization behavior

Before changing local files, GDrive Pull displays the complete operation plan
and asks for confirmation.

| Status | Meaning | Default action |
| --- | --- | --- |
| `NEW` | The item does not exist locally | Download or create |
| `UPDATE` | Drive changed and the local copy still matches the previous state | Replace atomically |
| `UNCHANGED` | Drive and local content match | Skip |
| `CONFLICT` | Local content changed or cannot be matched safely | Preserve and skip |
| `REMOVED_REMOTE` | A tracked, unchanged local file was removed from Drive | Move to recovery |
| `LOCAL_ONLY` | The local item is unknown to GDrive Pull | Preserve and skip |
| `SKIPPED` | The Drive format is unsupported | Skip |

Tracked files removed from Drive are moved to:

```text
<managed-destination>/.gdrivepull-recovery/<timestamp>/
```

GDrive Pull never permanently deletes unknown local content.

## Google-native file exports

Google-native files are exported as follows:

| Drive format | Local format |
| --- | --- |
| Google Docs | `.docx` |
| Google Sheets | `.xlsx` |
| Google Slides | `.pptx` |
| Google Drawings | `.pdf` |
| Google Apps Script | `.json` |

## Changing the destination

Run:

```bash
./run-gdrivepull.sh --configure
```

The previous destination and its managed state remain untouched.

## Optional Drive folder

By default, the selector displays the root of My Drive. An explicit Drive folder
ID can be used as the remote root:

```bash
./run-gdrivepull.sh --folder-id DRIVE_FOLDER_ID
```

## Local files

These files and directories are used or created locally and must not be
published:

```text
.venv/
credentials.json
token.json
settings.json
```

The managed destination contains:

```text
.gdrivepull-managed-state.json
```

The `.gdrivepull-recovery/` directory is created only when tracked local
content removed from Drive needs to be preserved.

## License

This project is licensed under the terms of the [LICENSE](LICENSE) file.

## Activity

![Repository activity](https://repobeats.axiom.co/api/embed/25976cb32f4e20b563591e534b83230e6b3d61f8.svg "Repobeats analytics image")

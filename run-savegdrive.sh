#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

# .venv/bin/python links to the system Python. After a minor upgrade of it
# (3.13 -> 3.14) that interpreter looks for a lib/python3.14 the venv lacks, so
# nothing imports and pip is gone: rebuild the venv instead of failing forever.
if [ -e "$script_dir/.venv" ] && ! "$script_dir/.venv/bin/python" -c \
    'import os, sys, sysconfig; sys.exit(not os.path.isdir(sysconfig.get_path("purelib")))' >/dev/null 2>&1; then
    echo "SaveGDrive: .venv does not match the installed Python any more; rebuilding it." >&2
    rm -rf "$script_dir/.venv"
fi

if [ ! -x "$script_dir/.venv/bin/python" ]; then
    if command -v python3 >/dev/null 2>&1; then
        python=python3
    elif command -v python >/dev/null 2>&1; then
        python=python
    else
        echo "SaveGDrive needs Python 3.11 or newer, and neither python3 nor python was found." >&2
        exit 1
    fi
    if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
        echo "SaveGDrive needs Python 3.11 or newer; $python is $("$python" -V 2>&1)." >&2
        exit 1
    fi
    "$python" -m venv "$script_dir/.venv"
fi

# The stamp is written only after a successful install. The dependencies are
# upgraded (transitive ones too) when it is missing, lists other packages, or
# is more than 30 days old, so an old .venv does not keep stale versions.
packages="google-api-python-client google-auth google-auth-oauthlib send2trash textual"
stamp="$script_dir/.venv/.savegdrive-deps"
stamp_packages=
if [ -f "$stamp" ]; then
    IFS= read -r stamp_packages < "$stamp" || :
fi
imports_ok=1
"$script_dir/.venv/bin/python" -c 'import googleapiclient, google_auth_oauthlib, send2trash, textual' >/dev/null 2>&1 || imports_ok=0

if [ "$imports_ok" = 0 ] || [ "$stamp_packages" != "$packages" ] \
    || [ -n "$(find "$stamp" -mtime +30 2>/dev/null)" ]; then
    # shellcheck disable=SC2086
    if "$script_dir/.venv/bin/python" -m pip install -U --upgrade-strategy eager $packages; then
        printf '%s\n' "$packages" > "$stamp"
    elif [ "$imports_ok" = 1 ]; then
        echo "SaveGDrive could not update its Python packages; it will try again next run." >&2
    else
        exit 1
    fi
fi

unset NO_COLOR

exec "$script_dir/.venv/bin/python" "$script_dir/savegdrive.py" "$@"

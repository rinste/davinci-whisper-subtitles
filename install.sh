#!/usr/bin/env bash
# Whisper Subtitles for DaVinci Resolve - installer.
#
#   ./install.sh            install (or update) the plugin
#   ./install.sh --uninstall remove it from Resolve, leave this folder alone
#
# It never touches your Resolve projects: it only creates a virtualenv here and
# drops a symlink in Resolve's Scripts folder.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
LINK="$SCRIPTS/Whisper Subtitles.py"
MARKER="$HOME/.whisper-subtitles-resolve"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$LINK" "$MARKER"
  say "Removed the plugin from Resolve."
  echo
  echo "Still on disk, delete them yourself if you want the space back:"
  echo "  rm -rf \"$REPO/.venv\""
  # NOTE: never suggest deleting ~/.cache/huggingface wholesale - other tools keep
  # their models in there too. Only the faster-whisper ones belong to us.
  models=$(ls -d "$HOME"/.cache/huggingface/hub/models--Systran--faster-whisper-* 2>/dev/null || true)
  if [ -n "$models" ]; then
    echo "  # the Whisper models this plugin downloaded:"
    while IFS= read -r m; do
      echo "  rm -rf \"$m\"    # $(du -sh "$m" 2>/dev/null | cut -f1)"
    done <<< "$models"
    echo "  # (leave the rest of ~/.cache/huggingface alone: other apps use it)"
  fi
  exit 0
fi

[ "$(uname)" = "Darwin" ] || die "This installer is for macOS. On Windows/Linux, see the README."

say "Whisper Subtitles for DaVinci Resolve"
echo "Repository: $REPO"
echo

# --- prerequisites -------------------------------------------------------------------
command -v python3 >/dev/null || die "python3 not found. Install it from python.org or with: brew install python"
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' \
  || die "Python 3.9 or newer is required (found $PYV)."
echo "  python3 $PYV"

if ! command -v ffmpeg >/dev/null; then
  warn "  ffmpeg not found - it is required to read the audio."
  if command -v brew >/dev/null; then
    read -r -p "  Install it now with Homebrew? [y/N] " reply
    [ "${reply:-n}" = "y" ] && brew install ffmpeg || die "Install ffmpeg, then run this again."
  else
    die "Install ffmpeg (brew install ffmpeg), then run this again."
  fi
fi
echo "  ffmpeg $(ffmpeg -version | head -1 | awk '{print $3}')"

[ -d "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve" ] \
  || warn "  DaVinci Resolve does not look installed - carrying on anyway."

# --- virtualenv ----------------------------------------------------------------------
echo
say "Setting up the virtualenv (this downloads ~150 MB the first time)"
[ -d "$REPO/.venv" ] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install --quiet --upgrade pip
"$REPO/.venv/bin/pip" install --quiet -r "$REPO/requirements.txt"
echo "  faster-whisper $("$REPO/.venv/bin/python" -c 'import faster_whisper; print(faster_whisper.__version__)')"

# --- install into Resolve ------------------------------------------------------------
echo
say "Installing into Resolve"
mkdir -p "$SCRIPTS"
ln -sfn "$REPO/Whisper Subtitles.py" "$LINK"
printf '%s\n' "$REPO" > "$MARKER"
echo "  $LINK"

cat <<DONE

Done. One thing left to do by hand:

  Restart DaVinci Resolve.

Resolve enumerates the Scripts menu at startup, so a freshly installed plugin only
shows up after a restart. Then open it from:

  Workspace > Scripts > Utility > Whisper Subtitles

The first run downloads the Whisper model (~3 GB for large-v3, less for the smaller
ones) and caches it; later runs reuse it. Logs go to ~/whisper_subtitles.log
DONE

#!/usr/bin/env bash
# =============================================================================
# refresh.sh -- rebuild rfp_data.json for the Texas Procurement Portal.
#
# Runs tx_rfp_scraper.py with sensible defaults: pulls the ESBD feed, reads
# saved portal pages from ./html, and (optionally) the URLs in ./portals.txt.
# The previous rfp_data.json is backed up before each run.
#
# USAGE
#   ./refresh.sh              ESBD + saved html/ pages + portals.txt URLs
#   ./refresh.sh --render     also render live URLs in a headless browser
#                             (needs Playwright; slow -- can take 15+ min)
#   ./refresh.sh --fast       ESBD + saved html/ pages only (skip URL list)
#   ./refresh.sh --no-esbd    skip the ESBD fetch (offline / portals only)
#   ./refresh.sh --help       show this help
#
# Flags combine, e.g.:  ./refresh.sh --fast --no-esbd   (fully offline)
#
# FIRST RUN:  chmod +x refresh.sh   then   ./refresh.sh
# =============================================================================

set -euo pipefail

# --- run from the script's own directory, so it works from anywhere ---------
cd "$(dirname "$0")"

# --- configuration (edit if your filenames differ) --------------------------
SCRAPER="tx_rfp_scraper.py"
PORTALS="portals.txt"
HTML_DIR="html"
OUT="rfp_data.json"
BACKUP_DIR="backups"
KEEP_BACKUPS=10

# --- parse options ----------------------------------------------------------
RENDER=""
ESBD_OFF=""
MODE="standard"
for arg in "$@"; do
  case "$arg" in
    --render)  RENDER="yes" ;;
    --fast)    MODE="fast" ;;
    --no-esbd) ESBD_OFF="yes" ;;
    --help|-h)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      echo "Unknown option: $arg   (try ./refresh.sh --help)" >&2
      exit 1 ;;
  esac
done

# --- activate a virtualenv if one is present --------------------------------
for v in venv .venv env; do
  if [ -f "$v/bin/activate" ]; then
    # shellcheck disable=SC1090
    source "$v/bin/activate"
    echo "Activated virtualenv: $v"
    break
  fi
done

# --- locate Python and the scraper ------------------------------------------
PY="python3"
command -v "$PY" >/dev/null 2>&1 || PY="python"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: Python not found. Install Python 3 from python.org." >&2
  exit 1
fi
if [ ! -f "$SCRAPER" ]; then
  echo "ERROR: $SCRAPER not found in $(pwd)" >&2
  exit 1
fi

# --- back up the previous data file -----------------------------------------
if [ -f "$OUT" ]; then
  mkdir -p "$BACKUP_DIR"
  STAMP="$(date +%Y%m%d_%H%M%S)"
  cp "$OUT" "$BACKUP_DIR/rfp_data_$STAMP.json"
  echo "Backed up previous $OUT -> $BACKUP_DIR/rfp_data_$STAMP.json"
  # keep only the most recent $KEEP_BACKUPS backups
  ( ls -1t "$BACKUP_DIR"/rfp_data_*.json 2>/dev/null || true ) \
    | tail -n "+$((KEEP_BACKUPS + 1))" \
    | while read -r old; do [ -n "$old" ] && rm -f "$old"; done
fi

# --- build the scraper command ----------------------------------------------
CMD=("$PY" "$SCRAPER" --out "$OUT")

if [ -n "$ESBD_OFF" ]; then
  CMD+=(--no-esbd)
fi
if [ -d "$HTML_DIR" ]; then
  COUNT="$(find "$HTML_DIR" -maxdepth 1 -iname '*.html' | wc -l | tr -d ' ')"
  CMD+=(--portal-dir "$HTML_DIR")
  echo "Using saved pages in ./$HTML_DIR ($COUNT html file(s))"
else
  echo "No ./$HTML_DIR folder -- skipping saved pages."
fi
if [ "$MODE" != "fast" ] && [ -f "$PORTALS" ]; then
  CMD+=(--portal-list "$PORTALS")
  echo "Using URL manifest ./$PORTALS"
elif [ "$MODE" = "fast" ]; then
  echo "Fast mode -- skipping the ./$PORTALS URL list."
fi
if [ -n "$RENDER" ]; then
  CMD+=(--render)
  echo "Headless rendering ON -- this can take 15+ minutes."
fi

# --- run --------------------------------------------------------------------
echo
echo "Running: ${CMD[*]}"
echo "-----------------------------------------------------------------------"
"${CMD[@]}"
echo "-----------------------------------------------------------------------"
echo
echo "Done.  Output: $(pwd)/$OUT"
echo "Load it in the console's \"Open Procurements\" tab."

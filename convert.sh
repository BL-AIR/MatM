#!/bin/bash
# ============================================================
#  MatM Audio Batch Converter
#  Converts broadcast MP3s to web-streaming formats and
#  generates VTT caption files + plain-text transcripts from
#  any matching SRT files found in the Scripts folder.
#
#  Outputs per episode (into the streaming/ folder):
#    MatM_XXXX.webm  — Opus audio (primary, ~48 kbps)
#    MatM_XXXX.m4a   — AAC audio  (Safari/iOS fallback, ~64 kbps)
#    MatM_XXXX.vtt   — WebVTT captions (if SRT exists)
#    MatM_XXXX.txt   — Plain-text transcript (if SRT exists)
#
#  Safe to re-run: already-converted files are skipped.
# ============================================================

SOURCE="/Users/blair/Library/Mobile Documents/com~apple~CloudDocs/MatM"
SCRIPTS="$SOURCE/Scripts"
OUTPUT="/Users/blair/Library/Mobile Documents/com~apple~CloudDocs/MatM/MatM/streaming"

# ── Preflight check ─────────────────────────────────────────
if ! command -v ffmpeg &>/dev/null; then
  echo ""
  echo "  ERROR: ffmpeg is not installed."
  echo ""
  echo "  Install it with Homebrew by running:"
  echo "    brew install ffmpeg"
  echo ""
  echo "  If you don't have Homebrew, install it first:"
  echo "    /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  echo ""
  exit 1
fi

# ── Setup ───────────────────────────────────────────────────
mkdir -p "$OUTPUT"

total=0
converted=0
with_srt=0

echo ""
echo "  MatM Audio Batch Converter"
echo "  ────────────────────────────────────────────────────"
echo "  Source : $SOURCE"
echo "  Scripts: $SCRIPTS"
echo "  Output : $OUTPUT"
echo "  ────────────────────────────────────────────────────"
echo ""

# ── Process each MP3 (source folder only, no subfolders) ────
for mp3 in "$SOURCE"/*.mp3; do
  [ -f "$mp3" ] || continue

  filename=$(basename "$mp3" .mp3)
  total=$((total + 1))

  echo "  ▶  $filename"

  # 1. Opus in WebM container (primary streaming format)
  opus_out="$OUTPUT/${filename}.webm"
  if [ ! -f "$opus_out" ]; then
    ffmpeg -i "$mp3" -c:a libopus -b:a 48k -vn -y "$opus_out" -loglevel error 2>&1
    echo "     ✓ Opus  → ${filename}.webm"
  else
    echo "     — Opus  already exists, skipping"
  fi

  # 2. AAC in M4A container (Safari / iOS fallback)
  aac_out="$OUTPUT/${filename}.m4a"
  if [ ! -f "$aac_out" ]; then
    ffmpeg -i "$mp3" -c:a aac -b:a 64k -vn -y "$aac_out" -loglevel error 2>&1
    echo "     ✓ AAC   → ${filename}.m4a"
  else
    echo "     — AAC   already exists, skipping"
  fi

  # 3. SRT → VTT + plain text (only if a matching SRT exists)
  srt_file="$SCRIPTS/${filename}.srt"
  if [ -f "$srt_file" ]; then
    with_srt=$((with_srt + 1))

    # WebVTT: same as SRT but with a WEBVTT header and
    # timestamp millisecond separator changed from comma to period
    vtt_out="$OUTPUT/${filename}.vtt"
    {
      printf "WEBVTT\n\n"
      sed 's/\([0-9][0-9]:[0-9][0-9]:[0-9][0-9]\),\([0-9][0-9][0-9]\)/\1.\2/g' "$srt_file"
    } > "$vtt_out"
    echo "     ✓ VTT   → ${filename}.vtt"

    # Plain text: strip sequence numbers, timestamps, and blank lines
    txt_out="$OUTPUT/${filename}.txt"
    grep -v '^[[:space:]]*[0-9][0-9]*[[:space:]]*$' "$srt_file" \
      | grep -v '^[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9]' \
      | sed '/^[[:space:]]*$/d' \
      | tr '\n' ' ' \
      | sed 's/  */ /g; s/^ //; s/ $//' \
      > "$txt_out"
    echo "     ✓ Text  → ${filename}.txt"
  fi

  converted=$((converted + 1))
  echo ""
done

# ── Summary ─────────────────────────────────────────────────
echo "  ────────────────────────────────────────────────────"
echo "  Finished."
echo "  Episodes processed : $converted of $total"
echo "  With SRT/VTT/text  : $with_srt"
echo "  Output folder      : $OUTPUT"
echo "  ────────────────────────────────────────────────────"
echo ""

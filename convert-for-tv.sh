#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# convert-for-tv.sh — make videos playable on a Geant GN-RS8 EVO / generic TV
#
# Converts every video to H.264 + AAC in MP4, 8-bit yuv420p, max 1080p,
# with faststart. Optionally converts sidecar subtitles to .srt (default)
# or embeds the first subtitle.
#
# Usage:
#   ./convert-for-tv.sh [options] <folder-or-files...>
#
# Output: ./TV/<same-title>.mp4, plus ./TV/<same-title>.srt
#
# Examples:
#   ./convert-for-tv.sh ~/Downloads/ytdpro/Playlists/"English Stories"
#   ./convert-for-tv.sh ~/Videos -o /media/USB/TV --max-height 720 --subs
#   ./convert-for-tv.sh ~/Videos --no-subs --overwrite
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

OUT_DIR="TV"
DO_SUBS=1
DO_EMBED=0
DEFAULT_SUB=1
DEFAULT_LANG=""
MAX_HEIGHT=1080
CRF=20
PRESET="medium"
FPS=0
OVERWRITE=0
DRY_RUN=0
INPUTS=()

VIDEO_EXTS=(mp4 mkv webm avi mov m4v ts mpg mpeg m2ts)
SUB_EXTS=(vtt srt ass ssa smi ttml)

die()  { printf '\n\033[31m✘ %s\033[0m\n' "$*" >&2; exit 1; }
info() { printf '\033[36m➜ %s\033[0m\n' "$*"; }
ok()   { printf '\033[32m✔ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m⚠ %s\033[0m\n' "$*"; }

usage() {
  cat <<'EOF'
convert-for-tv.sh — convert a playlist folder to TV-friendly MP4

Usage:
  ./convert-for-tv.sh [options] <folder-or-files...>

Options:
  -o, --output DIR        output folder (default: ./TV)
      --subs              convert subtitles to .srt sidecars (default)
      --no-subs           do not process subtitles
      --embed             embed the first subtitle into the MP4 instead
      --no-default-sub    do not also create <title>.srt
      --default-lang L    prefer this language for the default <title>.srt
  -H, --max-height N      maximum height, no upscaling (default: 1080)
      --crf N             H.264 quality, lower = better/bigger (default: 20)
      --preset NAME       x264 preset: ultrafast..veryslow (default: medium)
      --fps N             output frame rate, 0 = keep source (default: 0)
  -y, --overwrite         overwrite existing files in the output folder
      --dry-run           print what would be done, convert nothing
  -h, --help              show this help

Examples:
  ./convert-for-tv.sh ~/Downloads/ytdpro/Playlists/"English Stories"
  ./convert-for-tv.sh ~/Videos -o /media/USB/TV --max-height 720
  ./convert-for-tv.sh ~/Videos --embed --default-lang ar
EOF
}

# ── Parse arguments ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o|--output)        [[ $# -ge 2 ]] || die "Missing value for $1"; OUT_DIR="$2"; shift 2 ;;
    --subs)             DO_SUBS=1; DO_EMBED=0; shift ;;
    --no-subs)          DO_SUBS=0; DO_EMBED=0; shift ;;
    --embed)            DO_EMBED=1; DO_SUBS=0; shift ;;
    --no-default-sub)   DEFAULT_SUB=0; shift ;;
    --default-lang)     [[ $# -ge 2 ]] || die "Missing value for $1"; DEFAULT_LANG="$2"; shift 2 ;;
    -H|--max-height)    [[ $# -ge 2 ]] || die "Missing value for $1"; MAX_HEIGHT="$2"; shift 2 ;;
    --crf)              [[ $# -ge 2 ]] || die "Missing value for $1"; CRF="$2"; shift 2 ;;
    --preset)           [[ $# -ge 2 ]] || die "Missing value for $1"; PRESET="$2"; shift 2 ;;
    --fps)              [[ $# -ge 2 ]] || die "Missing value for $1"; FPS="$2"; shift 2 ;;
    -y|--overwrite)     OVERWRITE=1; shift ;;
    --dry-run)          DRY_RUN=1; shift ;;
    -h|--help)          usage; exit 0 ;;
    --)                 shift; INPUTS+=("$@"); break ;;
    -*)                 die "Unknown option: $1 (try --help)" ;;
    *)                  INPUTS+=("$1"); shift ;;
  esac
done

[[ ${#INPUTS[@]} -gt 0 ]] || { usage; die "No input folder or files given."; }
command -v ffmpeg  >/dev/null 2>&1 || die "ffmpeg is not installed."
command -v ffprobe >/dev/null 2>&1 || die "ffprobe is not installed."
[[ "$MAX_HEIGHT" =~ ^[0-9]+$ ]] || die "--max-height must be a number."
[[ "$FPS" =~ ^[0-9]+$ ]] || die "--fps must be a number (0 = keep source)."
[[ "$CRF" =~ ^[0-9]+$ ]] || die "--crf must be a number."

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

converted=0
skipped=0
failed=0
subs_done=0

# Map 2-letter language codes to the 3-letter tags MP4 likes.
lang3() {
  case "${1,,}" in
    ar) echo ara ;; en) echo eng ;; fr) echo fra ;; es) echo spa ;;
    de) echo deu ;; tr) echo tur ;; pt) echo por ;; ru) echo rus ;;
    hi) echo hin ;; zh) echo zho ;; ja) echo jpn ;; ko) echo kor ;;
    it) echo ita ;; nl) echo nld ;; pl) echo pol ;; id) echo ind ;;
    *)  echo "${1:-und}" ;;
  esac
}

# Find subtitle files that belong to <base>.
find_subs() {
  local dir="$1" base="$2" ext s
  FOUND_SUBS=()
  for ext in "${SUB_EXTS[@]}"; do
    for s in "$dir/$base.$ext" "$dir/$base".*."$ext"; do
      [[ -f "$s" ]] || continue
      [[ "$s" == "$OUT_DIR"/* ]] && continue
      FOUND_SUBS+=("$s")
    done
  done
  # de-duplicate
  if [[ ${#FOUND_SUBS[@]} -gt 1 ]]; then
    mapfile -t FOUND_SUBS < <(printf '%s\n' "${FOUND_SUBS[@]}" | sort -u)
  fi
}

# Convert one subtitle file to SRT. Sets LAST_SRT / LAST_LANG.
convert_sub() {
  local src="$1" base="$2" name stem ext lang out
  name="$(basename "$src")"
  stem="${name%.*}"
  ext="${name##*.}"
  if [[ "$stem" == "$base" ]]; then
    lang=""
  else
    lang="${stem#"$base".}"
  fi
  if [[ -n "$lang" ]]; then
    out="$OUT_DIR/$base.$lang.srt"
  else
    out="$OUT_DIR/$base.srt"
  fi
  LAST_SRT="$out"
  LAST_LANG="$lang"
  if [[ -f "$out" && $OVERWRITE -eq 0 ]]; then
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    info "[dry-run] subtitle: $src → $out"
    return 0
  fi
  if ffmpeg -hide_banner -nostdin -loglevel error -y -i "$src" -c:s srt "$out"; then
    subs_done=$((subs_done + 1))
    ok "subtitle: $(basename "$out")"
  else
    warn "subtitle failed: $src"
    return 1
  fi
}

# ── Walk every input ────────────────────────────────────────────────────────
for input in "${INPUTS[@]}"; do
  [[ -e "$input" ]] || { warn "Not found: $input"; continue; }

  files=()
  if [[ -d "$input" ]]; then
    while IFS= read -r -d '' f; do
      files+=("$f")
    done < <(find "$input" -maxdepth 1 -type f -print0 | sort -z)
  else
    files=("$input")
  fi

  for file in "${files[@]}"; do
    [[ -f "$file" ]] || continue
    ext="${file##*.}"; ext="${ext,,}"
    is_video=0
    for v in "${VIDEO_EXTS[@]}"; do [[ "$ext" == "$v" ]] && is_video=1 && break; done
    [[ $is_video -eq 1 ]] || continue
    abs_file="$(cd "$(dirname "$file")" && pwd)/$(basename "$file")"
    [[ "$abs_file" == "$OUT_DIR"/* ]] && continue

    dir="$(dirname "$file")"
    base="$(basename "${file%.*}")"
    out="$OUT_DIR/$base.mp4"

    printf '\n\033[1m▶ %s\033[0m\n' "$base"

    if [[ -f "$out" && $OVERWRITE -eq 0 ]]; then
      warn "exists, skipping: $(basename "$out")  (use --overwrite)"
      skipped=$((skipped + 1))
      continue
    fi

    # ── subtitles first (needed for --embed) ──────────────────────────────
    embed_sub=""
    embed_lang=""
    default_srt=""
    if [[ $DO_SUBS -eq 1 || $DO_EMBED -eq 1 ]]; then
      find_subs "$dir" "$base"
      if [[ ${#FOUND_SUBS[@]} -gt 0 ]]; then
        # Prefer --default-lang for the default sidecar/embedded track.
        ordered=("${FOUND_SUBS[@]}")
        if [[ -n "$DEFAULT_LANG" ]]; then
          preferred=()
          rest=()
          for s in "${ordered[@]}"; do
            n="$(basename "$s")"; st="${n%.*}"
            if [[ "$st" == "$base.$DEFAULT_LANG" || "$st" == "$base.$DEFAULT_LANG".* ]]; then
              preferred+=("$s")
            else
              rest+=("$s")
            fi
          done
          ordered=("${preferred[@]}" "${rest[@]}")
        fi

        if [[ $DO_EMBED -eq 1 ]]; then
          src="${ordered[0]}"
          if [[ $DRY_RUN -eq 0 ]]; then
            embed_sub="$TMP_DIR/embed.srt"
            if ! ffmpeg -hide_banner -nostdin -loglevel error -y -i "$src" -c:s srt "$embed_sub"; then
              warn "could not convert subtitle for embedding: $src"
              embed_sub=""
            fi
          fi
          n="$(basename "$src")"; st="${n%.*}"
          embed_lang="${st#"$base".}"; [[ "$st" == "$base" ]] && embed_lang=""
        fi

        if [[ $DO_SUBS -eq 1 ]]; then
          first_srt=""
          for s in "${ordered[@]}"; do
            convert_sub "$s" "$base" || continue
            srt="$LAST_SRT"
            [[ -n "$srt" ]] || continue
            [[ -z "$first_srt" ]] && first_srt="$srt"
            # If the file is already exactly <base>.srt, nothing left to do.
            [[ "$(basename "$srt")" == "$base.srt" ]] && default_srt="$srt"
          done
          if [[ $DEFAULT_SUB -eq 1 && -z "$default_srt" && -n "$first_srt" ]]; then
            if [[ $DRY_RUN -eq 0 ]]; then
              cp -f "$first_srt" "$OUT_DIR/$base.srt"
              ok "default subtitle: $(basename "$OUT_DIR/$base.srt")"
            else
              info "[dry-run] default subtitle → $OUT_DIR/$base.srt"
            fi
          fi
        fi
      else
        [[ $DO_EMBED -eq 1 ]] && warn "no subtitle found to embed"
        [[ $DO_SUBS -eq 1 ]] && info "no subtitles found"
      fi
    fi

    # ── ffmpeg command ────────────────────────────────────────────────────
    cmd=(ffmpeg -hide_banner -nostdin -loglevel error)
    [[ $OVERWRITE -eq 1 ]] && cmd+=(-y) || cmd+=(-n)
    cmd+=(-i "$file")
    if [[ $DO_EMBED -eq 1 && -n "$embed_sub" ]]; then
      cmd+=(-i "$embed_sub")
      cmd+=(-map 0:v:0 -map 0:a:0? -map 1:0)
      cmd+=(-c:s mov_text -metadata:s:s:0 "language=$(lang3 "$embed_lang")")
    else
      cmd+=(-map 0:v:0 -map 0:a:0?)
    fi
    cmd+=(
      -vf "scale=w='min(1920,iw)':h='min($MAX_HEIGHT,ih)':force_original_aspect_ratio=decrease:flags=lanczos,format=yuv420p"
      -c:v libx264 -profile:v main -level 4.0 -preset "$PRESET" -crf "$CRF"
    )
    [[ "$FPS" -gt 0 ]] && cmd+=(-r "$FPS")
    cmd+=(
      -c:a aac -b:a 160k -ar 48000 -ac 2
      -movflags +faststart
      "$out"
    )

    if [[ $DRY_RUN -eq 1 ]]; then
      printf '  \033[2m%s\033[0m\n' "${cmd[*]}"
      converted=$((converted + 1))
      continue
    fi

    if "${cmd[@]}"; then
      converted=$((converted + 1))
      ok "$(basename "$out") ($(du -h "$out" | cut -f1))"
      # FAT32 warning
      size=$(stat -c%s "$out" 2>/dev/null || echo 0)
      if (( size > 4294967296 )); then
        warn "file is larger than 4 GB — it will not fit on FAT32 USB"
      fi
    else
      failed=$((failed + 1))
      warn "conversion failed: $file"
    fi
  done
done

printf '\n\033[1mSummary\033[0m\n'
printf '  converted : %d\n' "$converted"
printf '  subtitles : %d\n' "$subs_done"
printf '  skipped   : %d\n' "$skipped"
printf '  failed    : %d\n' "$failed"
printf '  output    : %s\n\n' "$OUT_DIR"

[[ $failed -eq 0 ]] || exit 1

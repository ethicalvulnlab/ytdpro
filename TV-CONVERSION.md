# Put ytdpro videos on a TV / USB drive

Tested target: **Geant GN-RS8 EVO** (GX6605S) and similar satellite/TV media
players. The same recipe works for most TVs, DVD players and USB media boxes.

The receiver supports **MP4, MKV, TS, TP, TRP, MPG, AVI** containers and
**H.264 / MPEG-2 / MPEG-4** video, with **MP3 / AAC** audio and **`.srt` / `.sub`**
subtitles ([specs](https://www.alrsaaid-tech.net/2022/01/02/0528-2/amp/)).

---

## Why the MP4 does not start

YouTube often serves **VP9 or AV1 video + Opus audio**, or **H.265/HEVC**. Those
are valid MP4/WebM files, but the TV chip cannot decode them.

The fix is always the same:

| Stream | Target |
| --- | --- |
| Video | **H.264 (AVC)**, 8-bit `yuv420p`, Main profile, Level 4.0, max 1080p |
| Audio | **AAC-LC**, 48 kHz, stereo |
| Container | **MP4** with `+faststart` (or MKV/TS) |
| Subtitles | **SRT** sidecar with the same base name, or embedded `mov_text` |

---

## Skip conversion: download directly in TV format

In ytdpro, choose the **TV** quality chip before starting. It asks yt-dlp for
**H.264 video + AAC audio in MP4** and merges the streams without re-encoding,
so a download takes seconds instead of a long conversion.

Equivalent yt-dlp command:

```bash
yt-dlp \
  -f "bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/b[ext=mp4][vcodec^=avc1][height<=1080]/bv*[vcodec^=avc1]+ba/b[ext=mp4]/best" \
  --merge-output-format mp4 \
  -o "%(title)s.%(ext)s" \
  "https://www.youtube.com/watch?v=VIDEO_ID"
```

- YouTube's H.264 streams are normally available up to **1080p**.
- If a video has no H.264 version at all, yt-dlp falls back to another codec —
  check with `ffprobe` and use the conversion steps below in that case.
- To get SRT subtitles directly:
  ```bash
  yt-dlp -f "bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/b[ext=mp4]" \
    --merge-output-format mp4 \
    --write-subs --write-auto-subs --sub-langs ar \
    --convert-subs srt \
    -o "%(title)s.%(ext)s" "URL"
  ```

---

## 1. Check what is inside your file

```bash
ffprobe -v error \
  -show_entries stream=codec_type,codec_name,profile,width,height,pix_fmt \
  -of default=nw=1 "video.mp4"
```

Bad signs: `vp9`, `av1`, `hevc`, `opus`, `10-bit`.
Good signs: `h264`, `aac`, `yuv420p`.

---

## 2. Convert one video

```bash
ffmpeg -i "input.mp4" \
  -map 0:v:0 -map 0:a:0? \
  -vf "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:flags=lanczos,format=yuv420p" \
  -c:v libx264 -profile:v main -level 4.0 -preset medium -crf 20 \
  -c:a aac -b:a 160k -ar 48000 -ac 2 \
  -movflags +faststart \
  "output-tv.mp4"
```

If the TV still refuses, use the most conservative settings:

```bash
ffmpeg -i "input.mp4" \
  -map 0:v:0 -map 0:a:0? \
  -vf "scale=w='min(1920,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:flags=lanczos,format=yuv420p" \
  -c:v libx264 -profile:v baseline -level 3.1 -preset medium -crf 22 \
  -c:a aac -b:a 128k -ar 48000 -ac 2 \
  -movflags +faststart \
  "output-tv-720p.mp4"
```

> Do **not** use `-c copy` — that keeps the incompatible codecs.

---

## 3. Convert a whole playlist folder (recommended)

Use the included script:

```bash
# from the ytdpro folder
./convert-for-tv.sh ~/Downloads/ytdpro/Playlists/"English Stories"
```

It writes everything into a **`TV/`** folder in the current directory:

```
TV/
├── Video title 1.mp4
├── Video title 1.srt
├── Video title 1.ar.srt
├── Video title 1.en.srt
├── Video title 2.mp4
└── Video title 2.srt
```

- Video name is preserved; the extension becomes `.mp4`.
- Each existing subtitle is converted from `.vtt`/`.ass`/`.smi`/… to **`.srt`**.
- A default **`<title>.srt`** is created from the first subtitle, so simple TVs
  pick it up automatically. Use `--no-default-sub` to disable that copy.
- Use `--default-lang ar` to prefer Arabic for `<title>.srt`.
- Nested folders are not traversed (playlist files are flat).

### Common examples

```bash
# custom output folder
./convert-for-tv.sh ~/Downloads/ytdpro/Playlists/"Night drives" -o /media/USB/TV

# 720p for an older TV / smaller USB stick
./convert-for-tv.sh ~/Videos -o TV --max-height 720 --crf 22

# no subtitle conversion
./convert-for-tv.sh ~/Videos --no-subs

# embed the first subtitle instead of sidecar files
./convert-for-tv.sh ~/Videos --embed --default-lang ar

# preview commands without converting
./convert-for-tv.sh ~/Videos --dry-run

# overwrite previous conversions
./convert-for-tv.sh ~/Videos --overwrite

# help
./convert-for-tv.sh --help
```

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `-o, --output DIR` | `./TV` | output folder |
| `--subs` | on | convert subtitles to `.srt` sidecars |
| `--no-subs` | | skip subtitles |
| `--embed` | | embed first subtitle (`mov_text`), no sidecars |
| `--no-default-sub` | | don’t create `<title>.srt` |
| `--default-lang L` | first | preferred language for `<title>.srt` |
| `-H, --max-height N` | `1080` | maximum height, never upscales |
| `--crf N` | `20` | quality; lower = better/bigger |
| `--preset NAME` | `medium` | x264 speed preset |
| `--fps N` | `0` | 0 = keep source; `30` forces 30 fps |
| `-y, --overwrite` | off | replace existing files |
| `--dry-run` | off | print commands only |

---

## 4. Manual batch loop (without the script)

```bash
mkdir -p TV
for f in *.mp4 *.mkv *.webm; do
  [ -f "$f" ] || continue
  base="${f%.*}"
  ffmpeg -i "$f" \
    -map 0:v:0 -map 0:a:0? \
    -vf "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:flags=lanczos,format=yuv420p" \
    -c:v libx264 -profile:v main -level 4.0 -preset medium -crf 20 \
    -c:a aac -b:a 160k -ar 48000 -ac 2 \
    -movflags +faststart \
    "TV/${base}.mp4"
done
```

---

## 5. Subtitles

ytdpro writes `.vtt`. The TV expects `.srt`.

```bash
# one file
ffmpeg -i "Me at the zoo.en.vtt" "Me at the zoo.srt"
```

For multiple languages:

```bash
for s in *.vtt; do
  [ -f "$s" ] || continue
  ffmpeg -i "$s" "${s%.vtt}.srt"
done
```

Naming rules for the TV:

```
Movie.mp4
Movie.srt          ← default subtitle, same base name
Movie.ar.srt       ← optional Arabic
Movie.en.srt       ← optional English
```

Keep the subtitle in the **same folder** as the video and make the base name
**exactly identical** (including spaces and case). Then enable subtitles from the
player’s **Subtitle / CC** menu.

> Bitmap subtitles (PGS/VobSub) are not supported as `.srt`. Text formats
> (VTT, SRT, ASS, SSA, SMI, TTML) are converted by the script.

---

## 6. Prepare the USB drive

The safest filesystem for these receivers is **FAT32**:

- maximum **4 GB per file**;
- use an **MBR** partition table (not GPT);
- avoid very long names, emoji and unusual characters;
- a simple folder like `TV/` in the root is easiest to find.

Check the drive (Linux):

```bash
lsblk -f
```

> Replace `/dev/sdX1` with **your** USB partition. This erases the drive.

```bash
sudo umount /dev/sdX1
sudo mkfs.vfat -F 32 -n TV /dev/sdX1
sudo mkdir -p /media/usb
sudo mount /dev/sdX1 /media/usb
sudo cp -r TV/* /media/usb/TV/
sync
sudo umount /media/usb
```

If a movie is larger than 4 GB, either lower the quality/height or split it:

```bash
# split into roughly 3.7 GB pieces without re-encoding
ffmpeg -i "movie-tv.mp4" -c copy -fs 3700M -map 0 "movie-part-%02d.mp4"
```

(For FAT32, splitting with `-c copy` is safest; play the parts in order.)

---

## 7. Troubleshooting

**Still “format not supported”**
- Try the baseline/720p command above.
- Try MKV instead of MP4 (same H.264/AAC streams):
  ```bash
  ffmpeg -i output-tv.mp4 -c copy output-tv.mkv
  ```

**Video plays but no sound**
The TV may not support the audio codec. Re-encode audio only:
```bash
ffmpeg -i input.mp4 -c:v copy -c:a aac -b:a 160k -ac 2 -ar 48000 output-aac.mp4
```

**Subtitles do not appear**
- Check the file is `.srt`, not `.vtt`.
- Check the base name matches exactly.
- Enable subtitles in the player menu.
- Try the default `<title>.srt` copy, then a language file.

**File is cut at 4 GB**
FAT32 limit. Use a smaller output or split it.

**Colors look washed out / green**
The source may be HDR/10-bit. The script converts to 8-bit `yuv420p`; if colors
are still wrong, use a dedicated HDR→SDR tone-map step before conversion.

**Conversion is slow**
`libx264` is CPU-heavy. Use `--preset veryfast` for speed, or convert only the
files you need. A 1-hour 1080p video can take 10–40 minutes depending on the CPU.

---

## 8. Verify the result

```bash
ffprobe -v error \
  -show_entries stream=codec_type,codec_name,profile,pix_fmt \
  -of default=nw=1 "TV/Video title.mp4"
```

Expected:

```
codec_name=h264
profile=Main
pix_fmt=yuv420p
codec_name=aac
```

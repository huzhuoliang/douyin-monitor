# douyin_monitor — Project Memory

## Purpose

24/7 background service that monitors a Douyin streamer's live status and auto-records each session as an MP4 file. Runs as a systemd service with auto-restart.

---

## Sensitive Data & Desensitization Rules

The following information is **never committed to git**. Real values live in local-only files.

| Category | Placeholder in repo | Real value location |
|----------|--------------------|--------------------|
| Streamer room IDs & names | `ROOM_ID_1`, `主播A/B/C` | `config.json`, `CLAUDE.local.md` |
| Local file paths (username) | `/path/to/recordings`, `YOUR_USERNAME` | `config.json`, `CLAUDE.local.md` |
| Feishu webhook URL | `null` / omitted | `config.json`, `CLAUDE.local.md` |
| NAS host / dest dir | generic descriptions | `config.json`, `CLAUDE.local.md` |

**Gitignored files** (exist on disk, never pushed):
- `config.json` — runtime config with real values; use `config.example.json` as template
- `CLAUDE.local.md` — personal deployment notes (paths, streamer list, credentials)

**When adding new sensitive fields** to `config.json`:
1. Keep the field in `config.example.json` with a `null` or placeholder value
2. Add a row to the table above
3. Document the real value in `CLAUDE.local.md` if useful for reference

---

## File Map

| File | Role |
|------|------|
| `config.json` | All runtime config (URL, paths, intervals) |
| `monitor.py` | Main script — state machine + recording logic |
| `status.py` | Status display tool — reads per-streamer JSON status files |
| `install.sh` | One-shot installer (ffmpeg, streamlink, systemd) |
| `README.md` | User-facing documentation |
| `CLAUDE.md` | This file — project memory for Claude |

Recordings saved to: `{output_dir}` (configured in config.json)
Systemd unit: `/etc/systemd/system/douyin-monitor.service`

---

## Architecture

```
config.json
  streamers: [{url, name?}, ...]   ← multi-streamer list
  (shared: paths, intervals, log_level, nas_*, post_process_delay...)

main()
  ├─ load_config() — backward-compat: old streamer_url → single-element streamers
  ├─ create StreamerMonitor per streamer
  ├─ start each monitor in a daemon thread
  └─ join all threads

StreamerMonitor (class)
  ├─ instance vars: url, label, _sl_proc, _ff_proc, _sl_pgid, _ff_pgid
  │                 _session_files, _offline_since, _post_processing
  ├─ run()                   — main monitor loop (per-streamer state machine)
  ├─ stop()                  — send SIGTERM to both process groups
  ├─ log()                   — auto-prefix [label] on every message
  ├─ _transcribe()           — enqueue video; start _transcribe_worker if not running
  ├─ _do_transcribe()        — actual transcription using passed-in model instance
  └─ _post_process_session() — validate → merge → rsync → cleanup (daemon thread)

_transcribe_queue / _transcribe_lock / _transcribe_worker_running  (module-level globals)
  └─ _transcribe_worker() — load model → drain queue → del model + gc.collect()

_shutdown_event = threading.Event()
  ├─ SIGTERM/SIGINT → event.set() + all monitor.stop()
  └─ interruptible_sleep → event.wait(timeout) — no polling

Signal flow:
  SIGTERM/SIGINT → handle_signal() → _shutdown_event.set()
                                   → m.stop() for each monitor
```

### Post-processing flow (per session)

```
recording ends → _session_files.append(path.ts) → _segment_quality[path]=quality → poll loop continues immediately
↓
each offline poll: check (time.time() - offline_since) >= post_process_delay
↓  (threshold reached, session_files non-empty, not already processing)
_post_process_session() started in daemon thread "{label}-postproc"
  Step 0: ffmpeg -i seg.ts -c copy seg.mp4  (CONVERTING; converts each .ts → .mp4)
           .ts deleted after successful conversion; _segment_quality keys migrated .ts→.mp4
  Step 1: ffmpeg drawtext re-encode each .mp4 in-place (if timestamp_watermark=true) [WATERMARKING]
           start_ts parsed from filename (YYYYMMDD_HHMMSS), -threads watermark_threads
           quality label from _segment_quality[path] → "超清/高清/标清/流畅" prepended to timestamp text
           fontcolor=white; font=NotoSansCJK-Regular.ttc (supports Chinese)
  Step 2: ffprobe validate each .mp4 → valid_files list [VALIDATING]
  Step 3: ffmpeg -f concat → 抖音_{label}_{ts}_merged.mp4  (skip if 1 file) [MERGING]
           NAS upload renames to 抖音_{label}_{ts}.mp4 (strips _merged; local file kept as-is to avoid conflict with original segments)
  Step 4: ssh nas mkdir -p; rsync -av --remove-source-files merged → NAS [UPLOADING]
  Step 5: os.remove() each original segment [CLEANING]
  Shutdown check at start of every step; files preserved on abort
```

**Recording is never blocked by post-processing**: watermarking was moved from the poll loop into the postproc daemon thread. The poll loop detects a new live session immediately after ffmpeg exits, regardless of whether watermarking or NAS sync is in progress.

**Dependencies:**
- `ffmpeg` via apt
- `streamlink` via pip3 (`sudo pip3 install -U streamlink --break-system-packages`)
- `faster-whisper` via pip3 (optional, for `transcribe_audio`)
- `anthropic` via pip3 (optional, for `generate_outline`)

---

## Key Design Decisions

### Dual-process recording pipeline
`streamlink --stdout` pipes the raw stream into `ffmpeg -i pipe:0 -f mpegts`. Each process gets its own setsid process group. On shutdown, both pgids receive SIGTERM independently.

Recording uses **MPEG-TS** (`.ts`) format to avoid H.264 花屏 corruption. The root cause: when recording to MP4 via pipe with `-c copy`, ffmpeg writes the initial SPS/PPS into the `avcC` atom once; if the Douyin CDN reconnects mid-stream and the new HLS segment carries different SPS/PPS inline, players decode against the stale `avcC` and produce garbled video while audio remains normal. MPEG-TS uses Annex B format where each keyframe carries inline SPS/PPS — no `avcC` atom, no stale header issue. The `.ts` → `.mp4` conversion in post-processing (from a complete file, not a pipe) correctly builds the `avcC` from the full bitstream.

### Single network call per poll cycle
`check_live_info()` runs one `streamlink --json` call and returns `(is_live, uploader_name)` together — avoids a second request to get the author name.

### Waiting on ffmpeg, not streamlink
`StreamerMonitor.run()` polls `self._ff_proc.poll()` — ffmpeg finalizes the MP4 container only after it drains the pipe and flushes. Waiting on streamlink would race with the file write.

### Adaptive polling
- 0–29 consecutive offline checks → 60s interval
- 30–119 → 120s
- 120+ → 300s
- Resets to 60s on live detection

### Shutdown responsiveness
`interruptible_sleep()` uses `_shutdown_event.wait(timeout)` — responds immediately when event is set, no polling overhead.

### Filename format
Recording: `抖音_{sanitized_uploader}_{YYYYMMDD_HHMMSS}.ts` (raw MPEG-TS)
After post-processing conversion: `抖音_{sanitized_uploader}_{YYYYMMDD_HHMMSS}.mp4`
- `sanitize_filename()` strips `\ / : * ? " < > |`, collapses whitespace, preserves Chinese characters
- Uploader name sourced from `streamlink --json` → `metadata.author`
- Fallback: extracts `@username` or room ID from URL path

### Cookie auth
`cookies_from_browser` in config.json (e.g. `"chrome"`). Currently unused by streamlink backend — kept for future use. Set `null`.

---

## systemd Service Settings

```ini
User=YOUR_USERNAME
Restart=always
RestartSec=10
KillMode=mixed
TimeoutStopSec=30
After=network-online.target
```

---

## streamlink Recording Pipeline

```bash
streamlink --stdout {url} best \
  | ffmpeg -i pipe:0 -c copy -f mpegts -y {output_path.ts}
```

Live status check:
```bash
streamlink --json {url}
# → {"streams": {...}, "metadata": {"author": "..."}, ...}  # live
# → {"error": "No playable streams found ..."}              # offline
```

---

## Status Display (`douyin-status`)

Installed at `/usr/local/bin/douyin-status`. Reads per-streamer JSON status files written by `monitor.py` and displays a `docker ps`-style table:

```bash
douyin-status          # color table
douyin-status --no-color
douyin-status --json   # raw JSON for scripting
```

Sample output:
```
LABEL    PHASE        SINCE     DURATION  DETAIL                      NEXT
────────────────────────────────────────────────────────────────────────────────
主播A    RECORDING    21:18:04  3h27m10s  主播A_20260310_211804.mp4  waiting for stream end
主播B    WAITING      23:38:51  14m35s    1 file(s) pending           post-proc @ 01:59 (15m)
主播C    IDLE         00:56:07  1h05m     offline 1h05m  45 checks    poll in 55s (@ 02:02:15)
```

**Phase lifecycle:**
```
IDLE → CONNECTING → RECORDING
                        │
                 (ffmpeg exits, poll loop continues immediately)
                        ↓
                     WAITING
                        │ (post_process_delay elapsed)
                        ↓
               CONVERTING → WATERMARKING* → VALIDATING → MERGING** → UPLOADING → CLEANING → IDLE

* WATERMARKING: only when timestamp_watermark=true (runs in postproc thread, not poll loop)
** MERGING: only when multiple segments
CONVERTING: .ts → .mp4 (always; fixes H.264 SPS/PPS avcC corruption issue)
```

**Status JSON files** (written atomically by `_write_status()`):
- Location: `{output_dir}/.douyin_status_{label}.json`
- Written on every phase transition and every poll cycle (to keep `next_poll_at` fresh)
- Fields: `label`, `url`, `phase`, `phase_since`, `detail`, `rec_started_at`, `offline_since`, `consecutive_offline`, `next_poll_at`, `post_process_delay`, `session_files_count`, `updated_at`

**Thread safety:** postproc thread owns phases VALIDATING→CLEANING; poll loop detects `_post_processing=True` and calls `_write_status()` only (preserving postproc phase) instead of `_set_phase("IDLE")`.

---

## Common Operations

```bash
# Check all streamer statuses:
douyin-status

# After editing config.json:
sudo systemctl restart douyin-monitor

# Follow logs:
sudo journalctl -u douyin-monitor -f

# Update streamlink (do periodically — Douyin changes frequently):
sudo pip3 install -U streamlink --break-system-packages

# Manual test run (no systemd):
python3 monitor.py --config config.json
```

---

### Post-processing config fields

| Field | Default | Notes |
|-------|---------|-------|
| `nas_enabled` | `false` | Master switch; `false` skips everything |
| `nas_host` | `"nas"` | SSH alias in `~/.ssh/config`; must be passwordless |
| `nas_dest_dir` | `"/volume1/Share/LiveVideos"` | rsync destination path on NAS |
| `post_process_delay` | `1800` | Seconds offline before triggering post-process |

### Adaptive quality config fields

| Field | Default | Notes |
|-------|---------|-------|
| `adaptive_quality` | `true` | Master switch; `false` → fixed `streamlink_quality` |
| `streamlink_quality_ladder` | `["best","720p","480p","worst"]` | Quality tiers; index 0 = best |
| `quality_downgrade_threshold` | `60` | Seconds; error exit + duration < this → downgrade |
| `quality_upgrade_threshold` | `300` | Seconds; clean exit + duration ≥ this → upgrade |

**Adaptive quality state** (persisted in `.douyin_state_{label}.json`):
- `quality_index`: current position in ladder (0 = best); survives restart
- `segment_quality`: `{path: quality_string}` map; used by `_add_watermark()` to label each segment

**Quality label mapping** (class-level `_QUALITY_LABELS` dict):
- `best/hd/1080p/720p` → 超清/高清 ; `sd/480p/md/360p` → 标清/流畅 ; `ld/worst` → 流畅
- Font changed to `NotoSansCJK-Regular.ttc` (default) to support Chinese glyphs
- `watermark_font` in config overrides the font path

### Feishu webhook config fields

| Field | Default | Notes |
|-------|---------|-------|
| `feishu_webhook_url` | `null` | Feishu Flow webhook trigger URL; `null` disables all notifications |

Three events are sent via `_notify()` (stdlib `urllib.request`, 10s timeout, non-fatal):
- `stream_live` — after `start_recording()` succeeds
- `stream_ended` — after ffmpeg exits cleanly, before `_rec_start_ts = None`
- `nas_sync_done` — after rsync succeeds, before Step 4 local cleanup

Payload is **flat JSON** with `msg_type: "text"` (required by Feishu Flow webhook trigger).
Each event sends a different subset of fields; see README for per-event field list.
Feishu Flow webhook trigger "参数示例" should be the merged superset of all fields (see README).

### Transcription & outline config fields

| Field | Default | Notes |
|-------|---------|-------|
| `transcribe_audio` | `false` | Auto-transcribe after recording; writes `.srt` alongside `.mp4` |
| `whisper_model` | `"medium"` | faster-whisper model size |
| `whisper_language` | `"zh"` | ASR language hint |
| `generate_outline` | `false` | Call Claude API after transcription; writes `.outline.md` |
| `outline_model` | `"claude-haiku-4-5-20251001"` | Claude model for outline generation |
| `anthropic_api_key` | `null` | Fallback if `ANTHROPIC_API_KEY` env var not set |

### Transcription pipeline

```
Recording done → _transcribe() enqueues (monitor, video_path) into _transcribe_queue
  → if _transcribe_worker not running: start "whisper-worker" daemon thread
       _transcribe_worker():
         load WhisperModel once
         loop: pop item → _do_transcribe(video_path, model, language)
                            → write .srt
                            → if generate_outline: _generate_outline() → write .outline.md
               until queue empty
         del model; gc.collect()   ← releases ~1.5 GB
```

- Whisper model is **loaded on demand and released immediately after the queue is drained** — no persistent memory footprint between recording sessions
- All monitors share a single global worker thread; concurrent transcription requests are serialized (one model instance, processed in order)
- Transcript truncated at 80 000 chars before sending to Claude (well below 200K token limit)
- `ANTHROPIC_API_KEY` env var takes priority over `anthropic_api_key` in config
- Outline generation is a best-effort step — errors are logged but never crash the transcription thread
- **Note**: `generate_outline` requires a separate Anthropic API account (pay-as-you-go); Claude Code subscription does not grant API access

---

## State Persistence (restart-resilient)

Each `StreamerMonitor` persists restart-sensitive state to a per-streamer JSON file:

```
{output_dir}/.douyin_state_{label}.json
```

Fields saved:

| Field | Purpose |
|-------|---------|
| `consecutive_offline` | Adaptive polling counter — survives restart without resetting to 60s |
| `offline_since` | Wall time of offline transition — post-process delay continues counting |
| `session_files` | List of recorded segments — not lost if service restarts between segments |
| `current_recording` | `{path, rec_start_ts}` of the active recording — recovered into `_session_files` on restart |

**Write path**: every meaningful state change calls `_save_state()` which writes to `{path}.tmp` then `os.replace()` (atomic). No partially-written state files.

**Recovery flow** (at `run()` startup):
1. `_load_state()` restores all four fields
2. If `current_recording` is non-None → interrupted recording detected:
   - File exists on disk → add to `_session_files` (watermark 统一在 `_post_process_session` Step 0 执行)
   - File missing → log warning, skip
   - Clear `current_recording`, save state
3. Main poll loop starts with fully restored state

**`_save_state()` call sites** in `run()`:
- Offline branch: after `_consecutive_offline` increment + `_offline_since` set
- Post-process trigger: after `_session_files = []` and `_offline_since = None`
- Live detected: after `_consecutive_offline = 0` and `_offline_since = None`
- `start_recording()` success: `_current_recording` set
- Recording ends: `_current_recording = None`, `_session_files.append()`

---

## Known Gotchas

- yt-dlp's built-in Douyin extractor only supports `www.douyin.com/video/{id}` (VOD), **not** `live.douyin.com/{room_id}` — that's why we switched to streamlink
- Each streamer URL **must** use `https://live.douyin.com/{room_id}` format. `www.douyin.com/follow/live/...` URLs return `"No plugin can handle URL"` from streamlink — convert them manually
- `fallback_name_from_url()` regex supports `live.douyin.com/{id}`, `douyin.com/follow/live/{id}`, and `@username` patterns
- No automatic disk space management — operator must clean up recordings manually
- streamlink breaks occasionally with Douyin; run `sudo pip3 install -U streamlink --break-system-packages` when recording stops working
- On Ubuntu 24.04 (Python 3.12), pip install requires `--break-system-packages` due to PEP 668 externally-managed-environment restriction
- Each `StreamerMonitor` instance owns its own `_sl_proc/_ff_proc/_sl_pgid/_ff_pgid` — no shared mutable state between monitors
- `_session_files` / `_offline_since` / `_post_processing` are owned by the monitor thread; the postproc thread receives a copy of `files_to_process` and only writes back `_post_processing = False` (GIL guarantees bool assignment atomicity)
- `post_process_delay` uses `time.time()` wall clock, not poll count — immune to adaptive polling interval changes
- On shutdown during post-processing, files are deliberately preserved (not cleaned up) so nothing is lost

## Recovery: Post-processing Interrupted by Restart

When the service is restarted mid-post-process, `session_files` is already `[]` in the state file (cleared at trigger time), so the videos will **not** be automatically retried. The MP4 files remain on disk.

**Symptom:** `session_files: []` in state file but MP4 still exists locally and was never rsync'd.

**Fix:** patch the state file(s) to re-queue the video and set `offline_since` past the threshold:

```python
import json, time, os

delay = 1800
offline_since = time.time() - delay - 10  # already past threshold

path = '/path/to/recordings/.douyin_state_主播A.json'
with open(path, encoding='utf-8') as f:
    state = json.load(f)
state['session_files'] = ['/path/to/recordings/抖音_主播A_20260310_211804.mp4']
state['offline_since'] = offline_since
state['current_recording'] = None
tmp = path + '.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    json.dump(state, f, ensure_ascii=False)
os.replace(tmp, path)
```

Then `sudo systemctl restart douyin-monitor` — the monitor will pick it up on the first poll and trigger post-processing immediately.

## sing-box Proxy Compatibility

The host runs sing-box in TUN mode (`auto_route: true`). Recording traffic is **not** proxied:
- `douyincdn.com` CDN domains are covered by `geosite-cn` → `outbound: direct`
- Resolved IPs (e.g. 115.x, 180.x, 222.x) are covered by `geoip-cn` → `outbound: direct`

No extra configuration needed — Douyin CDN traffic bypasses the proxy automatically.

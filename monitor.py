#!/usr/bin/env python3
"""Douyin Live Stream Monitor & Auto-Recorder"""

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

from danmaku import DanmakuRecorder
from pathlib import Path

_shutdown_event = threading.Event()
_all_monitors: list["StreamerMonitor"] = []

# Global transcription queue — shared across all StreamerMonitor instances
_transcribe_queue: list[tuple["StreamerMonitor", str]] = []
_transcribe_lock = threading.Lock()
_transcribe_worker_running = False


def _transcribe_worker(config: dict) -> None:
    """Load Whisper model once, drain the queue, then release the model."""
    global _transcribe_worker_running
    model_name = config.get("whisper_model", "medium")
    language = config.get("whisper_language", "zh")

    logging.info("Loading Whisper model '%s'...", model_name)
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
    except Exception as e:
        logging.error("Failed to load Whisper model: %s", e)
        with _transcribe_lock:
            _transcribe_queue.clear()
            _transcribe_worker_running = False
        return

    try:
        while True:
            with _transcribe_lock:
                if not _transcribe_queue:
                    return
                monitor, video_path = _transcribe_queue.pop(0)
            monitor._do_transcribe(video_path, model, language)
    finally:
        logging.info("Releasing Whisper model")
        del model
        import gc
        gc.collect()
        with _transcribe_lock:
            _transcribe_worker_running = False


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_config(path: str) -> dict:
    with open(path) as f:
        config = json.load(f)
    # Backward compatibility: old single-URL format
    if "streamer_url" in config and "streamers" not in config:
        config["streamers"] = [{"url": config["streamer_url"]}]
    return config


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name)
    name = re.sub(r"\s+", "_", name.strip())
    return name or "unknown"


def fallback_name_from_url(url: str) -> str:
    match = re.search(r"douyin\.com/(?:@([^/?#]+)|(?:follow/)?live/(\d+)|(\d+))", url)
    if match:
        return match.group(1) or match.group(2) or match.group(3)
    return "unknown"


def interruptible_sleep(seconds: float) -> bool:
    """Return True if shutdown requested."""
    return _shutdown_event.wait(timeout=seconds)


def _ts_from_filename(path: str) -> int | None:
    """Parse recording start timestamp from filename (抖音_..._YYYYMMDD_HHMMSS.mp4)."""
    m = re.search(r"(\d{8}_\d{6})", os.path.basename(path))
    if m:
        try:
            return int(datetime.strptime(m.group(1), "%Y%m%d_%H%M%S").timestamp())
        except ValueError:
            return None
    return None


def _fmt_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


class StreamerMonitor:
    def __init__(self, url: str, name: str | None, config: dict):
        self.url = url
        self.label = name or fallback_name_from_url(url)
        self.config = config
        self._sl_proc = None
        self._ff_proc = None
        self._sl_pgid = None
        self._ff_pgid = None
        self._session_files: list[str] = []       # segments accumulated this session
        self._offline_since: float | None = None  # wall time of most recent offline transition
        self._post_processing = False             # guard against duplicate trigger
        self._rec_start_ts: int | None = None     # Unix timestamp when current recording started
        self._consecutive_offline: int = 0        # persisted across restarts for adaptive polling
        self._current_recording: dict | None = None  # {"path": ..., "rec_start_ts": ...}
        self._danmaku_recorder: DanmakuRecorder | None = None
        self._quality_index: int = 0             # index into quality_ladder; 0 = best
        self._segment_quality: dict[str, str] = {}  # path → quality string, for watermarking
        self._phase: str = "STARTING"
        self._phase_since: float = time.time()
        self._phase_detail: str | None = None
        self._next_poll_at: float | None = None

    def log(self, level: int, msg: str, *args) -> None:
        logging.log(level, f"[{self.label}] " + msg, *args)

    def check_live_info(self) -> tuple[bool | None, str | None]:
        """Single streamlink --json call. Returns (is_live, uploader_name)."""
        try:
            result = subprocess.run(
                [self.config["streamlink_path"], "--json", self.url],
                capture_output=True,
                text=True,
                timeout=30,
            )
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                self.log(logging.WARNING, "streamlink --json returned non-JSON: %s", result.stdout[:200])
                return None, None

            if data.get("error"):
                return False, None

            streams = data.get("streams", {})
            if not streams:
                return False, None

            author = data.get("metadata", {}).get("author")
            name = sanitize_filename(author) if author else None
            return True, name

        except subprocess.TimeoutExpired:
            self.log(logging.WARNING, "streamlink --json timed out")
            return None, None
        except Exception as e:
            self.log(logging.WARNING, "check_live_info error: %s", e)
            return None, None

    def check_live_info_with_retry(self) -> tuple[bool | None, str | None]:
        retries = self.config.get("network_retry_count", 3)
        wait = self.config.get("network_retry_wait", 30)
        for attempt in range(retries):
            is_live, name = self.check_live_info()
            if is_live is not None:
                return is_live, name
            if attempt < retries - 1:
                self.log(
                    logging.WARNING,
                    "Network error on live check (attempt %d/%d), retrying in %ds",
                    attempt + 1, retries, wait,
                )
                if interruptible_sleep(wait):
                    return None, None
        self.log(logging.WARNING, "All %d live check attempts failed", retries)
        return None, None

    def start_recording(self, output_path: str) -> bool:
        """Start streamlink | ffmpeg pipeline. Returns True on success."""
        streamlink_path = self.config["streamlink_path"]
        if self.config.get("adaptive_quality", True):
            ladder = self.config.get("streamlink_quality_ladder", ["best", "720p", "480p", "worst"])
            quality = ladder[min(self._quality_index, len(ladder) - 1)]
        else:
            quality = self.config.get("streamlink_quality", "best")
        ffmpeg_path = self.config["ffmpeg_path"]

        self.log(logging.INFO, "Starting recording [quality=%s]: %s", quality, output_path)
        try:
            sl_proc = subprocess.Popen(
                [streamlink_path, "--stdout", self.url, quality],
                stdout=subprocess.PIPE,
                preexec_fn=os.setsid,
            )
            ff_proc = subprocess.Popen(
                [ffmpeg_path, "-i", "pipe:0", "-c", "copy", "-y", output_path],
                stdin=sl_proc.stdout,
                preexec_fn=os.setsid,
            )
            # Release our reference so ffmpeg holds the only read end
            sl_proc.stdout.close()

            self._sl_proc = sl_proc
            self._ff_proc = ff_proc
            self._sl_pgid = os.getpgid(sl_proc.pid)
            self._ff_pgid = os.getpgid(ff_proc.pid)
            self._rec_start_ts = int(time.time())
            self._current_recording = {"path": output_path, "rec_start_ts": self._rec_start_ts, "quality": quality}
            self._save_state()

            if self.config.get("danmaku_enabled", False):
                if self._danmaku_recorder is not None:
                    self._danmaku_recorder.stop()
                    self._danmaku_recorder = None
                danmaku_path = os.path.splitext(output_path)[0] + ".danmaku.jsonl"
                self._danmaku_recorder = DanmakuRecorder(self.url, danmaku_path, self.config, self.label)
                self._danmaku_recorder.start()

            return True
        except Exception as e:
            self.log(logging.ERROR, "Failed to start recording: %s", e)
            return False

    # Quality label → display name (Chinese)
    _QUALITY_LABELS: dict[str, str] = {
        "best": "超清", "hd": "高清", "1080p": "超清",
        "720p": "高清", "sd": "标清", "480p": "标清",
        "360p": "流畅", "ld": "流畅", "md": "流畅",
        "worst": "流畅", "ao": "仅音频",
    }

    def _add_watermark(self, input_path: str, start_ts: int, quality: str | None = None) -> bool:
        """Re-encode input_path in-place with a timestamp (and optional quality) watermark."""
        # Default to a CJK-capable font so Chinese quality labels render correctly
        font = self.config.get(
            "watermark_font",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        )
        size = self.config.get("watermark_fontsize", 24)
        threads = self.config.get("watermark_threads", 1)
        # Quality label
        if quality:
            label = self._QUALITY_LABELS.get(quality.lower(), quality.upper())
            text = f"{label}  %Y-%m-%d %H\\:%M\\:%S"
        else:
            text = "%Y-%m-%d %H\\:%M\\:%S"
        drawtext = (
            f"drawtext=fontfile={font}:"
            f"text='{text}':"
            f"basetime={start_ts * 1000000}:expansion=strftime:"
            f"x=w-text_w-10:y=h-text_h-10:"
            f"fontsize={size}:fontcolor=white:"
            f"box=1:boxcolor=black@0.5:boxborderw=4"
        )
        tmp_path = input_path + ".wm.mp4"
        ffmpeg_path = self.config["ffmpeg_path"]
        try:
            result = subprocess.run(
                [ffmpeg_path, "-i", input_path,
                 "-vf", drawtext,
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-threads", str(threads),
                 "-c:a", "copy",
                 "-y", tmp_path],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.log(logging.ERROR, "Watermark encoding failed (rc=%d): %s",
                         result.returncode, result.stderr[-300:])
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                return False
            os.replace(tmp_path, input_path)
            return True
        except Exception as e:
            self.log(logging.ERROR, "Watermark encoding error: %s", e)
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return False

    def _transcribe(self, video_path: str) -> None:
        """Enqueue video_path for transcription; start global worker if not already running."""
        global _transcribe_worker_running
        with _transcribe_lock:
            _transcribe_queue.append((self, video_path))
            if not _transcribe_worker_running:
                _transcribe_worker_running = True
                t = threading.Thread(
                    target=_transcribe_worker,
                    args=(self.config,),
                    daemon=True,
                    name="whisper-worker",
                )
                t.start()
        self.log(logging.INFO, "Queued for transcription: %s", os.path.basename(video_path))

    def _do_transcribe(self, video_path: str, model, language: str) -> None:
        """Transcribe video_path using the provided model; write .srt alongside."""
        srt_path = os.path.splitext(video_path)[0] + ".srt"
        try:
            self.log(logging.INFO, "Transcribing: %s", os.path.basename(video_path))
            segments, _ = model.transcribe(video_path, language=language)
            text_lines = []
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, seg in enumerate(segments, start=1):
                    f.write(f"{i}\n")
                    f.write(f"{_fmt_srt_time(seg.start)} --> {_fmt_srt_time(seg.end)}\n")
                    line = seg.text.strip()
                    f.write(f"{line}\n\n")
                    text_lines.append(line)
            self.log(logging.INFO, "Transcription done: %s", os.path.basename(srt_path))
            if self.config.get("generate_outline", False):
                self._generate_outline(srt_path, text_lines)
        except Exception as e:
            self.log(logging.ERROR, "Transcription error: %s", e)

    def _generate_outline(self, srt_path: str, text_lines: list) -> None:
        """Call Claude API to generate a brief outline from transcribed text."""
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY") or self.config.get("anthropic_api_key")
        if not api_key:
            self.log(logging.WARNING, "ANTHROPIC_API_KEY not set, skipping outline generation")
            return

        model = self.config.get("outline_model", "claude-haiku-4-5-20251001")
        transcript = "\n".join(text_lines)
        if len(transcript) > 80000:
            transcript = transcript[:80000] + "\n[截断...]"

        prompt = (
            "以下是一段抖音直播的语音转文字记录（由 Whisper ASR 自动生成，可能含错别字和错误识别）。\n\n"
            "请：\n"
            "1. 自动忽略明显的 ASR 错别字\n"
            "2. 用中文生成一份简短的直播内容大纲（Markdown 格式，200 字以内）\n\n"
            f"直播转录：\n{transcript}"
        )

        outline_path = os.path.splitext(srt_path)[0] + ".outline.md"
        try:
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            outline = msg.content[0].text
            with open(outline_path, "w", encoding="utf-8") as f:
                f.write(outline)
            self.log(logging.INFO, "Outline saved: %s", os.path.basename(outline_path))
        except Exception as e:
            self.log(logging.ERROR, "Outline generation error: %s", e)

    def stop(self) -> None:
        """Terminate streamlink so the pipe closes; ffmpeg will see EOF and finalize the MP4 naturally."""
        if self._danmaku_recorder is not None:
            self._danmaku_recorder.stop()
            self._danmaku_recorder = None

        if self._sl_pgid is not None:
            try:
                os.killpg(self._sl_pgid, signal.SIGTERM)
                self.log(logging.INFO, "Sent SIGTERM to streamlink pgid %d", self._sl_pgid)
            except ProcessLookupError:
                pass
            except Exception as e:
                self.log(logging.WARNING, "Error killing streamlink: %s", e)

        if self._sl_proc is not None:
            try:
                self._sl_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.log(logging.WARNING, "streamlink did not exit after SIGTERM")
            except Exception:
                pass

        self._sl_proc = None
        self._sl_pgid = None
        # _ff_proc / _ff_pgid are intentionally left for run() to handle gracefully

    def _post_process_session(self, files: list[str]) -> None:
        """Validate → merge → upload to NAS → clean local segments. Runs in a daemon thread."""
        try:
            self.log(logging.INFO, "Post-processing %d session file(s)...", len(files))

            # Step 0: Watermark each segment
            if self.config.get("timestamp_watermark", False):
                if _shutdown_event.is_set():
                    self.log(logging.WARNING, "Shutdown detected, aborting post-processing (before watermark)")
                    return
                for f in files:
                    start_ts = _ts_from_filename(f)
                    if start_ts is None:
                        self.log(logging.WARNING, "Cannot parse timestamp from filename, skipping watermark: %s", os.path.basename(f))
                        continue
                    self._set_phase("WATERMARKING", os.path.basename(f))
                    self.log(logging.INFO, "Adding timestamp watermark: %s", os.path.basename(f))
                    quality = self._segment_quality.get(f)
                    self._add_watermark(f, start_ts, quality=quality)

            self._set_phase("VALIDATING", f"{len(files)} file(s)")

            # Step 1: Validate
            if _shutdown_event.is_set():
                self.log(logging.WARNING, "Shutdown detected, aborting post-processing (before validate)")
                return

            valid_files = []
            for f in files:
                result = subprocess.run(
                    [
                        "ffprobe", "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        f,
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    self.log(logging.WARNING, "Validation failed (skipping): %s", f)
                else:
                    valid_files.append(f)

            self.log(logging.INFO, "Validation: %d/%d files OK", len(valid_files), len(files))

            if not valid_files:
                self.log(logging.ERROR, "No valid files to process, aborting")
                return

            # Step 2: Merge (or pass through single file)
            if _shutdown_event.is_set():
                self.log(logging.WARNING, "Shutdown detected, aborting post-processing (before merge)")
                return

            output_dir = Path(self.config["output_dir"])

            # Extract timestamp from first filename; fallback to current time
            ts_match = re.search(r"\d{8}_\d{6}", os.path.basename(valid_files[0]))
            first_timestamp = ts_match.group(0) if ts_match else datetime.now().strftime("%Y%m%d_%H%M%S")

            if len(valid_files) == 1:
                merged_path = valid_files[0]
                self.log(logging.INFO, "Single valid file, skipping merge: %s", merged_path)
            else:
                merged_name = f"抖音_{self.label}_{first_timestamp}_merged.mp4"
                merged_path = str(output_dir / merged_name)

                ts = int(time.time())
                concat_file = f"/tmp/douyin_concat_{self.label}_{ts}.txt"
                with open(concat_file, "w") as f:
                    for vf in valid_files:
                        f.write(f"file '{vf}'\n")

                self._set_phase("MERGING", merged_name)
                self.log(logging.INFO, "Merging into %s", merged_name)
                ffmpeg_path = self.config["ffmpeg_path"]
                merge_result = subprocess.run(
                    [ffmpeg_path, "-f", "concat", "-safe", "0", "-i", concat_file,
                     "-c", "copy", "-y", merged_path],
                    capture_output=True,
                    text=True,
                )

                try:
                    os.remove(concat_file)
                except Exception:
                    pass

                if merge_result.returncode != 0:
                    self.log(
                        logging.ERROR,
                        "ffmpeg merge failed (rc=%d): %s",
                        merge_result.returncode,
                        merge_result.stderr[-500:],
                    )
                    return

            # Danmaku merge (mirrors MP4 merge logic)
            merged_danmaku_path = None
            if self.config.get("danmaku_enabled", False):
                danmaku_segments = [os.path.splitext(f)[0] + ".danmaku.jsonl" for f in valid_files]
                existing_danmaku = [d for d in danmaku_segments if os.path.exists(d)]
                if existing_danmaku:
                    if len(valid_files) == 1 or len(existing_danmaku) == 1:
                        merged_danmaku_path = existing_danmaku[0]
                    else:
                        danmaku_merged_name = os.path.splitext(os.path.basename(merged_path))[0] + ".danmaku.jsonl"
                        merged_danmaku_path = str(output_dir / danmaku_merged_name)
                        self.log(logging.INFO, "Merging %d danmaku file(s) → %s",
                                 len(existing_danmaku), danmaku_merged_name)
                        with open(merged_danmaku_path, "w", encoding="utf-8") as out_f:
                            for df in existing_danmaku:
                                try:
                                    with open(df, encoding="utf-8") as in_f:
                                        for line in in_f:
                                            stripped = line.strip()
                                            if stripped:
                                                out_f.write(stripped + "\n")
                                except Exception as e:
                                    self.log(logging.WARNING, "Could not read danmaku segment %s: %s",
                                             os.path.basename(df), e)

            # Step 3: Upload
            if _shutdown_event.is_set():
                self.log(logging.WARNING, "Shutdown detected, aborting post-processing (before upload)")
                return

            nas_host = self.config.get("nas_host", "nas")
            nas_base_dir = self.config.get("nas_dest_dir", "/volume1/Share/LiveVideos")
            month_str = first_timestamp[:4] + "-" + first_timestamp[4:6]  # YYYYMMDD_... → YYYY-MM
            nas_dest_dir = f"{nas_base_dir}/{self.label}/{month_str}"

            self._set_phase("UPLOADING", f"{nas_host}:{nas_dest_dir}/")
            self.log(logging.INFO, "Uploading to %s:%s/", nas_host, nas_dest_dir)

            mkdir_result = subprocess.run(
                ["ssh", nas_host, f"mkdir -p {nas_dest_dir}"],
                capture_output=True,
                text=True,
            )
            if mkdir_result.returncode != 0:
                self.log(logging.ERROR, "ssh mkdir -p failed: %s", mkdir_result.stderr)
                return

            # NAS filename: strip _merged suffix (kept locally to avoid conflict with segments)
            if len(valid_files) > 1:
                nas_mp4_name = os.path.basename(merged_path).replace("_merged.mp4", ".mp4")
            else:
                nas_mp4_name = os.path.basename(merged_path)

            rsync_result = subprocess.run(
                ["rsync", "-av", "--remove-source-files",
                 "--rsync-path=/usr/bin/rsync",
                 merged_path, f"{nas_host}:{nas_dest_dir}/{nas_mp4_name}"],
                capture_output=True,
                text=True,
            )
            if rsync_result.returncode != 0:
                self.log(
                    logging.ERROR,
                    "rsync failed (rc=%d): %s",
                    rsync_result.returncode,
                    rsync_result.stderr[-500:],
                )
                return

            # Upload danmaku file alongside MP4 (non-fatal if absent or fails)
            if merged_danmaku_path and os.path.exists(merged_danmaku_path):
                if len(valid_files) > 1:
                    nas_danmaku_name = os.path.basename(merged_danmaku_path).replace(
                        "_merged.danmaku.jsonl", ".danmaku.jsonl"
                    )
                else:
                    nas_danmaku_name = os.path.basename(merged_danmaku_path)
                danmaku_rsync = subprocess.run(
                    ["rsync", "-av", "--remove-source-files",
                     "--rsync-path=/usr/bin/rsync",
                     merged_danmaku_path, f"{nas_host}:{nas_dest_dir}/{nas_danmaku_name}"],
                    capture_output=True,
                    text=True,
                )
                if danmaku_rsync.returncode != 0:
                    self.log(logging.WARNING, "Danmaku rsync failed (rc=%d): %s",
                             danmaku_rsync.returncode, danmaku_rsync.stderr[-200:])
                else:
                    self.log(logging.INFO, "Danmaku uploaded: %s", nas_danmaku_name)

            self._notify({
                "msg_type": "text",
                "event": "nas_sync_done",
                "label": self.label,
                "url": self.url,
                "merged_filename": nas_mp4_name,
                "nas_dest_dir": nas_dest_dir,
            })

            # Step 5: Clean local segments
            if _shutdown_event.is_set():
                self.log(logging.WARNING, "Shutdown detected, skipping local cleanup (files preserved)")
                return

            self._set_phase("CLEANING")
            self.log(logging.INFO, "Upload done. Cleaning up %d local segment(s)...", len(files))
            for f in files:
                try:
                    os.remove(f)
                    self.log(logging.INFO, "Deleted local segment: %s", f)
                except FileNotFoundError:
                    pass  # already removed (e.g. single-file rsync --remove-source-files)
                except Exception as e:
                    self.log(logging.WARNING, "Failed to delete %s: %s", f, e)

            # Clean up individual danmaku segment files (merged file already removed by rsync)
            if merged_danmaku_path and len(valid_files) > 1:
                for f in valid_files:
                    danmaku_seg = os.path.splitext(f)[0] + ".danmaku.jsonl"
                    if os.path.exists(danmaku_seg) and danmaku_seg != merged_danmaku_path:
                        try:
                            os.remove(danmaku_seg)
                            self.log(logging.INFO, "Deleted danmaku segment: %s", os.path.basename(danmaku_seg))
                        except Exception as e:
                            self.log(logging.WARNING, "Failed to delete danmaku segment %s: %s",
                                     os.path.basename(danmaku_seg), e)

            self.log(logging.INFO, "Post-processing complete.")

        finally:
            self._post_processing = False
            self._set_phase("IDLE")

    @property
    def _state_path(self) -> str:
        safe = sanitize_filename(self.label)
        return os.path.join(self.config["output_dir"], f".douyin_state_{safe}.json")

    def _save_state(self) -> None:
        """Atomically persist state to disk."""
        state = {
            "consecutive_offline": self._consecutive_offline,
            "offline_since": self._offline_since,
            "session_files": self._session_files,
            "current_recording": self._current_recording,
            "quality_index": self._quality_index,
            "segment_quality": self._segment_quality,
        }
        tmp = self._state_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, self._state_path)
        except Exception as e:
            self.log(logging.WARNING, "Failed to save state: %s", e)

    def _load_state(self) -> None:
        """Restore persisted state from disk if available."""
        path = self._state_path
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            self._consecutive_offline = int(state.get("consecutive_offline", 0))
            self._offline_since = state.get("offline_since")
            self._session_files = state.get("session_files") or []
            self._current_recording = state.get("current_recording")
            self._quality_index = int(state.get("quality_index", 0))
            self._segment_quality = state.get("segment_quality") or {}
            self.log(logging.INFO,
                "Restored state: consecutive_offline=%d, session_files=%d, quality_index=%d",
                self._consecutive_offline, len(self._session_files), self._quality_index,
            )
        except Exception as e:
            self.log(logging.WARNING, "Failed to load state (ignoring): %s", e)

    def _notify(self, payload: dict) -> None:
        """POST JSON to Feishu webhook. Non-fatal."""
        url = self.config.get("feishu_webhook_url")
        if not url:
            return
        import urllib.request, urllib.error
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    self.log(logging.WARNING, "Feishu webhook HTTP %d", resp.status)
        except Exception as e:
            self.log(logging.WARNING, "Feishu webhook error: %s", e)

    def _set_phase(self, phase: str, detail: str | None = None) -> None:
        """Update current phase and persist to status file."""
        if phase != self._phase:
            self._phase_since = time.time()
        self._phase = phase
        self._phase_detail = detail
        self._write_status()

    def _write_status(self) -> None:
        """Atomically write current state to .douyin_status_{label}.json."""
        status = {
            "label": self.label,
            "url": self.url,
            "phase": self._phase,
            "phase_since": self._phase_since,
            "detail": self._phase_detail,
            "rec_started_at": self._rec_start_ts,
            "offline_since": self._offline_since,
            "consecutive_offline": self._consecutive_offline,
            "next_poll_at": self._next_poll_at,
            "post_process_delay": self.config.get("post_process_delay", 1800),
            "session_files_count": len(self._session_files),
            "updated_at": time.time(),
        }
        safe = sanitize_filename(self.label)
        status_path = os.path.join(self.config["output_dir"], f".douyin_status_{safe}.json")
        tmp = status_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False)
            os.replace(tmp, status_path)
        except Exception as e:
            self.log(logging.WARNING, "Failed to write status: %s", e)

    def run(self) -> None:
        output_dir = Path(self.config["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)

        poll_normal = self.config.get("poll_interval_online", 60)
        poll_slow = self.config.get("poll_interval_slow", 120)
        poll_very_slow = self.config.get("poll_interval_very_slow", 300)
        slow_threshold = self.config.get("slow_threshold_checks", 30)
        very_slow_threshold = self.config.get("very_slow_threshold_checks", 120)

        self._load_state()
        self.log(logging.INFO, "Starting monitor for: %s", self.url)

        # Recovery: handle recording interrupted by a previous restart
        if self._current_recording is not None:
            rec = self._current_recording
            rec_path = rec.get("path", "")
            if rec_path and os.path.exists(rec_path):
                self.log(logging.INFO, "Recovering interrupted recording: %s", os.path.basename(rec_path))
                if rec_path not in self._session_files:
                    self._session_files.append(rec_path)
                if self.config.get("danmaku_enabled", False):
                    danmaku_path = os.path.splitext(rec_path)[0] + ".danmaku.jsonl"
                    self.log(logging.INFO, "Resuming danmaku capture → %s", os.path.basename(danmaku_path))
                    self._danmaku_recorder = DanmakuRecorder(self.url, danmaku_path, self.config, self.label)
                    self._danmaku_recorder.start()
            else:
                self.log(logging.WARNING, "Interrupted recording not found on disk (skipping): %s", rec_path)
            self._current_recording = None
            self._save_state()

        self._set_phase("IDLE")
        while not _shutdown_event.is_set():
            is_live, uploader = self.check_live_info_with_retry()

            if _shutdown_event.is_set():
                break

            if is_live is None:
                interval = poll_slow
                self.log(logging.WARNING, "Network unavailable, backing off %ds", interval)
                interruptible_sleep(interval)
                continue

            if not is_live:
                self._consecutive_offline += 1
                if self._offline_since is None:
                    self._offline_since = time.time()
                self._save_state()

                if self._consecutive_offline >= very_slow_threshold:
                    interval = poll_very_slow
                elif self._consecutive_offline >= slow_threshold:
                    interval = poll_slow
                else:
                    interval = poll_normal
                self.log(logging.DEBUG, "Offline (check #%d), next check in %ds", self._consecutive_offline, interval)

                # Trigger post-processing if offline long enough
                delay = self.config.get("post_process_delay", 1800)
                if (
                    self.config.get("nas_enabled", False)
                    and self._offline_since is not None
                    and time.time() - self._offline_since >= delay
                    and self._session_files
                    and not self._post_processing
                ):
                    self._post_processing = True
                    files_to_process = self._session_files[:]
                    self._session_files = []
                    self._offline_since = None
                    self._save_state()
                    t = threading.Thread(
                        target=self._post_process_session,
                        args=(files_to_process,),
                        daemon=True,
                        name=f"{self.label}-postproc",
                    )
                    t.start()

                self._next_poll_at = time.time() + interval
                if self._post_processing:
                    self._write_status()  # update next_poll_at without overwriting postproc phase
                elif self._session_files:
                    self._set_phase("WAITING", f"{len(self._session_files)} file(s) pending")
                else:
                    self._set_phase("IDLE")
                interruptible_sleep(interval)
                continue

            # Stream is live
            self._consecutive_offline = 0
            self._offline_since = None  # reset offline timer; keep _session_files for this session
            self._save_state()
            name = uploader or self.label
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"抖音_{name}_{timestamp}.mp4"
            output_path = str(output_dir / filename)

            self._next_poll_at = None
            self._set_phase("CONNECTING")
            ok = self.start_recording(output_path)
            if not ok:
                self._set_phase("IDLE")
                interruptible_sleep(poll_normal)
                continue

            self._set_phase("RECORDING", os.path.basename(output_path))
            self._notify({
                "msg_type": "text",
                "event": "stream_live",
                "label": self.label,
                "url": self.url,
                "uploader": name,
                "started_at_str": datetime.fromtimestamp(self._rec_start_ts).strftime("%Y-%m-%d %H:%M:%S"),
                "output_path": output_path,
            })

            # Wait for ffmpeg to finish
            try:
                while self._ff_proc is not None and self._ff_proc.poll() is None:
                    if _shutdown_event.is_set():
                        self.stop()  # stop streamlink; ffmpeg will get EOF and finalize
                        # Wait up to 30s for ffmpeg to write the moov atom naturally
                        try:
                            self._ff_proc.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            self.log(logging.WARNING, "ffmpeg did not finalize in 30s, sending SIGTERM")
                            if self._ff_pgid is not None:
                                try:
                                    os.killpg(self._ff_pgid, signal.SIGTERM)
                                except ProcessLookupError:
                                    pass
                            try:
                                self._ff_proc.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                self.log(logging.WARNING, "ffmpeg did not exit after SIGTERM")
                        break
                    time.sleep(1)
            except Exception:
                pass

            if self._ff_proc is not None:
                returncode = self._ff_proc.returncode
            else:
                returncode = 0

            self._sl_proc = None
            self._ff_proc = None
            self._sl_pgid = None
            self._ff_pgid = None

            if self._danmaku_recorder is not None:
                self._danmaku_recorder.stop()
                self._danmaku_recorder = None

            if _shutdown_event.is_set():
                break

            # Adaptive quality: capture quality and duration before clearing state
            rec_quality = (self._current_recording or {}).get("quality", "best")
            rec_duration = int(time.time()) - (self._rec_start_ts or int(time.time()))

            if returncode not in (0, None):
                self.log(logging.WARNING, "ffmpeg exited with code %d (duration=%ds)", returncode, rec_duration)
                # Downgrade quality on short error exit (likely network issue)
                if self.config.get("adaptive_quality", True):
                    ladder = self.config.get("streamlink_quality_ladder", ["best", "720p", "480p", "worst"])
                    downgrade_thresh = self.config.get("quality_downgrade_threshold", 60)
                    if rec_duration < downgrade_thresh and self._quality_index < len(ladder) - 1:
                        self._quality_index += 1
                        self.log(logging.WARNING, "Downgrading quality → %s (short recording)",
                                 ladder[min(self._quality_index, len(ladder) - 1)])
                self._set_phase("IDLE")
            else:
                self.log(logging.INFO, "Recording finished: %s", output_path)
                self._segment_quality[output_path] = rec_quality
                self._session_files.append(output_path)
                # Upgrade quality after a long stable recording
                if self.config.get("adaptive_quality", True):
                    ladder = self.config.get("streamlink_quality_ladder", ["best", "720p", "480p", "worst"])
                    upgrade_thresh = self.config.get("quality_upgrade_threshold", 300)
                    if rec_duration >= upgrade_thresh and self._quality_index > 0:
                        self._quality_index -= 1
                        self.log(logging.INFO, "Upgrading quality → %s (stable recording %ds)",
                                 ladder[self._quality_index], rec_duration)
                _ended_at = int(time.time())
                self._notify({
                    "msg_type": "text",
                    "event": "stream_ended",
                    "label": self.label,
                    "url": self.url,
                    "filename": os.path.basename(output_path),
                    "started_at_str": datetime.fromtimestamp(self._rec_start_ts).strftime("%Y-%m-%d %H:%M:%S") if self._rec_start_ts else "",
                    "duration_seconds": _ended_at - (self._rec_start_ts or _ended_at),
                    "file_size_mb": round(os.path.getsize(output_path) / (1024 * 1024), 1) if os.path.exists(output_path) else 0,
                })
            self._current_recording = None
            self._rec_start_ts = None
            self._save_state()

            self.log(logging.INFO, "Stream ended, waiting 10s before next check...")
            interruptible_sleep(10)


def handle_signal(signum, frame):
    logging.info("Received signal %d, shutting down...", signum)
    _shutdown_event.set()
    for m in _all_monitors:
        m.stop()


def main():
    parser = argparse.ArgumentParser(description="Douyin Live Stream Monitor")
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "config.json"),
        help="Path to config.json",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config.get("log_level", "INFO"))

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    monitors = [
        StreamerMonitor(s["url"], s.get("name"), config)
        for s in config["streamers"]
    ]
    _all_monitors.extend(monitors)

    threads = [threading.Thread(target=m.run, daemon=True, name=m.label) for m in monitors]
    for t in threads:
        t.start()

    try:
        for t in threads:
            t.join()
    except Exception as e:
        logging.exception("Unexpected error: %s", e)
        sys.exit(1)

    logging.info("Monitor stopped.")


if __name__ == "__main__":
    main()

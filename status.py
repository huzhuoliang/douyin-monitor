#!/usr/bin/env python3
"""douyin-status — show current state of all monitored streamers (like docker ps)"""

import argparse
import json
import os
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path


def display_width(s: str) -> int:
    """Return terminal display width of string (CJK chars count as 2)."""
    w = 0
    for c in s:
        w += 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
    return w


def rpad(s: str, width: int) -> str:
    """Pad s to terminal display width."""
    return s + " " * max(0, width - display_width(s))


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 0:
        return "0s"
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def fmt_time(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


PHASE_COLOR = {
    "STARTING":    "\033[90m",   # gray
    "IDLE":        "\033[90m",   # gray
    "CONNECTING":  "\033[96m",   # cyan
    "RECORDING":   "\033[92m",   # bright green
    "WATERMARKING":"\033[93m",   # yellow
    "WAITING":     "\033[93m",   # yellow
    "VALIDATING":  "\033[94m",   # blue
    "MERGING":     "\033[94m",   # blue
    "TRANSCRIBING":"\033[95m",   # magenta
    "UPLOADING":   "\033[94m",   # blue
    "CLEANING":    "\033[94m",   # blue
}

POSTPROC_NEXT = {
    "WATERMARKING": "→ VALIDATING",
    "VALIDATING":   "→ MERGING / UPLOAD",
    "MERGING":      "→ UPLOADING",
    "TRANSCRIBING": "→ UPLOADING",
    "UPLOADING":    "→ CLEANING → done",
    "CLEANING":     "→ done",
}
RESET = "\033[0m"
DIM   = "\033[2m"
BOLD  = "\033[1m"


def compute_detail(s: dict) -> str:
    phase = s.get("phase", "")
    detail = s.get("detail") or ""
    now = time.time()

    if phase == "RECORDING":
        rec_ts = s.get("rec_started_at")
        if rec_ts and detail:
            dur = fmt_duration(now - rec_ts)
            return f"{detail} ({dur})"
        return detail

    if phase in ("IDLE", "WAITING"):
        parts = []
        offline_since = s.get("offline_since")
        cons = s.get("consecutive_offline", 0)
        sess = s.get("session_files_count", 0)
        if sess:
            parts.append(f"{sess} file(s) pending")
        if offline_since:
            parts.append(f"offline {fmt_duration(now - offline_since)}")
        if cons:
            parts.append(f"{cons} checks")
        return "  ".join(parts) if parts else "-"

    return detail or "-"


def compute_next(s: dict) -> str:
    phase = s.get("phase", "")
    now = time.time()

    if phase == "IDLE":
        npa = s.get("next_poll_at")
        if npa:
            diff = npa - now
            if diff > 0:
                return f"poll in {int(diff)}s  (@ {fmt_time(npa)})"
        return "polling..."

    if phase == "WAITING":
        offline_since = s.get("offline_since")
        delay = s.get("post_process_delay", 1800)
        if offline_since:
            eta = offline_since + delay
            diff = eta - now
            if diff > 0:
                return f"post-proc @ {fmt_time(eta)}  (in {fmt_duration(diff)})"
            return "post-proc imminent"
        return "-"

    next_map = {
        "STARTING":    "initializing...",
        "CONNECTING":  "→ RECORDING",
        "RECORDING":   "waiting for stream end",
        "WATERMARKING":"→ WAITING / IDLE",
        "VALIDATING":  "→ MERGE / TRANSCRIBE",
        "MERGING":     "→ TRANSCRIBE / UPLOAD",
        "TRANSCRIBING":"→ UPLOAD",
        "UPLOADING":   "→ CLEANUP → IDLE",
        "CLEANING":    "→ IDLE",
    }
    return next_map.get(phase, "-")


def main():
    parser = argparse.ArgumentParser(
        description="Show douyin monitor status (like docker ps)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "config.json"),
        help="Path to config.json (default: same dir as status.py)",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable color output")
    parser.add_argument("--json", action="store_true", dest="output_json",
                        help="Output raw JSON")
    args = parser.parse_args()

    try:
        with open(args.config, encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        print(f"Cannot read config: {e}", file=sys.stderr)
        sys.exit(1)

    output_dir = config.get("output_dir", ".")
    status_files = sorted(Path(output_dir).glob(".douyin_status_*.json"))

    if not status_files:
        print(f"No status files found in {output_dir}")
        print("Is the monitor running? (sudo systemctl status douyin-monitor)")
        return

    statuses = []
    for sf in status_files:
        try:
            with open(sf, encoding="utf-8") as f:
                statuses.append(json.load(f))
        except Exception:
            pass

    if not statuses:
        print("No readable status files found.")
        return

    if args.output_json:
        print(json.dumps(statuses, ensure_ascii=False, indent=2))
        return

    use_color = not args.no_color and sys.stdout.isatty()
    now = time.time()

    rows = []
    for s in statuses:
        phase = s.get("phase", "?")
        phase_since = s.get("phase_since")
        updated_at = s.get("updated_at")
        stale = updated_at and (now - updated_at) > 300  # stale if > 5 min

        rows.append({
            "label":    s.get("label", "?"),
            "phase":    phase,
            "since":    fmt_time(phase_since),
            "duration": fmt_duration(now - phase_since) if phase_since else "-",
            "detail":   compute_detail(s),
            "next":     compute_next(s),
            "stale":    stale,
            "is_sub":   False,
            "_raw":     s,
        })

        # Postproc sub-row: shown when postproc thread is running in parallel
        pp_phase = s.get("postproc_phase")
        if pp_phase:
            pp_since = s.get("postproc_phase_since")
            rows.append({
                "label":    "  └─",
                "phase":    pp_phase,
                "since":    fmt_time(pp_since),
                "duration": fmt_duration(now - pp_since) if pp_since else "-",
                "detail":   s.get("postproc_detail") or "-",
                "next":     POSTPROC_NEXT.get(pp_phase, "-"),
                "stale":    False,
                "is_sub":   True,
                "_raw":     s,
            })

    # Column widths (in display chars)
    col = {
        "label":    max(7,  max(display_width(r["label"])    for r in rows) + 1),
        "phase":    max(13, max(display_width(r["phase"])    for r in rows) + 1),
        "since":    10,
        "duration": 10,
        "detail":   max(20, min(45, max(display_width(r["detail"]) for r in rows) + 2)),
    }

    def header_line():
        return (
            rpad("LABEL",    col["label"])
            + rpad("PHASE",    col["phase"])
            + rpad("SINCE",    col["since"])
            + rpad("DURATION", col["duration"])
            + rpad("DETAIL",   col["detail"])
            + "NEXT"
        )

    total_w = sum(col.values()) + 30
    sep = "─" * total_w

    if use_color:
        print(f"\n{BOLD}{header_line()}{RESET}")
    else:
        print(f"\n{header_line()}")
    print(sep)

    for r in rows:
        phase = r["phase"]
        color = PHASE_COLOR.get(phase, "") if use_color else ""
        reset = RESET if use_color else ""

        label_str    = rpad(r["label"],    col["label"])
        phase_str    = rpad(r["phase"],    col["phase"])
        since_str    = rpad(r["since"],    col["since"])
        duration_str = rpad(r["duration"], col["duration"])
        detail_str   = rpad(r["detail"],   col["detail"])
        next_str     = r["next"]

        line = label_str + color + phase_str + reset + since_str + duration_str + detail_str + next_str

        if r.get("stale") and use_color:
            line = DIM + line + RESET
        elif r.get("is_sub") and use_color:
            # Sub-row: dim label and metadata, keep phase color
            line = (DIM + label_str + reset
                    + color + phase_str + reset
                    + DIM + since_str + duration_str + detail_str + next_str + reset)

        print(line)

    print(sep)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    n_streamers = sum(1 for r in rows if not r.get("is_sub"))
    print(f"  {now_str}  |  {n_streamers} streamer(s)  |  output: {output_dir}\n")


if __name__ == "__main__":
    main()

"""Douyin live danmaku recorder.

Connects to Douyin's WebSocket API to capture real-time chat messages and
events during a live stream, writing them to a JSONL file.

Requires: websocket-client  (pip3 install websocket-client)
Optional: set `danmaku_cookies` in config.json to a raw Cookie string
          (e.g. "__ac_nonce=xxx; ttwid=yyy") for a more stable connection.

Output format — one JSON object per line:
  {"ts": <unix_float>, "type": "chat",    "user": {"id": 123, "nickname": "xxx"}, "content": "..."}
  {"ts": ...,          "type": "gift",    "user": {...}, "gift_id": 1234, "repeat_count": 1}
  {"ts": ...,          "type": "like",    "user": {...}, "count": 1, "total": 999}
  {"ts": ...,          "type": "member",  "user": {...}}
  {"ts": ...,          "type": "social",  "user": {...}}
  {"ts": ...,          "type": "viewers", "total": 12345}
  {"ts": ...,          "type": "control", "status": 3}   # status 3 = stream ended
"""

import gzip
import http.cookiejar
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional

# ── Minimal protobuf wire-format parser (no dependencies) ────────────────────

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Decode a varint from *data* starting at *pos*. Returns (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise ValueError("Truncated varint")


def _parse_proto(data: bytes) -> dict:
    """Parse raw protobuf bytes into {field_num: [value, ...]} without a schema.

    Values are:
      wire type 0 (varint)          → int
      wire type 1 (64-bit)          → int
      wire type 2 (length-delimited) → bytes
      wire type 5 (32-bit)          → int
    """
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _read_varint(data, pos)
        except (ValueError, IndexError):
            break
        field_num = tag >> 3
        wire_type = tag & 0x7

        try:
            if wire_type == 0:
                val, pos = _read_varint(data, pos)
                fields.setdefault(field_num, []).append(val)
            elif wire_type == 1:
                val = int.from_bytes(data[pos:pos + 8], "little")
                pos += 8
                fields.setdefault(field_num, []).append(val)
            elif wire_type == 2:
                length, pos = _read_varint(data, pos)
                val = data[pos:pos + length]
                pos += length
                fields.setdefault(field_num, []).append(val)
            elif wire_type == 5:
                val = int.from_bytes(data[pos:pos + 4], "little")
                pos += 4
                fields.setdefault(field_num, []).append(val)
            else:
                break  # unknown wire type — stop
        except (ValueError, IndexError):
            break
    return fields


def _pb_str(fields: dict, field_num: int, default: str = "") -> str:
    val = fields.get(field_num, [b""])[0]
    if isinstance(val, bytes):
        try:
            return val.decode("utf-8")
        except UnicodeDecodeError:
            return default
    return str(val) if val else default


def _pb_int(fields: dict, field_num: int, default: int = 0) -> int:
    val = fields.get(field_num, [default])[0]
    return int(val) if isinstance(val, int) else default


def _pb_bytes(fields: dict, field_num: int) -> Optional[bytes]:
    val = fields.get(field_num, [None])[0]
    return val if isinstance(val, bytes) else None


# ── Per-message-type decoders ────────────────────────────────────────────────

def _decode_user(data: bytes) -> dict:
    f = _parse_proto(data)
    return {
        "id": _pb_int(f, 1),
        "nickname": _pb_str(f, 3),
    }


def _decode_chat(payload: bytes) -> Optional[dict]:
    f = _parse_proto(payload)
    user_b = _pb_bytes(f, 2)
    return {
        "type": "chat",
        "user": _decode_user(user_b) if user_b else {},
        "content": _pb_str(f, 3),
    }


def _decode_gift(payload: bytes) -> Optional[dict]:
    f = _parse_proto(payload)
    user_b = _pb_bytes(f, 2)
    return {
        "type": "gift",
        "user": _decode_user(user_b) if user_b else {},
        "gift_id": _pb_int(f, 5),
        "repeat_count": _pb_int(f, 11),
        "group_count": _pb_int(f, 8),
    }


def _decode_like(payload: bytes) -> Optional[dict]:
    f = _parse_proto(payload)
    user_b = _pb_bytes(f, 5)
    return {
        "type": "like",
        "user": _decode_user(user_b) if user_b else {},
        "count": _pb_int(f, 2),
        "total": _pb_int(f, 3),
    }


def _decode_member(payload: bytes) -> Optional[dict]:
    f = _parse_proto(payload)
    user_b = _pb_bytes(f, 2)
    return {
        "type": "member",
        "user": _decode_user(user_b) if user_b else {},
    }


def _decode_social(payload: bytes) -> Optional[dict]:
    f = _parse_proto(payload)
    user_b = _pb_bytes(f, 2)
    return {
        "type": "social",
        "user": _decode_user(user_b) if user_b else {},
    }


def _decode_viewers(payload: bytes) -> Optional[dict]:
    f = _parse_proto(payload)
    return {
        "type": "viewers",
        "total": _pb_int(f, 2),
    }


def _decode_control(payload: bytes) -> Optional[dict]:
    # status 3 = live ended
    f = _parse_proto(payload)
    return {
        "type": "control",
        "status": _pb_int(f, 2),
    }


_DECODERS = {
    "WebcastChatMessage":        _decode_chat,
    "WebcastGiftMessage":        _decode_gift,
    "WebcastLikeMessage":        _decode_like,
    "WebcastMemberMessage":      _decode_member,
    "WebcastSocialMessage":      _decode_social,
    "WebcastRoomUserSeqMessage": _decode_viewers,
    "WebcastControlMessage":     _decode_control,
}

# ── WebSocket frame decoder ──────────────────────────────────────────────────

# Minimal PushFrame heartbeat: field 7 (payload_type) = "hb"
# Tag = (7 << 3)|2 = 0x3A, length = 2, value = b"hb"
_WS_HEARTBEAT = b"\x3a\x02hb"


def _decode_ws_message(raw: bytes) -> list[dict]:
    """Decode one raw WebSocket binary frame from Douyin live.

    Outer envelope is a PushFrame (protobuf):
      field 6 = payload_encoding (string, "gzip" or "")
      field 8 = payload (bytes)

    After optional gzip-decompression, payload is a Response:
      field 1 = repeated Message
        field 1 = method (string)  e.g. "WebcastChatMessage"
        field 2 = payload (bytes)  inner message
    """
    records: list[dict] = []
    try:
        frame = _parse_proto(raw)
        payload_encoding = _pb_str(frame, 6)
        payload = _pb_bytes(frame, 8)
        if not payload:
            return records

        if payload_encoding == "gzip":
            try:
                payload = gzip.decompress(payload)
            except Exception:
                return records

        response = _parse_proto(payload)
        for msg_bytes in response.get(1, []):
            if not isinstance(msg_bytes, bytes):
                continue
            msg = _parse_proto(msg_bytes)
            method = _pb_str(msg, 1)
            msg_payload = _pb_bytes(msg, 2)
            if not method or not msg_payload:
                continue
            decoder = _DECODERS.get(method)
            if decoder:
                try:
                    rec = decoder(msg_payload)
                    if rec:
                        rec["method"] = method
                        records.append(rec)
                except Exception:
                    pass
    except Exception:
        pass
    return records


# ── DanmakuRecorder ──────────────────────────────────────────────────────────

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_WS_HOST = "webcast3-ws-web-lf.douyin.com"
_WS_PATH = "/webcast/im/push/v2/"


class DanmakuRecorder:
    """Records Douyin live danmaku to a JSONL file in a background daemon thread.

    Usage::
        recorder = DanmakuRecorder(live_url, output_path, config, label)
        recorder.start()
        # ... wait for recording to end ...
        recorder.stop()
    """

    def __init__(self, live_url: str, output_path: str, config: dict, label: str):
        self.live_url = live_url
        self.output_path = output_path
        self.config = config
        self.label = label
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._ws = None

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"{self.label}-danmaku",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None

    # ── Internals ─────────────────────────────────────────────────────────────

    def log(self, level: int, msg: str, *args) -> None:
        logging.log(level, "[%s][danmaku] " + msg, self.label, *args)

    def _run(self) -> None:
        """Main loop: connect → record → retry on disconnect (up to 3 times)."""
        max_retries = 3
        for attempt in range(max_retries):
            if self._stop_event.is_set():
                break
            try:
                self._try_connect()
            except Exception as e:
                self.log(logging.WARNING, "Unexpected error: %s", e)
            if self._stop_event.is_set():
                break
            if attempt < max_retries - 1:
                self.log(logging.INFO, "Reconnecting in 5s (attempt %d/%d)…", attempt + 1, max_retries)
                self._stop_event.wait(5)

    def _try_connect(self) -> None:
        m = re.search(r"live\.douyin\.com/(\d+)", self.live_url)
        if not m:
            self.log(logging.WARNING, "Cannot parse web_rid from URL: %s", self.live_url)
            return
        web_rid = m.group(1)

        room_id, cookie_str = self._fetch_room_id(web_rid)
        if not room_id:
            self.log(logging.WARNING, "Could not determine room_id — danmaku disabled for this session")
            return

        self.log(logging.INFO, "Connecting (room_id=%s)", room_id)
        self._connect_ws(room_id, cookie_str)

    # ── Room ID extraction ────────────────────────────────────────────────────

    def _fetch_room_id(self, web_rid: str) -> tuple[Optional[str], str]:
        """Resolve the internal room_id for *web_rid* and return a merged cookie string.

        Uses the official webcast room-enter API which returns a JSON response
        with the true room_id (a 19-digit integer, different from the URL web_rid).

        The live page is visited WITH existing config cookies so the server issues
        a session-bound ttwid (rather than an anonymous one).  The final cookie
        string is: config cookies + any new cookies set by the server, with
        config values taking priority for overlapping names.
        """
        custom_cookies: str = self.config.get("danmaku_cookies") or ""

        # Parse config cookies into a dict (used for merging later)
        config_dict: dict[str, str] = {}
        for part in custom_cookies.split(";"):
            if "=" in part:
                k, _, v = part.strip().partition("=")
                config_dict[k.strip()] = v.strip()

        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        base_headers = {
            "User-Agent": _UA,
            "Referer": "https://live.douyin.com/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

        # Step 1: visit live page WITH config cookies so the server can issue
        # a ttwid tied to this session (avoids mismatch with sessionid)
        try:
            visit_headers = dict(base_headers)
            if custom_cookies:
                visit_headers["Cookie"] = custom_cookies
            req = urllib.request.Request(
                f"https://live.douyin.com/{web_rid}", headers=visit_headers
            )
            with opener.open(req, timeout=15) as resp:
                resp.read()
        except Exception as e:
            self.log(logging.WARNING, "Failed to fetch live page: %s", e)

        # Merge: fresh server-set cookies (e.g. ttwid) + config cookies
        # Config cookies take priority so sessionid is never overwritten
        fresh_dict = {c.name: c.value for c in jar}
        merged = {**fresh_dict, **config_dict}
        cookie_str = "; ".join(f"{k}={v}" for k, v in merged.items())

        # Step 2: call webcast room-enter API to get the true room_id
        params = urllib.parse.urlencode({
            "aid": "6383", "app_name": "douyin_web", "live_id": "1",
            "device_platform": "web", "language": "zh-CN",
            "enter_from": "web_live", "cookie_enabled": "true",
            "screen_width": "1920", "screen_height": "1080",
            "browser_language": "zh-CN", "browser_platform": "Win32",
            "browser_name": "Chrome", "browser_version": "120.0.0.0",
            "browser_online": "true", "tz_name": "Asia/Shanghai",
            "web_rid": web_rid,
        })
        api_url = f"https://live.douyin.com/webcast/room/web/enter/?{params}"
        try:
            req = urllib.request.Request(api_url, headers={**base_headers, "Cookie": cookie_str})
            with opener.open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))

            rooms = data.get("data", {}).get("data", [])
            if rooms:
                room_id = rooms[0].get("id_str") or rooms[0].get("id")
                if room_id:
                    room_id = str(room_id)
                    self.log(logging.DEBUG, "room_id=%s (from API)", room_id)
                    return room_id, cookie_str

            # Fallback: use enter_room_id if present
            enter_id = str(data.get("data", {}).get("enter_room_id", ""))
            if enter_id and enter_id != "0":
                self.log(logging.DEBUG, "room_id=%s (from enter_room_id)", enter_id)
                return enter_id, cookie_str

        except Exception as e:
            self.log(logging.WARNING, "Room-enter API failed: %s", e)

        # Last resort: use web_rid directly
        self.log(logging.WARNING, "Could not resolve room_id from API, falling back to web_rid=%s", web_rid)
        return web_rid, cookie_str

    # ── WebSocket connection ──────────────────────────────────────────────────

    def _connect_ws(self, room_id: str, cookie_str: str) -> None:
        try:
            import websocket
        except ImportError:
            self.log(
                logging.WARNING,
                "websocket-client not installed — danmaku capture disabled. "
                "Install with: pip3 install websocket-client --break-system-packages",
            )
            return

        # Extract user_unique_id from cookies if available (helps with auth)
        uid = ""
        for part in cookie_str.split(";"):
            if "=" in part:
                k, _, v = part.strip().partition("=")
                if k.strip() == "uid_tt":
                    uid = v.strip()
                    break

        params = urllib.parse.urlencode({
            "app_name": "douyin_web",
            "version_code": "180800",
            "webcast_sdk_version": "1.0.14-beta.0",
            "room_id": room_id,
            "aid": "6383",
            "live_id": "1",
            "compress": "gzip",
            "device_platform": "web",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "124.0.0.0",
            "browser_online": "true",
            "tz_name": "Asia/Shanghai",
            **({"user_unique_id": uid} if uid else {}),
        })
        ws_url = f"wss://{_WS_HOST}{_WS_PATH}?{params}"

        headers = [
            f"User-Agent: {_UA}",
            "Origin: https://live.douyin.com",
            "Referer: https://live.douyin.com/",
        ]
        if cookie_str:
            headers.append(f"Cookie: {cookie_str}")

        stop = self._stop_event
        label = self.label
        output_path = self.output_path

        # Open output file in append mode (survives reconnects)
        try:
            output_file = open(output_path, "a", encoding="utf-8")
        except Exception as e:
            self.log(logging.ERROR, "Cannot open output file %s: %s", output_path, e)
            return

        def on_open(ws):
            self.log(logging.INFO, "Connected — writing to %s", os.path.basename(output_path))

            def _heartbeat():
                while not stop.is_set():
                    try:
                        ws.send(_WS_HEARTBEAT, opcode=websocket.ABNF.OPCODE_BINARY)
                    except Exception:
                        break
                    stop.wait(10)

            threading.Thread(target=_heartbeat, daemon=True, name=f"{label}-danmaku-hb").start()

        def on_message(ws, raw):
            try:
                records = _decode_ws_message(raw)
                if not records:
                    return
                ts = time.time()
                for rec in records:
                    rec["ts"] = ts
                    output_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
                output_file.flush()
            except Exception as e:
                logging.debug("[%s][danmaku] decode error: %s", label, e)

        def on_error(ws, error):
            self.log(logging.WARNING, "WebSocket error: %s", error)

        def on_close(ws, close_status_code, close_msg):
            self.log(logging.INFO, "Disconnected (code=%s)", close_status_code)
            try:
                output_file.close()
            except Exception:
                pass

        ws = websocket.WebSocketApp(
            ws_url,
            header=headers,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws = ws

        # run_forever blocks until the connection closes or ws.close() is called
        ws.run_forever()
        self._ws = None

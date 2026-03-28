#!/usr/bin/env python3
"""Douyin cookie helper — saves session cookies to config.json for danmaku auth.

Two modes:

  1. --paste  (default)
     Paste cookies you copied from your browser's DevTools (takes 30 seconds).
     Usage:
       python3 login.py
       python3 login.py --paste

  2. --qr  (experimental, may fail due to Douyin bot-protection)
     Tries the newer passport.douyin.com QR-login API and waits for confirmation.
     Usage:
       python3 login.py --qr

Why --paste is recommended:
  Douyin's QR login endpoints now require browser-generated JS signatures.
  Pure-Python HTTP requests cannot replicate them reliably, so the QR approach
  may return an error. Browser copy-paste always works.

How to copy cookies from Chrome/Edge (--paste mode):
  1. Open https://live.douyin.com/ in your browser while logged into Douyin.
  2. Press F12 → Network tab → refresh the page (F5).
  3. Click any request to live.douyin.com.
  4. In the Request Headers panel, find the "cookie:" line.
  5. Copy the entire value (the long string after "cookie:").
  6. Run this script and paste when prompted.

Requires (qr mode only): pip3 install qrcode --break-system-packages
"""

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.parse
import urllib.request

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_REFERER = "https://live.douyin.com/"


def _get(opener, url, extra_headers=None):
    headers = {"User-Agent": _UA, "Referer": _REFERER}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


def update_config(config_path, cookie_str):
    # Write cookie string to a separate file next to config.json
    config_dir = os.path.dirname(os.path.abspath(config_path))
    cookie_file = os.path.join(config_dir, "danmaku_cookies.txt")
    cookie_tmp = cookie_file + ".tmp"
    with open(cookie_tmp, "w", encoding="utf-8") as f:
        f.write(cookie_str)
    os.replace(cookie_tmp, cookie_file)

    # Store the absolute path to the cookie file in config (not the raw string)
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)
    config["danmaku_cookies"] = cookie_file
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(tmp, config_path)


_REQUIRED_COOKIES = ["sessionid"]
_IMPORTANT_COOKIES = ["__ac_nonce", "ttwid", "msToken"]


def validate_cookies(cookie_str: str) -> tuple[bool, list[str]]:
    """Check required/important cookies. Returns (ok, list_of_missing_important)."""
    ok = all(c in cookie_str for c in _REQUIRED_COOKIES)
    missing_important = [c for c in _IMPORTANT_COOKIES if c not in cookie_str]
    return ok, missing_important


def mode_paste(config_path: str) -> None:
    """Interactive paste mode: user copies cookie string from browser DevTools."""
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║          抖音 Cookie 提取助手（浏览器粘贴模式）          ║")
    print("╠══════════════════════════════════════════════════════════╣")
    print("║                                                          ║")
    print("║  步骤：                                                  ║")
    print("║  1. 用电脑浏览器打开 https://live.douyin.com/            ║")
    print("║     （确保已经用抖音账号登录）                           ║")
    print("║  2. 按 F12，切换到「网络 Network」标签                   ║")
    print("║  3. 等页面完整加载完毕（看到直播内容后再等 5 秒）        ║")
    print("║  4. 在过滤框输入 webcast，筛选出 XHR 请求               ║")
    print("║  5. 点击任意一条 webcast 请求（不是第一个 HTML 请求）    ║")
    print("║  6. 右侧「请求标头 Request Headers」→ 找到 cookie 行    ║")
    print("║  7. 复制 cookie: 后面的完整字符串                        ║")
    print("║                                                          ║")
    print("║  ⚠ 必须从 webcast XHR 请求复制，初次加载的 HTML 请求    ║")
    print("║    缺少 __ac_nonce / ttwid 等关键 Cookie！               ║")
    print("║                                                          ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()
    print("请将 Cookie 字符串粘贴到下方（粘贴后按回车）：")
    print()

    try:
        raw = input("Cookie> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消")
        sys.exit(0)

    if not raw:
        print("未输入内容，已退出")
        sys.exit(1)

    ok, missing_important = validate_cookies(raw)
    if not ok:
        print()
        print("错误: 粘贴的 Cookie 中未找到 sessionid 字段。")
        print("      请确保您已登录抖音，且从 webcast XHR 请求复制了 Cookie。")
        sys.exit(1)
    if missing_important:
        print()
        print(f"警告: 缺少以下关键 Cookie: {', '.join(missing_important)}")
        print("      这些 Cookie 由浏览器 JS 生成，只存在于页面加载后发出的")
        print("      XHR 请求中（不是初次 HTML 加载请求）。")
        print("      建议：按步骤说明，从 webcast 过滤后的请求中重新复制。")
        print()
        confirm = input("是否仍然保存？(y/N) ").strip().lower()
        if confirm != "y":
            print("已取消")
            sys.exit(1)

    try:
        update_config(config_path, raw)
    except Exception as e:
        print(f"写入失败: {e}")
        print("Cookie 字符串（请手动保存到 danmaku_cookies.txt，并在 config.json 中将 danmaku_cookies 设为该文件路径）:")
        print(f"  {raw}")
        sys.exit(1)

    cookie_names = [part.split("=")[0].strip() for part in raw.split(";") if "=" in part]
    print()
    print(f"已找到 {len(cookie_names)} 个 Cookie: {', '.join(cookie_names[:8])}" +
          (" ..." if len(cookie_names) > 8 else ""))
    print(f"已写入: {config_path}")
    print()
    print("请重启服务使配置生效:")
    print("  sudo systemctl restart douyin-monitor")


# ── QR mode (experimental) ────────────────────────────────────────────────────

def _print_ascii_qr(url: str) -> bool:
    try:
        import qrcode
    except ImportError:
        return False
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    return True


def _qr_generate(opener) -> tuple[str, str]:
    """Call passport.douyin.com QR generate API. Returns (token, qr_index_url)."""
    url = "https://www.douyin.com/passport/web/qrcode/generate/?aid=6383&service=https%3A%2F%2Fwww.douyin.com%2F"
    body = _get(opener, url, extra_headers={
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
    })
    data = json.loads(body)
    # Response shape: {"data": {"qrcode_index_url": "...", "token": "..."}, "status_code": 0}
    if data.get("status_code", -1) != 0:
        raise RuntimeError(f"generate API returned: {data}")
    d = data["data"]
    return d["token"], d.get("qrcode_index_url", "")


def _qr_query(opener, token: str) -> dict:
    """Poll QR status. Returns the inner data dict."""
    params = urllib.parse.urlencode({
        "token": token,
        "aid": "6383",
        "service": "https://www.douyin.com/",
    })
    body = _get(opener, f"https://www.douyin.com/passport/web/qrcode/query/?{params}",
                extra_headers={"Accept": "application/json, text/plain, */*"})
    return json.loads(body).get("data", {})


def _collect_cookies(jar) -> tuple[dict, str]:
    wanted = ("douyin.com", "bytedance.com", "snssdk.com")
    cookies = {
        c.name: c.value for c in jar
        if c.domain and any(d in c.domain for d in wanted)
    }
    return cookies, "; ".join(f"{k}={v}" for k, v in cookies.items())


def mode_qr(config_path: str) -> None:
    """Experimental QR-code login mode via passport.douyin.com."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    # Warm up: visit live.douyin.com to pick up ttwid
    try:
        _get(opener, "https://live.douyin.com/")
    except Exception:
        pass

    print("正在请求登录二维码...", flush=True)
    try:
        token, qr_img_url = _qr_generate(opener)
    except Exception as e:
        print(f"\n获取二维码失败: {e}")
        print()
        print("QR 模式需要 Douyin 服务器配合，可能因 bot 防护而失败。")
        print("请改用默认的浏览器粘贴模式:")
        print("  python3 login.py")
        sys.exit(1)

    print()
    print("┌" + "─" * 58 + "┐")
    if qr_img_url:
        ascii_ok = _print_ascii_qr(qr_img_url)
        if ascii_ok:
            print("│ 用手机摄像头扫上面的码 → 手机浏览器打开二维码图片       │")
            print("│ 然后用抖音 App 扫描图片里的二维码完成登录               │")
        else:
            print("│ 提示: 安装 qrcode 库可在终端显示二维码:                │")
            print("│   pip3 install qrcode --break-system-packages           │")
            print("│                                                          │")
            print("│ 在浏览器打开以下链接，再用抖音 App 扫码:               │")
            for i in range(0, min(len(qr_img_url), 112), 56):
                print(f"│ {qr_img_url[i:i+56]:<56} │")
    else:
        print("│ 未能获取二维码图片链接                                   │")
        print("└" + "─" * 58 + "┘")
        sys.exit(1)
    print("└" + "─" * 58 + "┘")
    print()

    print("等待扫码", end="", flush=True)
    deadline = time.time() + 180
    prev_status = 0

    while time.time() < deadline:
        time.sleep(2)
        try:
            status_data = _qr_query(opener, token)
        except Exception as e:
            print(f"\n轮询失败: {e}，继续重试...", end="", flush=True)
            continue

        status = status_data.get("status", 0)
        if status != prev_status:
            if status == 2:
                print("\n已扫码，请在手机抖音 App 上点击「确认登录」", end="", flush=True)
            elif status not in (1, 0):
                print(f"\n[status={status}]", end="", flush=True)
            prev_status = status
        else:
            print(".", end="", flush=True)

        if status == 3:
            print("\n登录成功！正在获取 Cookie...", flush=True)
            redirect_url = status_data.get("redirect_url", "")
            if redirect_url:
                try:
                    _get(opener, redirect_url)
                except Exception:
                    pass
            try:
                _get(opener, "https://live.douyin.com/")
            except Exception:
                pass

            cookie_map, cookie_str = _collect_cookies(jar)
            if not cookie_map:
                print("警告: 未能捕获到任何 Cookie，登录可能未成功")
                sys.exit(1)

            try:
                update_config(config_path, cookie_str)
            except Exception as e:
                print(f"写入失败: {e}")
                print(f"请手动保存到 danmaku_cookies.txt 并将路径填入 danmaku_cookies:\n  {cookie_str}")
                sys.exit(1)

            print(f"Cookie 字段: {', '.join(cookie_map.keys())}")
            print(f"已写入: {config_path}")
            print()
            print("请重启服务使配置生效:")
            print("  sudo systemctl restart douyin-monitor")
            return

        if status == 4:
            print("\n二维码已过期，请重新运行脚本")
            sys.exit(1)

    print("\n等待超时（3分钟），请重新运行脚本")
    sys.exit(1)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="获取抖音弹幕所需的 Cookie 并写入 config.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python3 login.py           # 浏览器粘贴模式（推荐）\n"
            "  python3 login.py --paste   # 同上\n"
            "  python3 login.py --qr      # 扫码模式（实验性，可能失败）\n"
        ),
    )
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
        help="config.json 路径（默认同目录）",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--paste", action="store_true", default=False,
                       help="浏览器粘贴模式（默认）")
    group.add_argument("--qr", action="store_true", default=False,
                       help="扫码登录模式（实验性）")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"找不到配置文件: {args.config}")
        sys.exit(1)

    if args.qr:
        mode_qr(args.config)
    else:
        mode_paste(args.config)


if __name__ == "__main__":
    main()

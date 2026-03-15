# 抖音直播监控自动录制工具

自动监控指定抖音主播的直播状态，开播时自动录制为 MP4 文件，以 systemd 服务形式 24/7 后台运行。

---

## 文件结构

```
douyin_monitor/
├── config.json      # 配置文件（主播 URL、输出目录、轮询间隔等）
├── monitor.py       # 主监控脚本
├── danmaku.py       # 弹幕录制模块（由 monitor.py 调用）
├── login.py         # 弹幕 Cookie 获取助手（手动运行一次）
├── status.py        # 状态查看工具（需安装为 douyin-status 命令）
├── install.sh       # 一键安装脚本
└── README.md        # 本文档
```

录制文件保存在（可在 config.json 中修改）：
```
~/recordings/抖音_{主播名}_{YYYYMMDD_HHMMSS}.mp4
```

---

## 快速开始

### 第一步：编辑配置文件

打开 `config.json`，在 `streamers` 数组中填入要监控的直播间地址：

```json
{
  "streamers": [
    {"url": "https://live.douyin.com/你的直播间ID"},
    {"url": "https://live.douyin.com/另一个直播间ID", "name": "可选显示名"}
  ],
  ...
}
```

- 支持同时监控多个主播，每路独立运行、互不干扰
- `name` 字段可选，用于日志前缀和录制文件名；省略时自动从 URL 提取房间 ID
- 直播间 URL 格式：`https://live.douyin.com/123456789`

> **向后兼容**：旧版 `streamer_url` 字符串格式仍然支持，脚本会自动转换为单元素 `streamers` 列表。

### 第二步：运行安装脚本

```bash
cd ~/claude-workspace/douyin_monitor
bash install.sh
```

安装脚本会自动完成：
1. 安装 `ffmpeg`（apt）
2. 安装 `streamlink`（pip3）
3. 创建录制目录 `~/recordings/`
4. 写入 systemd 服务文件并启动

> **注意**：如果 `streamers` 列表为空或 URL 仍为占位符，安装脚本会报错退出，需先修改配置。

---

## 配置说明（config.json）

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `streamers` | （必填） | 主播列表，每项含 `url`（必填）和 `name`（可选）|
| `output_dir` | `~/recordings` | 录制文件保存目录 |
| `streamlink_path` | `/usr/local/bin/streamlink` | streamlink 可执行文件路径 |
| `streamlink_quality` | `best` | 录制画质（best / worst / 720p 等） |
| `ffmpeg_path` | `/usr/bin/ffmpeg` | ffmpeg 路径 |
| `cookies_from_browser` | `null` | 保留字段，暂未启用 |
| `poll_interval_online` | `60` | 正常轮询间隔（秒） |
| `poll_interval_slow` | `120` | 长时间未开播后的轮询间隔（秒） |
| `poll_interval_very_slow` | `300` | 超长时间未开播后的轮询间隔（秒） |
| `slow_threshold_checks` | `30` | 触发慢速轮询的连续离线次数 |
| `very_slow_threshold_checks` | `120` | 触发极慢轮询的连续离线次数 |
| `network_retry_count` | `3` | 网络错误重试次数 |
| `network_retry_wait` | `30` | 每次重试等待时间（秒） |
| `log_level` | `INFO` | 日志级别（DEBUG / INFO / WARNING） |
| `nas_enabled` | `false` | 停播后处理总开关；`false` 时跳过全部后处理 |
| `nas_host` | `"nas"` | SSH 主机别名（`~/.ssh/config` 中定义，需无密码可连） |
| `nas_dest_dir` | `"/volume1/Share/LiveVideos"` | NAS 目标目录（rsync 目标路径） |
| `post_process_delay` | `1800` | 停播后等待多少秒再触发后处理（秒） |
| `transcribe_audio` | `false` | 录制结束后用 Whisper 自动生成 SRT 字幕（需安装 `faster-whisper`） |
| `whisper_model` | `"medium"` | Whisper 模型大小（tiny / base / small / medium / large） |
| `whisper_language` | `"zh"` | Whisper 识别语言 |
| `generate_outline` | `false` | 字幕生成后自动调用 Claude API 生成直播大纲（需设置 `ANTHROPIC_API_KEY`） |
| `outline_model` | `"claude-haiku-4-5-20251001"` | 生成大纲所用的 Claude 模型 |
| `anthropic_api_key` | `null` | Anthropic API Key（优先读取环境变量 `ANTHROPIC_API_KEY`） |

---

## 状态查看（douyin-status）

类似 `docker ps`，一条命令查看所有主播当前处于哪个阶段、已持续多久、下一步是什么：

```bash
douyin-status
```

输出示例：

```
LABEL    PHASE        SINCE     DURATION  DETAIL                      NEXT
────────────────────────────────────────────────────────────────────────────────────────────
主播A     RECORDING    21:18:04  3h27m10s  主播A_20260310_211804.mp4   waiting for stream end
主播C WAITING      23:38:51  14m35s    1 file(s) pending           post-proc @ 01:59 (15m)
主播B     IDLE         00:56:07  1h05m34s  offline 1h05m  45 checks    poll in 55s (@ 02:02:15)
────────────────────────────────────────────────────────────────────────────────────────────
  2026-03-11 01:03:00  |  3 streamer(s)  |  output: /path/to/recordings
```

**阶段说明：**

| 阶段 | 含义 |
|------|------|
| `IDLE` | 离线，定时轮询中 |
| `CONNECTING` | 检测到开播，正在启动录制 |
| `RECORDING` | 正在录制（ffmpeg 运行中） |
| `WATERMARKING` | 录制完成，正在添加时间水印 |
| `WAITING` | 停播后等待触发后处理（倒计时显示在 NEXT 列） |
| `VALIDATING` | 后处理第 1 步：用 ffprobe 验证文件完整性 |
| `MERGING` | 后处理第 2 步：合并多段录制 |
| `UPLOADING` | 后处理第 3 步：rsync 上传到 NAS |
| `CLEANING` | 后处理第 4 步：删除本地文件 |

其他选项：

```bash
douyin-status --no-color   # 禁用颜色（用于脚本/日志）
douyin-status --json       # 输出原始 JSON（用于脚本处理）
```

状态数据来自各主播在录制目录下自动维护的状态文件（`.douyin_status_{主播名}.json`），每次状态变化时原子写入，服务重启后 4 秒内即可看到最新状态。

---

## 服务管理

```bash
# 查看服务状态
sudo systemctl status douyin-monitor

# 实时查看日志
sudo journalctl -u douyin-monitor -f

# 停止服务
sudo systemctl stop douyin-monitor

# 启动服务
sudo systemctl start douyin-monitor

# 重启服务（修改配置后执行）
sudo systemctl restart douyin-monitor

# 禁用开机自启
sudo systemctl disable douyin-monitor
```

---

## 手动运行（调试用）

不启动 systemd 服务，直接在终端运行脚本：

```bash
python3 ~/claude-workspace/douyin_monitor/monitor.py --config ~/claude-workspace/douyin_monitor/config.json
```

按 `Ctrl+C` 停止。

---

## 工作原理

### 并发架构

每个主播对应一个 `StreamerMonitor` 实例，运行在独立线程中，互不干扰。日志每条带 `[主播标签]` 前缀便于区分：

```
[主播1] Starting monitor for: https://live.douyin.com/ROOM_ID_1
[主播2] Starting monitor for: https://live.douyin.com/ROOM_ID_2
[主播A] Starting recording: 抖音_主播A_20260308_230537.mp4
[主播C] Starting recording: 抖音_主播C_20260308_230537.mp4
```

### 状态机（每路独立）

```
[轮询] streamlink --json {URL}
  ├─ 网络错误 → 重试 3 次（每次等 30s）→ 退避后继续轮询
  ├─ offline（streams 为空 / error 字段存在）
  │       → 累计离线次数++，按自适应间隔休眠 → 继续轮询
  └─ live（streams 非空）
          → 重置离线计数
          → 从同一 JSON 的 metadata.author 获取主播名
          → 生成文件名：抖音_{主播名}_{时间戳}.mp4
          → 启动录制管道（阻塞等待 ffmpeg 结束）
          → 录制结束后等待 10s → 继续轮询
```

### 自适应轮询间隔

| 连续离线次数 | 轮询间隔 |
|------------|--------|
| 0 ~ 29 次 | 60 秒 |
| 30 ~ 119 次 | 120 秒（约 1 小时后切换） |
| 120 次及以上 | 300 秒（约 4 小时后切换） |

检测到开播后，间隔立即重置为 60 秒。

### 录制管道

```
streamlink --stdout {url} best
    │  (原始流数据)
    ▼
ffmpeg -i pipe:0 -c copy -y {output.mp4}
```

streamlink 和 ffmpeg 分别在各自独立的进程组（`os.setsid`）中运行。收到 `systemctl stop` 时，两个进程组分别收到 SIGTERM，ffmpeg 写完文件后退出。monitor.py 等待 ffmpeg 退出（而非 streamlink），确保 MP4 容器完整写入。

---

## 重启恢复机制

服务重启后，监控状态会从磁盘自动恢复，不会丢失进度：

| 恢复内容 | 说明 |
|---------|------|
| 自适应轮询计数 | 不因重启归零，继续按原有间隔轮询 |
| 停播计时 | `offline_since` 从磁盘恢复，`post_process_delay` 从中断处继续计时 |
| 本次直播分段列表 | `session_files` 持久化，重启后不丢失已录制的分段文件路径 |
| 中断录制恢复 | 若服务在录制中途重启，重启后自动找回中断的 MP4 文件，补打水印（若已开启），并加入本次会话待合并队列 |

状态文件保存在录制目录中（每个主播一个）：

```
~/recordings/.douyin_state_{主播名}.json
```

状态文件采用原子写入（先写 `.tmp` 再重命名），服务崩溃不会产生损坏的状态文件。

---

## 停播后自动后处理（上传 NAS）

当 `nas_enabled` 设为 `true`，主播停播超过 `post_process_delay` 秒后，监控服务会自动：

1. **验证**：用 `ffprobe` 逐一检查本次直播的所有录制分段，跳过损坏文件
2. **合并**：用 `ffmpeg concat demuxer` 将所有有效分段合并为单个 MP4（仅一段时直接上传）
3. **上传**：`rsync -av --remove-source-files` 上传 MP4 到 NAS，传完自动删除本地合并文件
4. **清理**：删除本地原始分段文件

合并后本地文件命名格式（保留 `_merged` 后缀以避免与原始分段文件名冲突）：
```
抖音_主播名_20260308_153045_merged.mp4
```

上传到 NAS 时自动去除 `_merged` 后缀：
```
抖音_主播名_20260308_153045.mp4
```

后处理在独立线程中运行，不影响其他主播的监控。收到 SIGTERM 时会安全中止（保留文件）。

### 启用方式

```json
{
  "nas_enabled": true,
  "nas_host": "nas",
  "nas_dest_dir": "/volume1/Share/LiveVideos",
  "post_process_delay": 1800
}
```

### 验证 NAS 上的文件

```bash
ssh nas ls /volume1/Share/LiveVideos/
```

---

## 语音转文字（字幕）

当 `transcribe_audio` 设为 `true`，录制结束后会自动在后台对视频进行语音识别，生成同名 `.srt` 字幕文件：

```
抖音_主播名_20260308_153045.mp4
抖音_主播名_20260308_153045.srt   ← 自动生成
```

依赖安装：

```bash
pip3 install faster-whisper --break-system-packages
```

模型首次运行时会自动从 HuggingFace 下载（国内建议配置 `HF_ENDPOINT=https://hf-mirror.com`，已在 systemd 服务中设置）。

### 自动生成直播大纲

当 `generate_outline` 设为 `true`，字幕生成完成后会自动调用 Claude API，生成简短的直播内容大纲（Markdown 格式），保存为同名 `.outline.md`：

```
抖音_主播名_20260308_153045.srt
抖音_主播名_20260308_153045.outline.md   ← 自动生成
```

**前提条件**：需要 Anthropic API Key（在 [console.anthropic.com](https://console.anthropic.com) 单独购买，与 Claude Code 订阅无关）。

配置方式（二选一）：

```bash
# 方式一：systemd 服务环境变量（推荐）
# 编辑 /etc/systemd/system/douyin-monitor.service，追加：
Environment=ANTHROPIC_API_KEY=sk-ant-xxx
sudo systemctl daemon-reload && sudo systemctl restart douyin-monitor

# 方式二：config.json
"anthropic_api_key": "sk-ant-xxx"
```

依赖安装：

```bash
pip3 install anthropic --break-system-packages
```

---

## 弹幕录制

当 `danmaku_enabled` 设为 `true`，每次录制开始时会同步连接 Douyin WebSocket，将弹幕实时写入与 MP4 同名的 `.danmaku.jsonl` 文件：

```
抖音_主播名_20260308_153045.mp4
抖音_主播名_20260308_153045.danmaku.jsonl   ← 自动生成
```

每行一条消息，格式：

```jsonl
{"ts": 1741430400.12, "type": "chat",    "user": {"id": 123, "nickname": "xxx"}, "content": "哈哈哈"}
{"ts": 1741430401.50, "type": "gift",    "user": {...}, "gift_id": 1234, "repeat_count": 1}
{"ts": 1741430402.00, "type": "viewers", "total": 12345}
```

消息类型：`chat`（聊天）、`gift`（礼物）、`like`（点赞）、`member`（进场）、`social`（关注）、`viewers`（在线人数）、`control`（控制，如直播结束）

**弹幕连接需要登录 Cookie**，否则 WebSocket 握手返回 `auth failed 417`。

### 获取登录 Cookie

运行辅助脚本，按提示将浏览器 Cookie 粘贴进去，约 30 秒完成：

```bash
python3 login.py
```

操作步骤：
1. 用电脑浏览器打开 `https://live.douyin.com/`（确保已登录抖音）
2. 按 `F12` → 切换到「网络 Network」标签 → 按 `F5` 刷新
3. 点击任意一条发往 `live.douyin.com` 的请求
4. 在右侧「请求标头 Request Headers」中找到 `cookie:` 行
5. 复制 `cookie:` 后面的完整字符串
6. 粘贴到脚本提示符后，按回车

脚本会自动验证 Cookie 并写入 `config.json`，然后提示重启服务。

```bash
python3 login.py --qr   # 实验性：扫码模式（可能因 bot 防护失败）
```

### 配置字段

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `danmaku_enabled` | `false` | 总开关 |
| `danmaku_cookies` | `null` | 登录 Cookie 字符串（由 `login.py` 写入） |

### 弹幕合并

多段录制时（同一场直播因网络中断导致多个 MP4 分段），后处理阶段会将对应的 `.danmaku.jsonl` 文件按时间顺序拼接，连同 MP4 一起上传到 NAS。

### 重启恢复

服务重启时，若上次有进行中的录制，弹幕录制会自动从同一 `.danmaku.jsonl` 文件末尾继续追加，不丢失已有内容。

---

## 飞书 Webhook 通知

当 `feishu_webhook_url` 设为有效地址时，以下三个事件会自动推送通知到飞书流程：

| 事件 | 触发时机 |
|------|--------|
| `stream_live` | 检测到开播并成功启动录制后 |
| `stream_ended` | 单段录制结束（ffmpeg 正常退出）后 |
| `nas_sync_done` | rsync 上传到 NAS 成功后 |

### 配置方式

```json
"feishu_webhook_url": "https://www.feishu.cn/flow/api/trigger-webhook/你的触发器ID"
```

设为 `null` 则关闭通知。

### 飞书流程 Webhook 触发器参数示例

在飞书流程「Webhook 触发器」配置页，「参数示例」字段填入以下 JSON（包含所有事件的全部字段，供飞书解析变量名）：

```json
{
  "msg_type": "text",
  "event": "stream_live",
  "label": "主播1",
  "url": "https://live.douyin.com/ROOM_ID",
  "uploader": "主播1",
  "started_at_str": "2026-03-09 01:27:18",
  "output_path": "/path/to/recordings/抖音_主播1_20260309_012718.mp4",
  "filename": "抖音_主播1_20260309_012718.mp4",
  "duration_seconds": 3600,
  "file_size_mb": 1024,
  "merged_filename": "抖音_主播1_20260309_012718.mp4",
  "nas_dest_dir": "/path/to/nas/dir/主播1/2026-03"
}
```

> **说明**：三种事件共用一个扁平结构，飞书流程中用「条件分支」节点判断 `event` 字段值来区分处理逻辑。

### 各事件实际发送的字段

**`stream_live`**：`msg_type` / `event` / `label` / `url` / `uploader` / `started_at_str` / `output_path`

**`stream_ended`**：`msg_type` / `event` / `label` / `url` / `filename` / `started_at_str` / `duration_seconds` / `file_size_mb`

**`nas_sync_done`**：`msg_type` / `event` / `label` / `url` / `merged_filename` / `nas_dest_dir`

### 手动测试

```bash
curl -X POST https://www.feishu.cn/flow/api/trigger-webhook/你的触发器ID \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{
    "msg_type": "text",
    "event": "stream_live",
    "label": "测试",
    "url": "https://live.douyin.com/test",
    "uploader": "测试主播",
    "started_at_str": "2026-03-09 01:27:18",
    "output_path": "/tmp/test.mp4"
  }'
```

---

## 录制文件

录制完成的文件位于 `~/recordings/`，命名格式：

```
抖音_主播名_20260308_153045.mp4
```

> **磁盘空间**：未启用 NAS 后处理时，脚本不做自动清理，请定期手动管理录制文件。

---

## 更新 streamlink

抖音经常更新，建议定期更新 streamlink：

```bash
sudo pip3 install -U streamlink --break-system-packages
```

> Ubuntu 24.04（Python 3.12）因 PEP 668 限制，pip 安装系统级包需要加 `--break-system-packages`。

---

## 手动测试

验证 streamlink 能否识别直播状态（不启动录制）：

```bash
# 直播中：输出 JSON 含 "streams": {...}
# 未开播：输出含 "error": "No playable streams found"
streamlink --json https://live.douyin.com/你的直播间ID
```

验证录制管道（直播中时执行）：

```bash
streamlink --stdout https://live.douyin.com/你的直播间ID best \
  | ffmpeg -i pipe:0 -c copy -y /tmp/test.mp4
```

---

## 常见问题

**Q：安装后日志一直显示 Offline，但主播明明在播？**
A：可能是 streamlink 版本过旧或抖音更新了接口。先执行 `sudo pip3 install -U streamlink --break-system-packages`，然后重启服务。

**Q：日志显示 "No plugin can handle URL"？**
A：URL 格式不正确。streamlink 只支持 `https://live.douyin.com/{房间ID}` 格式。`www.douyin.com/follow/live/...` 等分享链接需手动转换：从 URL 中提取数字房间 ID，拼成 `https://live.douyin.com/{ID}`。

**Q：为什么不用 yt-dlp？**
A：yt-dlp 内置的 Douyin 提取器只支持 `www.douyin.com/video/{id}`（录播），不支持 `live.douyin.com/{room_id}` 直播流，会报 "Unsupported URL" 错误。streamlink 有原生 Douyin 直播插件。

**Q：如何更换或新增监控的主播？**
A：修改 `config.json` 中的 `streamers` 数组（增删元素），然后执行 `sudo systemctl restart douyin-monitor`。

**Q：录制文件损坏怎么办？**
A：直播中途断网或强制停止可能导致文件不完整。可用 `ffmpeg -i 文件名.mp4` 检查文件完整性。

**Q：如何查看历史日志？**
A：`sudo journalctl -u douyin-monitor --since "2026-03-01"` 或 `sudo journalctl -u douyin-monitor -n 200`。

**Q：开了 sing-box 代理，录制流量会走代理吗？**
A：不会。sing-box 路由规则中 `geosite-cn` 和 `geoip-cn` 均设为 `direct`，抖音 CDN 域名（`douyincdn.com`）和其解析出的国内 IP 都会直连，不经过代理节点。

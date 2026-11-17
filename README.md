# 大象日记 · 局域网视频服务

零依赖 Node 服务 + artplayer 播放器，内网任意设备浏览器直接观看。

## 快速开始

```bash
node server.mjs
```

启动后终端会打印可用地址，浏览器打开 `http://<内网IP>:8080` 即可。

自定义参数：

```bash
node server.mjs --port 9000          # 换端口（默认 8080）
node server.mjs --host 127.0.0.1     # 只监听本机（默认 0.0.0.0）
node server.mjs --key 我的口令        # 开启访问口令
node server.mjs --root /path/to/videos  # 指定视频目录（默认脚本所在目录）
```

后台常驻：

```bash
setsid nohup node server.mjs --port 8080 > .server.log 2>&1 < /dev/null &
```

停止：`pgrep -f "^node server.mjs" | xargs kill`

## 功能

- **剧集列表**：自动扫描目录，识别 `S01E02` / `第3集` 等命名，按季分组
- **封面缩略图**：ffmpeg 抽帧，结果缓存在 `.cache/`
- **多字幕切换**：自动发现同名字幕（`.srt` / `.vtt` / `.ass` / `.ssa`），
  按文件名后缀识别语言，无后缀则读内容判断中英文；支持 `关闭` 选项
- **记忆播放**：每 5 秒记一次进度，切集 / 关页面后回来可续播
- **进度条拖动**：完整支持 HTTP Range（206），大文件随意跳转
- **快捷键**：`←/→` 快退快进 10s，`↑/↓` 音量，`Space` 暂停，`F` 全屏，`N`/`P` 上/下一集
- **移动端**：窄屏自动折叠侧栏为抽屉（宽 `min(86vw, 340px)`），打开抽屉时自动退出播放器全屏
  / 网页全屏 / 设置面板，并隐藏播放器控件层，避免与侧栏菜单重叠；底部工具栏图标化压缩为单行
- **断网可用**：artplayer 放在 `public/vendor/`，不依赖任何 CDN

## 目录结构

```
.
├── server.mjs            # 服务端（Node 内置模块，无 npm 依赖）
├── public/
│   ├── index.html        # 播放页
│   └── vendor/
│       └── artplayer.js  # ArtPlayer v5.4.0（CSS 已内联在 JS 中）
├── .cache/               # 封面缓存（可随时删除）
├── S1/  S2/              # 视频与字幕
```

## 接口

| 路径 | 说明 |
|---|---|
| `GET /` | 播放页 |
| `GET /api/list` | 视频列表（含字幕信息）；`?refresh` 强制刷新 |
| `GET /media/<相对路径>` | 视频流，支持 Range；`?download=1` 触发下载 |
| `GET /subs/<相对路径>` | 字幕，自动转 WebVTT；`?raw=1` 返回原文件 |
| `GET /api/poster?f=<相对路径>` | 封面 JPEG |
| `GET /health` | 健康检查 |

## 字幕命名规则

同一视频目录下，以下文件会被自动关联（`stem` = 视频主名，如 `大象日记.S01E01`）：

- `stem.srt` — 无语言后缀，读内容判断
- `stem.en.chs.srt` — 中英双语
- `stem.zh.srt` / `stem.chs.srt` / `stem.cn.srt` — 中文
- `stem.en.srt` / `stem.eng.srt` — English
- `stem.cht.srt` / `stem.tc.srt` — 繁体中文

排序：中文 → 中英双语 → 繁体 → 其他 → English。默认选中第一个。

## 注意

- **Opus 音轨**：`S2/E01`、`E02`、`E04` 的音轨是 Opus-in-MP4，Chrome/Edge/Firefox 正常，
  Safari/iOS 不支持（会提示）。如需全平台兼容，可重编码音轨：
  ```bash
  ffmpeg -i in.mp4 -c:v copy -c:a aac -b:a 192k out.mp4
  ```
- **中文字体**：页面用 `PingFang SC` / `Microsoft YaHei` 等系统字体，Windows / macOS / 多数
  Android 都自带。若某设备显示方块（缺 CJK 字体的 Linux 环境），用浏览器打开即可。
- **路径安全**：只允许访问 `--root` 目录内的视频/字幕，已做目录穿越防护。
- **`--key` 口令**：首次带 `?key=口令` 访问会写入 cookie，之后正常访问。

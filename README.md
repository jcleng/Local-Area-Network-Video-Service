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
├── srt2bilingual.py      # 双语字幕生成：LibreTranslate 后端
├── srt2bilingual_llm.py  # 双语字幕生成：本地大模型（OpenAI 兼容接口）后端
├── public/
│   ├── index.html        # 播放页
│   └── vendor/
│       └── artplayer.js  # ArtPlayer v5.4.0（CSS 已内联在 JS 中）
├── .cache/               # 封面缓存（可随时删除）
├── S1/  S2/              # 视频与字幕
```

## 双语字幕生成脚本

两个脚本解决同一件事：把英文 `.srt` 转成「英文 + 中文」双语 `.srt`，供播放器直接识别为
`*.en.chs.srt` 中英双语字幕。区别只在翻译后端：

| | `srt2bilingual.py` | `srt2bilingual_llm.py` |
|---|---|---|
| 翻译后端 | 本地 LibreTranslate（`http://127.0.0.1:5000`） | 本地 OpenAI 兼容接口（llama.cpp / vLLM / Ollama），默认 `http://127.0.0.1:8090` |
| 默认模型 | 无（LibreTranslate 自带引擎） | `Hy-MT2-1.8B-Q4_K_M` |
| 质量 / 速度 | 快、稳定，译文偏机翻 | 慢，上下文更自然；需要显存/CPU |
| 行数对齐 | 依赖 LibreTranslate 保留换行；行数不符则整批退回逐条 | 编号校验 + 递归拆批 + 逐条降级，专门解决「多行被合并成一句」的漏句 / 错位问题 |
| 适用场景 | 只想快速批量出稿 | 对译文质量有要求、且本机已跑推理服务 |

### 共同行为

- **输入**：目录（递归扫描）、文件列表，不带参数则处理当前目录；只挑 `.srt`
- **输出路径**：`foo.srt` → `foo.en.chs.srt`（同目录，已存在则覆盖）
- **防重复翻译**：文件名以 `.en.chs.srt` / `.chs.srt` / `.zh.srt` / `.en.srt` 结尾的一律跳过
- **容错解析**：兼容 CRLF、BOM、缺序号、乱序序号的 SRT；缺少序号时按顺序补
- **文本清洗**：翻译前去掉 `<i>`、`{\an8}` 之类标签并合并多行；生成的每条字幕仍保留原始时间轴
- **输出顺序**：默认英文在上、中文在下；加 `--zh-first` 改为中文在上
- **本地服务不走代理**：访问 127.0.0.1 时忽略 `http_proxy` / `https_proxy`，避免本地请求被转发出去

### `srt2bilingual.py`（LibreTranslate）

```bash
python3 srt2bilingual.py                # 递归处理当前目录所有 .srt
python3 srt2bilingual.py S1             # 只处理 S1
python3 srt2bilingual.py a.srt b.srt    # 处理指定文件
python3 srt2bilingual.py --jobs 4 S1    # 4 个文件并发
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--url` | `http://127.0.0.1:5000` | LibreTranslate 地址（或用环境变量 `LIBRETRANSLATE_URL`） |
| `--source` / `--target` | `en` / `zh-Hans` | 源 / 目标语言 |
| `--api-key` | 空 | 可选（或 `LIBRETRANSLATE_API_KEY`） |
| `--batch` | 16 | 单次请求合并的字幕条数，越大越快 |
| `--jobs` | 1 | 并发文件数 |
| `--retries` | 5 | 单次请求重试次数（指数退避，最长 15s） |
| `--timeout` | 120 | 单次请求超时秒数 |
| `--zh-first` | 关 | 中文在上、英文在下 |
| `--use-proxy` | 关 | 允许走系统代理（默认 `--no-proxy`） |

要点：一批文本用换行拼成一次请求，若返回行数与输入不一致，则整批退回逐条翻译以保证对齐；
纯符号 / 音乐标记等无英文字母的条目跳过翻译，只保留原文。

启动 LibreTranslate（Docker）：

```bash
docker run -d -p 5000:5000 libretranslate/libretranslate --load-only en,zh
```

### `srt2bilingual_llm.py`（本地大模型）

```bash
python3 srt2bilingual_llm.py                  # 递归处理当前目录
python3 srt2bilingual_llm.py --jobs 2 S2      # 两个文件并发（服务端需支持并行槽位）
python3 srt2bilingual_llm.py a.srt --batch 4  # 减小批大小，减少拆批
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--base-url` | `http://127.0.0.1:8090` | OpenAI 兼容接口根地址，自动补 `/v1/chat/completions`（环境变量 `LLM_BASE_URL`） |
| `--model` | `Hy-MT2-1.8B-Q4_K_M` | 模型名（`LLM_MODEL`） |
| `--target-name` | `简体中文` | 写进提示词的目标语言名 |
| `--prompt-template` | 内置 | 自定义提示词，用 `{target}` 占位目标语言 |
| `--api-key` | 空 | 可选（`LLM_API_KEY`） |
| `--batch` | 10 | 单次请求条数；该模型 `>=20` 会漏尾部条目 |
| `--jobs` | 1 | 并发文件数 |
| `--max-tokens` | 2048 | 单次生成上限 |
| `--timeout` | 300 | 请求超时秒数 |
| `--retries` | 4 | 请求重试次数 |
| `--zh-first` | 关 | 中文在上、英文在下 |
| `--use-proxy` | 关 | 允许走系统代理 |

**逐条对齐机制**（本脚本的重点）：Hy-MT 类模型遇到「一句话被字幕切成连续几行」时会合并成
一段通顺译文，导致输出条数与输入不一致。因此：

1. 每次请求给待译文本加 `1. 2. 3.` 编号，要求按编号逐条输出；
2. 解析返回结果，校验编号是否齐全、条数是否匹配，并过滤「原样回显」「作为AI」「抱歉」等异常输出；
3. 有缺失就**二分拆批递归重试**（最多 4 层），最终降级为**逐条翻译**，保证不漏句、不错位；
4. 单条翻译失败重试 3 次；最终仍为空的条目保留英文行，并在结束时汇总「空译」数量。

退出码：`0` 全部成功；`1` 没找到可处理的 `.srt`；`2` 有文件失败（会列出失败清单）。

以 llama.cpp 启动示例：

```bash
llama-server -m Hy-MT2-1.8B-Q4_K_M.gguf --port 8090 --host 127.0.0.1 -c 4096
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

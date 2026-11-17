#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把英文字幕 (.srt) 翻译成中文，并生成"英文 + 中文"双语的 .srt 字幕。

用法:
    python3 srt2bilingual.py                 # 处理当前目录下所有 .srt（递归）
    python3 srt2bilingual.py S1              # 处理指定目录
    python3 srt2bilingual.py a.srt b.srt     # 处理指定文件
    python3 srt2bilingual.py --jobs 4 S1     # 并发数

规则:
    foo.srt  ->  foo.en.chs.srt
    - 若输入文件名已包含 .en.chs.srt 后缀（即之前生成的产物）则跳过，避免重复翻译。
    - 输出文件已存在则覆盖。
    - 翻译使用本地 LibreTranslate (默认 http://127.0.0.1:5000)。

可选参数:
    --url  URL        LibreTranslate 地址，默认 http://127.0.0.1:5000
    --source LANG     源语言，默认 en
    --target LANG     目标语言，默认 zh-Hans
    --api-key KEY     可选 API KEY
    --batch N         每次请求合并的字幕条数，默认 16
    --jobs N          并发文件数，默认 1
    --retries N       请求失败重试次数，默认 5
    --timeout N       单次请求超时秒数，默认 120
    --no-proxy        强制忽略系统代理环境变量（访问本机服务时建议开启，默认开启）

注意: 访问 127.0.0.1 时不会使用 http_proxy/https_proxy/all_proxy，
      避免系统代理把本地请求转发出去导致失败。
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


# --------------------------------------------------------------------------
# SRT 解析 / 生成
# --------------------------------------------------------------------------

TIMESTAMP_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


class Cue:
    __slots__ = ("index", "timing", "text")

    def __init__(self, index, timing, text):
        self.index = index      # 原始序号（保留原样，可能是乱序/空）
        self.timing = timing    # "00:00:00,000 --> 00:00:04,240"
        self.text = text        # 原始多行文本（未 strip）

    def plain_text(self):
        """用于翻译的纯文本：去掉 HTML 标签、合并多行。"""
        t = re.sub(r"<[^>]+>", "", self.text)
        t = re.sub(r"\{[^}]*\}", "", t)
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        return " ".join(lines)


def parse_srt(content):
    """解析 SRT 内容，返回 Cue 列表。容错：缺序号也能解析。"""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    content = content.lstrip("\ufeff")
    blocks = re.split(r"\n\s*\n", content.strip())
    cues = []
    for block in blocks:
        lines = block.split("\n")
        if not lines:
            continue
        # 找到时间轴所在行
        timing_idx = None
        for i, ln in enumerate(lines):
            if TIMESTAMP_RE.match(ln):
                timing_idx = i
                break
        if timing_idx is None:
            continue
        timing = lines[timing_idx].strip()
        index = ""
        if timing_idx > 0:
            prev = lines[0].strip()
            if prev.isdigit():
                index = prev
        text = "\n".join(lines[timing_idx + 1:])
        if not text.strip():
            continue
        cues.append(Cue(index, timing, text))
    return cues


def build_srt(cues, translations, chinese_second=True):
    """生成双语 SRT：原文 + 译文。"""
    out = []
    for i, (cue, zh) in enumerate(zip(cues, translations), start=1):
        en = cue.text.strip()
        zh = (zh or "").strip()
        idx = cue.index or str(i)
        body = [idx, cue.timing]
        if chinese_second:
            body.append(en)
            if zh and zh != en:
                body.append(zh)
        else:
            if zh:
                body.append(zh)
            body.append(en)
        out.append("\n".join(body))
    return "\n\n".join(out) + "\n"


# --------------------------------------------------------------------------
# LibreTranslate 客户端
# --------------------------------------------------------------------------

class Translator:
    def __init__(self, url, source="en", target="zh-Hans", api_key=None,
                 timeout=120, retries=5, no_proxy=True):
        self.url = url.rstrip("/") + "/translate"
        self.source = source
        self.target = target
        self.api_key = api_key
        self.timeout = timeout
        self.retries = retries
        if no_proxy:
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            self.opener = urllib.request.build_opener()

    def _post(self, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        with self.opener.open(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def translate_batch(self, texts):
        """翻译一批文本，返回等长的译文列表。"""
        # 空文本直接返回空
        results = ["" for _ in texts]
        idxs = [i for i, t in enumerate(texts) if t.strip()]
        if not idxs:
            return results

        # 用换行合并成一段，LibreTranslate 对多行文本会保留换行结构
        joined = "\n".join(texts[i].strip() for i in idxs)
        payload = {
            "q": joined,
            "source": self.source,
            "target": self.target,
            "format": "text",
        }
        if self.api_key:
            payload["api_key"] = self.api_key

        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self._post(payload)
                translated = resp.get("translatedText", "")
                parts = [p.strip() for p in translated.split("\n")]

                if len(parts) == len(idxs):
                    for pos, i in enumerate(idxs):
                        results[i] = parts[pos]
                    return results

                # 行数不匹配：逐条翻译，保证对齐
                for i in idxs:
                    results[i] = self._translate_one(texts[i].strip())
                return results
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 15))

        # 全部重试失败
        raise RuntimeError(f"翻译请求失败（{self.retries} 次重试后）: {last_err}")

    def _translate_one(self, text):
        payload = {
            "q": text,
            "source": self.source,
            "target": self.target,
            "format": "text",
        }
        if self.api_key:
            payload["api_key"] = self.api_key
        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self._post(payload)
                return resp.get("translatedText", "").strip()
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 15))
        raise RuntimeError(f"翻译请求失败: {last_err}")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

OUTPUT_SUFFIX = ".en.chs.srt"
SKIP_MARKERS = (".en.chs.srt", ".chs.srt", ".zh.srt", ".en.srt")


def output_path_for(src):
    base, _ext = os.path.splitext(src)
    return base + OUTPUT_SUFFIX


def is_generated(name):
    low = name.lower()
    return any(low.endswith(m) for m in SKIP_MARKERS)


def collect_srt_files(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for n in sorted(names):
                    if n.lower().endswith(".srt") and not is_generated(n):
                        files.append(os.path.join(root, n))
        elif os.path.isfile(p):
            if p.lower().endswith(".srt") and not is_generated(os.path.basename(p)):
                files.append(p)
        else:
            print(f"[警告] 路径不存在: {p}", file=sys.stderr)
    # 去重并保持稳定顺序
    seen = set()
    out = []
    for f in sorted(files):
        real = os.path.abspath(f)
        if real not in seen:
            seen.add(real)
            out.append(real)
    return out


def process_file(src, translator, batch_size, chinese_second=True):
    t0 = time.time()
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    cues = parse_srt(content)
    if not cues:
        return src, 0, f"跳过（未解析到字幕条目）"

    texts = [c.plain_text() for c in cues]
    translations = ["" for _ in texts]

    # 只翻译非空、非纯符号的文本
    todo = [i for i, t in enumerate(texts) if t and re.search(r"[A-Za-z]", t)]
    for start in range(0, len(todo), batch_size):
        chunk = todo[start:start + batch_size]
        sub_texts = [texts[i] for i in chunk]
        try:
            sub_trans = translator.translate_batch(sub_texts)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{os.path.basename(src)}: {e}") from e
        for i, tr in zip(chunk, sub_trans):
            translations[i] = tr

    # 纯符号/音乐标记等保留原文（不重复输出）
    for i, t in enumerate(texts):
        if not translations[i]:
            translations[i] = ""

    out_content = build_srt(cues, translations, chinese_second=chinese_second)
    dst = output_path_for(src)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(out_content)

    dur = time.time() - t0
    return dst, len(cues), f"{dur:.1f}s"


def main():
    ap = argparse.ArgumentParser(
        description="用 LibreTranslate 把英文字幕转成中英双语字幕",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="*", default=None,
                    help="要处理的目录或 .srt 文件（默认当前目录，递归）")
    ap.add_argument("--url", default=os.environ.get("LIBRETRANSLATE_URL",
                                                    "http://127.0.0.1:5000"),
                    help="LibreTranslate 地址（默认 http://127.0.0.1:5000）")
    ap.add_argument("--source", default="en", help="源语言（默认 en）")
    ap.add_argument("--target", default="zh-Hans", help="目标语言（默认 zh-Hans）")
    ap.add_argument("--api-key", default=os.environ.get("LIBRETRANSLATE_API_KEY"),
                    help="可选 API key")
    ap.add_argument("--batch", type=int, default=16, help="每次请求合并的字幕条数（默认 16）")
    ap.add_argument("--jobs", type=int, default=1, help="并发文件数（默认 1）")
    ap.add_argument("--retries", type=int, default=5, help="请求重试次数（默认 5）")
    ap.add_argument("--timeout", type=int, default=120, help="请求超时秒数（默认 120）")
    ap.add_argument("--zh-first", action="store_true",
                    help="中文在前、英文在后（默认英文在前）")
    ap.add_argument("--no-proxy", dest="no_proxy", action="store_true", default=True,
                    help="忽略系统代理（默认开启）")
    ap.add_argument("--use-proxy", dest="no_proxy", action="store_false",
                    help="使用系统代理环境变量")
    args = ap.parse_args()

    paths = args.paths or ["."]
    files = collect_srt_files(paths)
    if not files:
        print("没有找到需要处理的 .srt 文件。", file=sys.stderr)
        return 1

    translator = Translator(
        args.url, source=args.source, target=args.target,
        api_key=args.api_key, timeout=args.timeout,
        retries=args.retries, no_proxy=args.no_proxy,
    )

    print(f"LibreTranslate: {args.url}  ({args.source} -> {args.target})")
    print(f"共 {len(files)} 个字幕文件待处理，并发 {args.jobs}\n")

    failures = []
    done = 0

    def worker(f):
        return process_file(f, translator, args.batch,
                            chinese_second=not args.zh_first)

    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(worker, f): f for f in files}
            for fut in as_completed(futs):
                f = futs[fut]
                done += 1
                try:
                    dst, n, info = fut.result()
                    print(f"[{done}/{len(files)}] ✅ {os.path.basename(f)} -> "
                          f"{os.path.basename(dst)}  {n} 条  {info}")
                except Exception as e:  # noqa: BLE001
                    failures.append((f, str(e)))
                    print(f"[{done}/{len(files)}] ❌ {os.path.basename(f)}: {e}",
                          file=sys.stderr)
    else:
        for f in files:
            done += 1
            print(f"[{done}/{len(files)}] 处理中: {os.path.basename(f)} ...", flush=True)
            try:
                dst, n, info = worker(f)
                print(f"[{done}/{len(files)}] ✅ {os.path.basename(f)} -> "
                      f"{os.path.basename(dst)}  {n} 条  {info}")
            except Exception as e:  # noqa: BLE001
                failures.append((f, str(e)))
                print(f"[{done}/{len(files)}] ❌ {os.path.basename(f)}: {e}",
                      file=sys.stderr)

    print()
    if failures:
        print(f"完成 {len(files) - len(failures)}/{len(files)}，失败 {len(failures)}:")
        for f, err in failures:
            print(f"  - {f}: {err}")
        return 2
    print(f"全部完成: {len(files)} 个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

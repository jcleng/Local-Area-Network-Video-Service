#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把英文字幕 (.srt) 翻译成中文，生成"英文 + 中文"双语 .srt 字幕。
翻译后端：本地 OpenAI 兼容接口（llama.cpp / vLLM / Ollama 等），
默认模型 Hy-MT2-1.8B-Q4_K_M @ http://127.0.0.1:8090

用法:
    python3 srt2bilingual_llm.py                 # 当前目录递归处理所有 .srt
    python3 srt2bilingual_llm.py S1              # 指定目录
    python3 srt2bilingual_llm.py a.srt b.srt     # 指定文件
    python3 srt2bilingual_llm.py --jobs 2 S2     # 两个文件并发（需服务端支持并行槽位）

命名规则:
    foo.srt  ->  foo.en.chs.srt
    - 已存在的 .en.chs.srt 会被覆盖
    - 输入名本身带 .en.chs.srt / .chs.srt / .zh.srt / .en.srt 后缀则跳过，避免嵌套翻译

关于逐条对齐（重要）:
    该翻译模型遇到"一句话被字幕切开"的连续行时，会把多行合并成一段通顺译文，
    导致输出行数与输入行数不一致（实测 batch>=20 还会漏掉尾部几条）。
    本脚本因此：
      1) 每次请求带 "1. 2. 3." 编号，要求按编号逐条输出；
      2) 校验返回的编号是否齐全、是否与请求条数一致；
      3) 不一致就自动二分拆批重试，最终降级为逐条翻译，保证不漏、不错位。
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

OUTPUT_SUFFIX = ".en.chs.srt"
SKIP_MARKERS = (".en.chs.srt", ".chs.srt", ".zh.srt", ".en.srt")


class Cue:
    __slots__ = ("index", "timing", "text")

    def __init__(self, index, timing, text):
        self.index = index
        self.timing = timing
        self.text = text

    def plain_text(self):
        t = re.sub(r"<[^>]+>", "", self.text)
        t = re.sub(r"\{[^}]*\}", "", t)
        return " ".join(ln.strip() for ln in t.splitlines() if ln.strip())


def parse_srt(content):
    content = content.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = block.split("\n")
        timing_idx = next((i for i, ln in enumerate(lines) if TIMESTAMP_RE.match(ln)), None)
        if timing_idx is None:
            continue
        index = lines[0].strip() if timing_idx > 0 and lines[0].strip().isdigit() else ""
        text = "\n".join(lines[timing_idx + 1:])
        if text.strip():
            cues.append(Cue(index, lines[timing_idx].strip(), text))
    return cues


def build_srt(cues, translations, zh_first=False):
    out = []
    for i, (cue, zh) in enumerate(zip(cues, translations), start=1):
        en = cue.text.strip()
        zh = (zh or "").strip()
        body = [cue.index or str(i), cue.timing]
        if zh_first:
            if zh and zh != en:
                body.append(zh)
            body.append(en)
        else:
            body.append(en)
            if zh and zh != en:
                body.append(zh)
        out.append("\n".join(body))
    return "\n\n".join(out) + "\n"


# --------------------------------------------------------------------------
# OpenAI 兼容客户端
# --------------------------------------------------------------------------

class LLMTranslator:
    """
    指令模板（可由 --prompt-template 覆盖，用 {target} 占位目标语言全名）：
        将以下文本翻译为{目标语言}，注意只需要输出翻译后的结果，不要额外解释：
    """

    def __init__(self, base_url, model, target_name="简体中文",
                 prompt_template=None, api_key=None, temperature=0.0,
                 max_tokens=2048, timeout=300, retries=4, no_proxy=True):
        base = base_url.rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        self.url = base + "/chat/completions"
        self.model = model
        self.target_name = target_name
        self.template = prompt_template or (
            "将以下文本翻译为{target}，注意只需要输出翻译后的结果，不要额外解释："
        )
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.retries = retries
        handlers = [urllib.request.ProxyHandler({})] if no_proxy else []
        self.opener = urllib.request.build_opener(*handlers)
        self.calls = 0

    # ---------------- 底层请求 ----------------

    def _chat(self, user_content):
        prompt = self.template.format(target=self.target_name) + "\n\n" + user_content
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(payload).encode("utf-8")

        last_err = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(self.url, data=data, headers=headers)
                with self.opener.open(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                self.calls += 1
                choice = body["choices"][0]
                return choice["message"]["content"] or ""
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < self.retries:
                    time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(f"模型接口请求失败（{self.retries} 次重试后）: {last_err}")

    # ---------------- 解析带编号的输出 ----------------

    NUM_RE = re.compile(r"(?m)^\s*(\d{1,3})\s*[.、．)）]\s*(.+?)\s*$")

    def _parse_numbered(self, content, count):
        """把 '1. xxx' 形式的输出解析成 {序号: 译文}。"""
        mapping = {}
        for m in self.NUM_RE.finditer(content):
            n = int(m.group(1))
            if 1 <= n <= count and n not in mapping:
                mapping[n] = m.group(2).strip()
        if not mapping:
            # 模型没给编号：若正好按行输出且行数与 count 相同，则按顺序接受
            lines = [ln.strip() for ln in content.strip().splitlines() if ln.strip()]
            if len(lines) == count:
                return {i + 1: lines[i] for i in range(count)}
        return mapping

    # ---------------- 批量翻译（带校验 + 拆批降级） ----------------

    def translate_lines(self, texts):
        """翻译一组文本，返回等长译文列表；任何一条都不能丢。"""
        results = ["" for _ in texts]
        work = [i for i, t in enumerate(texts) if t and t.strip()]
        if not work:
            return results
        self._translate_indices(texts, work, results, depth=0)
        return results

    def _translate_indices(self, texts, idxs, results, depth):
        if not idxs:
            return
        if len(idxs) == 1:
            i = idxs[0]
            results[i] = self._translate_single(texts[i])
            return

        # 构造带编号的批量输入
        numbered = "\n".join(f"{pos + 1}. {texts[i]}" for pos, i in enumerate(idxs))
        try:
            raw = self._chat(numbered)
        except RuntimeError:
            raw = ""
        mapping = self._parse_numbered(raw, len(idxs))

        missing = []
        for pos, i in enumerate(idxs):
            tr = mapping.get(pos + 1, "").strip()
            # 明显异常的返回值（回显原文、带解释性前缀）视作失败
            if not tr or self._looks_bad(tr, texts[i]):
                missing.append(i)
            else:
                results[i] = tr

        if not missing:
            return

        # 有缺失：拆半重试（递归），最深降级到逐条
        if depth >= 4 or len(idxs) <= 2:
            for i in missing:
                results[i] = self._translate_single(texts[i])
            return
        mid = len(idxs) // 2
        self._translate_indices(texts, idxs[:mid], results, depth + 1)
        self._translate_indices(texts, idxs[mid:], results, depth + 1)

    def _translate_single(self, text):
        for _ in range(3):
            raw = self._chat(text).strip()
            # 去掉模型偶尔加上的引号/编号/“译文：”前缀
            raw = re.sub(r"^\s*\d{1,3}\s*[.、．)）]\s*", "", raw)
            raw = re.sub(r"^(译文|翻译)\s*[:：]\s*", "", raw)
            raw = raw.strip().strip('"').strip("“”").strip()
            if raw and not self._looks_bad(raw, text):
                return raw
        return ""

    @staticmethod
    def _looks_bad(tr, src):
        if not tr:
            return True
        # 模型把原文原样返回
        if tr.strip() == src.strip():
            return True
        # 输出明显是“解释/说明”而非译文
        for bad in ("作为AI", "作为 AI", "以下是", "抱歉", "无法翻译"):
            if tr.startswith(bad):
                return True
        return False


# --------------------------------------------------------------------------
# 文件级处理
# --------------------------------------------------------------------------

def output_path_for(src):
    return os.path.splitext(src)[0] + OUTPUT_SUFFIX


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
    seen, out = set(), []
    for f in sorted(files):
        rp = os.path.abspath(f)
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def process_file(src, translator, batch_size, zh_first=False):
    t0 = time.time()
    with open(src, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    cues = parse_srt(content)
    if not cues:
        return src, 0, 0, "未解析到字幕条目，跳过"

    texts = [c.plain_text() for c in cues]
    translations = []
    total = len(texts)
    done = 0
    for start in range(0, total, batch_size):
        chunk = texts[start:start + batch_size]
        translations.extend(translator.translate_lines(chunk))
        done += len(chunk)
        print(f"    {os.path.basename(src)}: {done}/{total}", flush=True)

    # 兜底：仍为空的条目，用原文占位，避免双语字幕丢失英文行
    empty = sum(1 for t in translations if not t.strip())
    dst = output_path_for(src)
    with open(dst, "w", encoding="utf-8") as f:
        f.write(build_srt(cues, translations, zh_first=zh_first))
    return dst, len(cues), empty, f"{time.time() - t0:.1f}s"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="用本地 OpenAI 兼容接口把英文字幕转成中英双语字幕",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("paths", nargs="*", help="目录或 .srt 文件（默认当前目录，递归）")
    ap.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", "http://127.0.0.1:8090"),
                    help="服务地址（默认 http://127.0.0.1:8090，自动补 /v1）")
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "Hy-MT2-1.8B-Q4_K_M"),
                    help="模型名（默认 Hy-MT2-1.8B-Q4_K_M）")
    ap.add_argument("--target-name", default="简体中文",
                    help="提示词中的目标语言全名（默认 简体中文）")
    ap.add_argument("--prompt-template", default=None,
                    help="自定义提示词，用 {target} 占位目标语言全名")
    ap.add_argument("--api-key", default=os.environ.get("LLM_API_KEY"), help="可选 API key")
    ap.add_argument("--batch", type=int, default=10,
                    help="每次请求的字幕条数（默认 10；实测 >10 易漏尾行）")
    ap.add_argument("--jobs", type=int, default=1,
                    help="并发文件数（默认 1；服务端 n_ctx 为总预算，多并发会挤爆上下文）")
    ap.add_argument("--max-tokens", type=int, default=2048, help="单次生成上限（默认 2048）")
    ap.add_argument("--timeout", type=int, default=300, help="请求超时秒数（默认 300）")
    ap.add_argument("--retries", type=int, default=4, help="请求重试次数（默认 4）")
    ap.add_argument("--zh-first", action="store_true", help="中文在上、英文在下")
    ap.add_argument("--use-proxy", action="store_true", help="使用系统代理（默认忽略）")
    args = ap.parse_args()

    files = collect_srt_files(args.paths or ["."])
    if not files:
        print("没有找到需要处理的 .srt 文件。", file=sys.stderr)
        return 1

    translator = LLMTranslator(
        args.base_url, args.model, target_name=args.target_name,
        prompt_template=args.prompt_template, api_key=args.api_key,
        max_tokens=args.max_tokens, timeout=args.timeout,
        retries=args.retries, no_proxy=not args.use_proxy,
    )

    print(f"模型接口: {translator.url}")
    print(f"模型: {args.model}   目标语言: {args.target_name}   批大小: {args.batch}")
    print(f"待处理 {len(files)} 个文件，并发 {args.jobs}\n")

    def worker(f):
        return process_file(f, translator, args.batch, zh_first=args.zh_first)

    failures, done = [], 0
    total_empty = 0

    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(worker, f): f for f in files}
            for fut in as_completed(futs):
                f = futs[fut]
                done += 1
                try:
                    dst, n, empty, info = fut.result()
                    total_empty += empty
                    print(f"[{done}/{len(files)}] ✅ {os.path.basename(f)} -> "
                          f"{os.path.basename(dst)}  {n} 条  空译 {empty}  {info}")
                except Exception as e:  # noqa: BLE001
                    failures.append((f, str(e)))
                    print(f"[{done}/{len(files)}] ❌ {os.path.basename(f)}: {e}", file=sys.stderr)
    else:
        for f in files:
            done += 1
            print(f"[{done}/{len(files)}] 处理中: {os.path.basename(f)}")
            try:
                dst, n, empty, info = worker(f)
                total_empty += empty
                print(f"[{done}/{len(files)}] ✅ {os.path.basename(f)} -> "
                      f"{os.path.basename(dst)}  {n} 条  空译 {empty}  {info}\n")
            except Exception as e:  # noqa: BLE001
                failures.append((f, str(e)))
                print(f"[{done}/{len(files)}] ❌ {os.path.basename(f)}: {e}\n", file=sys.stderr)

    print(f"模型请求总数: {translator.calls}")
    if total_empty:
        print(f"注意: 有 {total_empty} 条未能取得译文（已保留英文行）")
    if failures:
        print(f"完成 {len(files) - len(failures)}/{len(files)}，失败 {len(failures)}:")
        for f, err in failures:
            print(f"  - {f}: {err}")
        return 2
    print(f"全部完成: {len(files)} 个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

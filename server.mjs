#!/usr/bin/env node
/**
 * 局域网视频服务 - artplayer 播放器
 * 零依赖，仅用 Node 内置模块。
 *
 *   node server.mjs [--port 8080] [--host 0.0.0.0] [--root .] [--key 口令]
 *
 * 访问 http://<内网IP>:8080
 */

import http from 'node:http';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ---------- 参数 ----------
function argOf(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : def;
}
const PORT = Number(argOf('port', process.env.PORT || 8080));
const HOST = argOf('host', process.env.HOST || '0.0.0.0');
const ROOT = path.resolve(argOf('root', __dirname));
const ACCESS_KEY = argOf('key', process.env.KEY || '');
const PUBLIC_DIR = path.join(__dirname, 'public');
const CACHE_DIR = path.join(__dirname, '.cache');

const VIDEO_EXT = new Set(['.mp4', '.m4v', '.mkv', '.webm', '.mov', '.avi', '.ts', '.flv']);
const MIME = {
  '.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
  '.webm': 'video/webm', '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo',
  '.ts': 'video/mp2t', '.flv': 'video/x-flv',
  '.vtt': 'text/vtt; charset=utf-8', '.srt': 'text/plain; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8', '.mjs': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.html': 'text/html; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml',
  '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
};

// ---------- 工具 ----------
const log = (...a) => console.log(`[${new Date().toLocaleTimeString('zh-CN', { hour12: false })}]`, ...a);

/** 把请求里的相对路径安全地解析到 ROOT 内，越界返回 null */
function safeJoin(root, rel) {
  let decoded;
  try { decoded = decodeURIComponent(rel); } catch { return null; }
  if (decoded.includes('\0')) return null;
  const abs = path.resolve(root, '.' + path.posix.normalize('/' + decoded));
  const r = root.endsWith(path.sep) ? root : root + path.sep;
  return abs === root || abs.startsWith(r) ? abs : null;
}

function sendJSON(res, code, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function sendText(res, code, text, type = 'text/plain; charset=utf-8') {
  res.writeHead(code, { 'Content-Type': type, 'Content-Length': Buffer.byteLength(text) });
  res.end(text);
}

// ---------- 字幕 ----------
const decoderCache = new Map();

/** 猜测编码：优先 UTF-8，失败再用 GB18030 */
function decodeBuffer(buf) {
  if (buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf) buf = buf.subarray(3);
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(buf);
  } catch {
    if (!decoderCache.has('gb')) decoderCache.set('gb', new TextDecoder('gb18030'));
    return decoderCache.get('gb').decode(buf);
  }
}

/** SRT / ASS 文本 -> WebVTT */
function toVtt(text) {
  const t = text.replace(/\r\n?/g, '\n');
  if (/^WEBVTT/.test(t)) return t;
  // ASS/SSA
  if (/^\[Script Info\]/im.test(t)) {
    const out = ['WEBVTT', ''];
    const lines = t.split('\n');
    const fmtIdx = { Start: 1, End: 2, Text: 9 };
    let inEvents = false;
    for (const raw of lines) {
      const line = raw.trim();
      if (/^\[.*\]$/.test(line)) { inEvents = /^\[Events\]/i.test(line); continue; }
      if (!inEvents) continue;
      if (/^Format:/i.test(line)) {
        line.slice(7).split(',').forEach((f, i) => { fmtIdx[f.trim()] = i; });
        continue;
      }
      if (!/^Dialogue:/i.test(line)) continue;
      const parts = line.slice(9).trim().split(',');
      const start = assTime(parts[fmtIdx.Start]);
      const end = assTime(parts[fmtIdx.End]);
      const txt = parts.slice(fmtIdx.Text).join(',')
        .replace(/\{[^}]*\}/g, '')
        .replace(/\\N|\\n/gi, '\n')
        .replace(/\\h/g, ' ')
        .trim();
      if (start && end && txt) out.push(`${start} --> ${end}`, txt, '');
    }
    return out.join('\n');
  }
  // SRT：删掉“纯数字序号”行（只删紧跟在空行/开头之后、且下一行是时间轴的行）
  const src = t.replace(/^\uFEFF/, '').split('\n');
  const kept = [];
  for (let i = 0; i < src.length; i++) {
    const line = src[i];
    const isIndex = /^\s*\d+\s*$/.test(line);
    const nextIsTime = /-->/.test(src[i + 1] || '');
    if (isIndex && nextIsTime) continue;
    kept.push(line);
  }
  const body = kept.join('\n')
    .replace(/(\d{2}:\d{2}:\d{2}),(\d{3})/g, '$1.$2')   // 时间轴逗号 -> 点
    .replace(/^(\d{1,2}:\d{2}\.\d{3})/gm, '00:$1');    // 补全为 hh:mm:ss.mmm
  return 'WEBVTT\n\n' + body.replace(/\n{3,}/g, '\n\n').trim() + '\n';
}

function assTime(s) {
  const m = /^(\d+):(\d{2}):(\d{2})[.:](\d{1,3})$/.exec((s || '').trim());
  if (!m) return null;
  return `${m[1]}:${m[2]}:${m[3]}.${m[4].padEnd(3, '0')}`;
}

// ---------- 目录扫描 ----------
const SEASON_RE = /[Ss](\d{1,2})/;
const EPISODE_RE = /[Ss](\d{1,2})\s*[Ee](\d{1,3})|[Ee][Pp]?(\d{1,3})|第\s*(\d{1,3})\s*[集话]/;

function parseName(file) {
  const base = path.basename(file, path.extname(file));
  let season = null, episode = null, m;
  if ((m = /[Ss](\d{1,2})\s*[Ee](\d{1,3})/.exec(base))) { season = +m[1]; episode = +m[2]; }
  else if ((m = /[Ss](\d{1,2})/.exec(base))) { season = +m[1]; }
  if (episode === null) {
    if ((m = /第\s*(\d{1,3})\s*[集话]/.exec(base))) episode = +m[1];
    else if ((m = /[Ee][Pp]?\.?\s*(\d{1,3})(?!\d)/.exec(base))) episode = +m[1];
  }
  return { season, episode, title: base.replace(/[._]/g, ' ').trim() || base };
}

async function scanDir(dir, relBase) {
  let entries;
  try { entries = await fsp.readdir(dir, { withFileTypes: true }); } catch { return []; }
  const items = [];
  for (const e of entries) {
    if (e.name.startsWith('.')) continue;
    const abs = path.join(dir, e.name);
    const rel = relBase ? `${relBase}/${e.name}` : e.name;
    if (e.isDirectory()) {
      items.push(...await scanDir(abs, rel));
      continue;
    }
    const ext = path.extname(e.name).toLowerCase();
    if (!VIDEO_EXT.has(ext)) continue;
    let st;
    try { st = await fsp.stat(abs); } catch { continue; }
    const info = parseName(e.name);
    // { 
    //   file, name, title, season, episode, size, mtime, subtitle, subtitleLabel, subtitles: []
    // }
    // 所在目录名里的季号兜底
    const dirSeason = SEASON_RE.exec(path.basename(path.dirname(abs)));
    items.push({
      file: rel,
      name: e.name,
      title: info.title,
      season: info.season ?? (dirSeason ? +dirSeason[1] : null),
      episode: info.episode,
      size: st.size,
      mtime: st.mtimeMs,
      subtitle: null,
      subtitleLabel: null,
      subtitles: [],
    });
  }
  return items;
}

/** 字幕语言标签：优先看文件名后缀，无后缀时看内容（是否含汉字） */
function subLabelFor(videoStem, subFile) {
  // 例如 videoStem='大象日记.S01E01', subFile='大象日记.S01E01.en.chs.srt'
  const subStem = path.basename(subFile, path.extname(subFile));
  const rest = subStem.slice(videoStem.length).replace(/^[.\-_\s]+/, '').toLowerCase();
  const parts = rest.split(/[.\-_\s]+/).filter(Boolean);
  const has = (...keys) => keys.some((k) => parts.includes(k));
  const hasZh = has('chs', 'zh', 'zhcn', 'zh-cn', 'sc', 'chi', 'cn', '中', '中文');
  const hasEn = has('en', 'eng', 'english', '英文');
  if (!rest) return { label: null, lang: null };            // 无后缀，交给内容检测
  if (hasZh && hasEn) return { label: '中英双语', lang: 'zh' };
  if (hasZh) return { label: '中文', lang: 'zh' };
  if (has('cht', 'tc', 'zhhant', '繁')) return { label: '繁体中文', lang: 'zh-Hant' };
  if (hasEn) return { label: 'English', lang: 'en' };
  if (has('jp', 'ja', 'jpn', '日')) return { label: '日本語', lang: 'ja' };
  if (has('kor', 'ko', '韩')) return { label: '한국어', lang: 'ko' };
  return { label: rest.replace(/\./g, ' '), lang: 'und' };
}

const HAN_RE = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/;

/** 无语言后缀的字幕：读内容判断中/英 */
async function detectSubLang(absPath) {
  try {
    const fh = await fsp.open(absPath, 'r');
    try {
      const { buffer, bytesRead } = await fh.read(Buffer.alloc(64 * 1024), 0, 64 * 1024, 0);
      const text = new TextDecoder('utf-8', { fatal: false }).decode(buffer.subarray(0, bytesRead));
      return HAN_RE.test(text) ? { label: '中文', lang: 'zh' } : { label: 'English', lang: 'en' };
    } finally { await fh.close(); }
  } catch {
    return { label: '默认', lang: 'und' };
  }
}

/** 为每个视频找出所有同名字幕，按优先级排序 */
async function attachSubtitles(items) {
  // 按目录分组，避免同一目录重复 readdir
  const dirs = new Map();
  for (const it of items) {
    const d = path.posix.dirname(it.file);
    if (!dirs.has(d)) dirs.set(d, []);
    dirs.get(d).push(it);
  }
  const SUB_EXT = new Set(['.srt', '.vtt', '.ass', '.ssa']);
  for (const [dirRel, group] of dirs) {
    const dirAbs = safeJoin(ROOT, dirRel);
    if (!dirAbs) continue;
    let files;
    try { files = await fsp.readdir(dirAbs); } catch { continue; }
    const subFiles = files.filter((f) => SUB_EXT.has(path.extname(f).toLowerCase()));
    for (const it of group) {
      const stem = path.basename(it.name, path.extname(it.name));
      const found = [];
      for (const sf of subFiles) {
        const sfStem = path.basename(sf, path.extname(sf));
        // 必须是 "视频主名" 或 "视频主名.xxx"，不能是别集的字幕
        if (sfStem !== stem && !sfStem.startsWith(stem + '.')) continue;
        const { label, lang } = subLabelFor(stem, sf);
        found.push({
          file: dirRel === '.' ? sf : `${dirRel}/${sf}`,
          label,
          lang,
          ext: path.extname(sf).toLowerCase(),
          abs: path.join(dirAbs, sf),
        });
      }
      // 无明确语言后缀的字幕按内容补标签
      for (const f of found) {
        if (!f.label) Object.assign(f, await detectSubLang(f.abs));
      }
      const rank = (x) =>
        x.label === '中文' ? 0 : x.label === '中英双语' ? 1 : x.label === '繁体中文' ? 2 :
        x.label === 'English' ? 6 : x.label === '默认' ? 5 : 4;
      found.sort((a, b) => rank(a) - rank(b) || a.file.localeCompare(b.file, 'zh'));
      it.subtitles = found.map(({ abs, ...r }) => r);
      it.subtitle = found[0]?.file ?? null;
      it.subtitleLabel = found[0]?.label ?? null;
    }
  }
}

let listCache = { at: 0, data: null };
async function getList(force = false) {
  if (!force && listCache.data && Date.now() - listCache.at < 5000) return listCache.data;
  const items = await scanDir(ROOT, '');
  await attachSubtitles(items);
  items.sort((a, b) =>
    (a.season ?? 99) - (b.season ?? 99) ||
    (a.episode ?? 999) - (b.episode ?? 999) ||
    a.name.localeCompare(b.name, 'zh'));
  const seasons = new Map();
  for (const it of items) {
    const key = it.season ?? 0;
    if (!seasons.has(key)) seasons.set(key, []);
    seasons.get(key).push(it);
  }
  const data = {
    root: ROOT,
    total: items.length,
    items,
    seasons: [...seasons.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([season, list]) => ({ season, items: list })),
  };
  listCache = { at: Date.now(), data };
  return data;
}

// ---------- 封面（ffmpeg 抽帧）----------
const posterJobs = new Map();
async function posterPath(relFile) {
  const abs = safeJoin(ROOT, relFile);
  if (!abs) return null;
  let st;
  try { st = await fsp.stat(abs); } catch { return null; }
  await fsp.mkdir(CACHE_DIR, { recursive: true });
  const hash = Buffer.from(relFile).toString('hex').slice(0, 40);
  const out = path.join(CACHE_DIR, `${hash}-${Math.round(st.mtimeMs)}.jpg`);
  try { await fsp.access(out); return out; } catch { /* 生成 */ }
  if (posterJobs.has(out)) return posterJobs.get(out);
  const job = new Promise((resolve) => {
    // 优先用 ffprobe 取时长，抽 15% 位置（避开片头黑场与片尾字幕），失败则用固定秒数
    const finish = (seek) => {
      const ff = spawn('ffmpeg', [
        '-hide_banner', '-loglevel', 'error', '-ss', String(seek), '-i', abs,
        '-frames:v', '1', '-vf', 'scale=480:-2', '-q:v', '5', '-y', out,
      ], { stdio: 'ignore' });
      const kill = setTimeout(() => ff.kill('SIGKILL'), 25000);
      ff.on('close', (code) => {
        clearTimeout(kill);
        posterJobs.delete(out);
        resolve(code === 0 ? out : null);
      });
      ff.on('error', () => { clearTimeout(kill); posterJobs.delete(out); resolve(null); });
    };
    const probe = spawn('ffprobe', [
      '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', abs,
    ], { stdio: ['ignore', 'pipe', 'ignore'] });
    let buf = '';
    const pkill = setTimeout(() => probe.kill('SIGKILL'), 8000);
    probe.stdout.on('data', (d) => { buf += d; });
    probe.on('close', () => {
      clearTimeout(pkill);
      const dur = parseFloat(buf.trim());
      finish(isFinite(dur) && dur > 30 ? Math.round(dur * 0.15) : 30);
    });
    probe.on('error', () => { clearTimeout(pkill); finish(30); });
  });
  posterJobs.set(out, job);
  return job;
}

// ---------- Range 传输 ----------
function serveFileRange(req, res, abs, { mime, download = false } = {}) {
  let stat;
  try { stat = fs.statSync(abs); } catch { return sendText(res, 404, 'Not Found'); }
  if (stat.isDirectory()) return sendText(res, 404, 'Not Found');

  const type = mime || MIME[path.extname(abs).toLowerCase()] || 'application/octet-stream';
  const etag = `W/"${stat.size.toString(16)}-${Math.round(stat.mtimeMs).toString(16)}"`;
  const lastModified = new Date(stat.mtimeMs).toUTCString();
  const isVideo = type.startsWith('video/');

  const headers = {
    'Content-Type': type,
    'Accept-Ranges': 'bytes',
    'ETag': etag,
    'Last-Modified': lastModified,
    'Cache-Control': isVideo ? 'no-cache' : 'public, max-age=86400',
    'Access-Control-Allow-Origin': '*',
  };
  if (download) headers['Content-Disposition'] = `attachment; filename*=UTF-8''${encodeURIComponent(path.basename(abs))}`;

  // 条件请求
  if (req.headers['if-none-match'] === etag && !req.headers.range) {
    res.writeHead(304, headers);
    return res.end();
  }

  const range = req.headers.range;
  if (!range) {
    headers['Content-Length'] = stat.size;
    res.writeHead(200, headers);
    if (req.method === 'HEAD') return res.end();
    return fs.createReadStream(abs).on('error', () => res.destroy()).pipe(res);
  }

  // 只支持单区间 Range，多区间退化为 200（浏览器播放器只用单区间）
  const m = /^bytes=(\d*)-(\d*)$/.exec(range.trim());
  if (!m || (m[1] === '' && m[2] === '')) {
    headers['Content-Range'] = `bytes */${stat.size}`;
    headers['Content-Length'] = 0;
    res.writeHead(416, headers);
    return res.end();
  }
  let start, end;
  if (m[1] === '') {                       // bytes=-500 末尾 N 字节
    const n = Number(m[2]);
    start = Math.max(0, stat.size - n);
    end = stat.size - 1;
  } else {
    start = Number(m[1]);
    end = m[2] === '' ? stat.size - 1 : Math.min(Number(m[2]), stat.size - 1);
  }
  if (start > end || start >= stat.size) {
    headers['Content-Range'] = `bytes */${stat.size}`;
    headers['Content-Length'] = 0;
    res.writeHead(416, headers);
    return res.end();
  }
  headers['Content-Range'] = `bytes ${start}-${end}/${stat.size}`;
  headers['Content-Length'] = end - start + 1;
  res.writeHead(206, headers);
  if (req.method === 'HEAD') return res.end();
  const stream = fs.createReadStream(abs, { start, end });
  stream.on('error', () => res.destroy());
  res.on('close', () => stream.destroy());
  stream.pipe(res);
}

// ---------- 访问控制 ----------
function keyOK(req, url) {
  if (!ACCESS_KEY) return true;
  if (url.searchParams.get('key') === ACCESS_KEY) return true;
  const cookie = req.headers.cookie || '';
  if (cookie.includes(`vdkey=${ACCESS_KEY}`)) return true;
  const auth = req.headers.authorization || '';
  if (auth.startsWith('Basic ')) {
    const pass = Buffer.from(auth.slice(6), 'base64').toString().split(':').slice(1).join(':');
    if (pass === ACCESS_KEY) return true;
  }
  return false;
}

// ---------- 路由 ----------
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  const pathname = url.pathname;

  try {
    if (!keyOK(req, url)) {
      res.writeHead(401, { 'WWW-Authenticate': 'Basic realm="video", charset="UTF-8"' });
      return sendText(res, 401, '需要口令访问：在 URL 后加 ?key=你的口令');
    }
    // 用 ?key= 登录成功后落 cookie，后续 /media 的 Range 请求不必再带 key
    if (ACCESS_KEY && url.searchParams.get('key') === ACCESS_KEY &&
        !(req.headers.cookie || '').includes(`vdkey=${ACCESS_KEY}`)) {
      res.setHeader('Set-Cookie', `vdkey=${ACCESS_KEY}; Path=/; Max-Age=2592000; SameSite=Lax`);
    }

    // 静态前端
    if (pathname === '/' || pathname === '/index.html') {
      return serveFileRange(req, res, path.join(PUBLIC_DIR, 'index.html'));
    }
    if (pathname.startsWith('/vendor/') || pathname === '/favicon.ico') {
      const abs = safeJoin(PUBLIC_DIR, pathname);
      if (!abs) return sendText(res, 403, 'Forbidden');
      return serveFileRange(req, res, abs);
    }

    // 列表
    if (pathname === '/api/list') {
      const data = await getList(url.searchParams.has('refresh'));
      return sendJSON(res, 200, data);
    }

    // 封面
    if (pathname === '/api/poster') {
      const f = url.searchParams.get('f') || '';
      const p = await posterPath(f);
      if (!p) { res.writeHead(404); return res.end(); }
      return serveFileRange(req, res, p, { mime: 'image/jpeg' });
    }

    // 视频
    if (pathname.startsWith('/media/')) {
      const rel = pathname.slice('/media/'.length);
      const abs = safeJoin(ROOT, rel);
      if (!abs) return sendText(res, 403, 'Forbidden');
      if (!VIDEO_EXT.has(path.extname(abs).toLowerCase())) return sendText(res, 403, 'Forbidden');
      return serveFileRange(req, res, abs, { download: url.searchParams.has('download') });
    }

    // 字幕：.srt/.ass 转 vtt；加 ?raw=1 返回原始文件
    if (pathname.startsWith('/subs/')) {
      const rel = pathname.slice('/subs/'.length);
      const abs = safeJoin(ROOT, rel);
      if (!abs) return sendText(res, 403, 'Forbidden');
      const ext = path.extname(abs).toLowerCase();
      if (!['.srt', '.vtt', '.ass', '.ssa'].includes(ext)) return sendText(res, 403, 'Forbidden');
      if (url.searchParams.has('raw')) return serveFileRange(req, res, abs);
      let buf;
      try { buf = await fsp.readFile(abs); } catch { return sendText(res, 404, 'Not Found'); }
      const text = ext === '.vtt' ? decodeBuffer(buf) : toVtt(decodeBuffer(buf));
      // 可选按秒偏移
      res.writeHead(200, {
        'Content-Type': 'text/vtt; charset=utf-8',
        'Cache-Control': 'public, max-age=3600',
        'Access-Control-Allow-Origin': '*',
      });
      return res.end(text);
    }

    if (pathname === '/health') return sendJSON(res, 200, { ok: true, ...server.address() });

    sendText(res, 404, 'Not Found');
  } catch (err) {
    log('ERROR', req.method, pathname, err?.message);
    if (!res.headersSent) sendText(res, 500, 'Internal Server Error');
    else res.destroy();
  }
});

// 大文件长连接
server.headersTimeout = 0;
server.requestTimeout = 0;
server.keepAliveTimeout = 65_000;
server.timeout = 0;

server.listen(PORT, HOST, async () => {
  const data = await getList(true).catch(() => ({ total: 0, items: [] }));
  const ips = Object.values((await import('node:os')).networkInterfaces())
    .flat().filter((i) => i && i.family === 'IPv4' && !i.internal).map((i) => i.address);
  log(`视频目录: ${ROOT}`);
  log(`共 ${data.total} 个视频`);
  log(`服务已启动:`);
  for (const ip of ips) log(`  http://${ip}:${PORT}${ACCESS_KEY ? `/?key=${ACCESS_KEY}` : ''}`);
  log(`  本机 http://127.0.0.1:${PORT}${ACCESS_KEY ? `/?key=${ACCESS_KEY}` : ''}`);
});

process.on('SIGINT', () => { log('关闭中...'); server.close(() => process.exit(0)); setTimeout(() => process.exit(0), 2000); });

# -*- coding: utf-8 -*-
"""字幕文件读写：SRT（主输出，UTF-8）/ VTT / TXT / 词级时间戳 JSON。

分句策略：完全跟随 ASR 引擎原生分段，不做固定时长切割，保证时间轴准确。
"""
import json
import os
import re
import stat
import time
import unicodedata
from typing import List

from .asr.base import Segment


def _ensure_writable(path: str):
    """覆盖写前清除目标文件的只读属性（如存在），降低 Permission denied 概率"""
    if os.path.exists(path):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass


def fmt_srt_time(seconds: float) -> str:
    """秒 -> SRT 时间戳 00:00:00,000"""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:  # 四舍五入进位
        s += 1
        ms = 0
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def fmt_vtt_time(seconds: float) -> str:
    """秒 -> VTT 时间戳 00:00:00.000"""
    return fmt_srt_time(seconds).replace(",", ".")


def _base_output_path(media_path: str, out_dir: str) -> str:
    base = os.path.splitext(os.path.basename(media_path))[0]
    return os.path.join(out_dir, base)


def write_srt(segments: List[Segment], path: str):
    """写标准 SRT，强制 UTF-8（带 BOM 兼容性处理：PR/剪映本地版均可识别无 BOM UTF-8）"""
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append("%s --> %s" % (fmt_srt_time(seg.start), fmt_srt_time(seg.end)))
        lines.append(seg.text.strip())
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


def write_vtt(segments: List[Segment], path: str):
    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append("%s --> %s" % (fmt_vtt_time(seg.start), fmt_vtt_time(seg.end)))
        lines.append(seg.text.strip())
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


def write_txt(segments: List[Segment], path: str):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(seg.text.strip() for seg in segments))


def write_json(segments: List[Segment], path: str):
    """词级时间戳 JSON（二次开发用）"""
    data = []
    for seg in segments:
        data.append({
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text,
            "words": [
                {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word}
                for w in seg.words
            ],
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---- 字幕排版：每段限制行数，超长按标点拆成多个连续时段 ----
_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*"   # 英文单词/数字整体保留
    r"|[\u4e00-\u9fff]"                      # 单个汉字
    r"|[^\sA-Za-z0-9\u4e00-\u9fff]"          # 标点/其他符号
)
_BREAK_PUNCT = "，,、。.！!？?；;：:…）)】」』"
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")

DEFAULT_MAX_LINES = 2
# 视觉宽度：汉字/全角算 2，英文算 1（≈ 每行 20 个汉字或 40 个英文字符）
DEFAULT_MAX_WIDTH = 40


def _visual_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _join_tokens(tokens: List[str]) -> str:
    """拼接排版单元：英文单词之间补空格，中文与标点之间不加空格"""
    out = ""
    for tok in tokens:
        if out and _ALNUM_RE.match(tok[0]) and _ALNUM_RE.match(out[-1]):
            out += " "
        out += tok
    return out


def _find_break(cur: List[str], max_width: int) -> int:
    """在当前行内找断点（不含）：在行尾约一半宽度范围内优先找标点"""
    acc = 0
    limit = max_width * 0.5
    for i in range(len(cur) - 1, 0, -1):
        acc += _visual_width(cur[i])
        if acc > limit:
            break
        if cur[i] in _BREAK_PUNCT:
            return i + 1
    return len(cur)


def _split_long_token(tok: str, max_width: int) -> List[str]:
    """ASR 偶尔产出没有空格的长串（如 50 个字母连写），按宽度硬切开，保证不超宽"""
    if _visual_width(tok) <= max_width:
        return [tok]
    parts: List[str] = []
    cur = ""
    width = 0
    for ch in tok:
        cw = _visual_width(ch)
        if cur and width + cw > max_width:
            parts.append(cur)
            cur, width = "", 0
        cur += ch
        width += cw
    if cur:
        parts.append(cur)
    return parts


def _wrap_tokens(text: str, max_width: int) -> List[str]:
    """按视觉宽度折行，返回行文本列表"""
    lines: List[str] = []
    cur: List[str] = []
    tokens = [p for tok in _TOKEN_RE.findall(text)
              for p in _split_long_token(tok, max_width)]
    for tok in tokens:
        if cur:
            width = _visual_width(_join_tokens(cur + [tok]))
            if width > max_width:
                # 标点允许悬挂在行尾（最多超出 2 个宽度），避免句子被标点硬切开
                if tok in _BREAK_PUNCT and width <= max_width + 2:
                    cur.append(tok)
                    continue
                cut = _find_break(cur, max_width)
                lines.append(_join_tokens(cur[:cut]))
                cur = cur[cut:]
        cur.append(tok)
    if cur:
        lines.append(_join_tokens(cur))
    return [ln for ln in lines if ln.strip()]


def _balanced_two_lines(text: str, max_width: int) -> str:
    """宽度在 (max_width, 2*max_width] 时优先按标点均分成两行，
    避免贪心折行产生的「行满 + 孤零零短行」（如行尾只剩『然后』）。"""
    toks = [p for tok in _TOKEN_RE.findall(text)
            for p in _split_long_token(tok, max_width)]
    total = _visual_width(_join_tokens(toks))
    if len(toks) < 2 or total <= max_width or total > 2 * max_width:
        return ""
    best = None
    for i in range(1, len(toks)):
        lw = _visual_width(_join_tokens(toks[:i]))
        rw = _visual_width(_join_tokens(toks[i:]))
        if lw > max_width or rw > max_width:
            continue
        # 两行宽度差越小越好；断点落在标点后给奖励
        score = abs(lw - rw) - (6 if toks[i - 1] in _BREAK_PUNCT else 0)
        if best is None or score < best[0]:
            best = (score, i)
    if best is None:
        return ""
    i = best[1]
    return _join_tokens(toks[:i]) + "\n" + _join_tokens(toks[i:])


def split_segments(segments: List[Segment], max_lines: int = DEFAULT_MAX_LINES,
                   max_width: int = DEFAULT_MAX_WIDTH) -> List[Segment]:
    """把每段字幕重排为最多 max_lines 行；一行装不下时按字数比例拆成多个连续时段。

    时间轴只做切分、不改变总起止时间，切分点按各行视觉宽度加权分配。
    """
    if max_lines <= 0:
        return list(segments)
    result: List[Segment] = []
    for seg in segments:
        text = " ".join(seg.text.split())
        if not text:
            result.append(seg)
            continue
        lines = _wrap_tokens(text, max_width)
        if max_lines >= 2:
            two = _balanced_two_lines(text, max_width)
            if two:
                lines = two.splitlines()
        if len(lines) <= max_lines or seg.end <= seg.start:
            result.append(Segment(seg.start, seg.end, "\n".join(lines) or text, seg.words))
            continue
        groups = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)]
        weights = [max(1, sum(_visual_width(ln) for ln in g)) for g in groups]
        total = float(sum(weights))
        duration = seg.end - seg.start
        cur_start = seg.start
        for idx, (group, weight) in enumerate(zip(groups, weights)):
            end = seg.end if idx == len(groups) - 1 else cur_start + duration * weight / total
            result.append(Segment(cur_start, end, "\n".join(group),
                                  seg.words if idx == 0 else []))
            cur_start = end
    return result


# ---- 中英混排拆分：同一段字幕里的英文/中文各自成条 ----
_LANG_TOKEN_RE = re.compile(
    r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*"   # 英文单词/数字
    r"|[\u4e00-\u9fff]"                      # 汉字
    r"|[，。！？、；：（）《》「」『』“”‘’…—·]"  # 中文标点
    r"|[^\s]"                                # 其他符号
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_ZH_PUNCT = set("，。！？、；：（）《》「」『』“”‘’…—·")


def _classify_token(tok: str) -> str:
    if _CJK_RE.fullmatch(tok) or tok[0] in _ZH_PUNCT:
        return "zh"
    return "en"


def _is_strong_run(lang: str, toks: List[str]) -> bool:
    """英文片段需达到一定长度才算独立语句，避免把中文句里的
    英文术语（REFINE、ANIMATION 等）拆出去；中文片段必为强。"""
    if lang == "zh":
        return True
    words = sum(1 for t in toks if t[0].isalnum())
    letters = sum(len(t) for t in toks if t[0].isalnum())
    return words >= 3 and letters >= 10


def _lang_blocks(text: str) -> List[str]:
    """把一段文本按语言切成若干块，返回块文本列表（顺序不变）"""
    runs = []
    for tok in _LANG_TOKEN_RE.findall(text):
        lang = _classify_token(tok)
        if runs and runs[-1][0] == lang:
            runs[-1][1].append(tok)
        else:
            runs.append([lang, [tok]])

    blocks: List[List] = []  # [lang, tokens, strong]
    for lang, toks in runs:
        strong = _is_strong_run(lang, toks)
        if not blocks:
            blocks.append([lang, list(toks), strong])
        elif blocks[-1][0] == lang:
            blocks[-1][1].extend(toks)
        elif strong:
            blocks.append([lang, list(toks), strong])
        else:  # 弱片段并入前一块（保留在所在语言的句子里）
            blocks[-1][1].extend(toks)
    # 开头的弱片段并入后一块
    if len(blocks) >= 2 and not blocks[0][2]:
        blocks[1][1] = blocks[0][1] + blocks[1][1]
        blocks.pop(0)

    out = []
    for lang, toks, _ in blocks:
        t = _join_tokens(toks) if lang == "en" else "".join(toks)
        if t.strip():
            out.append(t.strip())
    return out


def split_by_lang(segments: List[Segment], max_lines: int = DEFAULT_MAX_LINES,
                  max_width: int = DEFAULT_MAX_WIDTH) -> List[Segment]:
    """先把中英混排的段按语种拆成独立字幕条（时间按视觉宽度比例分配，
    总起止时间不变），再逐条做行数排版。纯单语段不受影响。"""
    result: List[Segment] = []
    for seg in segments:
        blocks = _lang_blocks(" ".join(seg.text.split()))
        if len(blocks) <= 1:
            result.extend(split_segments([seg], max_lines, max_width))
            continue
        widths = [max(1, _visual_width(t)) for t in blocks]
        total = float(sum(widths))
        span = seg.end - seg.start
        cursor = seg.start
        for i, (text, w) in enumerate(zip(blocks, widths)):
            end = seg.end if i == len(blocks) - 1 else cursor + span * w / total
            if end <= cursor:
                end = min(seg.end, cursor + 0.001)
            result.extend(split_segments([Segment(cursor, end, text)], max_lines, max_width))
            cursor = end
    return result


def _write_with_retry(fn, path: str, segments: List[Segment],
                      attempts: int = 3, delay: float = 1.0):
    """写文件遇 PermissionError 自动重试：杀毒实时扫描/DLP/索引器瞬时占用文件
    是常见诱因，短暂等待后重试大多可成功；仍失败则抛出原始异常。"""
    last_err = None
    for _ in range(attempts):
        try:
            _ensure_writable(path)
            fn(segments, path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(delay)
    raise last_err


def write_outputs(segments: List[Segment], media_path: str, out_dir: str,
                  export_txt: bool = False, export_json: bool = False,
                  export_vtt: bool = False,
                  max_lines: int = DEFAULT_MAX_LINES,
                  max_width: int = DEFAULT_MAX_WIDTH,
                  split_lang: bool = False) -> List[str]:
    """按设置输出字幕产物，返回生成的文件列表。命名：原文件名.srt（与源文件同名）

    文本类产物（srt/vtt/txt）统一走排版，保证每段最多 max_lines 行；split_lang=True 时先按语种拆分中英混排段；
    词级 JSON 保留原始分段，不拆分，避免破坏词时间戳对齐。
    """
    os.makedirs(out_dir, exist_ok=True)
    base = _base_output_path(media_path, out_dir)
    if split_lang:
        laid = split_by_lang(segments, max_lines, max_width)
    else:
        laid = split_segments(segments, max_lines, max_width)
    jobs = [(write_srt, base + ".srt", laid)]
    if export_vtt:
        jobs.append((write_vtt, base + ".vtt", laid))
    if export_txt:
        jobs.append((write_txt, base + ".txt", laid))
    if export_json:
        jobs.append((write_json, base + ".json", segments))
    written = []
    for fn, p, segs in jobs:
        _write_with_retry(fn, p, segs)
        written.append(p)
    return written


# ---- SRT 解析（字幕预览/校对用） ----
_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{3})"
)


def _to_seconds(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(path: str) -> List[Segment]:
    """解析 SRT 文件为分段列表"""
    with open(path, "r", encoding="utf-8-sig") as f:
        content = f.read()
    blocks = re.split(r"\n\s*\n", content.strip())
    segments: List[Segment] = []
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        # 第一行可能是序号
        time_line_idx = 0
        if lines[0].strip().isdigit() and len(lines) > 1:
            time_line_idx = 1
        if time_line_idx >= len(lines):
            continue
        m = _SRT_TIME_RE.search(lines[time_line_idx])
        if not m:
            continue
        start = _to_seconds(*m.groups()[:4])
        end = _to_seconds(*m.groups()[4:])
        text = "\n".join(lines[time_line_idx + 1:]).strip()
        segments.append(Segment(start=start, end=end, text=text))
    return segments


def parse_time_text(text: str) -> float:
    """解析用户输入的时间文本（支持 00:00:00,000 / 00:00:00.000 / 秒数）"""
    text = text.strip()
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})$", text)
    if m:
        return _to_seconds(m.group(1), m.group(2), m.group(3), m.group(4).ljust(3, "0"))
    m = re.match(r"^(\d{1,2}):(\d{2})[,.](\d{1,3})$", text)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3).ljust(3, "0")) / 1000.0
    try:
        return float(text)
    except ValueError:
        return 0.0

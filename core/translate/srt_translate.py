# -*- coding: utf-8 -*-
"""SRT 字幕批量翻译：递归扫描 → 逐句翻译 → 输出双语/译文 SRT。

安全约定：
- 仅读取用户手动选择的 .srt 文件，不扫描其他目录；
- 翻译全程本地推理，不联网、不上传任何字幕文本；
- 时间轴完全沿用原字幕，绝不改动起止时间。
"""
import os
import time
from typing import Callable, List, Optional, Tuple

from ..asr.base import Segment
from ..srt_utils import _ensure_writable, parse_srt, split_by_lang, write_srt, DEFAULT_MAX_WIDTH
from .nllb_backend import (
    DIRECTIONS, LANG_EN, LANG_ZH, NLLBTranslator, TranslateError, detect_direction,
)

OUTPUT_SUFFIX = "_translated"

# 输出方式
OUTPUT_MODES = {
    "bilingual": "双语双行（原文+译文）",
    "translated": "仅译文",
}

# 日志里显示的语言名
_LANG_LABELS = {
    (LANG_ZH, LANG_EN): ("中文", "英文"),
    (LANG_EN, LANG_ZH): ("英文", "中文"),
}
_AUTO_LABEL = ("自动识别", "译成中文")


def scan_srt_files(folder: str) -> List[str]:
    """递归扫描文件夹内全部 .srt（含所有子文件夹）"""
    found: List[str] = []
    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if name.lower().endswith(".srt"):
                found.append(os.path.join(root, name))
    return found


def resolve_languages(texts: List[str], direction: str) -> Tuple[str, str]:
    """按用户选择的模式确定 (源语言, 目标语言)"""
    if direction == "zh2en":
        return "zho_Hans", "eng_Latn"
    if direction == "en2zh":
        return "eng_Latn", "zho_Hans"
    return detect_direction(texts)


def build_out_path(src_path: str, root_dir: str, out_dir: str) -> str:
    """计算输出路径。

    同目录：<原名>_translated.srt
    统一输出目录：保留相对 root 的子目录结构，避免不同日期目录同名文件互相覆盖。
    """
    base = os.path.splitext(os.path.basename(src_path))[0] + OUTPUT_SUFFIX + ".srt"
    if not out_dir:
        return os.path.join(os.path.dirname(src_path), base)
    rel_dir = ""
    if root_dir:
        try:
            rel = os.path.relpath(os.path.dirname(src_path), root_dir)
            if not rel.startswith(".."):
                rel_dir = rel if rel != "." else ""
        except ValueError:
            rel_dir = ""
    target_dir = os.path.join(out_dir, rel_dir) if rel_dir else out_dir
    return os.path.join(target_dir, base)


def _write_with_retry(segments: List[Segment], path: str,
                      attempts: int = 3, delay: float = 1.0):
    """写盘遇 PermissionError 自动重试（杀毒/DLP 瞬时占用常见）"""
    last_err = None
    for _ in range(attempts):
        try:
            _ensure_writable(path)
            write_srt(segments, path)
            return
        except PermissionError as e:
            last_err = e
            time.sleep(delay)
    raise last_err


def _translate_auto(originals: List[str], translator: NLLBTranslator) -> List[str]:
    """自动模式：逐句判断语种，统一译成中文。

    - 英文句（含英文为主的混排句）→ 中文译文；
    - 中文句（中文为主）→ 不翻译，返回空串，输出时保持单行原文。

    中英混合素材里同一文件常两种语言夹杂，按整篇判定会译错一半，因此逐句判定。
    """
    translated = [""] * len(originals)
    en_idx = [i for i, text in enumerate(originals)
              if text.strip() and detect_direction([text])[0] == LANG_EN]
    if en_idx:
        res = translator.translate_texts([originals[i] for i in en_idx], LANG_ZH,
                                         source_lang=LANG_EN)
        for i, text in zip(en_idx, res):
            translated[i] = text
    return translated


def _source_text(text: str) -> str:
    """取待翻译原文：已是双语字幕（两行语种不同）则只取第一行，普通多行字幕合并成一句"""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    if len(lines) >= 2 and detect_direction([lines[0]])[0] != detect_direction([lines[1]])[0]:
        return lines[0]
    return " ".join(lines)


def translate_srt_file(src_path: str, translator: NLLBTranslator,
                       root_dir: str = "", out_dir: str = "",
                       direction: str = "auto", output_mode: str = "bilingual",
                       progress_cb: Optional[Callable[[int, int], None]] = None) -> Tuple[str, str, str, int]:
    """翻译单个 SRT 文件。

    返回 (输出文件路径, 源语言显示名, 目标语言显示名, 字幕条数)
    """
    segments = parse_srt(src_path)
    if not segments:
        raise TranslateError("字幕文件为空或格式无法解析：%s" % os.path.basename(src_path))

    # 先取每段原文（双语字幕只取原文行），再按语种拆分混排段并切成每段 1 行，
    # 保证每条待翻译文本都是单一语言、双语输出每段正好 2 行
    base = [Segment(s.start, s.end, _source_text(s.text)) for s in segments]
    segments = split_by_lang(base, max_lines=1, max_width=DEFAULT_MAX_WIDTH)
    originals = [s.text for s in segments]

    if direction == "auto":
        translated = _translate_auto(originals, translator)
        src_label, tgt_label = _AUTO_LABEL
    else:
        src_lang, tgt_lang = resolve_languages(originals, direction)
        translated = translator.translate_texts(originals, tgt_lang, source_lang=src_lang,
                                                progress_cb=progress_cb)
        src_label, tgt_label = _LANG_LABELS.get((src_lang, tgt_lang), (src_lang, tgt_lang))

    merged: List[Segment] = []
    for seg, orig, trans in zip(segments, originals, translated):
        trans = (trans or "").strip()
        if output_mode == "translated":
            text = trans or orig
        else:
            # 双语双行：原文一行、译文一行（同一时间轴内）
            if trans and trans != orig:
                text = orig + "\n" + trans
            else:
                text = orig
        merged.append(Segment(start=seg.start, end=seg.end, text=text))

    out_path = build_out_path(src_path, root_dir, out_dir)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    _write_with_retry(merged, out_path)
    return out_path, src_label, tgt_label, len(merged)

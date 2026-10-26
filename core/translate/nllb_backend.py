# -*- coding: utf-8 -*-
"""NLLB-200 600M 本地翻译后端（ctranslate2 + sentencepiece）。

纯本地推理：模型加载后不再有任何网络请求，也不上传任何文本。
模型仓库（ct2 int8，约 0.7GB）：
    JustFrederik/nllb-200-distilled-600M-ct2-int8
需包含：model.bin / config.json / sentencepiece.bpe.model

说明：
- 一个 SRT 文件整体判定翻译方向（按中文字符占比），避免逐句方向抖动；
- 逐句翻译，时间轴完全沿用原字幕，不重排、不合并、不拆分。
"""
import os
import re
from typing import List, Optional, Tuple

# NLLB 语言代码
LANG_ZH = "zho_Hans"
LANG_EN = "eng_Latn"

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

DIRECTIONS = {
    "auto": "自动识别（英文译成中文，中文保留）",
    "zh2en": "中 → 英",
    "en2zh": "英 → 中",
}


class TranslateError(Exception):
    """翻译相关错误（面向用户的中文提示）"""


def detect_direction(texts: List[str]) -> Tuple[str, str]:
    """根据整篇字幕的中英文占比判断翻译方向，返回 (源语言, 目标语言)"""
    cjk = 0
    latin = 0
    for t in texts:
        cjk += len(_CJK_RE.findall(t))
        latin += len(_LATIN_RE.findall(t))
    if cjk == 0 and latin == 0:
        return LANG_ZH, LANG_EN
    # 中文字符数量是英文单词量的 1.5 倍以上即视为中文素材
    if cjk >= latin * 1.5:
        return LANG_ZH, LANG_EN
    return LANG_EN, LANG_ZH


class NLLBTranslator:
    """NLLB-200 distilled 600M（int8）本地翻译器"""

    display_name = "NLLB-200 600M int8"

    def __init__(self, model_dir: str, device: str = "auto", beam_size: int = 4):
        self.model_dir = model_dir
        self.device = device
        self.beam_size = beam_size
        self._translator = None
        self._sp = None
        self._actual_device = ""

    # ---------------- 加载 ----------------
    def _check_files(self) -> Tuple[str, str]:
        model_bin = os.path.join(self.model_dir, "model.bin")
        sp_model = os.path.join(self.model_dir, "sentencepiece.bpe.model")
        missing = [p for p in (model_bin, sp_model) if not os.path.isfile(p)]
        if missing:
            raise TranslateError(
                "翻译模型文件不完整，缺少：%s。请在【模型管理】中下载 NLLB-200 模型。"
                % "、".join(os.path.basename(p) for p in missing)
            )
        return model_bin, sp_model

    def load(self, status_cb=None):
        if self._translator is not None:
            return
        model_bin, sp_model = self._check_files()
        try:
            import ctranslate2
            import sentencepiece as spm
        except ImportError:
            raise TranslateError(
                "缺少翻译依赖。请执行：pip install ctranslate2 sentencepiece"
            )

        if status_cb:
            status_cb("正在加载 NLLB 翻译模型…")
        self._sp = spm.SentencePieceProcessor(model_file=sp_model)

        device = self.device
        if device == "auto":
            try:
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:
                device = "cpu"

        def _build(dev: str, ctype: str):
            return ctranslate2.Translator(
                self.model_dir, device=dev, compute_type=ctype,
                inter_threads=1, intra_threads=max(1, (os.cpu_count() or 4) // 2),
            )

        # GPU 上 int8_float16，CPU 上 int8
        compute_type = "int8_float16" if device == "cuda" else "int8"
        try:
            translator = _build(device, compute_type)
        except Exception as e:
            if device != "cuda":
                raise TranslateError("翻译模型加载失败：%s" % str(e).splitlines()[0][:200])
            device, compute_type, translator = "cpu", "int8", _build("cpu", "int8")

        # 预热一次：CUDA 缺少 cublas 等运行库时只有真正推理才会报错，这里提前暴露并降级
        if device == "cuda":
            try:
                translator.translate_batch(
                    [self._sp.encode("test", out_type=str)],
                    target_prefix=[["eng_Latn"]], beam_size=1, max_decoding_length=4,
                )
            except Exception as e:
                if status_cb:
                    status_cb("GPU 不可用（%s），自动切换 CPU int8"
                              % str(e).splitlines()[0][:60])
                del translator
                device, compute_type = "cpu", "int8"
                translator = _build("cpu", "int8")

        self._translator = translator
        self._actual_device = device
        if status_cb:
            status_cb("翻译模型加载完成（%s / %s）" % (device, compute_type))

    @property
    def loaded(self) -> bool:
        return self._translator is not None

    @property
    def device_name(self) -> str:
        return self._actual_device or "-"

    # ---------------- 翻译 ----------------
    def _decode(self, hyp: List[str], target_lang: str) -> str:
        """把子词序列解码为文本，并去掉句首的目标语言标记"""
        if hyp and hyp[0] == target_lang:
            hyp = hyp[1:]
        if not hyp:
            return ""
        text = self._sp.decode(hyp)
        return text.strip()

    def _encode_source(self, text: str, source_lang: str) -> List[str]:
        """按 NLLB 官方用法编码源句：源语言 token + 子词 + 结尾 </s>。

        ct2 不会为 Transformers 模型自动补特殊符号，缺少结尾 </s> 会导致解码退化为重复乱码。
        """
        tokens = self._sp.encode(text, out_type=str)
        if source_lang:
            tokens = [source_lang] + tokens
        return tokens + ["</s>"]

    def translate_texts(self, texts: List[str], target_lang: str, source_lang: str = "",
                        progress_cb=None, batch_size: int = 16) -> List[str]:
        """批量翻译文本列表；空串原样保留（返回等长列表）"""
        if self._translator is None or self._sp is None:
            raise TranslateError("翻译模型尚未加载")

        results: List[str] = [""] * len(texts)
        # 过滤空文本，避免浪费算力并保持索引对齐
        todo_idx = [i for i, t in enumerate(texts) if t and t.strip()]
        if not todo_idx:
            return results

        for start in range(0, len(todo_idx), batch_size):
            chunk_idx = todo_idx[start:start + batch_size]
            chunk_texts = [texts[i].strip().replace("\n", " ") for i in chunk_idx]
            tokens = [self._encode_source(t, source_lang) for t in chunk_texts]
            try:
                out = self._translator.translate_batch(
                    tokens,
                    target_prefix=[[target_lang]] * len(tokens),
                    beam_size=self.beam_size,
                    max_batch_size=batch_size,
                    batch_type="examples",
                )
            except Exception as e:
                raise TranslateError("翻译推理失败：%s" % str(e).splitlines()[0][:200])
            for i, res in zip(chunk_idx, out):
                hyp = res.hypotheses[0] if res.hypotheses else []
                results[i] = self._decode(hyp, target_lang)
            if progress_cb:
                progress_cb(min(len(todo_idx), start + len(chunk_idx)), len(todo_idx))
        return results

    def unload(self):
        self._translator = None
        self._sp = None

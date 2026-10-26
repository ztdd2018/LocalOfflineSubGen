# -*- coding: utf-8 -*-
"""faster-whisper 后端（默认首选）。

- 模型：large-v3（精度优先）/ large-v3-turbo（速度优先）
- 中英混合：language=auto 时由 Whisper 自动检测语种
- 硬件：自动探测 CUDA；显存溢出自动降级 CPU int8，并给出友好提示
- 大文件：faster-whisper 内部按 30s 窗口流式读取音频，不会一次性载入内存
"""
import os
from typing import List, Optional

from .base import (ASRBackend, ASRError, ProgressCallback, Segment, StatusCallback,
                   TranscribeParams, Word)

# GUI 展示名 -> faster-whisper 内部模型名
MODEL_NAME_MAP = {
    "large-v3": "large-v3",
    "large-v3-turbo": "turbo",
}


class FasterWhisperBackend(ASRBackend):
    name = "faster-whisper"
    display_name = "faster-whisper"

    def __init__(self, model_name: str = "large-v3", compute_type: str = "auto",
                 model_local_path: str = "", cache_dir: str = ""):
        self.model_name = model_name
        self.compute_type = compute_type
        self.model_local_path = model_local_path
        self.cache_dir = cache_dir
        self._model = None
        self.device_info = ""

    # ---- 设备与精度选择 ----
    def _resolve_device_and_compute(self):
        device = self.detect_device()
        if self.compute_type == "auto":
            compute = "float16" if device == "cuda" else "int8"
        else:
            compute = self.compute_type
            # CPU 不支持 float16，强制回退 int8
            if device == "cpu" and compute == "float16":
                compute = "int8"
        return device, compute

    def _model_source(self) -> str:
        """模型来源：本地 whisper 模型文件夹优先（内网拷贝场景），否则按名字从缓存/HF 下载。

        注意：model_local_path 与 sherpa 后端共用同一配置项，这里仅当目录确实是
        CTranslate2 whisper 模型（含 model.bin）时才使用，避免指向 sherpa 模型目录
        后切回 faster-whisper 导致加载失败。
        """
        if self.model_local_path and os.path.isfile(os.path.join(self.model_local_path, "model.bin")):
            return self.model_local_path
        return MODEL_NAME_MAP.get(self.model_name, self.model_name)

    def load(self, status_cb: Optional[StatusCallback] = None):
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise ASRError("未安装 faster-whisper，请先执行：pip install -r requirements.txt")

        device, compute = self._resolve_device_and_compute()
        source = self._model_source()
        kwargs = {"device": device, "compute_type": compute}
        # 非本地路径时走统一模型缓存目录，便于内网拷贝与离线复用
        if not self.model_local_path and self.cache_dir:
            kwargs["download_root"] = self.cache_dir

        def _notify(msg):
            if status_cb:
                status_cb(msg)

        _notify("正在加载模型 %s（%s / %s）…" % (self.model_name, device, compute))
        try:
            self._model = WhisperModel(source, **kwargs)
        except Exception as e:
            msg = str(e)
            is_oom = ("CUDA out of memory" in msg) or ("cublas" in msg.lower() and "alloc" in msg.lower())
            if device == "cuda" and (is_oom or "CUDA" in msg):
                # 显存不足/CUDA 异常 → 自动降级 CPU int8，友好提示
                _notify("CUDA 加载失败（%s），自动降级 CPU int8 继续运行" % msg.splitlines()[0][:80])
                kwargs = {"device": "cpu", "compute_type": "int8"}
                if not self.model_local_path and self.cache_dir:
                    kwargs["download_root"] = self.cache_dir
                try:
                    self._model = WhisperModel(source, **kwargs)
                    device, compute = "cpu", "int8"
                except Exception as e2:
                    raise ASRError("模型加载失败：%s" % str(e2).splitlines()[0][:200])
            else:
                if "offline" in msg.lower() or "connection" in msg.lower() or "Offline" in msg:
                    raise ASRError(
                        "模型未下载且当前处于离线状态。请关闭离线锁定后下载模型，或将模型文件手动放入缓存目录（见 README）。"
                    )
                raise ASRError("模型加载失败：%s" % msg.splitlines()[0][:200])

        self.device_info = "%s / %s" % (device, compute)
        _notify("模型加载完成（%s）" % self.device_info)

    def transcribe(self, audio_path: str, params: TranscribeParams,
                   progress_cb: Optional[ProgressCallback] = None) -> List[Segment]:
        if self._model is None:
            raise ASRError("模型尚未加载")

        language = None if params.language == "auto" else params.language
        segments_gen, info = self._model.transcribe(
            audio_path,
            language=language,
            vad_filter=params.vad_filter,
            vad_parameters={"threshold": params.vad_threshold} if params.vad_filter else None,
            beam_size=params.beam_size,
            word_timestamps=params.word_timestamps,
        )
        total = float(getattr(info, "duration", 0.0) or 0.0)

        results: List[Segment] = []
        # 逐段消费生成器（内部流式解码，内存占用与文件大小无关）
        for seg in segments_gen:
            words = []
            if params.word_timestamps and getattr(seg, "words", None):
                words = [Word(start=w.start, end=w.end, word=w.word) for w in seg.words]
            results.append(Segment(start=seg.start, end=seg.end, text=seg.text.strip(), words=words))
            if progress_cb:
                progress_cb(min(seg.end, total) if total else seg.end, total)
        return results

    def unload(self):
        self._model = None

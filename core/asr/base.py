# -*- coding: utf-8 -*-
"""ASR 后端抽象基类：统一 faster-whisper / sherpa-onnx 等后端的调用接口"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional


class ASRError(Exception):
    """ASR 相关错误（中文可读）"""
    pass


class TaskCanceled(Exception):
    """任务被用户取消/停止"""
    pass


@dataclass
class Word:
    start: float
    end: float
    word: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: List[Word] = field(default_factory=list)


@dataclass
class TranscribeParams:
    language: str = "auto"          # auto / zh / en
    vad_filter: bool = True
    vad_threshold: float = 0.5
    beam_size: int = 5
    word_timestamps: bool = False


# 进度回调：(已完成秒数, 总时长秒数)；可在回调内抛 TaskCanceled 中止
ProgressCallback = Callable[[float, float], None]
# 状态回调：纯文本提示（模型加载进度等）
StatusCallback = Callable[[str], None]


class ASRBackend:
    """所有 ASR 后端需实现的接口"""

    #: 后端标识名
    name = "base"
    #: 展示名
    display_name = "基础后端"

    def load(self, status_cb: Optional[StatusCallback] = None):
        """加载模型到内存/显存。首次可能需要联网下载（受离线锁定管控）。"""
        raise NotImplementedError

    def transcribe(self, audio_path: str, params: TranscribeParams,
                   progress_cb: Optional[ProgressCallback] = None) -> List[Segment]:
        """识别单个音频文件，返回分段列表。"""
        raise NotImplementedError

    def unload(self):
        """释放模型（可选实现）"""

    @staticmethod
    def detect_device() -> str:
        """探测可用计算设备：cuda / cpu"""
        try:
            import ctranslate2
            if ctranslate2.get_cuda_device_count() > 0:
                return "cuda"
        except Exception:
            pass
        return "cpu"


def create_backend(name: str, model_name: str, compute_type: str = "auto",
                   model_local_path: str = "", cache_dir: str = "") -> ASRBackend:
    """后端工厂"""
    if name == "faster-whisper":
        from .faster_whisper_backend import FasterWhisperBackend
        return FasterWhisperBackend(model_name, compute_type, model_local_path, cache_dir)
    if name == "sherpa-onnx":
        from .sherpa_backend import SherpaOnnxBackend
        return SherpaOnnxBackend(model_name, compute_type, model_local_path, cache_dir)
    raise ASRError("未知的 ASR 后端：%s" % name)

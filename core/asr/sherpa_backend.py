# -*- coding: utf-8 -*-
"""sherpa-onnx 备选后端：FireRedASR2（中文为主、少量英文场景）。

说明：
- 该后端为可选项，需 pip install sherpa-onnx soundfile，并放置 sherpa-onnx 官方
  导出的 ONNX 模型（encoder/decoder/tokens.txt），在模型面板中选择该目录。
- sherpa-onnx 为纯本地推理框架，不产生任何网络请求。
- FireRedASR2 AED 编码器对单次输入时长有限制，长音频必须先切块：
  本实现采用"静音感知切分"（目标 28s，静音处下刀，50s 硬上限），
  再按标点结合 token 级时间戳组句，保证字幕时间轴准确。
"""
import os
import subprocess
import sys
from typing import List, Optional, Tuple

import numpy as np

from .base import (ASRBackend, ASRError, ProgressCallback, Segment, StatusCallback,
                   TranscribeParams)

# 模型目录内需要包含的关键文件（sherpa-onnx 官方导出结构）
_REQUIRED_HINT = "encoder.onnx / decoder.onnx / tokens.txt"
_SAMPLE_RATE = 16000

# 长音频切分参数
_CHUNK_TARGET_SEC = 28.0   # 目标块长（尽量在静音处切）
_CHUNK_MAX_SEC = 50.0      # 硬上限：找不到静音点也强制切
_MIN_SILENCE_SEC = 0.25    # 判定静音的最短持续时长

# 字幕分句标点
_END_PUNCT = set("。！？!?；;…")
_SOFT_PUNCT = set("，,、")
_SOFT_SPLIT_MIN_CHARS = 15  # 软标点仅在句长达到该值时才切，避免碎句


class SherpaOnnxBackend(ASRBackend):
    name = "sherpa-onnx"
    display_name = "sherpa-onnx FireRedASR2"

    def __init__(self, model_name: str = "FireRedASR2", compute_type: str = "int8",
                 model_local_path: str = "", cache_dir: str = ""):
        self.model_name = model_name
        self.model_local_path = model_local_path
        self._recognizer = None

    def _find_file(self, directory: str, keyword: str) -> str:
        """在模型目录中按关键字定位 onnx/txt 文件"""
        for name in sorted(os.listdir(directory)):
            low = name.lower()
            if keyword in low and (low.endswith(".onnx") or low.endswith(".txt")):
                return os.path.join(directory, name)
        return ""

    def load(self, status_cb: Optional[StatusCallback] = None):
        if self._recognizer is not None:
            return
        try:
            import sherpa_onnx
        except ImportError:
            raise ASRError(
                "sherpa-onnx 后端未安装。该后端为可选项，请执行：pip install sherpa-onnx soundfile，"
                "或切换回默认的 faster-whisper 后端。"
            )
        directory = self.model_local_path
        if not directory or not os.path.isdir(directory):
            raise ASRError(
                "请先在【模型管理】中选择 FireRedASR2 模型文件夹（需包含 %s）。" % _REQUIRED_HINT
            )
        encoder = self._find_file(directory, "encoder")
        decoder = self._find_file(directory, "decoder")
        tokens = self._find_file(directory, "tokens")
        if not (encoder and decoder and tokens):
            raise ASRError("模型目录文件不完整，需包含 %s。" % _REQUIRED_HINT)

        if status_cb:
            status_cb("正在加载 FireRedASR2 模型…")
        try:
            # 新版本叫 from_fire_red_asr2；sherpa-onnx 2.0 等版本走 from_fire_red_asr（同架构兼容）
            factory = getattr(sherpa_onnx.OfflineRecognizer, "from_fire_red_asr2", None) or \
                getattr(sherpa_onnx.OfflineRecognizer, "from_fire_red_asr", None)
            if factory is None:
                raise ASRError("当前 sherpa-onnx 版本不支持 FireRedASR 系列模型，请升级 sherpa-onnx。")
            self._recognizer = factory(
                encoder=encoder, decoder=decoder, tokens=tokens,
                num_threads=max(1, (os.cpu_count() or 4) // 2),
                provider="cpu",
            )
        except ASRError:
            raise
        except Exception as e:
            raise ASRError("FireRedASR2 模型加载失败：%s" % str(e).splitlines()[0][:200])
        if status_cb:
            status_cb("FireRedASR2 模型加载完成（CPU）")

    # ---------------- 音频读取 ----------------
    @staticmethod
    def _load_samples_16k(path: str) -> np.ndarray:
        """读取任意格式/采样率的音频为 16kHz 单声道 float32。

        快路径：16k 单声道 WAV 直接用 soundfile 读；
        其余（mp3/m4a/aac/flac/44.1k 等）走内置 ffmpeg 管道转换，纯本地无网络。
        """
        try:
            import soundfile as sf
            info = sf.info(path)
            if info.format == "WAV" and info.samplerate == _SAMPLE_RATE and info.channels == 1:
                samples, _ = sf.read(path, dtype="float32")
                return samples
        except Exception:
            pass
        from ..ffmpeg_utils import FFmpegError, ffmpeg_path
        cmd = [ffmpeg_path(), "-v", "error", "-i", path,
               "-vn", "-ac", "1", "-ar", str(_SAMPLE_RATE), "-f", "s16le", "pipe:1"]
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=creationflags)
        if proc.returncode != 0 or not proc.stdout:
            raise FFmpegError("音频解码失败：%s" % os.path.basename(path))
        return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0

    # ---------------- 长音频静音切分 ----------------
    @staticmethod
    def _energy_db(samples: np.ndarray) -> Tuple[np.ndarray, float]:
        """10ms 帧移的能量包络（dB）。返回 (帧 dB 数组, 每帧秒数)"""
        frame = int(0.025 * _SAMPLE_RATE)
        hop = int(0.010 * _SAMPLE_RATE)
        if len(samples) < frame:
            return np.array([-60.0]), hop / _SAMPLE_RATE
        sq = samples.astype(np.float64) ** 2
        cs = np.concatenate(([0.0], np.cumsum(sq)))
        starts = np.arange(0, len(samples) - frame + 1, hop)
        means = (cs[starts + frame] - cs[starts]) / frame
        return 10.0 * np.log10(means + 1e-12), hop / _SAMPLE_RATE

    @classmethod
    def _split_points(cls, samples: np.ndarray) -> List[int]:
        """计算切分点（样本索引列表，不含首尾）。短于硬上限则不切。"""
        total_sec = len(samples) / _SAMPLE_RATE
        if total_sec <= _CHUNK_MAX_SEC:
            return []
        db, frame_sec = cls._energy_db(samples)
        noise_floor = float(np.percentile(db, 20))
        silence = db < (noise_floor + 6.0)
        # 收集 ≥ _MIN_SILENCE_SEC 的静音段中点（秒）
        sil_centers = []
        i, n = 0, len(silence)
        min_frames = max(1, int(_MIN_SILENCE_SEC / frame_sec))
        while i < n:
            if silence[i]:
                j = i
                while j < n and silence[j]:
                    j += 1
                if j - i >= min_frames:
                    sil_centers.append(((i + j) / 2.0) * frame_sec)
                i = j
            else:
                i += 1
        # 贪心选点：优先靠近目标块长的静音点，找不到则在硬上限处切
        points = []
        start = 0.0
        while total_sec - start > _CHUNK_MAX_SEC:
            target = start + _CHUNK_TARGET_SEC
            hard = start + _CHUNK_MAX_SEC
            cands = [c for c in sil_centers if start + 1.0 < c <= hard]
            cut = min(cands, key=lambda c: abs(c - target)) if cands else hard
            points.append(int(cut * _SAMPLE_RATE))
            start = cut
        return points

    # ---------------- 分段组装 ----------------
    @staticmethod
    def _tokens_to_text(tokens: List[str]) -> str:
        return "".join(tokens).replace("▁", " ").strip()

    def _decode_chunk(self, chunk: np.ndarray, offset_sec: float) -> List[Segment]:
        """识别单个块，并按标点结合 token 时间戳组句（时间为全片绝对时间）"""
        stream = self._recognizer.create_stream()
        stream.accept_waveform(_SAMPLE_RATE, chunk)
        self._recognizer.decode_stream(stream)
        result = stream.result
        text = (result.text or "").strip()
        if not text:
            return []
        tokens = list(getattr(result, "tokens", []) or [])
        timestamps = list(getattr(result, "timestamps", []) or [])
        dur = len(chunk) / _SAMPLE_RATE

        # 无 token 时间戳时退化为整块一句
        if not tokens or not timestamps or len(tokens) != len(timestamps):
            return [Segment(start=offset_sec, end=offset_sec + dur, text=text)]

        # 按标点切句：硬标点必切；软标点在句长足够时切
        groups = []  # (起始时间戳, 末token时间戳, token列表)
        cur_tokens, cur_start = [], None
        for tok, ts in zip(tokens, timestamps):
            if cur_start is None:
                cur_start = ts
            cur_tokens.append(tok)
            joined = self._tokens_to_text(cur_tokens)
            if tok in _END_PUNCT or (tok in _SOFT_PUNCT and len(joined) >= _SOFT_SPLIT_MIN_CHARS):
                groups.append((cur_start, ts, list(cur_tokens)))
                cur_tokens, cur_start = [], None
        if cur_tokens:
            groups.append((cur_start if cur_start is not None else 0.0,
                           timestamps[-1], cur_tokens))

        segs: List[Segment] = []
        for idx, (g_start, g_last_ts, g_tokens) in enumerate(groups):
            g_text = self._tokens_to_text(g_tokens)
            if not g_text:
                continue
            # 结束时间：以下一句开始时间为界；末句给 0.6s 尾长且不越块界
            if idx + 1 < len(groups):
                g_end = groups[idx + 1][0]
            else:
                g_end = min(g_last_ts + 0.6, dur)
            segs.append(Segment(
                start=offset_sec + max(0.0, g_start),
                end=offset_sec + max(g_start + 0.2, g_end),
                text=g_text,
            ))
        return segs

    def transcribe(self, audio_path: str, params: TranscribeParams,
                   progress_cb: Optional[ProgressCallback] = None) -> List[Segment]:
        if self._recognizer is None:
            raise ASRError("模型尚未加载")
        samples = self._load_samples_16k(audio_path)
        duration = len(samples) / float(_SAMPLE_RATE)
        if duration < 0.2:
            return []

        points = self._split_points(samples)
        bounds = [0] + points + [len(samples)]
        results: List[Segment] = []
        for i in range(len(bounds) - 1):
            chunk = samples[bounds[i]:bounds[i + 1]]
            if len(chunk) < int(0.2 * _SAMPLE_RATE):
                continue
            offset = bounds[i] / float(_SAMPLE_RATE)
            results.extend(self._decode_chunk(chunk, offset))
            if progress_cb:
                progress_cb(bounds[i + 1] / float(_SAMPLE_RATE), duration)
        return results

    def unload(self):
        self._recognizer = None

# -*- coding: utf-8 -*-
"""全局配置管理：JSON 持久化，绿色便携（配置保存在程序目录下，不写注册表）"""
import json
import os
import sys
import threading


def app_root() -> str:
    """程序根目录（兼容 Nuitka/PyInstaller 打包后与源码运行）"""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    # 源码运行：core/ 的上一级
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# 支持的输入格式
VIDEO_EXTS = {".mts", ".mpg", ".mp4", ".mkv", ".mov", ".avi", ".flv", ".wmv"}
AUDIO_EXTS = {".wav", ".m4a", ".mp3", ".flac"}

DEFAULTS = {
    # ---- ASR 参数 ----
    "backend": "faster-whisper",          # faster-whisper / sherpa-onnx
    "model_name": "large-v3",             # large-v3 / large-v3-turbo / FireRedASR2
    "model_local_path": "",               # 手动放置的本地模型文件夹（内网环境）
    "language": "auto",                   # auto / zh / en
    "vad_filter": True,                   # VAD 静音过滤
    "vad_threshold": 0.5,                 # VAD 阈值
    "beam_size": 5,
    "compute_type": "auto",               # auto / float16 / int8
    "word_timestamps": False,             # 是否输出词级时间戳 json

    # ---- 音频提取 ----
    "audio_format": "wav",                # wav / m4a
    "denoise": False,                     # ffmpeg afftdn 降噪
    "auto_clean_temp": True,              # 处理完自动删除临时音频（保密推荐）
    "keep_audio": False,                  # 保留音频文件用于复核

    # ---- 输出 ----
    "output_mode": "same_dir",            # same_dir / custom
    "output_dir": "",                     # custom 模式下的统一输出目录
    "export_txt": False,
    "export_json": False,
    "export_vtt": False,

    # ---- 字幕排版（每段最多几行，超长拆时段） ----
    "subtitle_max_lines": 2,              # 1 / 2 / 0=不处理
    "subtitle_max_width": 40,             # 视觉宽度：汉字算 2、英文算 1（≈20 汉字/行）
    "subtitle_split_lang": True,          # 中英混排段按语种拆成独立字幕条

    # ---- 字幕翻译 ----
    "translate_direction": "auto",        # auto / zh2en / en2zh
    "translate_output": "bilingual",      # bilingual（双语双行）/ translated（仅译文）
    "translate_output_mode": "same_dir",  # same_dir / custom
    "translate_out_dir": "",              # custom 模式下的统一输出目录

    # ---- 调度 ----
    "concurrency": 1,                     # 并发数（默认串行，防显存爆）

    # ---- 安全 / 系统 ----
    "offline_lock": False,                # 离线锁定开关
    "ffmpeg_path": "",                    # 用户自定义 ffmpeg（留空用内置/系统）
    "cache_dir": "",                      # 模型/临时缓存目录（留空用默认）
    "log_dir": "",                        # 日志目录（留空用默认）
    "first_run_done": False,              # 首次启动向导是否已展示
}


class Config:
    """线程安全的简单 JSON 配置"""

    def __init__(self):
        self._lock = threading.Lock()
        self._dir = os.path.join(app_root(), "config")
        self._path = os.path.join(self._dir, "settings.json")
        self._data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            if os.path.exists(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    if k in DEFAULTS:
                        self._data[k] = v
        except Exception:
            # 配置损坏时回退默认值，不影响启动
            pass

    def save(self):
        with self._lock:
            try:
                os.makedirs(self._dir, exist_ok=True)
                with open(self._path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
        self.save()

    # ---- 派生路径 ----
    def cache_dir(self) -> str:
        d = self.get("cache_dir") or os.path.join(app_root(), "cache")
        os.makedirs(d, exist_ok=True)
        return d

    def temp_dir(self) -> str:
        d = os.path.join(self.cache_dir(), "temp")
        os.makedirs(d, exist_ok=True)
        return d

    def model_cache_dir(self) -> str:
        d = os.path.join(self.cache_dir(), "models")
        os.makedirs(d, exist_ok=True)
        return d

    def log_dir(self) -> str:
        d = self.get("log_dir") or os.path.join(app_root(), "logs")
        os.makedirs(d, exist_ok=True)
        return d

    def tasks_file(self) -> str:
        return os.path.join(self._dir, "tasks.json")


config = Config()

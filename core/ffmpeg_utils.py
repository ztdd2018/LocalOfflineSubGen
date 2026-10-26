# -*- coding: utf-8 -*-
"""FFmpeg / FFprobe 定位与调用封装。

查找顺序：用户在设置中指定的路径 > 程序内置 bin/ 目录 > 系统 PATH。
所有调用均为本地子进程，无任何网络行为。
"""
import json
import os
import shutil
import subprocess
import sys

from .config import app_root, config


class FFmpegError(Exception):
    """FFmpeg 相关错误（已翻译为易懂中文）"""
    pass


def _exe_name(base: str) -> str:
    return base + ".exe" if sys.platform == "win32" else base


def find_tool(base: str) -> str:
    """定位 ffmpeg/ffprobe 可执行文件"""
    exe = _exe_name(base)
    # 1. 用户自定义
    custom = config.get("ffmpeg_path", "")
    if custom:
        cand = custom if os.path.isfile(custom) else os.path.join(custom, exe)
        if os.path.isfile(cand):
            return cand
    # 2. 内置 bin 目录（绿色打包随附）
    bundled = os.path.join(app_root(), "bin", exe)
    if os.path.isfile(bundled):
        return bundled
    # 3. 系统 PATH
    found = shutil.which(base)
    if found:
        return found
    raise FFmpegError(
        "未找到 %s。请确认程序目录下 bin/%s 存在，或在【设置】中指定外部 FFmpeg 路径。" % (base, exe)
    )


def ffmpeg_path() -> str:
    return find_tool("ffmpeg")


def ffprobe_path() -> str:
    return find_tool("ffprobe")


def _run(cmd, timeout=None) -> subprocess.CompletedProcess:
    """静默执行子进程（Windows 下不弹黑窗）"""
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=creationflags,
        )
    except subprocess.TimeoutExpired:
        raise FFmpegError("FFmpeg 执行超时，文件可能损坏或过大")


def probe_media(path: str) -> dict:
    """用 ffprobe 读取时长 / 是否含音频流 / 文件大小"""
    probe = ffprobe_path()
    cmd = [
        probe, "-v", "error",
        "-show_entries", "format=duration:stream=codec_type",
        "-of", "json", path,
    ]
    result = _run(cmd, timeout=60)
    if result.returncode != 0:
        raise FFmpegError("无法读取媒体信息，文件可能已损坏：%s" % os.path.basename(path))
    try:
        info = json.loads(result.stdout.decode("utf-8", "ignore"))
        duration = float(info.get("format", {}).get("duration", 0) or 0)
        streams = info.get("streams", []) or []
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
    except (ValueError, KeyError):
        duration, has_audio = 0.0, True
    size = 0
    try:
        size = os.path.getsize(path)
    except OSError:
        pass
    return {"duration": duration, "has_audio": has_audio, "size": size}


def extract_audio(video_path: str, out_dir: str, fmt: str = "wav", denoise: bool = False) -> str:
    """从视频抽取音频。

    - wav：16kHz 单声道 pcm_s16le（ASR 最优输入）
    - m4a：AAC 128k（节省空间）
    - denoise：ffmpeg 本地 afftdn 降噪（纯本地 DSP，无网络）
    返回输出文件路径。
    """
    ffmpeg = ffmpeg_path()
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    out_path = os.path.join(out_dir, base + "_extracted." + fmt)

    cmd = [ffmpeg, "-y", "-v", "error", "-i", video_path, "-vn"]
    if denoise:
        cmd += ["-af", "afftdn"]
    if fmt == "wav":
        cmd += ["-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd.append(out_path)

    result = _run(cmd)
    if result.returncode != 0 or not os.path.exists(out_path):
        err = result.stderr.decode("utf-8", "ignore").strip().splitlines()
        detail = err[-1] if err else "未知错误"
        raise FFmpegError("音频提取失败：%s（%s）" % (os.path.basename(video_path), detail))
    return out_path

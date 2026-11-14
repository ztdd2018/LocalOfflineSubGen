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


# 人声增强（RNN 降噪）模型注册表：key -> (下载 URL, 文件名)
# 模型来自 GregorR/rnnoise-models（开源 RNN 降噪模型，供 ffmpeg arnndn 使用）
RNNOISE_MODELS = {
    "somnolent": (
        "https://raw.githubusercontent.com/GregorR/rnnoise-models/master/"
        "somnolent-hogwash-2018-09-01/sh.rnnn",
        "somnolent.rnnn",
    ),
}


def _escape_filter_path(path: str) -> str:
    """ffmpeg filtergraph 内的路径转义（反斜杠/冒号会破坏参数解析）"""
    return path.replace("\\", "/").replace(":", r"\:")


def rnnn_model_path() -> str:
    """人声增强模型文件路径；已就绪返回路径，否则返回空串。

    查找顺序：缓存目录 cache/denoise/ → 随程序分发的 models/denoise/。
    注意必须走 config.cache_dir()（与 ASR/翻译模型同一缓存基点），
    打包版 app_root() 下的 cache 仅为兜底，历史上曾因基点不一致导致
    模型明明在缓存里却提示需联网下载。
    """
    _, name = RNNOISE_MODELS["somnolent"]
    for base in (config.cache_dir(), os.path.join(app_root(), "models", "denoise")):
        p = os.path.join(base, name)
        if os.path.isfile(p) and os.path.getsize(p) > 10240:
            return p
    return ""


def ensure_rnnn_model(status_cb=None) -> str:
    """确保降噪模型就绪，返回模型路径；下载受离线锁管控"""
    p = rnnn_model_path()
    if p:
        return p
    if config.get("offline_lock"):
        raise FFmpegError(
            "人声增强需要首次下载降噪模型（约 300KB），当前已开启离线锁定。"
            "请临时关闭离线锁定，或手动将 .rnnn 模型放入 cache/denoise/ 目录。"
        )
    url, name = RNNOISE_MODELS["somnolent"]
    dst = os.path.join(config.cache_dir(), "denoise")
    os.makedirs(dst, exist_ok=True)
    out = os.path.join(dst, name)
    tmp = out + ".part"
    if status_cb:
        status_cb("正在下载人声增强降噪模型（约 220KB）…")
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
            f.write(resp.read())
    except OSError as e:
        raise FFmpegError("降噪模型下载失败：%s。可手动下载 %s 放到 %s" % (e, url, dst))
    os.replace(tmp, out)
    if os.path.getsize(out) < 10240:
        os.remove(out)
        raise FFmpegError("降噪模型下载内容异常，已删除。请检查网络后重试。")
    return out


def extract_audio(video_path: str, out_dir: str, fmt: str = "wav",
                  denoise: bool = False, vocal_enhance: bool = False,
                  model_path: str = "") -> str:
    """从视频抽取音频。

    - wav：16kHz 单声道 pcm_s16le（ASR 最优输入）
    - m4a：AAC 128k（节省空间）
    - denoise：ffmpeg 本地 afftdn 降噪（纯本地 DSP，无网络）
    - vocal_enhance：人声增强链（人声频段滤波 + RNN 降噪 + FFT 降噪兜底），
      优先于 denoise；对背景音乐/环境噪音素材可显著提升识别准确度。
      RNN 降噪在 CPU 上运行，速度约 0.2x 实时，耗时更长。
    返回输出文件路径。
    """
    ffmpeg = ffmpeg_path()
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    out_path = os.path.join(out_dir, base + "_extracted." + fmt)

    cmd = [ffmpeg, "-y", "-v", "error", "-i", video_path, "-vn"]
    if vocal_enhance:
        if not model_path:
            raise FFmpegError("人声增强模型未就绪")
        chain = (
            "aformat=sample_rates=48000:channel_layouts=mono,"
            "highpass=f=70,"
            "arnndn=m='%s',"
            "afftdn=nf=-25" % _escape_filter_path(model_path)
        )
        if fmt == "wav":
            chain += ",aresample=16000"
        cmd += ["-af", chain]
    elif denoise:
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

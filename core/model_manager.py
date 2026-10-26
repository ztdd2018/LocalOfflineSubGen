# -*- coding: utf-8 -*-
"""模型管理：查询/下载/删除本地 ASR 模型。

faster-whisper 模型以 HuggingFace 缓存结构保存在 缓存目录/models/ 下，
内网用户可直接拷贝该目录到其他机器实现离线部署（见 README）。
"""
import os
import shutil
from typing import Dict, List

from .config import config

# 已知模型注册表
MODEL_REGISTRY: List[Dict] = [
    {
        "id": "large-v3",
        "name": "faster-whisper large-v3",
        "backend": "faster-whisper",
        "hf_repo": "Systran/faster-whisper-large-v3",
        "approx_size": "约 3.1 GB",
        "desc": "精度优先，中英混合推荐（默认）",
    },
    {
        "id": "large-v3-turbo",
        "name": "faster-whisper large-v3-turbo",
        "backend": "faster-whisper",
        "hf_repo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        "approx_size": "约 1.6 GB",
        "desc": "速度优先，精度略降",
    },
    {
        "id": "FireRedASR2",
        "name": "sherpa-onnx FireRedASR2",
        "backend": "sherpa-onnx",
        "hf_repo": "",  # 手动下载放置
        "approx_size": "约 1.1 GB",
        "desc": "中文为主少量英文场景（可选后端，手动放置模型文件）",
    },
    {
        "id": "nllb-200-distilled-600M-int8",
        "name": "NLLB-200 600M int8（字幕翻译）",
        "backend": "nllb",
        "hf_repo": "JustFrederik/nllb-200-distilled-600M-ct2-int8",
        "approx_size": "约 0.7 GB",
        "desc": "中英互译，纯本地推理；下载完成后可断网使用",
    },
]

# NLLB 模型：下载到普通目录（便于内网整目录拷贝），非 HF 缓存结构
NLLB_LOCAL_DIRNAME = "nllb-200-distilled-600M-int8"
NLLB_REQUIRED_FILES = ("model.bin", "sentencepiece.bpe.model")


def _hf_cache_dirname(repo: str) -> str:
    """HuggingFace 缓存目录命名规则：models--org--name"""
    return "models--" + repo.replace("/", "--")


def model_local_dir(model_id: str) -> str:
    """模型在本机的缓存目录（不存在则返回空串）"""
    for m in MODEL_REGISTRY:
        if m["id"] == model_id:
            if m["backend"] == "faster-whisper" and m["hf_repo"]:
                return os.path.join(config.model_cache_dir(), _hf_cache_dirname(m["hf_repo"]))
            if m["backend"] == "nllb":
                return translation_model_dir()
            # sherpa-onnx：用户自选的本地目录
            return config.get("model_local_path", "") or ""
    return ""


def translation_model_dir() -> str:
    """本地翻译模型目录（NLLB）"""
    return os.path.join(config.model_cache_dir(), NLLB_LOCAL_DIRNAME)


def is_translation_model_ready() -> bool:
    """翻译模型是否已可离线使用"""
    d = translation_model_dir()
    return all(os.path.isfile(os.path.join(d, f)) for f in NLLB_REQUIRED_FILES)


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def format_size(num_bytes: int) -> str:
    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        return "%.2f GB" % gb
    return "%.1f MB" % (num_bytes / (1024 ** 2))


def list_models() -> List[Dict]:
    """列出全部注册模型及其本地状态"""
    result = []
    for m in MODEL_REGISTRY:
        item = dict(m)
        local = model_local_dir(m["id"])
        if m["backend"] == "faster-whisper":
            # HF 缓存快照目录存在即视为已下载
            downloaded = bool(local) and os.path.isdir(os.path.join(local, "snapshots"))
        elif m["backend"] == "nllb":
            # 关键文件齐全才算可用（避免下到一半被误判）
            downloaded = is_translation_model_ready()
        else:
            downloaded = bool(local) and os.path.isdir(local)
        item["downloaded"] = downloaded
        item["local_path"] = local if downloaded else ""
        item["local_size"] = format_size(_dir_size(local)) if downloaded else "-"
        result.append(item)
    return result


def is_model_downloaded(model_id: str) -> bool:
    for m in list_models():
        if m["id"] == model_id:
            return m["downloaded"]
    return False


def download_model(model_id: str, status_cb=None):
    """下载指定模型（faster-whisper 系列走 HF；此操作需要联网，受离线锁定管控）。

    通过实例化 WhisperModel 触发官方下载逻辑，保证缓存结构标准、可离线复用。
    """
    from .asr.faster_whisper_backend import FasterWhisperBackend, MODEL_NAME_MAP

    for m in MODEL_REGISTRY:
        if m["id"] != model_id:
            continue
        if m["backend"] == "sherpa-onnx":
            raise RuntimeError("FireRedASR2 模型请手动下载后放入本地目录，并在模型面板中选择该目录。")
        if m["backend"] == "nllb":
            _download_nllb(m["hf_repo"], status_cb)
            return
        backend = FasterWhisperBackend(
            MODEL_NAME_MAP.get(model_id, model_id), "int8", "", config.model_cache_dir()
        )
        backend.load(status_cb)
        backend.unload()
        return
    raise RuntimeError("未知模型：%s" % model_id)


def _download_nllb(repo: str, status_cb=None):
    """下载 NLLB 翻译模型到普通目录（下载仅包含模型权重，不上传任何用户数据）

    默认源 huggingface.co 在部分网络环境不可达，依次尝试可用镜像。
    """
    from huggingface_hub import snapshot_download

    target = translation_model_dir()
    os.makedirs(target, exist_ok=True)
    patterns = ["model.bin", "config.json", "sentencepiece.bpe.model",
                "shared_vocabulary.txt", "special_tokens_map.json",
                "tokenizer.json", "tokenizer_config.json"]

    endpoints = []
    if os.environ.get("HF_ENDPOINT"):
        endpoints.append(os.environ["HF_ENDPOINT"])
    endpoints += ["https://huggingface.co", "https://hf-mirror.com"]

    last_err = None
    for ep in endpoints:
        try:
            if status_cb:
                status_cb("正在从 %s 下载翻译模型（约 0.7GB）…" % ep)
            snapshot_download(repo_id=repo, local_dir=target, endpoint=ep,
                              allow_patterns=patterns, max_workers=4)
            if is_translation_model_ready():
                if status_cb:
                    status_cb("翻译模型下载完成：%s" % target)
                return
            raise RuntimeError("下载后缺少关键文件")
        except Exception as e:
            last_err = e
            if status_cb:
                status_cb("该源不可用：%s，尝试下一个源…" % str(e).splitlines()[0][:100])
    raise RuntimeError(
        "翻译模型下载失败。可在内网机器上手动下载仓库 %s 的 model.bin / config.json / "
        "sentencepiece.bpe.model 等文件，放入目录：%s。原始错误：%s"
        % (repo, target, str(last_err).splitlines()[0][:150] if last_err else "未知")
    )


def delete_model(model_id: str) -> bool:
    """删除本地模型文件，释放磁盘"""
    local = model_local_dir(model_id)
    if local and os.path.isdir(local):
        shutil.rmtree(local, ignore_errors=True)
        return True
    return False

# -*- coding: utf-8 -*-
"""批量任务调度器。

- 默认串行；可选并行（每个 worker 独立加载一份模型，显存占用随并发数线性增长，
  GUI 中已做数量限制与提示，防止显存溢出）
- 单文件失败/损坏仅标记该任务，不中断队列
- 支持全局暂停/停止、单任务取消、失败重试
- 任务列表可保存/恢复（仅保存文件路径，不缓存任何音频内容）
"""
import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from .asr.base import ASRError, TaskCanceled, TranscribeParams, create_backend
from .config import AUDIO_EXTS, VIDEO_EXTS, config
from .ffmpeg_utils import FFmpegError, extract_audio, probe_media
from .logger import logger
from .srt_utils import write_outputs


class TaskStatus(str, Enum):
    PENDING = "待处理"
    RUNNING = "处理中"
    SUCCESS = "成功"
    FAILED = "失败"
    CANCELED = "已取消"


@dataclass
class TaskItem:
    path: str
    task_id: int = 0
    size: int = 0
    duration: float = 0.0
    status: TaskStatus = TaskStatus.PENDING
    error: str = ""
    progress: float = 0.0                 # 0~100，当前文件识别进度
    checked: bool = True                  # 列表勾选状态
    outputs: List[str] = field(default_factory=list)
    cancel_flag: threading.Event = field(default_factory=threading.Event)


def is_video(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in VIDEO_EXTS


def is_audio(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in AUDIO_EXTS


def is_supported(path: str) -> bool:
    return is_video(path) or is_audio(path)


def _check_dir_writable(d: str):
    """识别前预检：输出目录必须可写，否则立即失败并给出明确提示"""
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".wtest_%d.tmp" % os.getpid())
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(probe)
    except OSError as e:
        raise ASRError(
            "输出目录不可写：%s（%s）。可在【设置-输出设置】改用自定义输出目录。" % (d, e)
        )


class _Worker(QThread):
    """单个工作线程：从共享队列取任务执行完整流水线"""

    def __init__(self, controller: "TaskController"):
        super().__init__()
        self._ctl = controller
        self._backend = None

    def _ensure_backend(self):
        """每个 worker 懒加载一份独立模型实例"""
        if self._backend is None:
            backend = create_backend(
                config.get("backend"),
                config.get("model_name"),
                config.get("compute_type"),
                config.get("model_local_path"),
                config.model_cache_dir(),
            )
            backend.load(lambda msg: self._ctl.log_message.emit(msg))
            self._backend = backend

    def run(self):
        # 模型加载失败要让整个批次以明确错误结束，而不是每个任务各报一次
        try:
            self._ensure_backend()
        except ASRError as e:
            self._ctl.log_message.emit(str(e))
            self._ctl._backend_load_failed(str(e))
            return

        while True:
            if self._ctl._stop_flag.is_set():
                break
            try:
                task: TaskItem = self._ctl._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._process(task)
            except TaskCanceled:
                task.status = TaskStatus.CANCELED
                task.error = "已取消"
                self._ctl.log_message.emit("已取消：%s" % os.path.basename(task.path))
            except (FFmpegError, ASRError) as e:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                logger.error("任务失败 %s: %s", os.path.basename(task.path), task.error)
            except Exception as e:  # 兜底：任何异常都不允许崩溃整个队列
                task.status = TaskStatus.FAILED
                task.error = "未知错误：%s" % str(e).splitlines()[0][:150]
                logger.exception("任务异常 %s", os.path.basename(task.path))
            finally:
                self._ctl._queue.task_done()
                self._ctl.task_updated.emit(task.task_id)
                self._ctl._on_task_finished(task)

    def _wait_if_paused(self):
        while self._ctl._pause_flag.is_set() and not self._ctl._stop_flag.is_set():
            time.sleep(0.2)

    def _process(self, task: TaskItem):
        if task.cancel_flag.is_set():
            raise TaskCanceled()
        self._wait_if_paused()

        task.status = TaskStatus.RUNNING
        task.error = ""
        task.progress = 0.0
        self._ctl.task_updated.emit(task.task_id)
        self._ctl.log_message.emit("开始处理：%s" % os.path.basename(task.path))
        logger.info("开始处理 %s", task.path)

        media_path = task.path
        audio_path = media_path
        temp_audio = ""

        # 0. 预检输出目录可写性：先确定字幕输出目录并试写，
        #    避免跑完十几分钟 ASR 才发现写不了字幕
        if config.get("output_mode") == "custom" and config.get("output_dir"):
            srt_dir = config.get("output_dir")
        else:
            srt_dir = os.path.dirname(media_path)
        _check_dir_writable(srt_dir)

        # 1. 视频 → 抽取音频；音频文件直接识别
        if is_video(media_path):
            self._wait_if_paused()
            fmt = config.get("audio_format")
            denoise = config.get("denoise")
            keep = config.get("keep_audio")
            out_dir = os.path.dirname(media_path) if keep else config.temp_dir()
            audio_path = extract_audio(media_path, out_dir, fmt, denoise)
            if not keep:
                temp_audio = audio_path
            self._ctl.log_message.emit("音频提取完成：%s" % os.path.basename(audio_path))

        try:
            # 2. ASR 识别
            params = TranscribeParams(
                language=config.get("language"),
                vad_filter=config.get("vad_filter"),
                vad_threshold=config.get("vad_threshold"),
                beam_size=config.get("beam_size"),
                word_timestamps=config.get("word_timestamps") or config.get("export_json"),
            )

            def on_progress(done: float, total: float):
                if task.cancel_flag.is_set() or self._ctl._stop_flag.is_set():
                    raise TaskCanceled()
                self._wait_if_paused()
                if total > 0:
                    task.progress = min(99.0, done / total * 100.0)
                    self._ctl.task_progress.emit(task.task_id, task.progress)

            segments = self._backend.transcribe(audio_path, params, on_progress)

            # 3. 输出字幕产物
            self._wait_if_paused()
            try:
                task.outputs = write_outputs(
                    segments, media_path, srt_dir,
                    export_txt=config.get("export_txt"),
                    export_json=config.get("export_json"),
                    export_vtt=config.get("export_vtt"),
                    max_lines=int(config.get("subtitle_max_lines") or 0),
                    max_width=int(config.get("subtitle_max_width") or 40),
                    split_lang=bool(config.get("subtitle_split_lang")),
                )
            except OSError as e:
                raise ASRError(
                    "字幕文件写入失败：%s。请检查输出目录是否有写入权限，"
                    "或该 .srt 文件是否正被其他程序（如播放器/编辑器）占用。" % e
                )
            task.status = TaskStatus.SUCCESS
            task.progress = 100.0
            self._ctl.log_message.emit(
                "完成：%s → %s" % (os.path.basename(media_path), os.path.basename(task.outputs[0]))
            )
            logger.info("任务成功 %s", media_path)
        finally:
            # 4. 临时音频自动清理（保密推荐，默认开启）
            if temp_audio and config.get("auto_clean_temp"):
                try:
                    os.remove(temp_audio)
                except OSError:
                    pass


class TaskController(QObject):
    """任务调度中心（GUI 线程持有，worker 线程通过信号通信）"""

    task_updated = pyqtSignal(int)          # 任务状态变化
    task_progress = pyqtSignal(int, float)  # 任务进度
    log_message = pyqtSignal(str)           # 实时日志面板
    global_progress = pyqtSignal(int, int)  # 已完成数, 总数
    queue_finished = pyqtSignal()           # 队列全部结束
    backend_error = pyqtSignal(str)         # 模型加载失败

    def __init__(self):
        super().__init__()
        self.tasks: List[TaskItem] = []
        self._task_map: Dict[int, TaskItem] = {}
        self._next_id = 1
        self._queue: "queue.Queue[TaskItem]" = queue.Queue()
        self._workers: List[_Worker] = []
        self._stop_flag = threading.Event()
        self._pause_flag = threading.Event()
        self._running = False
        self._lock = threading.Lock()
        self._finished_count = 0
        self._total_count = 0
        self._backend_failed = False

    # ---- 任务列表管理 ----
    def add_files(self, paths: List[str]) -> int:
        """添加文件（自动过滤不支持的格式），返回新增数量"""
        added = 0
        existing = {t.path for t in self.tasks}
        for p in paths:
            p = os.path.normpath(p)
            if not os.path.isfile(p) or not is_supported(p) or p in existing:
                continue
            task = TaskItem(path=p, task_id=self._next_id)
            self._next_id += 1
            try:
                info = probe_media(p)
                task.size = info["size"]
                task.duration = info["duration"]
                if not info["has_audio"]:
                    task.status = TaskStatus.FAILED
                    task.error = "文件不含音频流"
            except FFmpegError as e:
                task.status = TaskStatus.FAILED
                task.error = str(e)
            self.tasks.append(task)
            self._task_map[task.task_id] = task
            added += 1
        return added

    def add_folder(self, folder: str) -> int:
        """添加文件夹：递归读取其下所有子文件夹中支持的音视频文件。

        仅扫描用户手动选择的这个目录树，不会触碰磁盘其他位置。
        """
        paths = []
        for root, _dirs, files in os.walk(folder):
            for name in files:
                p = os.path.join(root, name)
                if is_supported(p):
                    paths.append(p)
        return self.add_files(sorted(paths))

    def remove_tasks(self, task_ids: List[int]):
        for tid in task_ids:
            task = self._task_map.get(tid)
            if task is None:
                continue
            if task.status == TaskStatus.RUNNING:
                task.cancel_flag.set()  # 运行中任务先标记取消
            self.tasks.remove(task)
            del self._task_map[tid]

    def clear_tasks(self):
        if self._running:
            self.stop()
        self.tasks.clear()
        self._task_map.clear()

    def get_task(self, task_id: int) -> Optional[TaskItem]:
        return self._task_map.get(task_id)

    # ---- 调度控制 ----
    def start(self, task_ids: Optional[List[int]] = None):
        """开始处理。task_ids 为 None 时处理所有勾选的待处理/失败任务"""
        if self._running:
            return
        selected = task_ids if task_ids is not None else [
            t.task_id for t in self.tasks
            if t.checked and t.status in (TaskStatus.PENDING, TaskStatus.FAILED, TaskStatus.CANCELED)
        ]
        runnable = [self._task_map[tid] for tid in selected if tid in self._task_map]
        if not runnable:
            self.log_message.emit("没有可处理的任务")
            return

        self._stop_flag.clear()
        self._pause_flag.clear()
        self._backend_failed = False
        self._finished_count = 0
        self._total_count = len(runnable)

        for t in runnable:
            t.status = TaskStatus.PENDING
            t.error = ""
            t.progress = 0.0
            t.cancel_flag.clear()
            self._queue.put(t)
            self.task_updated.emit(t.task_id)

        concurrency = max(1, min(3, int(config.get("concurrency", 1))))
        self._running = True
        self._workers = []
        for _ in range(min(concurrency, len(runnable))):
            w = _Worker(self)
            w.finished.connect(self._on_worker_finished)
            self._workers.append(w)
            w.start()
        self.log_message.emit("队列启动：%d 个任务，并发 %d" % (len(runnable), concurrency))

    def pause(self):
        self._pause_flag.set()
        self.log_message.emit("已暂停（当前分段处理完成后挂起）")

    def resume(self):
        self._pause_flag.clear()
        self.log_message.emit("继续处理")

    @property
    def paused(self) -> bool:
        return self._pause_flag.is_set()

    @property
    def running(self) -> bool:
        return self._running

    def stop(self):
        self._stop_flag.set()
        self._pause_flag.clear()
        for t in self.tasks:
            if t.status == TaskStatus.RUNNING:
                t.cancel_flag.set()
        self.log_message.emit("正在停止…")

    def cancel_task(self, task_id: int):
        task = self._task_map.get(task_id)
        if task and task.status in (TaskStatus.RUNNING, TaskStatus.PENDING):
            task.cancel_flag.set()
            if task.status == TaskStatus.PENDING:
                # 还在队列里，直接标记（worker 取出时会跳过）
                task.status = TaskStatus.CANCELED
                self.task_updated.emit(task_id)

    def retry_task(self, task_id: int):
        task = self._task_map.get(task_id)
        if task and task.status in (TaskStatus.FAILED, TaskStatus.CANCELED):
            self.start([task_id])

    def retry_failed(self):
        ids = [t.task_id for t in self.tasks
               if t.status in (TaskStatus.FAILED, TaskStatus.CANCELED) and t.checked]
        if ids:
            self.start(ids)

    # ---- worker 回调 ----
    def _on_task_finished(self, task: TaskItem):
        with self._lock:
            self._finished_count += 1
        self.global_progress.emit(self._finished_count, self._total_count)

    def _on_worker_finished(self):
        self._workers = [w for w in self._workers if not w.isFinished()] + \
                        [w for w in self._workers if w.isFinished()]
        if all(not w.isRunning() for w in self._workers):
            self._running = False
            # 清理队列中因停止而未处理的任务状态
            while True:
                try:
                    t = self._queue.get_nowait()
                    if t.status == TaskStatus.PENDING:
                        t.status = TaskStatus.CANCELED if self._stop_flag.is_set() else t.status
                        self.task_updated.emit(t.task_id)
                    self._queue.task_done()
                except queue.Empty:
                    break
            self._workers = []
            self.queue_finished.emit()

    def _backend_load_failed(self, msg: str):
        self._backend_failed = True
        self._stop_flag.set()
        self.backend_error.emit(msg)
        # 队列中剩余任务全部标记失败
        while True:
            try:
                t = self._queue.get_nowait()
                t.status = TaskStatus.FAILED
                t.error = "模型加载失败"
                self.task_updated.emit(t.task_id)
                self._queue.task_done()
            except queue.Empty:
                break

    # ---- 任务列表持久化（仅保存文件路径） ----
    def save_task_list(self, path: Optional[str] = None):
        path = path or config.tasks_file()
        data = [{"path": t.path, "checked": t.checked} for t in self.tasks]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_task_list(self, path: Optional[str] = None) -> int:
        path = path or config.tasks_file()
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return 0
        paths = [d["path"] for d in data if os.path.isfile(d.get("path", ""))]
        added = self.add_files(paths)
        checked_map = {d["path"]: d.get("checked", True) for d in data}
        for t in self.tasks:
            if t.path in checked_map:
                t.checked = checked_map[t.path]
        return added

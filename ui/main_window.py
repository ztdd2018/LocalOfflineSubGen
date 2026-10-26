# -*- coding: utf-8 -*-
"""主窗口：五个页签【任务】【模型管理】【字幕翻译】【字幕预览】【设置】"""
import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QMainWindow, QMessageBox, QTabWidget

from core.config import config
from core.model_manager import is_model_downloaded
from core.offline_guard import guard
from core.task_runner import TaskController, TaskStatus
from .model_panel import ModelPanel
from .settings_panel import SettingsPanel
from .subtitle_editor import SubtitleEditor
from .task_panel import TaskPanel
from .translate_panel import TranslatePanel

TAB_TASK, TAB_MODEL, TAB_TRANSLATE, TAB_PREVIEW, TAB_SETTINGS = range(5)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LocalOfflineSubGen 本地离线批量音视频字幕生成器")
        self.resize(1280, 800)

        # 启动时按配置应用离线锁定（双保险：本软件自身也无任何上传逻辑）
        guard.apply(config.get("offline_lock"))

        self.ctl = TaskController()
        self.tabs = QTabWidget(self)
        self.setCentralWidget(self.tabs)

        self.task_panel = TaskPanel(self.ctl)
        self.model_panel = ModelPanel(self.task_panel.append_log)
        self.translate_panel = TranslatePanel(self.task_panel.append_log)
        self.subtitle_editor = SubtitleEditor(self.task_panel.append_log)
        self.settings_panel = SettingsPanel(self.task_panel.append_log)

        self.tabs.addTab(self.task_panel, "任务")
        self.tabs.addTab(self.model_panel, "模型管理")
        self.tabs.addTab(self.translate_panel, "字幕翻译")
        self.tabs.addTab(self.subtitle_editor, "字幕预览")
        self.tabs.addTab(self.settings_panel, "设置")

        self.task_panel.open_preview.connect(self._on_open_preview)
        self.translate_panel.model_required.connect(self._on_model_required)

    # ---- 双击任务 → 字幕预览 ----
    def _on_open_preview(self, task_id: int):
        task = self.ctl.get_task(task_id)
        if task is None:
            return
        # 找到该任务生成的 srt（优先任务记录，其次按命名规则推断）
        srt_path = ""
        for out in task.outputs:
            if out.endswith(".srt") and os.path.isfile(out):
                srt_path = out
                break
        if not srt_path:
            if config.get("output_mode") == "custom" and config.get("output_dir"):
                out_dir = config.get("output_dir")
            else:
                out_dir = os.path.dirname(task.path)
            candidate = os.path.join(out_dir, os.path.splitext(os.path.basename(task.path))[0] + ".srt")
            if os.path.isfile(candidate):
                srt_path = candidate
        if not srt_path:
            status = task.status.value
            QMessageBox.information(
                self, "未找到字幕",
                "该任务尚未生成 SRT 字幕（当前状态：%s）。识别成功后双击即可预览校对。" % status)
            return
        if self.subtitle_editor.load_subtitle(srt_path, task.path):
            self.tabs.setCurrentIndex(TAB_PREVIEW)

    # ---- 翻译页请求下载模型 → 跳转模型管理 ----
    def _on_model_required(self):
        self.tabs.setCurrentIndex(TAB_MODEL)
        self.model_panel.select_model("nllb-200-distilled-600M-int8")

    # ---- 首次启动向导 ----
    def showEvent(self, event):
        super().showEvent(event)
        if config.get("first_run_done"):
            return
        config.set("first_run_done", True)
        if is_model_downloaded(config.get("model_name")):
            return
        box = QMessageBox(self)
        box.setWindowTitle("首次启动向导")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "欢迎使用本地离线批量音视频字幕生成器。\n\n"
            "首次使用需要下载默认识别模型 faster-whisper large-v3（约 3.1GB），"
            "这是全流程唯一需要联网的操作。\n\n"
            "模型下载完成后即可完全断网使用，建议届时在【设置】中开启【离线锁定】。\n\n"
            "内网用户也可提前拷贝模型缓存目录到本机（见 README），选择【稍后手动处理】。"
        )
        btn_now = box.addButton("立即下载模型", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("稍后手动处理", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is btn_now:
            self.tabs.setCurrentIndex(TAB_MODEL)
            self.model_panel.table.selectRow(0)
            self.model_panel._on_download()

    # ---- 关闭时保存任务列表（仅路径） ----
    def closeEvent(self, event):
        translating = self.translate_panel._worker is not None and self.translate_panel._worker.isRunning()
        if self.ctl.running or translating:
            ret = QMessageBox.question(self, "确认退出", "仍有任务在处理中，确定停止并退出吗？")
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.ctl.stop()
            if translating:
                self.translate_panel._worker.stop()
        try:
            self.ctl.save_task_list()
        except OSError:
            pass
        event.accept()

# -*- coding: utf-8 -*-
"""【设置】页签：系统与安全设置（离线锁定 / 日志 / FFmpeg / 缓存）"""
import os
import shutil

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core.config import config
from core.ffmpeg_utils import find_tool
from core.logger import clear_logs, setup_logger
from core.offline_guard import guard


class SettingsPanel(QWidget):
    def __init__(self, log_cb, parent=None):
        super().__init__(parent)
        self._log = log_cb
        self._build_ui()
        self._reload()

    def _build_ui(self):
        root = QVBoxLayout(self)

        # ---- 安全 ----
        sec_box = QGroupBox("安全与保密")
        sec_form = QFormLayout(sec_box)
        self.chk_offline = QCheckBox("离线锁定（禁止一切网络请求，模型未下完也不联网）")
        self.chk_offline.setToolTip("开启后进程内所有网络连接被强制阻断；仅模型下载需要临时关闭")
        sec_form.addRow(self.chk_offline)
        sec_tip = QLabel("本软件无任何上传逻辑、无埋点、无遥测、无崩溃上报；所有文件仅在本机磁盘处理。")
        sec_tip.setWordWrap(True)
        sec_form.addRow(sec_tip)
        root.addWidget(sec_box)

        # ---- FFmpeg ----
        ff_box = QGroupBox("FFmpeg")
        ff_form = QFormLayout(ff_box)
        ff_row = QHBoxLayout()
        self.edit_ffmpeg = QLineEdit()
        self.edit_ffmpeg.setPlaceholderText("留空：优先使用程序内置 bin/ffmpeg，其次系统 PATH")
        self.btn_ffmpeg = QPushButton("浏览…")
        self.btn_ffmpeg_detect = QPushButton("检测")
        ff_row.addWidget(self.edit_ffmpeg, 1)
        ff_row.addWidget(self.btn_ffmpeg)
        ff_row.addWidget(self.btn_ffmpeg_detect)
        ff_form.addRow("FFmpeg 路径", ff_row)
        self.lbl_ffmpeg_status = QLabel("")
        ff_form.addRow(self.lbl_ffmpeg_status)
        root.addWidget(ff_box)

        # ---- 缓存 ----
        cache_box = QGroupBox("缓存与临时文件")
        cache_form = QFormLayout(cache_box)
        cache_row = QHBoxLayout()
        self.edit_cache = QLineEdit()
        self.edit_cache.setPlaceholderText("留空：使用程序目录下 cache/")
        self.btn_cache = QPushButton("浏览…")
        self.btn_cache_open = QPushButton("打开")
        cache_row.addWidget(self.edit_cache, 1)
        cache_row.addWidget(self.btn_cache)
        cache_row.addWidget(self.btn_cache_open)
        cache_form.addRow("缓存目录", cache_row)
        self.btn_clear_cache = QPushButton("一键清空全部缓存/临时音频（含模型）")
        cache_form.addRow(self.btn_clear_cache)
        root.addWidget(cache_box)

        # ---- 日志 ----
        log_box = QGroupBox("日志（仅记录文件名/状态/报错，不记录任何音频与文本内容）")
        log_form = QFormLayout(log_box)
        log_row = QHBoxLayout()
        self.edit_log = QLineEdit()
        self.edit_log.setPlaceholderText("留空：使用程序目录下 logs/")
        self.btn_log = QPushButton("浏览…")
        self.btn_log_open = QPushButton("打开")
        log_row.addWidget(self.edit_log, 1)
        log_row.addWidget(self.btn_log)
        log_row.addWidget(self.btn_log_open)
        log_form.addRow("日志目录", log_row)
        self.btn_clear_log = QPushButton("一键清空日志")
        log_form.addRow(self.btn_clear_log)
        root.addWidget(log_box)

        root.addStretch(1)

        # ---- 事件 ----
        self.chk_offline.toggled.connect(self._on_offline_toggled)
        self.btn_ffmpeg.clicked.connect(self._on_pick_ffmpeg)
        self.btn_ffmpeg_detect.clicked.connect(self._on_detect_ffmpeg)
        self.edit_ffmpeg.editingFinished.connect(
            lambda: config.set("ffmpeg_path", self.edit_ffmpeg.text().strip()))
        self.btn_cache.clicked.connect(self._on_pick_cache)
        self.btn_cache_open.clicked.connect(lambda: self._open_dir(config.cache_dir()))
        self.edit_cache.editingFinished.connect(self._on_cache_changed)
        self.btn_clear_cache.clicked.connect(self._on_clear_cache)
        self.btn_log.clicked.connect(self._on_pick_log)
        self.btn_log_open.clicked.connect(lambda: self._open_dir(config.log_dir()))
        self.edit_log.editingFinished.connect(self._on_log_changed)
        self.btn_clear_log.clicked.connect(self._on_clear_log)

    def _reload(self):
        self.chk_offline.blockSignals(True)
        self.chk_offline.setChecked(config.get("offline_lock"))
        self.chk_offline.blockSignals(False)
        self.edit_ffmpeg.setText(config.get("ffmpeg_path"))
        self.edit_cache.setText(config.get("cache_dir"))
        self.edit_log.setText(config.get("log_dir"))

    # ---- 安全 ----
    def _on_offline_toggled(self, checked: bool):
        config.set("offline_lock", checked)
        guard.apply(checked)
        self._log("离线锁定已" + ("开启（网络已封锁）" if checked else "关闭"))

    # ---- FFmpeg ----
    def _on_pick_ffmpeg(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 ffmpeg 可执行文件", "",
                                              "ffmpeg (ffmpeg.exe);;所有文件 (*)")
        if path:
            self.edit_ffmpeg.setText(path)
            config.set("ffmpeg_path", path)

    def _on_detect_ffmpeg(self):
        try:
            p = find_tool("ffmpeg")
            self.lbl_ffmpeg_status.setText("检测成功：%s" % p)
        except Exception as e:
            self.lbl_ffmpeg_status.setText(str(e))

    # ---- 缓存 ----
    def _on_pick_cache(self):
        folder = QFileDialog.getExistingDirectory(self, "选择缓存目录")
        if folder:
            self.edit_cache.setText(folder)
            self._on_cache_changed()

    def _on_cache_changed(self):
        config.set("cache_dir", self.edit_cache.text().strip())
        self._log("缓存目录已更新：" + config.cache_dir())

    def _on_clear_cache(self):
        ret = QMessageBox.question(
            self, "确认清空", "将删除缓存目录下全部内容（含已下载模型与临时音频），确定继续？")
        if ret != QMessageBox.StandardButton.Yes:
            return
        d = config.cache_dir()
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
            except OSError:
                pass
        self._log("缓存已清空：" + d)

    # ---- 日志 ----
    def _on_pick_log(self):
        folder = QFileDialog.getExistingDirectory(self, "选择日志目录")
        if folder:
            self.edit_log.setText(folder)
            self._on_log_changed()

    def _on_log_changed(self):
        config.set("log_dir", self.edit_log.text().strip())
        setup_logger()  # 按新目录重建文件日志
        self._log("日志目录已更新：" + config.log_dir())

    def _on_clear_log(self):
        clear_logs()
        self._log("日志已清空")

    @staticmethod
    def _open_dir(path: str):
        os.makedirs(path, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

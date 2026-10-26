# -*- coding: utf-8 -*-
"""【模型管理】页签：查看 / 下载 / 删除 本地 ASR 模型"""
import os

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.config import config
from core.model_manager import delete_model, download_model, list_models
from core.offline_guard import guard

COL_NAME, COL_BACKEND, COL_APPROX, COL_STATUS, COL_LOCAL_SIZE, COL_PATH = range(6)


class _DownloadThread(QThread):
    """模型下载线程（下载是唯一的联网操作，受离线锁定管控）"""
    message = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, model_id: str):
        super().__init__()
        self.model_id = model_id

    def run(self):
        try:
            download_model(self.model_id, lambda m: self.message.emit(m))
            self.done.emit(True, "下载完成")
        except Exception as e:
            self.done.emit(False, str(e).splitlines()[0][:200])


class ModelPanel(QWidget):
    def __init__(self, log_cb, parent=None):
        super().__init__(parent)
        self._log = log_cb
        self._download_thread = None
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        root = QVBoxLayout(self)

        tip = QLabel(
            "模型仅首次下载需要联网；下载完成后可开启【离线锁定】断网使用。"
            "内网机器可直接拷贝缓存目录中的 models--* 文件夹实现离线部署。"
        )
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["模型", "后端", "预估大小", "状态", "本地大小", "本地路径"])
        self.table.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COL_PATH, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

        btns = QHBoxLayout()
        self.btn_download = QPushButton("下载选中模型")
        self.btn_delete = QPushButton("删除本地模型")
        self.btn_refresh = QPushButton("刷新")
        self.btn_pick_local = QPushButton("选择本地模型文件夹…")
        self.btn_clear_local = QPushButton("恢复自动缓存")
        for b in (self.btn_download, self.btn_delete, self.btn_refresh,
                  self.btn_pick_local, self.btn_clear_local):
            btns.addWidget(b)
        btns.addStretch(1)
        root.addLayout(btns)

        self.status_label = QLabel("")
        root.addWidget(self.status_label)

        self.btn_download.clicked.connect(self._on_download)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_pick_local.clicked.connect(self._on_pick_local)
        self.btn_clear_local.clicked.connect(self._on_clear_local)

    # ---------------- 数据刷新 ----------------
    def refresh(self):
        models = list_models()
        self.table.setRowCount(len(models))
        for row, m in enumerate(models):
            if m["downloaded"]:
                status = "已下载"
            elif m["backend"] == "sherpa-onnx":
                status = "需手动安装"
            else:
                status = "未下载"
            values = [
                m["name"], m["backend"], m["approx_size"],
                status,
                m["local_size"], m["local_path"] or "-",
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col == COL_PATH:
                    item.setToolTip(text)
                self.table.setItem(row, col, item)
            self.table.item(row, 0).setData(0x0100, m["id"])  # UserRole 存模型 id

    def select_model(self, model_id: str):
        """按模型 id 选中对应行（供其他页跳转使用）"""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.data(0x0100) == model_id:
                self.table.selectRow(row)
                return

    def _selected_model_id(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.table.item(rows[0].row(), 0).data(0x0100)

    # ---------------- 操作 ----------------
    def _on_download(self):
        model_id = self._selected_model_id()
        if not model_id:
            QMessageBox.information(self, "提示", "请先选中一个模型")
            return
        # sherpa-onnx 系列不在 HuggingFace 上一键下载，需手动放置模型文件
        info = next((m for m in list_models() if m["id"] == model_id), None)
        if info and info["backend"] == "sherpa-onnx":
            ret = QMessageBox.information(
                self, "FireRedASR2 需手动安装",
                "FireRedASR2 为可选后端，不提供一键下载。安装步骤：\n\n"
                "1. pip install sherpa-onnx soundfile\n"
                "2. 下载 FireRedASR2 模型文件（encoder.onnx / decoder.onnx / tokens.txt），\n"
                "   可在 sherpa-onnx 官方发布页查找：\n"
                "   https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models\n"
                "3. 解压到本地文件夹后，点击下方【选择本地模型文件夹】指向该目录\n"
                "4. 在【任务】页将 ASR 后端切换为 sherpa-onnx\n\n"
                "是否现在选择本地模型文件夹？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret == QMessageBox.StandardButton.Yes:
                self._on_pick_local()
            return
        if guard.enabled:
            QMessageBox.warning(self, "离线锁定", "离线锁定已开启，禁止联网下载。请先在【设置】中关闭离线锁定。")
            return
        if self._download_thread and self._download_thread.isRunning():
            QMessageBox.information(self, "提示", "已有下载任务进行中")
            return
        self.btn_download.setEnabled(False)
        self.status_label.setText("正在下载，请保持网络畅通…（下载完成后即可断网使用）")
        self._log("开始下载模型：%s" % model_id)
        self._download_thread = _DownloadThread(model_id)
        self._download_thread.message.connect(lambda m: self._log(m))
        self._download_thread.done.connect(self._on_download_done)
        self._download_thread.start()

    def _on_download_done(self, ok: bool, msg: str):
        self.btn_download.setEnabled(True)
        self.status_label.setText(msg)
        self._log(("模型下载成功：" if ok else "模型下载失败：") + msg)
        self.refresh()

    def _on_delete(self):
        model_id = self._selected_model_id()
        if not model_id:
            QMessageBox.information(self, "提示", "请先选中一个模型")
            return
        ret = QMessageBox.question(self, "确认删除", "确定删除该模型的本地文件吗？删除后需重新下载才能使用。")
        if ret != QMessageBox.StandardButton.Yes:
            return
        if delete_model(model_id):
            self._log("已删除本地模型：%s" % model_id)
        self.refresh()

    def _on_pick_local(self):
        folder = QFileDialog.getExistingDirectory(self, "选择本地模型文件夹（内网拷贝的模型目录）")
        if folder:
            config.set("model_local_path", folder)
            self._log("已设置本地模型路径：%s" % folder)
            self.refresh()

    def _on_clear_local(self):
        config.set("model_local_path", "")
        self._log("已恢复为自动缓存目录：%s" % config.model_cache_dir())
        self.refresh()

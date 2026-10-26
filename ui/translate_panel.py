# -*- coding: utf-8 -*-
"""【字幕翻译】页签：批量选择字幕文件（含子文件夹），用本地 NLLB 模型翻译。

安全约定：
- 只读取用户手动选择/拖入的 .srt，不主动扫描其他目录；
- 翻译全程本地推理，不联网、不上传任何字幕文本；
- 输出文件名固定追加 _translated 后缀，绝不覆盖原字幕。
"""
import os

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.config import config
from core.model_manager import is_translation_model_ready, translation_model_dir
from core.translate.nllb_backend import DIRECTIONS, NLLBTranslator
from core.translate.srt_translate import OUTPUT_MODES, scan_srt_files, translate_srt_file

COL_NAME, COL_DIR, COL_STATUS, COL_ERROR = range(4)


class _TranslateWorker(QThread):
    """翻译线程：模型只加载一次，逐个文件串行翻译"""

    item_status = pyqtSignal(int, str, str)   # 行号, 状态, 错误
    progress = pyqtSignal(int, int)           # 已完成, 总数
    log = pyqtSignal(str)
    finished_all = pyqtSignal(int, int)       # 成功数, 失败数

    def __init__(self, jobs, options):
        super().__init__()
        self.jobs = jobs
        self.options = options
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        ok = fail = 0
        translator = NLLBTranslator(translation_model_dir())
        try:
            translator.load(lambda m: self.log.emit(m))
        except Exception as e:
            msg = str(e).splitlines()[0][:200]
            self.log.emit("翻译模型加载失败：" + msg)
            for idx, _job in self.jobs:
                self.item_status.emit(idx, "失败", msg)
            self.finished_all.emit(0, len(self.jobs))
            return

        total = len(self.jobs)
        for n, (idx, job) in enumerate(self.jobs, 1):
            if self._stop:
                self.log.emit("已停止翻译")
                break
            self.item_status.emit(idx, "翻译中", "")
            name = os.path.basename(job["path"])
            try:
                out, src, tgt, count = translate_srt_file(
                    job["path"], translator,
                    root_dir=job["root"],
                    out_dir=self.options["out_dir"],
                    direction=self.options["direction"],
                    output_mode=self.options["output_mode"],
                )
                ok += 1
                self.item_status.emit(idx, "成功", "")
                self.log.emit("%s：%d 条字幕，%s → %s，输出 %s"
                              % (name, count, src, tgt, out))
            except Exception as e:
                fail += 1
                msg = str(e).splitlines()[0][:200]
                self.item_status.emit(idx, "失败", msg)
                self.log.emit("%s 翻译失败：%s" % (name, msg))
            self.progress.emit(n, total)

        translator.unload()
        self.finished_all.emit(ok, fail)


class TranslatePanel(QWidget):
    """字幕翻译页签"""

    model_required = pyqtSignal()   # 请求跳转到模型管理页

    def __init__(self, log_cb, parent=None):
        super().__init__(parent)
        self._log = log_cb
        self._items = []            # [{"path": ..., "root": ...}]
        self._worker = None
        self.setAcceptDrops(True)
        self._build_ui()
        self._connect_signals()
        self.reload_settings()
        self.refresh_model_state()

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self.btn_add_files = QPushButton("添加字幕文件")
        self.btn_add_folder = QPushButton("添加文件夹（含子文件夹）")
        self.btn_remove = QPushButton("删除选中")
        self.btn_clear = QPushButton("清空列表")
        self.btn_start = QPushButton("开始翻译")
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        for b in (self.btn_add_files, self.btn_add_folder, self.btn_remove, self.btn_clear):
            toolbar.addWidget(b)
        toolbar.addStretch(1)
        toolbar.addWidget(self.btn_start)
        toolbar.addWidget(self.btn_stop)
        root.addLayout(toolbar)

        tip = QLabel("本地离线翻译：仅读取你选中的 .srt 文件，推理在本机完成，"
                     "不上传任何字幕文本。输出文件追加 _translated 后缀，不覆盖原字幕。")
        tip.setWordWrap(True)
        root.addWidget(tip)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["文件名", "所在目录", "状态", "错误信息"])
        self.table.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COL_DIR, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COL_ERROR, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        splitter.addWidget(self.table)

        splitter.addWidget(self._build_settings_box())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("进度"))
        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("%v / %m")
        bottom.addWidget(self.progress_bar, 1)
        root.addLayout(bottom)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        self.log_view.setMaximumHeight(140)
        root.addWidget(self.log_view)

    def _build_settings_box(self) -> QGroupBox:
        box = QGroupBox("翻译设置")
        form = QFormLayout(box)

        self.cmb_direction = QComboBox()
        for key, label in DIRECTIONS.items():
            self.cmb_direction.addItem(label, key)
        form.addRow("翻译方向", self.cmb_direction)

        self.cmb_output = QComboBox()
        for key, label in OUTPUT_MODES.items():
            self.cmb_output.addItem(label, key)
        form.addRow("输出方式", self.cmb_output)

        self.cmb_out_mode = QComboBox()
        self.cmb_out_mode.addItem("同原文件目录", "same_dir")
        self.cmb_out_mode.addItem("统一输出文件夹", "custom")
        form.addRow("输出目录", self.cmb_out_mode)

        out_row = QHBoxLayout()
        self.edit_out_dir = QLineEdit()
        self.edit_out_dir.setPlaceholderText("选择统一输出文件夹…")
        self.btn_out_dir = QPushButton("浏览")
        out_row.addWidget(self.edit_out_dir, 1)
        out_row.addWidget(self.btn_out_dir)
        form.addRow("输出文件夹", out_row)

        self.lbl_model = QLabel("")
        self.lbl_model.setWordWrap(True)
        form.addRow("翻译模型", self.lbl_model)

        self.btn_model = QPushButton("去下载翻译模型")
        form.addRow("", self.btn_model)
        return box

    def _connect_signals(self):
        self.btn_add_files.clicked.connect(self._on_add_files)
        self.btn_add_folder.clicked.connect(self._on_add_folder)
        self.btn_remove.clicked.connect(self._on_remove_selected)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_out_dir.clicked.connect(self._on_pick_out_dir)
        self.btn_model.clicked.connect(self.model_required.emit)

        self.cmb_direction.currentIndexChanged.connect(self._save_settings)
        self.cmb_output.currentIndexChanged.connect(self._save_settings)
        self.cmb_out_mode.currentIndexChanged.connect(self._save_settings)
        self.edit_out_dir.editingFinished.connect(self._save_settings)

    # ---------------- 设置读写 ----------------
    def reload_settings(self):
        widgets = [self.cmb_direction, self.cmb_output, self.cmb_out_mode, self.edit_out_dir]
        for w in widgets:
            w.blockSignals(True)
        self.cmb_direction.setCurrentIndex(self.cmb_direction.findData(config.get("translate_direction")))
        self.cmb_output.setCurrentIndex(self.cmb_output.findData(config.get("translate_output")))
        self.cmb_out_mode.setCurrentIndex(self.cmb_out_mode.findData(config.get("translate_output_mode")))
        self.edit_out_dir.setText(config.get("translate_out_dir"))
        for w in widgets:
            w.blockSignals(False)

    def _save_settings(self):
        config.set("translate_direction", self.cmb_direction.currentData())
        config.set("translate_output", self.cmb_output.currentData())
        config.set("translate_output_mode", self.cmb_out_mode.currentData())
        config.set("translate_out_dir", self.edit_out_dir.text().strip())

    # ---------------- 文件导入 ----------------
    def _on_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择字幕文件", "", "字幕文件 (*.srt)")
        if paths:
            self._add_paths([(p, "") for p in paths])

    def _on_add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹（含全部子文件夹）")
        if not folder:
            return
        found = scan_srt_files(folder)
        self._add_paths([(p, folder) for p in found])
        if not found:
            self.append_log("该文件夹及其子文件夹中没有 .srt 字幕文件")

    def _add_paths(self, pairs):
        existing = {it["path"] for it in self._items}
        added = 0
        for path, root in pairs:
            if path in existing:
                continue
            existing.add(path)
            self._items.append({"path": path, "root": root})
            added += 1
        self.refresh_table()
        self.append_log("新增 %d 个字幕文件" % added)

    def _on_remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self._items):
                self._items.pop(r)
        self.refresh_table()

    def _on_clear(self):
        self._items.clear()
        self.refresh_table()
        self.progress_bar.setMaximum(1)
        self.progress_bar.setValue(0)

    def _on_pick_out_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "选择统一输出文件夹")
        if folder:
            self.edit_out_dir.setText(folder)
            self._save_settings()

    # ---------------- 表格 ----------------
    def refresh_table(self):
        self.table.setRowCount(len(self._items))
        for row, it in enumerate(self._items):
            self.table.setItem(row, COL_NAME, QTableWidgetItem(os.path.basename(it["path"])))
            self.table.setItem(row, COL_DIR, QTableWidgetItem(os.path.dirname(it["path"])))
            self.table.setItem(row, COL_STATUS, QTableWidgetItem("待翻译"))
            self.table.setItem(row, COL_ERROR, QTableWidgetItem(""))
            self.table.item(row, COL_NAME).setToolTip(it["path"])

    def _set_status(self, row: int, status: str, error: str):
        if row < 0 or row >= self.table.rowCount():
            return
        self.table.item(row, COL_STATUS).setText(status)
        self.table.item(row, COL_ERROR).setText(error)

    # ---------------- 翻译控制 ----------------
    def refresh_model_state(self):
        if is_translation_model_ready():
            self.lbl_model.setText("已就绪（本地）\n%s" % translation_model_dir())
            self.btn_model.setVisible(False)
        else:
            self.lbl_model.setText("未下载。翻译需要本地模型（约 0.7GB），只需联网下载一次。")
            self.btn_model.setVisible(True)

    def _on_start(self):
        if not self._items:
            QMessageBox.information(self, "提示", "请先添加字幕文件或文件夹")
            return
        if not is_translation_model_ready():
            ret = QMessageBox.question(
                self, "缺少翻译模型",
                "本地翻译模型尚未下载（约 0.7GB，仅需联网下载一次，之后可断网使用）。\n\n"
                "是否前往【模型管理】下载？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ret == QMessageBox.StandardButton.Yes:
                self.model_required.emit()
            return
        if self._worker and self._worker.isRunning():
            QMessageBox.information(self, "提示", "已有翻译任务进行中")
            return

        self._save_settings()
        out_dir = ""
        if self.cmb_out_mode.currentData() == "custom":
            out_dir = self.edit_out_dir.text().strip()
            if not out_dir:
                QMessageBox.information(self, "提示", "请先选择统一输出文件夹")
                return
            os.makedirs(out_dir, exist_ok=True)

        jobs = list(enumerate([dict(it) for it in self._items]))
        options = {
            "direction": self.cmb_direction.currentData(),
            "output_mode": self.cmb_output.currentData(),
            "out_dir": out_dir,
        }
        self.progress_bar.setMaximum(max(1, len(jobs)))
        self.progress_bar.setValue(0)
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)

        self._worker = _TranslateWorker(jobs, options)
        self._worker.item_status.connect(self._set_status)
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self.append_log)
        self._worker.finished_all.connect(self._on_finished)
        self._worker.start()

    def _on_stop(self):
        if self._worker:
            self._worker.stop()
            self.btn_stop.setEnabled(False)

    def _on_progress(self, done: int, total: int):
        self.progress_bar.setMaximum(max(1, total))
        self.progress_bar.setValue(done)

    def _on_finished(self, ok: int, fail: int):
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.append_log("翻译结束：成功 %d，失败 %d" % (ok, fail))

    # ---------------- 日志 / 拖拽 ----------------
    def append_log(self, msg: str):
        self.log_view.appendPlainText(msg)
        self._log(msg)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        files, roots = [], []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isfile(p) and p.lower().endswith(".srt"):
                files.append((p, ""))
            elif os.path.isdir(p):
                roots.append(p)
        for folder in roots:
            files.extend((p, folder) for p in scan_srt_files(folder))
        if files:
            self._add_paths(files)

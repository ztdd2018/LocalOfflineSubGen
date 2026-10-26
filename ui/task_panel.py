# -*- coding: utf-8 -*-
"""【任务】页签：左侧任务列表 + 右侧识别参数设置，下方进度条 + 实时日志"""
import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QSplitter, QDoubleSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget, QLineEdit,
)

from core.config import AUDIO_EXTS, VIDEO_EXTS, config
from core.task_runner import TaskController, TaskStatus


def _fmt_size(num: int) -> str:
    if num >= 1024 ** 3:
        return "%.2f GB" % (num / 1024 ** 3)
    return "%.1f MB" % (num / 1024 ** 2)


def _fmt_duration(sec: float) -> str:
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


COL_CHECK, COL_NAME, COL_SIZE, COL_DUR, COL_PROGRESS, COL_STATUS, COL_ERROR = range(7)


class TaskPanel(QWidget):
    """任务页签"""

    open_preview = pyqtSignal(int)  # 双击任务请求打开字幕预览

    def __init__(self, controller: TaskController, parent=None):
        super().__init__(parent)
        self.ctl = controller
        self.setAcceptDrops(True)
        self._build_ui()
        self._connect_signals()
        self.reload_settings()

    # ---------------- UI 构建 ----------------
    def _build_ui(self):
        root = QVBoxLayout(self)

        # 工具栏
        toolbar = QHBoxLayout()
        self.btn_add_files = QPushButton("添加文件")
        self.btn_add_folder = QPushButton("添加文件夹")
        self.btn_remove = QPushButton("删除选中")
        self.btn_clear = QPushButton("清空列表")
        self.btn_start = QPushButton("开始")
        self.btn_pause = QPushButton("暂停")
        self.btn_stop = QPushButton("停止")
        self.btn_retry_failed = QPushButton("重试失败项")
        self.btn_save_list = QPushButton("保存列表")
        self.btn_load_list = QPushButton("载入列表")
        for b in (self.btn_add_files, self.btn_add_folder, self.btn_remove, self.btn_clear):
            toolbar.addWidget(b)
        toolbar.addStretch(1)
        for b in (self.btn_start, self.btn_pause, self.btn_stop, self.btn_retry_failed,
                  self.btn_save_list, self.btn_load_list):
            toolbar.addWidget(b)
        root.addLayout(toolbar)

        # 中部：左任务表 / 右设置
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["", "文件名", "大小", "时长", "进度", "状态", "错误信息"])
        self.table.horizontalHeader().setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COL_ERROR, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setColumnWidth(COL_CHECK, 30)
        self.table.verticalHeader().setVisible(False)
        splitter.addWidget(self.table)

        splitter.addWidget(self._build_settings_box())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        # 下方：进度 + 日志
        bottom = QVBoxLayout()
        prog_row = QHBoxLayout()
        prog_row.addWidget(QLabel("全局进度"))
        self.global_bar = QProgressBar()
        self.global_bar.setFormat("%v / %m")
        prog_row.addWidget(self.global_bar, 1)
        prog_row.addWidget(QLabel("当前文件"))
        self.current_bar = QProgressBar()
        self.current_bar.setRange(0, 100)
        prog_row.addWidget(self.current_bar, 1)
        bottom.addLayout(prog_row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        bottom.addWidget(self.log_view)
        root.addLayout(bottom)

    def _build_settings_box(self) -> QGroupBox:
        box = QGroupBox("识别与输出设置")
        form = QFormLayout(box)

        self.cmb_backend = QComboBox()
        self.cmb_backend.addItem("faster-whisper（默认）", "faster-whisper")
        self.cmb_backend.addItem("sherpa-onnx FireRedASR2（备选）", "sherpa-onnx")
        form.addRow("ASR 后端", self.cmb_backend)

        self.cmb_model = QComboBox()
        self.cmb_model.addItem("large-v3（精度优先）", "large-v3")
        self.cmb_model.addItem("large-v3-turbo（速度优先）", "large-v3-turbo")
        form.addRow("识别模型", self.cmb_model)

        self.cmb_language = QComboBox()
        self.cmb_language.addItem("auto（中英混合自动）", "auto")
        self.cmb_language.addItem("中文 zh", "zh")
        self.cmb_language.addItem("英文 en", "en")
        form.addRow("语种模式", self.cmb_language)

        self.chk_vad = QCheckBox("开启（减少幻觉空字幕）")
        form.addRow("VAD 静音过滤", self.chk_vad)

        self.spin_vad = QDoubleSpinBox()
        self.spin_vad.setRange(0.0, 1.0)
        self.spin_vad.setSingleStep(0.05)
        self.spin_vad.setDecimals(2)
        form.addRow("VAD 阈值", self.spin_vad)

        self.spin_beam = QSpinBox()
        self.spin_beam.setRange(1, 10)
        form.addRow("Beam size", self.spin_beam)

        self.cmb_compute = QComboBox()
        self.cmb_compute.addItem("auto（自动推荐）", "auto")
        self.cmb_compute.addItem("float16（N 卡）", "float16")
        self.cmb_compute.addItem("int8（CPU/省显存）", "int8")
        form.addRow("计算精度", self.cmb_compute)

        self.spin_concurrency = QSpinBox()
        self.spin_concurrency.setRange(1, 3)
        self.spin_concurrency.setToolTip("并行任务数（>1 时每路独立加载模型，显存占用成倍增加）")
        form.addRow("并发数", self.spin_concurrency)

        self.cmb_audio_fmt = QComboBox()
        self.cmb_audio_fmt.addItem("wav（ASR 最优）", "wav")
        self.cmb_audio_fmt.addItem("m4a（节省空间）", "m4a")
        form.addRow("抽取音频格式", self.cmb_audio_fmt)

        self.chk_denoise = QCheckBox("ffmpeg afftdn 本地降噪")
        form.addRow("降噪", self.chk_denoise)

        self.chk_auto_clean = QCheckBox("完成后自动删除临时音频（保密推荐）")
        form.addRow("临时音频清理", self.chk_auto_clean)

        self.chk_keep_audio = QCheckBox("保留音频文件（复核用，存至源文件目录）")
        form.addRow("保留音频", self.chk_keep_audio)

        self.cmb_output_mode = QComboBox()
        self.cmb_output_mode.addItem("同原文件目录", "same_dir")
        self.cmb_output_mode.addItem("统一输出文件夹", "custom")
        form.addRow("输出目录", self.cmb_output_mode)

        out_row = QHBoxLayout()
        self.edit_output_dir = QLineEdit()
        self.edit_output_dir.setPlaceholderText("选择统一输出文件夹…")
        self.btn_output_dir = QPushButton("浏览")
        out_row.addWidget(self.edit_output_dir, 1)
        out_row.addWidget(self.btn_output_dir)
        form.addRow("输出文件夹", out_row)

        self.chk_txt = QCheckBox("纯文本 .txt")
        self.chk_json = QCheckBox("词级时间戳 .json")
        self.chk_vtt = QCheckBox("WebVTT .vtt")
        form.addRow("附加输出", self.chk_txt)
        form.addRow("", self.chk_json)
        form.addRow("", self.chk_vtt)
        return box

    # ---------------- 信号绑定 ----------------
    def _connect_signals(self):
        self.btn_add_files.clicked.connect(self._on_add_files)
        self.btn_add_folder.clicked.connect(self._on_add_folder)
        self.btn_remove.clicked.connect(self._on_remove_selected)
        self.btn_clear.clicked.connect(self._on_clear)
        self.btn_start.clicked.connect(self._on_start)
        self.btn_pause.clicked.connect(self._on_pause_resume)
        self.btn_stop.clicked.connect(self.ctl.stop)
        self.btn_retry_failed.clicked.connect(self.ctl.retry_failed)
        self.btn_save_list.clicked.connect(self._on_save_list)
        self.btn_load_list.clicked.connect(self._on_load_list)
        self.btn_output_dir.clicked.connect(self._on_pick_output_dir)

        self.ctl.task_updated.connect(self._on_task_updated)
        self.ctl.task_progress.connect(self._on_task_progress)
        self.ctl.log_message.connect(self.append_log)
        self.ctl.global_progress.connect(self._on_global_progress)
        self.ctl.queue_finished.connect(self._on_queue_finished)
        self.ctl.backend_error.connect(self._on_backend_error)

        self.table.itemChanged.connect(self._on_item_changed)
        self.table.cellDoubleClicked.connect(self._on_double_click)

        # 设置变更即时保存
        self.cmb_backend.currentIndexChanged.connect(self._save_settings)
        self.cmb_model.currentIndexChanged.connect(self._save_settings)
        self.cmb_language.currentIndexChanged.connect(self._save_settings)
        self.chk_vad.toggled.connect(self._save_settings)
        self.spin_vad.valueChanged.connect(self._save_settings)
        self.spin_beam.valueChanged.connect(self._save_settings)
        self.cmb_compute.currentIndexChanged.connect(self._save_settings)
        self.spin_concurrency.valueChanged.connect(self._save_settings)
        self.cmb_audio_fmt.currentIndexChanged.connect(self._save_settings)
        self.chk_denoise.toggled.connect(self._save_settings)
        self.chk_auto_clean.toggled.connect(self._save_settings)
        self.chk_keep_audio.toggled.connect(self._save_settings)
        self.cmb_output_mode.currentIndexChanged.connect(self._save_settings)
        self.edit_output_dir.editingFinished.connect(self._save_settings)
        self.chk_txt.toggled.connect(self._save_settings)
        self.chk_json.toggled.connect(self._save_settings)
        self.chk_vtt.toggled.connect(self._save_settings)

    # ---------------- 设置读写 ----------------
    def reload_settings(self):
        """从 config 回填界面"""
        widgets_block = [
            self.cmb_backend, self.cmb_model, self.cmb_language, self.chk_vad,
            self.spin_vad, self.spin_beam, self.cmb_compute, self.spin_concurrency,
            self.cmb_audio_fmt, self.chk_denoise, self.chk_auto_clean, self.chk_keep_audio,
            self.cmb_output_mode, self.edit_output_dir, self.chk_txt, self.chk_json, self.chk_vtt,
        ]
        for w in widgets_block:
            w.blockSignals(True)
        self.cmb_backend.setCurrentIndex(self.cmb_backend.findData(config.get("backend")))
        self.cmb_model.setCurrentIndex(self.cmb_model.findData(config.get("model_name")))
        self.cmb_language.setCurrentIndex(self.cmb_language.findData(config.get("language")))
        self.chk_vad.setChecked(config.get("vad_filter"))
        self.spin_vad.setValue(config.get("vad_threshold"))
        self.spin_beam.setValue(config.get("beam_size"))
        self.cmb_compute.setCurrentIndex(self.cmb_compute.findData(config.get("compute_type")))
        self.spin_concurrency.setValue(config.get("concurrency"))
        self.cmb_audio_fmt.setCurrentIndex(self.cmb_audio_fmt.findData(config.get("audio_format")))
        self.chk_denoise.setChecked(config.get("denoise"))
        self.chk_auto_clean.setChecked(config.get("auto_clean_temp"))
        self.chk_keep_audio.setChecked(config.get("keep_audio"))
        self.cmb_output_mode.setCurrentIndex(self.cmb_output_mode.findData(config.get("output_mode")))
        self.edit_output_dir.setText(config.get("output_dir"))
        self.chk_txt.setChecked(config.get("export_txt"))
        self.chk_json.setChecked(config.get("export_json"))
        self.chk_vtt.setChecked(config.get("export_vtt"))
        for w in widgets_block:
            w.blockSignals(False)

    def _save_settings(self):
        config.set("backend", self.cmb_backend.currentData())
        config.set("model_name", self.cmb_model.currentData())
        config.set("language", self.cmb_language.currentData())
        config.set("vad_filter", self.chk_vad.isChecked())
        config.set("vad_threshold", self.spin_vad.value())
        config.set("beam_size", self.spin_beam.value())
        config.set("compute_type", self.cmb_compute.currentData())
        config.set("concurrency", self.spin_concurrency.value())
        config.set("audio_format", self.cmb_audio_fmt.currentData())
        config.set("denoise", self.chk_denoise.isChecked())
        config.set("auto_clean_temp", self.chk_auto_clean.isChecked())
        config.set("keep_audio", self.chk_keep_audio.isChecked())
        config.set("output_mode", self.cmb_output_mode.currentData())
        config.set("output_dir", self.edit_output_dir.text().strip())
        config.set("export_txt", self.chk_txt.isChecked())
        config.set("export_json", self.chk_json.isChecked())
        config.set("export_vtt", self.chk_vtt.isChecked())

    # ---------------- 任务导入 ----------------
    def _on_add_files(self):
        exts = " ".join("*" + e for e in sorted(VIDEO_EXTS | AUDIO_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(self, "选择音视频文件", "", "音视频文件 (%s)" % exts)
        if paths:
            self._add_and_refresh(paths)

    def _on_add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择文件夹（含全部子文件夹）")
        if folder:
            added = self.ctl.add_folder(folder)
            self.refresh_table()
            self.append_log("从文件夹导入 %d 个文件" % added)

    def _add_and_refresh(self, paths):
        added = self.ctl.add_files(paths)
        self.refresh_table()
        if added == 0:
            self.append_log("没有新增任务（可能格式不支持或已在列表中）")
        else:
            self.append_log("新增 %d 个任务" % added)

    def _on_remove_selected(self):
        ids = self._selected_task_ids()
        if ids:
            self.ctl.remove_tasks(ids)
            self.refresh_table()

    def _on_clear(self):
        self.ctl.clear_tasks()
        self.refresh_table()
        self.global_bar.setValue(0)
        self.global_bar.setMaximum(1)

    # ---------------- 调度控制 ----------------
    def _on_start(self):
        self._save_settings()
        self.ctl.start()

    def _on_pause_resume(self):
        if self.ctl.paused:
            self.ctl.resume()
            self.btn_pause.setText("暂停")
        else:
            self.ctl.pause()
            self.btn_pause.setText("继续")

    def _on_save_list(self):
        self.ctl.save_task_list()
        self.append_log("任务列表已保存（仅保存文件路径）")

    def _on_load_list(self):
        added = self.ctl.load_task_list()
        self.refresh_table()
        self.append_log("载入任务列表，恢复 %d 个任务" % added)

    def _on_pick_output_dir(self):
        folder = QFileDialog.getExistingDirectory(self, "选择统一输出文件夹")
        if folder:
            self.edit_output_dir.setText(folder)
            self._save_settings()

    # ---------------- 列表刷新 ----------------
    def refresh_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.ctl.tasks))
        for row, task in enumerate(self.ctl.tasks):
            self._fill_row(row, task)
        self.table.blockSignals(False)

    def _fill_row(self, row: int, task):
        check = QTableWidgetItem()
        check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        check.setCheckState(Qt.CheckState.Checked if task.checked else Qt.CheckState.Unchecked)
        check.setData(Qt.ItemDataRole.UserRole, task.task_id)
        self.table.setItem(row, COL_CHECK, check)

        values = {
            COL_NAME: os.path.basename(task.path),
            COL_SIZE: _fmt_size(task.size),
            COL_DUR: _fmt_duration(task.duration),
            COL_PROGRESS: "%d%%" % int(task.progress),
            COL_STATUS: task.status.value,
            COL_ERROR: task.error,
        }
        for col, text in values.items():
            item = QTableWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, task.task_id)
            if col == COL_NAME:
                item.setToolTip(task.path)
            self.table.setItem(row, col, item)

    def _find_row(self, task_id: int) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_CHECK)
            if item and item.data(Qt.ItemDataRole.UserRole) == task_id:
                return row
        return -1

    def _selected_task_ids(self):
        ids = set()
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), COL_CHECK)
            if item:
                ids.add(item.data(Qt.ItemDataRole.UserRole))
        return list(ids)

    # ---------------- 控制器信号 ----------------
    def _on_task_updated(self, task_id: int):
        row = self._find_row(task_id)
        task = self.ctl.get_task(task_id)
        if row < 0 or task is None:
            return
        self.table.blockSignals(True)
        for col in (COL_PROGRESS, COL_STATUS, COL_ERROR):
            item = self.table.item(row, col)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row, col, item)
        self.table.item(row, COL_PROGRESS).setText("%d%%" % int(task.progress))
        self.table.item(row, COL_STATUS).setText(task.status.value)
        self.table.item(row, COL_ERROR).setText(task.error)
        self.table.blockSignals(False)

    def _on_task_progress(self, task_id: int, percent: float):
        row = self._find_row(task_id)
        if row >= 0 and self.table.item(row, COL_PROGRESS):
            self.table.item(row, COL_PROGRESS).setText("%d%%" % int(percent))
        self.current_bar.setValue(int(percent))

    def _on_global_progress(self, done: int, total: int):
        self.global_bar.setMaximum(max(1, total))
        self.global_bar.setValue(done)

    def _on_queue_finished(self):
        self.append_log("队列处理结束")
        self.btn_pause.setText("暂停")
        self.current_bar.setValue(0)

    def _on_backend_error(self, msg: str):
        self.append_log("【模型错误】" + msg)

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() != COL_CHECK:
            return
        task_id = item.data(Qt.ItemDataRole.UserRole)
        task = self.ctl.get_task(task_id)
        if task:
            task.checked = item.checkState() == Qt.CheckState.Checked

    def _on_double_click(self, row: int, _col: int):
        item = self.table.item(row, COL_CHECK)
        if item:
            self.open_preview.emit(item.data(Qt.ItemDataRole.UserRole))

    # ---------------- 日志 ----------------
    def append_log(self, msg: str):
        self.log_view.appendPlainText(msg)

    # ---------------- 拖拽导入 ----------------
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if os.path.isfile(p):
                paths.append(p)
            elif os.path.isdir(p):
                self.ctl.add_folder(p)
        if paths:
            self._add_and_refresh(paths)
        else:
            self.refresh_table()

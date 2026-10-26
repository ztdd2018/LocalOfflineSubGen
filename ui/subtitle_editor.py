# -*- coding: utf-8 -*-
"""【字幕预览】页签：内置简易校对（文本/时间轴编辑 + 音频联动播放 + 覆盖保存）"""
import os

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.asr.base import Segment
from core.srt_utils import fmt_srt_time, parse_srt, parse_time_text, write_srt

COL_IDX, COL_START, COL_END, COL_TEXT = range(4)


class SubtitleEditor(QWidget):
    def __init__(self, log_cb, parent=None):
        super().__init__(parent)
        self._log = log_cb
        self._srt_path = ""
        self._build_ui()
        self._build_player()

    # ---------------- UI ----------------
    def _build_ui(self):
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.lbl_file = QLabel("未加载字幕（在【任务】页签双击已完成的任务可直接打开）")
        top.addWidget(self.lbl_file, 1)
        self.btn_open = QPushButton("打开 SRT…")
        self.btn_save = QPushButton("保存（覆盖原 SRT）")
        self.btn_add_row = QPushButton("新增行")
        self.btn_del_row = QPushButton("删除行")
        for b in (self.btn_open, self.btn_save, self.btn_add_row, self.btn_del_row):
            top.addWidget(b)
        root.addLayout(top)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["#", "开始时间", "结束时间", "字幕文本"])
        self.table.horizontalHeader().setSectionResizeMode(COL_TEXT, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

        # 播放控制条
        play_bar = QHBoxLayout()
        self.btn_play = QPushButton("播放")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.lbl_time = QLabel("00:00:00 / 00:00:00")
        play_bar.addWidget(self.btn_play)
        play_bar.addWidget(self.slider, 1)
        play_bar.addWidget(self.lbl_time)
        root.addLayout(play_bar)

        tip = QLabel("时间格式支持 00:00:00,000 / 00:00:00.000 / 秒数；点击某行可跳转到该行开始时间播放。")
        root.addWidget(tip)

        self.btn_open.clicked.connect(self._on_open)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_add_row.clicked.connect(self._on_add_row)
        self.btn_del_row.clicked.connect(self._on_del_row)
        self.table.cellClicked.connect(self._on_row_clicked)

    def _build_player(self):
        self._player = QMediaPlayer(self)
        self._audio_out = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_out)
        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self.btn_play.clicked.connect(self._on_play_toggle)
        self.slider.sliderMoved.connect(self._on_seek)

    # ---------------- 加载 ----------------
    def load_subtitle(self, srt_path: str, media_path: str = "") -> bool:
        """加载 SRT 并可选项加载对应音视频用于播放"""
        if not os.path.isfile(srt_path):
            return False
        try:
            segments = parse_srt(srt_path)
        except Exception as e:
            QMessageBox.warning(self, "读取失败", "SRT 解析失败：%s" % e)
            return False
        self._srt_path = srt_path
        self.lbl_file.setText(srt_path)
        self._fill_table(segments)
        if media_path and os.path.isfile(media_path):
            self._player.setSource(QUrl.fromLocalFile(media_path))
        self._log("字幕预览已加载：%s" % os.path.basename(srt_path))
        return True

    def _fill_table(self, segments):
        self.table.setRowCount(len(segments))
        for row, seg in enumerate(segments):
            self._set_row(row, seg)

    def _set_row(self, row: int, seg: Segment):
        values = [str(row + 1), fmt_srt_time(seg.start), fmt_srt_time(seg.end), seg.text]
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            if col == COL_IDX:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, col, item)

    # ---------------- 编辑 ----------------
    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(self, "打开 SRT 字幕", "", "SRT 字幕 (*.srt)")
        if path:
            media, _ = QFileDialog.getOpenFileName(self, "（可选）选择对应音视频用于播放", "",
                                                   "音视频 (*.*);;所有文件 (*)")
            self.load_subtitle(path, media)

    def _on_save(self):
        if not self._srt_path:
            QMessageBox.information(self, "提示", "尚未加载字幕文件")
            return
        segments = []
        for row in range(self.table.rowCount()):
            start = parse_time_text(self._cell_text(row, COL_START))
            end = parse_time_text(self._cell_text(row, COL_END))
            text = self._cell_text(row, COL_TEXT)
            if end <= start:
                end = start + 1.0  # 兜底：非法时间轴给 1 秒时长，避免产生坏文件
            segments.append(Segment(start=start, end=end, text=text))
        try:
            write_srt(segments, self._srt_path)
            self._log("字幕已保存（UTF-8 覆盖）：%s" % os.path.basename(self._srt_path))
            QMessageBox.information(self, "已保存", "已覆盖保存：%s" % self._srt_path)
        except OSError as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def _cell_text(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text().strip() if item else ""

    def _on_add_row(self):
        row = self.table.currentRow()
        insert_at = row + 1 if row >= 0 else self.table.rowCount()
        # 默认插在选中行之后：开始=上一行结束，时长 2 秒
        start = 0.0
        if row >= 0:
            start = parse_time_text(self._cell_text(row, COL_END))
        self.table.insertRow(insert_at)
        self._set_row(insert_at, Segment(start=start, end=start + 2.0, text=""))
        self._renumber()

    def _on_del_row(self):
        rows = sorted({idx.row() for idx in self.table.selectionModel().selectedRows()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self._renumber()

    def _renumber(self):
        for row in range(self.table.rowCount()):
            self.table.setItem(row, COL_IDX, QTableWidgetItem(str(row + 1)))

    # ---------------- 播放联动 ----------------
    def _on_row_clicked(self, row: int, _col: int):
        if self._player.source().isEmpty():
            return
        start = parse_time_text(self._cell_text(row, COL_START))
        self._player.setPosition(int(start * 1000))
        self._player.play()
        self.btn_play.setText("暂停")

    def _on_play_toggle(self):
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self.btn_play.setText("播放")
        else:
            self._player.play()
            self.btn_play.setText("暂停")

    def _on_position(self, ms: int):
        if not self.slider.isSliderDown():
            self.slider.setValue(ms)
        self._update_time_label()

    def _on_duration(self, ms: int):
        self.slider.setRange(0, ms)
        self._update_time_label()

    def _on_seek(self, ms: int):
        self._player.setPosition(ms)

    def _update_time_label(self):
        def fmt(ms):
            s = ms // 1000
            return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)
        self.lbl_time.setText("%s / %s" % (fmt(self._player.position()), fmt(self._player.duration())))

# -*- coding: utf-8 -*-
"""LocalOfflineSubGen 程序入口。

本软件承诺：除首次下载 ASR 模型权重外，全程不进行任何网络通信；
无埋点、无遥测、无崩溃上报、无日志回传。所有音视频/音频/文本仅在本机磁盘处理。
"""
import sys

from PyQt6.QtWidgets import QApplication

from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("LocalOfflineSubGen")
    app.setOrganizationName("LocalOffline")  # 仅本地标识，不用于任何联网行为
    win = MainWindow()
    # 多显示器时系统默认位置可能落在副屏（甚至副屏已断开），强制先摆到主屏中央
    area = app.primaryScreen().availableGeometry()
    win.move(area.center() - win.rect().center())
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

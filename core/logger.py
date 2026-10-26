# -*- coding: utf-8 -*-
"""本地日志：仅记录文件名/状态/报错，禁止记录任何音频或识别文本内容"""
import logging
import os
import time

from .config import config


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("LocalOfflineSubGen")
    logger.setLevel(logging.INFO)
    # 重复调用时先清掉旧 handler（例如日志目录变更后重建）
    for h in list(logger.handlers):
        h.close()
        logger.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    # 文件日志（本地，按天一个文件）
    log_file = os.path.join(config.log_dir(), "app_%s.log" % time.strftime("%Y%m%d"))
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # 控制台日志（源码调试时用）
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


logger = setup_logger()


def clear_logs():
    """一键清空日志"""
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler):
            h.close()
            logger.removeHandler(h)
    d = config.log_dir()
    for name in os.listdir(d):
        if name.endswith(".log"):
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass
    setup_logger()

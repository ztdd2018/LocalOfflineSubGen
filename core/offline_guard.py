# -*- coding: utf-8 -*-
"""离线锁定开关：开启后在进程内封堵一切网络连接。

实现方式：patch socket 层的 connect / create_connection / getaddrinfo，
并设置 HuggingFace 离线环境变量。本软件自身不包含任何上传逻辑，
此模块是"双保险"，防止模型下载之外的任何意外联网行为。
"""
import os
import socket


class OfflineLockedError(ConnectionError):
    def __init__(self, *args, **kwargs):
        super().__init__("离线锁定已开启：程序已禁止一切网络请求。如需下载模型，请先在【设置】中关闭离线锁定。")


class OfflineGuard:
    def __init__(self):
        self._enabled = False
        self._orig_socket_connect = None
        self._orig_create_connection = None
        self._orig_getaddrinfo = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        if self._enabled:
            return
        self._enabled = True

        # HuggingFace / Transformers 离线模式（模型加载只读本地缓存）
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

        self._orig_socket_connect = socket.socket.connect
        self._orig_create_connection = socket.create_connection
        self._orig_getaddrinfo = socket.getaddrinfo

        def _blocked_connect(*args, **kwargs):
            raise OfflineLockedError()

        def _blocked_create_connection(*args, **kwargs):
            raise OfflineLockedError()

        def _blocked_getaddrinfo(*args, **kwargs):
            raise OfflineLockedError()

        socket.socket.connect = _blocked_connect
        socket.create_connection = _blocked_create_connection
        socket.getaddrinfo = _blocked_getaddrinfo

    def disable(self):
        if not self._enabled:
            return
        self._enabled = False
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
        if self._orig_socket_connect is not None:
            socket.socket.connect = self._orig_socket_connect
        if self._orig_create_connection is not None:
            socket.create_connection = self._orig_create_connection
        if self._orig_getaddrinfo is not None:
            socket.getaddrinfo = self._orig_getaddrinfo

    def apply(self, enabled: bool):
        if enabled:
            self.enable()
        else:
            self.disable()


guard = OfflineGuard()

# -*- coding: utf-8 -*-
"""通用工具:磁盘空间、流式复制、哈希与时间。"""

import hashlib
import os
import shutil
from datetime import datetime

import config
from errors import ApiError


def ensure_free_space(path, incoming=0):
    root = path if os.path.isdir(path) else os.path.dirname(path) or config.BASE_DIR
    os.makedirs(root, exist_ok=True)
    if shutil.disk_usage(root).free - incoming < config.MIN_FREE_BYTES:
        raise ApiError("磁盘剩余空间不足，请清理空间后重试", 507, "INSUFFICIENT_DISK_SPACE")


def stream_copy_limited(source, output, expected=None, disk_path=None, hasher=None):
    written = 0
    remaining = expected
    while remaining is None or remaining > 0:
        chunk = source.read(65536 if remaining is None else min(65536, remaining))
        if not chunk:
            if remaining:
                raise ApiError("上传内容中断", 400, "UPLOAD_INTERRUPTED")
            break
        if disk_path:
            ensure_free_space(disk_path, len(chunk))
        output.write(chunk)
        if hasher:
            hasher.update(chunk)
        written += len(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    return written


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def file_in_use_error(error):
    if isinstance(error, PermissionError):
        return ApiError("文件正在被其他程序使用，请关闭相关程序后重试", 409, "ATTACHMENT_FILE_IN_USE")
    return error

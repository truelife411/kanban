# -*- coding: utf-8 -*-
"""配置常量:路径、端口、版本号与各类限制。"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "kanban.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
ATTACHMENTS_DIR = os.path.join(BASE_DIR, "attachments")
HOST = "127.0.0.1"
PORT = int(os.environ.get("KANBAN_PORT", "8000"))
SCHEMA_VERSION = 6
EXPORT_VERSION = 2
MAX_JSON_BODY = 1024 * 1024
MAX_IMPORT_BODY = 10 * 1024 * 1024
MAX_ATTACHMENT_BODY = int(os.environ.get("KANBAN_MAX_ATTACHMENT_BODY", str(100 * 1024 * 1024)))
MAX_ZIP_BODY = int(os.environ.get("KANBAN_MAX_ZIP_BODY", str(1024 * 1024 * 1024)))
VALID_PRIORITY = ("high", "medium", "low")
MIN_FREE_BYTES = int(os.environ.get("KANBAN_MIN_FREE_BYTES", str(512 * 1024 * 1024)))
MAX_ZIP_ENTRIES = int(os.environ.get("KANBAN_MAX_ZIP_ENTRIES", "100000"))
MAX_ZIP_RATIO = int(os.environ.get("KANBAN_MAX_ZIP_RATIO", "200"))
MAX_ZIP_EXPANDED_BYTES = int(os.environ.get("KANBAN_MAX_ZIP_EXPANDED_BYTES", "0"))

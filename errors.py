# -*- coding: utf-8 -*-
"""错误类型与请求字段校验。"""

from datetime import datetime

from config import VALID_PRIORITY


class ApiError(Exception):
    def __init__(self, message, status=400, code="BAD_REQUEST", details=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details


def fail(message, status=422, code="VALIDATION_ERROR", field=None):
    raise ApiError(message, status, code, {"field": field} if field else None)


def require_object(value):
    if not isinstance(value, dict):
        fail("JSON 请求体必须是对象", 400, "INVALID_JSON")
    return value


def require_int(value, field, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail("%s 必须是大于等于 %s 的整数" % (field, minimum), field=field)
    return value


def require_string(value, field, minimum=0, maximum=1000, trim=True):
    if not isinstance(value, str):
        fail("%s 必须是字符串" % field, field=field)
    value = value.strip() if trim else value
    if len(value) < minimum:
        fail("%s 不能为空" % field, field=field)
    if len(value) > maximum:
        fail("%s 不能超过 %s 个字符" % (field, maximum), field=field)
    return value


def validate_priority(value):
    if value not in VALID_PRIORITY:
        fail("优先级必须是 high、medium 或 low", field="priority")
    return value


def validate_due_date(value):
    value = require_string(value, "due_date", 0, 16)
    if not value:
        return ""
    if len(value) not in (10, 16):
        fail("截止日期格式必须是 YYYY-MM-DD 或 YYYY-MM-DD HH:MM", field="due_date")
    try:
        datetime.strptime(value, "%Y-%m-%d %H:%M" if len(value) == 16 else "%Y-%m-%d")
    except ValueError:
        fail("截止日期无效", field="due_date")
    return value


def validate_planned_date(value):
    value = require_string(value, "planned_date", 0, 10)
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        fail("计划日期格式必须是 YYYY-MM-DD", field="planned_date")
    return value

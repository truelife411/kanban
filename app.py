# -*- coding: utf-8 -*-
"""
看板系统后端 —— 纯 Python 标准库实现
依赖: 仅 Python 3 自带模块 (http.server, sqlite3, json, urllib 等)
运行: python app.py  ->  浏览器打开 http://localhost:8000
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "kanban.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")
HOST = "0.0.0.0"
PORT = 8000

VALID_PRIORITY = ("high", "medium", "low")


# ---------------------------------------------------------------------------
# 数据库初始化与连接
# ---------------------------------------------------------------------------
def get_conn():
    """每次请求新建连接。SQLite 对短连接开销很小，且避免线程安全问题。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row          # 返回类似字典的行
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """首次运行自动建表 + 插入默认三列。"""
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS columns (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT    NOT NULL,
            position  INTEGER NOT NULL DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS cards (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            column_id   INTEGER NOT NULL,
            title       TEXT    NOT NULL,
            description TEXT    DEFAULT '',
            labels      TEXT    DEFAULT '',
            due_date    TEXT    DEFAULT '',
            priority    TEXT    NOT NULL DEFAULT 'medium',
            position    INTEGER NOT NULL DEFAULT 0,
            archived    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL,
            updated_at  TEXT    NOT NULL,
            FOREIGN KEY (column_id) REFERENCES columns(id) ON DELETE CASCADE
        )
        """
    )

    # 仅当 columns 表为空时插入默认列
    cur.execute("SELECT COUNT(*) AS c FROM columns")
    if cur.fetchone()["c"] == 0:
        defaults = [("待办", 0), ("进行中", 1), ("已完成", 2)]
        cur.executemany(
            "INSERT INTO columns (name, position) VALUES (?, ?)", defaults
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def row_to_card(row):
    """把数据库行转换成前端需要的 dict。"""
    return {
        "id": row["id"],
        "column_id": row["column_id"],
        "title": row["title"],
        "description": row["description"] or "",
        "labels": row["labels"] or "",
        "due_date": row["due_date"] or "",
        "priority": row["priority"],
        "position": row["position"],
        "archived": row["archived"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def row_to_column(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "position": row["position"],
    }


# ---------------------------------------------------------------------------
# HTTP 响应辅助
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# 业务逻辑：columns
# ---------------------------------------------------------------------------
def list_columns(conn):
    rows = conn.execute(
        "SELECT * FROM columns ORDER BY position ASC, id ASC"
    ).fetchall()
    return [row_to_column(r) for r in rows]


def create_column(conn, data):
    name = (data.get("name") or "").strip()
    if not name:
        raise ApiError("列名不能为空")
    # 新列 position 取当前最大值 +1
    row = conn.execute("SELECT MAX(position) AS m FROM columns").fetchone()
    pos = (row["m"] or -1) + 1
    cur = conn.execute(
        "INSERT INTO columns (name, position) VALUES (?, ?)", (name, pos)
    )
    conn.commit()
    return row_to_column(
        conn.execute("SELECT * FROM columns WHERE id = ?", (cur.lastrowid,)).fetchone()
    )


def update_column(conn, cid, data):
    row = conn.execute("SELECT * FROM columns WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise ApiError("列不存在", 404)
    name = (data.get("name") or row["name"]).strip() or row["name"]
    position = data.get("position")
    if position is None:
        position = row["position"]
    conn.execute(
        "UPDATE columns SET name = ?, position = ? WHERE id = ?",
        (name, position, cid),
    )
    conn.commit()
    return row_to_column(
        conn.execute("SELECT * FROM columns WHERE id = ?", (cid,)).fetchone()
    )


def delete_column(conn, cid):
    """删除列前，先把该列下活跃卡片归档（历史保留），再删列。"""
    row = conn.execute("SELECT * FROM columns WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise ApiError("列不存在", 404)
    conn.execute("UPDATE cards SET archived = 1 WHERE column_id = ? AND archived = 0", (cid,))
    conn.execute("DELETE FROM columns WHERE id = ?", (cid,))
    conn.commit()
    return {"ok": True}


def reorder_columns(conn, ids):
    """按给定的 id 顺序批量更新列的 position。

    ids: [id1, id2, ...] 期望的顺序。
    """
    if not isinstance(ids, list) or not ids:
        raise ApiError("需要传入列 id 列表")
    # 校验 id 都是整数且存在
    valid_ids = set()
    for r in conn.execute("SELECT id FROM columns").fetchall():
        valid_ids.add(r["id"])
    for i in ids:
        if not isinstance(i, int):
            raise ApiError("列 id 必须是整数")
        if i not in valid_ids:
            raise ApiError("列 id %s 不存在" % i)
    for idx, cid in enumerate(ids):
        conn.execute(
            "UPDATE columns SET position = ? WHERE id = ?", (idx, cid)
        )
    conn.commit()
    return list_columns(conn)


# ---------------------------------------------------------------------------
# 业务逻辑：cards
# ---------------------------------------------------------------------------
def list_cards(conn, column_id=None, archived=0):
    sql = "SELECT * FROM cards WHERE archived = ?"
    params = [archived]
    if column_id is not None:
        sql += " AND column_id = ?"
        params.append(column_id)
    sql += " ORDER BY position ASC, id ASC"
    rows = conn.execute(sql, params).fetchall()
    return [row_to_card(r) for r in rows]


def get_card(conn, cid):
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise ApiError("卡片不存在", 404)
    return row_to_card(row)


def create_card(conn, data):
    column_id = data.get("column_id")
    if column_id is None:
        raise ApiError("缺少 column_id")
    title = (data.get("title") or "").strip()
    if not title:
        raise ApiError("标题不能为空")
    # 校验列存在
    if conn.execute("SELECT 1 FROM columns WHERE id = ?", (column_id,)).fetchone() is None:
        raise ApiError("所属列不存在", 404)

    labels = (data.get("labels") or "").strip()
    due_date = (data.get("due_date") or "").strip()
    priority = (data.get("priority") or "medium").strip()
    if priority not in VALID_PRIORITY:
        priority = "medium"

    # 新卡片 position 取该列内最大值 +1
    row = conn.execute(
        "SELECT MAX(position) AS m FROM cards WHERE column_id = ? AND archived = 0",
        (column_id,),
    ).fetchone()
    pos = (row["m"] or -1) + 1

    ts = now_iso()
    cur = conn.execute(
        """
        INSERT INTO cards
            (column_id, title, description, labels, due_date, priority,
             position, archived, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            column_id,
            title,
            (data.get("description") or "").strip(),
            labels,
            due_date,
            priority,
            pos,
            ts,
            ts,
        ),
    )
    conn.commit()
    return get_card(conn, cur.lastrowid)


def update_card(conn, cid, data):
    old = conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
    if old is None:
        raise ApiError("卡片不存在", 404)

    title = (data.get("title") or old["title"]).strip() or old["title"]
    description = data.get("description", old["description"] or "")
    labels = data.get("labels", old["labels"] or "")
    due_date = data.get("due_date", old["due_date"] or "")
    priority = data.get("priority", old["priority"])
    if priority not in VALID_PRIORITY:
        priority = old["priority"]

    conn.execute(
        """
        UPDATE cards
        SET title = ?, description = ?, labels = ?, due_date = ?,
            priority = ?, updated_at = ?
        WHERE id = ?
        """,
        (title, description, labels, due_date, priority, now_iso(), cid),
    )
    conn.commit()
    return get_card(conn, cid)


def move_card(conn, cid, data):
    """移动卡片到目标列的目标位置（跨列/同列重排都走这里）。

    请求体: { "column_id": int, "position": int }
    position 是希望插入到的索引（0 表示最前）。实现采用"占位+重排"策略。
    """
    old = conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
    if old is None:
        raise ApiError("卡片不存在", 404)
    new_col = data.get("column_id")
    new_pos = data.get("position")
    if new_col is None or new_pos is None:
        raise ApiError("需要 column_id 和 position")

    if conn.execute("SELECT 1 FROM columns WHERE id = ?", (new_col,)).fetchone() is None:
        raise ApiError("目标列不存在", 404)

    old_col = old["column_id"]

    # 1) 先把被拖卡片"摘出"，放到一个极大临时位置，避免影响重排
    conn.execute(
        "UPDATE cards SET position = 9999999 WHERE id = ?", (cid,)
    )

    # 2) 把目标列从指定位置开始的卡片整体后移一位
    conn.execute(
        """
        UPDATE cards SET position = position + 1
        WHERE column_id = ? AND archived = 0 AND position >= ? AND id <> ?
        """,
        (new_col, new_pos, cid),
    )

    # 3) 放置被拖卡片到目标列的目标位置
    conn.execute(
        "UPDATE cards SET column_id = ?, position = ?, updated_at = ? WHERE id = ?",
        (new_col, new_pos, now_iso(), cid),
    )

    conn.commit()

    # 4) 整理两列的 position，使其连续（0,1,2,...），避免长期使用后出现空洞
    _reorder_positions(conn, old_col)
    if old_col != new_col:
        _reorder_positions(conn, new_col)
    conn.commit()
    return get_card(conn, cid)


def _reorder_positions(conn, column_id):
    rows = conn.execute(
        "SELECT id FROM cards WHERE column_id = ? AND archived = 0 ORDER BY position ASC, id ASC",
        (column_id,),
    ).fetchall()
    for idx, r in enumerate(rows):
        conn.execute("UPDATE cards SET position = ? WHERE id = ?", (idx, r["id"]))


def archive_card(conn, cid):
    """归档卡片（软删除），保留历史。"""
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise ApiError("卡片不存在", 404)
    conn.execute(
        "UPDATE cards SET archived = 1, updated_at = ? WHERE id = ?",
        (now_iso(), cid),
    )
    conn.commit()
    _reorder_positions(conn, row["column_id"])
    conn.commit()
    return {"ok": True}


def restore_card(conn, cid):
    """从归档恢复卡片到原列。"""
    row = conn.execute("SELECT * FROM cards WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise ApiError("卡片不存在", 404)
    # 若原列已被删除，恢复到"第一列"
    target_col = row["column_id"]
    if conn.execute("SELECT 1 FROM columns WHERE id = ?", (target_col,)).fetchone() is None:
        first = conn.execute(
            "SELECT id FROM columns ORDER BY position ASC, id ASC LIMIT 1"
        ).fetchone()
        if first is None:
            raise ApiError("没有可用列，请先新建列")
        target_col = first["id"]

    # 放到目标列末尾
    maxrow = conn.execute(
        "SELECT MAX(position) AS m FROM cards WHERE column_id = ? AND archived = 0",
        (target_col,),
    ).fetchone()
    pos = (maxrow["m"] or -1) + 1
    conn.execute(
        "UPDATE cards SET archived = 0, column_id = ?, position = ?, updated_at = ? WHERE id = ?",
        (target_col, pos, now_iso(), cid),
    )
    conn.commit()
    return get_card(conn, cid)


def search_cards(conn, q=None, date_from=None, date_to=None, priority=None, include_active=False):
    """搜索（默认只搜归档历史，可 include_active 也搜活跃卡片）。

    搜索范围：标题、描述、标签全文 LIKE 匹配 + 可选日期/优先级过滤。
    """
    sql = "SELECT * FROM cards WHERE 1=1"
    params = []

    if include_active:
        # 全部
        pass
    else:
        sql += " AND archived = 1"

    if q:
        sql += " AND (title LIKE ? OR description LIKE ? OR labels LIKE ?)"
        kw = f"%{q}%"
        params.extend([kw, kw, kw])

    if date_from:
        # 按截止日期过滤；due_date 可能是 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM"，
        # 取前 10 位日期部分比较，保证时间格式兼容
        sql += " AND substr(due_date,1,10) >= ?"
        params.append(date_from)

    if date_to:
        sql += " AND substr(due_date,1,10) <= ?"
        params.append(date_to)

    if priority and priority in VALID_PRIORITY:
        sql += " AND priority = ?"
        params.append(priority)

    sql += " ORDER BY updated_at DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    return [row_to_card(r) for r in rows]


def export_data(conn):
    """导出全部数据为 JSON 结构。"""
    columns = list_columns(conn)
    cards_active = list_cards(conn, archived=0)
    cards_archived = list_cards(conn, archived=1)
    return {
        "exported_at": now_iso(),
        "columns": columns,
        "cards": cards_active + cards_archived,
    }


# ---------------------------------------------------------------------------
# 请求分发
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "KanbanServer/1.0"

    # ----- 日志：精简 -----
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ----- 通用工具 -----
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError("请求体不是有效的 JSON")

    def _serve_static(self, rel_path):
        """安全地提供静态文件。rel_path 已去掉前导 / 。"""
        # 防目录穿越
        rel_path = rel_path.replace("\\", "/")
        if rel_path.startswith("/"):
            rel_path = rel_path[1:]

        if rel_path == "" or rel_path == "index.html":
            file_path = os.path.join(STATIC_DIR, "index.html")
        else:
            # 只允许 static 目录下
            safe = os.path.normpath(os.path.join(STATIC_DIR, rel_path))
            if not safe.startswith(os.path.abspath(STATIC_DIR)):
                self._send_text("Forbidden", 403)
                return
            file_path = safe

        if not os.path.isfile(file_path):
            self._send_text("Not Found", 404)
            return

        ext = os.path.splitext(file_path)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")

        with open(file_path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ----- 路由匹配 -----
    def _route(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # 静态文件与首页
        if method == "GET" and (path in ("/", "/index.html") or path.startswith("/static/")):
            rel = path[len("/static/"):] if path.startswith("/static/") else path
            self._serve_static(rel)
            return True

        # API 路由
        if path.startswith("/api/"):
            try:
                self._route_api(method, path, query)
            except ApiError as e:
                self._send_json({"error": e.message}, e.status)
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": "服务器内部错误: %s" % e}, 500)
            return True

        self._send_text("Not Found", 404)
        return True

    # ----- API 分发 -----
    def _route_api(self, method, path, query):
        conn = get_conn()
        try:
            # columns 集合
            if path == "/api/columns" and method == "GET":
                self._send_json(list_columns(conn))
                return
            if path == "/api/columns" and method == "POST":
                data = self._read_json_body()
                self._send_json(create_column(conn, data), 201)
                return

            # columns 批量重排
            if path == "/api/columns/reorder" and method == "POST":
                data = self._read_json_body()
                ids = data.get("ids")
                self._send_json(reorder_columns(conn, ids))
                return

            # columns 单条
            m = re.fullmatch(r"/api/columns/(\d+)", path)
            if m:
                cid = int(m.group(1))
                if method == "PUT":
                    data = self._read_json_body()
                    self._send_json(update_column(conn, cid, data))
                    return
                if method == "DELETE":
                    self._send_json(delete_column(conn, cid))
                    return

            # cards 集合
            if path == "/api/cards" and method == "GET":
                col = query.get("column_id", [None])[0]
                col = int(col) if col is not None else None
                self._send_json(list_cards(conn, column_id=col, archived=0))
                return
            if path == "/api/cards" and method == "POST":
                data = self._read_json_body()
                self._send_json(create_card(conn, data), 201)
                return

            # cards 单条
            m = re.fullmatch(r"/api/cards/(\d+)", path)
            if m:
                cid = int(m.group(1))
                if method == "GET":
                    self._send_json(get_card(conn, cid))
                    return
                if method == "PUT":
                    data = self._read_json_body()
                    self._send_json(update_card(conn, cid, data))
                    return
                if method == "DELETE":
                    self._send_json(archive_card(conn, cid))
                    return

            # cards 移动
            m = re.fullmatch(r"/api/cards/(\d+)/move", path)
            if m and method == "PUT":
                cid = int(m.group(1))
                data = self._read_json_body()
                self._send_json(move_card(conn, cid, data))
                return

            # cards 恢复
            m = re.fullmatch(r"/api/cards/(\d+)/restore", path)
            if m and method == "POST":
                cid = int(m.group(1))
                self._send_json(restore_card(conn, cid))
                return

            # 搜索
            if path == "/api/search" and method == "GET":
                q = query.get("q", [""])[0]
                date_from = query.get("from", [None])[0]
                date_to = query.get("to", [None])[0]
                priority = query.get("priority", [None])[0]
                include_active = query.get("all", ["0"])[0] in ("1", "true", "True")
                self._send_json(
                    search_cards(
                        conn, q=q, date_from=date_from, date_to=date_to,
                        priority=priority, include_active=include_active,
                    )
                )
                return

            # 导出 JSON
            if path == "/api/export" and method == "GET":
                data = export_data(conn)
                body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                fname = "kanban-export-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header(
                    "Content-Disposition", 'attachment; filename="%s"' % fname
                )
                self.end_headers()
                self.wfile.write(body)
                return

            # 备份 db 文件
            if path == "/api/backup" and method == "GET":
                # 先确保所有变更落盘
                conn.commit()
                conn.close()
                with open(DB_PATH, "rb") as f:
                    data = f.read()
                fname = "kanban-backup-%s.db" % datetime.now().strftime("%Y%m%d-%H%M%S")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header(
                    "Content-Disposition", 'attachment; filename="%s"' % fname
                )
                self.end_headers()
                self.wfile.write(data)
                return

            self._send_json({"error": "未知的 API 路径"}, 404)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # ----- HTTP 方法入口 -----
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def do_OPTIONS(self):
        # 便于未来跨域扩展；当前同源不需要
        self.send_response(204)
        self.end_headers()


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 60)
    print("  看板系统已启动")
    print("  本地访问: http://localhost:%d" % PORT)
    print("  局域网访问: http://%s:%d" % (_get_lan_ip(), PORT))
    print("  数据库: %s" % DB_PATH)
    print("  按 Ctrl+C 停止")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...")
        server.shutdown()


def _get_lan_ip():
    """获取本机局域网 IP，仅用于提示。"""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return HOST


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""安全:富文本描述清洗与附件文件名/路径校验。"""

import os
import unicodedata
from html import escape
from html.parser import HTMLParser

import config
from errors import ApiError, require_string, fail


class SafeHtmlParser(HTMLParser):
    allowed = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u", "s", "span"}
    blocked_tags = {"script", "style", "iframe", "object", "svg"}
    text_color_classes = {"rt-fg-default", "rt-fg-red", "rt-fg-yellow", "rt-fg-green", "rt-fg-blue", "rt-fg-purple"}
    highlight_classes = {"rt-bg-red", "rt-bg-yellow", "rt-bg-green", "rt-bg-blue", "rt-bg-purple"}
    checkbox_classes = {"rt-checkbox", "rt-checkbox-checked"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = []
        self.stack = [(None, (), self.root)]
        self.blocked = 0

    def _span_classes(self, attrs):
        values = dict(attrs).get("class", "").split()
        checkbox = next((value for value in values if value in self.checkbox_classes), None)
        if checkbox:
            return (checkbox,)
        foreground = next((value for value in values if value in self.text_color_classes), None)
        highlight = next((value for value in values if value in self.highlight_classes), None)
        return tuple(value for value in (foreground, highlight) if value)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.blocked:
            if tag in self.blocked_tags:
                self.blocked += 1
            return
        if tag in self.blocked_tags:
            self.blocked = 1
            return
        if tag == "br":
            self.stack[-1][2].append(("br", (), []))
        elif tag in self.allowed or tag in {"div", "strike"}:
            normalized_tag = {"b": "strong", "i": "em", "strike": "s"}.get(tag, tag)
            classes = self._span_classes(attrs) if normalized_tag == "span" else ()
            node = (normalized_tag, classes, [])
            self.stack[-1][2].append(node)
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() != "br":
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.blocked:
            if tag in self.blocked_tags:
                self.blocked = max(0, self.blocked - 1)
            return
        normalized_tag = {"b": "strong", "i": "em", "strike": "s"}.get(tag, tag)
        if normalized_tag == "br" or (normalized_tag not in self.allowed and normalized_tag != "div"):
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index][0] == normalized_tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if not self.blocked:
            self.stack[-1][2].append(data)

    @staticmethod
    def _has_content(nodes):
        for node in nodes:
            if isinstance(node, str):
                if node.strip():
                    return True
            elif node[0] == "br" or SafeHtmlParser._has_content(node[2]):
                return True
        return False

    def _canonical_nodes(self, nodes):
        result = []
        for node in nodes:
            if isinstance(node, str):
                result.append(node)
                continue
            tag, classes, children = node
            children = self._canonical_nodes(children)
            if tag == "span":
                checkbox = next((value for value in classes if value in self.checkbox_classes), None)
                if checkbox:
                    result.append(("span", (checkbox,), ["☑" if checkbox == "rt-checkbox-checked" else "☐"]))
                    continue
                if not self._has_content(children):
                    continue
                if not classes:
                    result.extend(children)
                    continue
                flattened = []
                for child in children:
                    if not isinstance(child, str) and child[0] == "span" and child[1] == classes and not any(value in self.checkbox_classes for value in classes):
                        flattened.extend(child[2])
                    else:
                        flattened.append(child)
                children = flattened
            current = (tag, classes, children)
            if tag == "span" and result and not isinstance(result[-1], str) and result[-1][0] == "span" and result[-1][1] == classes and not any(value in self.checkbox_classes for value in classes):
                previous = result[-1]
                result[-1] = ("span", classes, previous[2] + children)
            else:
                result.append(current)
        return result

    def _render(self, nodes, parent=None):
        output = []
        for node in nodes:
            if isinstance(node, str):
                output.append(escape(node, quote=False))
                continue
            tag, classes, children = node
            if tag == "br":
                output.append("<br>")
                continue
            rendered = self._render(children, tag)
            if tag == "div" and parent is not None:
                output.append(rendered)
                continue
            if tag == "span":
                output.append("<span class=\"%s\">%s</span>" % (" ".join(classes), rendered))
                continue
            normalized_tag = "p" if tag == "div" else tag
            if normalized_tag == "p" and not self._has_content(children):
                rendered = "<br>"
            output.append("<%s>%s</%s>" % (normalized_tag, rendered, normalized_tag))
        return "".join(output)

    @property
    def output(self):
        return self._render(self._canonical_nodes(self.root))


def sanitize_description(value):
    parser = SafeHtmlParser()
    parser.feed(require_string(value, "description", 0, 100000, trim=False))
    parser.close()
    return parser.output.strip()


def validate_attachment_name(value):
    if not isinstance(value, str) or not value or len(value) > 255:
        fail("文件名不能为空且不能超过 255 个字符", field="file_name")
    name = value
    if name in (".", "..") or any(char in name for char in '/\\:*?"<>|') or name.endswith((" ", ".")):
        fail("文件名包含当前系统不支持的字符，请重命名文件后再上传", field="file_name")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        fail("文件名包含当前系统不支持的字符，请重命名文件后再上传", field="file_name")
    if len(name.encode("utf-8")) > 255:
        raise ApiError("文件名过长，请缩短后再上传", 422, "ATTACHMENT_NAME_TOO_LONG", {"field": "file_name"})
    stem = name.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL"} | {"COM%s" % i for i in range(1, 10)} | {"LPT%s" % i for i in range(1, 10)}
    if stem in reserved:
        fail("文件名是系统保留名称，请重命名后再上传", field="file_name")
    return name


def column_name_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def attachment_name_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def attachment_directory(card_id):
    return os.path.join(config.ATTACHMENTS_DIR, str(card_id))


def attachment_path(card_id, file_name):
    root = os.path.abspath(config.ATTACHMENTS_DIR)
    path = os.path.abspath(os.path.join(attachment_directory(card_id), file_name))
    if os.path.commonpath((root, path)) != root:
        raise ApiError("附件路径无效", 400, "INVALID_ATTACHMENT_PATH")
    return path

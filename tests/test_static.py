import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ElementIndex(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.ids = set()
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        self.elements.append((tag, attributes))
        if tag == "script":
            self.scripts.append(attributes)
        if attributes.get("id"):
            self.ids.add(attributes["id"])


class AccessibilityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        cls.css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
        cls.parser = ElementIndex()
        cls.parser.feed(cls.html)

    def test_dialogs_have_modal_semantics_and_valid_labels(self):
        dialogs = [attrs for _, attrs in self.parser.elements if attrs.get("role") == "dialog"]
        self.assertGreaterEqual(len(dialogs), 2)
        for dialog in dialogs:
            self.assertEqual(dialog.get("aria-modal"), "true")
            self.assertIn(dialog.get("aria-labelledby"), self.parser.ids)

    def test_editor_and_live_regions_are_present(self):
        editor = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "card-description")
        self.assertEqual(editor.get("role"), "textbox")
        self.assertEqual(editor.get("aria-multiline"), "true")
        toast = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "toast")
        self.assertEqual(toast.get("aria-live"), "polite")

    def test_topbar_icon_actions_have_tooltips(self):
        theme = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "theme-button")
        self.assertEqual(theme.get("title"), "切换主题")
        self.assertEqual(theme.get("aria-label"), "切换主题")
        self.assertGreaterEqual(self.html.count("toolbar-icon-only"), 4)

    def test_auto_archive_tooltip_is_delayed_on_completed_header(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        compact_css = "".join(self.css.split())
        self.assertIn('column.name.trim()==="已完成"', app_js)
        self.assertNotIn("auto-archive-hint", app_js + self.css)
        self.assertIn("auto-archive-tooltip", app_js)
        self.assertIn('setAttribute("role","tooltip")', app_js)
        self.assertIn("满 30 天后", app_js)
        self.assertIn("setTimeout(()=>", app_js)
        self.assertIn("},1000)", app_js)
        self.assertIn("clearTimeout(autoArchiveTooltipTimer)", app_js)
        self.assertIn("header.isConnected", app_js)
        self.assertIn('closest(".column-actions")', app_js)
        self.assertIn('addEventListener("pointerdown",hideAutoArchiveTooltip)', app_js)
        self.assertIn("function onColumnDragStart(event){hideAutoArchiveTooltip()", app_js)
        self.assertIn("function renderBoard(){hideAutoArchiveTooltip()", app_js)
        self.assertIn(".auto-archive-tooltip{position:absolute", compact_css)
        self.assertIn("pointer-events:none", compact_css)

    def test_view_switcher_uses_tab_semantics(self):
        self.assertIn('role="tablist"', self.html)
        self.assertEqual(self.html.count('role="tab"'), 2)
        self.assertEqual(self.html.count('role="tabpanel"'), 2)
        self.assertIn('aria-controls="view-board"', self.html)
        self.assertIn('aria-controls="view-history"', self.html)
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        self.assertIn("setupViewTabs", app_js)
        self.assertIn('event.key==="ArrowRight"', app_js)
        self.assertIn('event.key==="ArrowLeft"', app_js)

    def test_theme_menu_and_four_themes_exist(self):
        self.assertIn('id="theme-menu"', self.html)
        self.assertIn('data-theme="douban-classic"', self.html)
        for theme in ("douban-classic", "dark-tech", "office", "cloud-blue", "aurora-blue"):
            self.assertIn(theme, self.html)
        self.assertNotIn("douban-modern", self.html)
        self.assertNotIn("navy-blue", self.html)
        self.assertIn("豆瓣绿", self.html)
        self.assertIn("暗夜黑", self.html)
        self.assertIn("简约灰", self.html)
        self.assertIn("极光紫", self.html)
        for removed_name in ("经典豆瓣绿", "现代豆瓣绿", "海军蓝", "深色科技", "简约办公", "极光蓝紫"):
            self.assertNotIn(removed_name, self.html)
        for selector in ('data-theme="dark-tech"', 'data-theme="office"', 'data-theme="cloud-blue"', 'data-theme="aurora-blue"'):
            self.assertIn(selector, self.css)
        compact_css = "".join(self.css.split())
        self.assertIn(".history-view{width:100%;max-width:none;min-height:calc(100vh-48px);margin:0;", compact_css)
        self.assertIn(':root[data-theme="dark-tech"].board-view,:root[data-theme="dark-tech"].history-view{', compact_css)
        self.assertIn(':root[data-theme="office"].board-view,:root[data-theme="office"].history-view{', compact_css)
        self.assertIn(':root[data-theme="cloud-blue"].board-view,:root[data-theme="cloud-blue"].history-view{', compact_css)
        self.assertIn(':root[data-theme="aurora-blue"].board-view,:root[data-theme="aurora-blue"].history-view{', compact_css)

    def test_attachment_ui_contract(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        attachment_input = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "attachment-input")
        self.assertIn("multiple", attachment_input)
        self.assertIn('aria-live="polite"', self.html)
        for name in ("selectAttachments", "processAttachmentQueue", "uploadAttachment", "toggleResultAttachments", "attachment_count"):
            self.assertIn(name, app_js)
        for selector in (".attachment-item", ".attachment-progress", ".history-attachments"):
            self.assertIn(selector, self.css)
        self.assertIn("application/zip,.zip", self.html)
        self.assertIn("完整备份（含附件）", self.html)
        self.assertIn('fetch("/api/backup/check")', app_js)
        self.assertIn('download("/api/backup")', app_js)
        self.assertIn("INCOMPLETE_BACKUP", app_js)
        self.assertNotIn('fetch("/api/backup")', app_js)
        self.assertNotIn("response.blob()", app_js)

    def test_native_date_controls_are_styled_and_accessible(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        dates_js = (ROOT / "static" / "js" / "dates.js").read_text(encoding="utf-8")
        due = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "card-due")
        due_time = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "card-due-time")
        self.assertEqual(due.get("type"), "date")
        self.assertEqual(due.get("aria-label"), "截止日期")
        self.assertEqual(due_time.get("type"), "time")
        self.assertEqual(due_time.get("step"), "60")
        self.assertEqual(due_time.get("aria-label"), "截止时间（可选）")
        self.assertNotIn('id="card-time-hint"', self.html)
        self.assertIn('id="card-date-clear"', self.html)
        self.assertIn('id="card-time-clear"', self.html)
        self.assertIn("date-picker-trigger", self.html)
        self.assertIn("history-date-range", self.html)
        self.assertIn('id="search-from-clear"', self.html)
        self.assertIn('id="search-to-clear"', self.html)
        self.assertIn("history-date-trigger", self.html)
        self.assertIn("date-range-separator", self.html)
        self.assertIn("input[type=time]", self.css)
        self.assertIn(".date-input-shell", self.css)
        self.assertIn(".history-date-range", self.css)
        self.assertIn("syncHistoryDateControl", dates_js)
        self.assertIn("clearHistoryDate", dates_js)
        self.assertIn("起始日期不能晚于结束日期", app_js)
        self.assertIn("calendarDayNumber", dates_js)
        self.assertIn("openNativePicker", dates_js)
        self.assertIn("syncCardDateControl", dates_js)
        self.assertIn("clearCardDate", dates_js)
        self.assertIn("showPicker", dates_js)
        self.assertIn("syncCardTimeControl", dates_js)
        self.assertIn("clearCardTime", dates_js)
        self.assertIn("请先选择截止日期", app_js)
        self.assertIn(".time-clear-btn", self.css)

    def test_scripts_and_static_actions_are_csp_compatible(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        self.assertTrue((ROOT / "static" / "theme-init.js").is_file())
        self.assertEqual([script.get("src") for script in self.parser.scripts], ["/static/theme-init.js", "/static/kanban.js"])
        for _, attributes in self.parser.elements:
            self.assertFalse(any(name.lower().startswith("on") for name in attributes))
        self.assertIn("setupStaticActions", app_js)
        self.assertNotIn("window.showView=", app_js)

    def test_es_module_and_busy_state_contracts(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        self.assertIn('type="module"', self.html)
        for module in ("state.js", "api.js", "ui.js", "dates.js"):
            self.assertTrue((ROOT / "static" / "js" / module).is_file())
        self.assertIn("AbortController", app_js)
        self.assertIn("aria-pressed", app_js)
        self.assertIn("search-load-more", app_js)
        self.assertIn("卡片和附件均已保存", app_js)
        self.assertIn("附件上传、覆盖和删除会立即生效", self.html)
        self.assertIn('id="choice-modal"', self.html)

    def test_description_wysiwyg_contract(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        compact_css = "".join(self.css.split())
        self.assertIn(".card-desc{", compact_css)
        self.assertIn("white-space:pre-wrap", compact_css)
        self.assertIn(".card-descp,.card-descdiv,.editorp,.editordiv{margin:0;min-height:1.5em;}", compact_css)
        self.assertIn(".card-descul,.card-descol,.editorul,.editorol{padding-left:24px;margin:4px0;white-space:normal;}", compact_css)
        self.assertIn('description.innerHTML=card.description', app_js)
        self.assertIn('document.getElementById("card-description").innerHTML=card.description||""', app_js)
        self.assertIn('description:document.getElementById("card-description").innerHTML', app_js)

    def test_filter_reorder_and_compatibility_contracts(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        ui_js = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")
        hint = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "filter-reorder-hint")
        self.assertIn("hidden", hint)
        self.assertLess(self.html.index('id="filter-reorder-hint"'), self.html.index('id="board"'))
        self.assertIn('document.getElementById("filter-reorder-hint").hidden=!isManualReorderDisabled()', app_js)
        self.assertNotIn('createElement("div");hint.className="filter-reorder-hint"', app_js)
        self.assertIn("header.draggable=!isManualReorderDisabled()", app_js)
        self.assertIn('if(isManualReorderDisabled()||event.target.closest(".column-actions")', app_js)
        self.assertIn('if(isManualReorderDisabled()||colDrag.colId==null)return', app_js)
        self.assertNotIn("||=", app_js + ui_js)
        self.assertIn("export function clearChildren", ui_js)
        self.assertIn('typeof node.replaceChildren === "function"', ui_js)
        self.assertIn("clearChildren(box)", ui_js)
        self.assertIn("clearChildren(board)", app_js)
        self.assertIn("clearChildren(dueBox)", app_js)
        self.assertNotIn(".replaceChildren()", app_js)
        compact_css = "".join(self.css.split())
        self.assertIn(".board-view{height:calc(100vh-48px);display:flex;flex-direction:column;", compact_css)
        self.assertIn(".board{display:flex;gap:12px;align-items:flex-start;flex:1;min-height:0;", compact_css)
        self.assertNotIn(".filter-reorder-hint{flex:0 0 100%", compact_css)
        self.assertNotIn(".board{height:calc(100%-", compact_css)

    def test_native_picker_progressive_enhancement_contract(self):
        dates_js = (ROOT / "static" / "js" / "dates.js").read_text(encoding="utf-8")
        compact_css = "".join(self.css.split())
        self.assertIn("input.disabled || input.readOnly", dates_js)
        self.assertIn('typeof input.showPicker === "function"', dates_js)
        self.assertIn("input.focus()", dates_js)
        self.assertNotIn("input.click()", dates_js)
        self.assertIn("appearance:auto;-webkit-appearance:auto", compact_css)
        self.assertIn("::-webkit-calendar-picker-indicator{display:block;opacity:1", compact_css)
        self.assertIn(".date-clear-btn,.time-clear-btn{position:absolute;right:32px", compact_css)
        self.assertIn(".history-date-clear{position:absolute;right:27px", compact_css)

    def test_per_column_sort_contract(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        dates_js = (ROOT / "static" / "js" / "dates.js").read_text(encoding="utf-8")
        state_js = (ROOT / "static" / "js" / "state.js").read_text(encoding="utf-8")
        self.assertIn('id="filter-summary"', self.html)
        self.assertNotIn('id="card-sort"', self.html)
        self.assertNotIn("sort-filter-group", self.html + self.css)
        for removed_id in ("time-filter-button", "time-filter-panel", "created-time-mode", "updated-time-mode"):
            self.assertNotIn(f'id="{removed_id}"', self.html)
        for sort_value in ("position", "updated_desc", "updated_asc", "created_desc", "created_asc"):
            self.assertIn(f'["{sort_value}",', app_js)
        self.assertIn("parseLocalTimestamp", dates_js)
        self.assertIn("relativeTimestamp", dates_js)
        self.assertNotIn("new Date(value)", dates_js)
        self.assertIn("sortCards(cards,column.id).forEach", app_js)
        self.assertIn("renderColumnSort(column,actions)", app_js)
        self.assertIn('aria-haspopup","menu', app_js)
        self.assertIn('role","menuitemradio', app_js)
        self.assertIn("column-sort-button", app_js)
        self.assertIn("column-sort-menu", app_js)
        self.assertIn("card-created-text", app_js)
        self.assertNotIn("card-relative-time", app_js)
        self.assertNotIn("refreshRelativeTimes", app_js)
        self.assertIn('document.getElementById("card-move-controls").hidden=isManualReorderDisabled()', app_js)
        self.assertIn("SORT_STORAGE_KEY=\"kanban-column-sorts\"", app_js)
        self.assertIn("localStorage.getItem(SORT_STORAGE_KEY)", app_js)
        self.assertIn("localStorage.setItem(SORT_STORAGE_KEY,JSON.stringify(values))", app_js)
        self.assertIn('localStorage.removeItem("kanban-card-sort")', app_js)
        self.assertIn("sortByColumn", state_js)
        self.assertIn("return hasActiveFilters()", state_js)
        self.assertNotIn("state.sort=", app_js + state_js)
        self.assertNotIn("hasActiveTimeFilters", state_js)
        self.assertNotIn(".time-filter-panel", self.css)
        self.assertIn(".card-created-text", self.css)
        self.assertIn(".card.priority-high", self.css)
        self.assertIn("border-left-color: #eb5a46 !important", self.css)
        self.assertIn("border-left-color: #ff9f1a !important", self.css)
        self.assertIn("border-left-color: #61bd4f !important", self.css)

    def test_history_sort_and_full_timestamp_contract(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        dates_js = (ROOT / "static" / "js" / "dates.js").read_text(encoding="utf-8")
        state_js = (ROOT / "static" / "js" / "state.js").read_text(encoding="utf-8")
        self.assertIn('id="history-sort-button"', self.html)
        self.assertIn('id="history-sort-menu"', self.html)
        for sort_value in ("archived_desc", "archived_asc", "updated_desc", "updated_asc", "created_desc", "created_asc"):
            self.assertIn(f'["{sort_value}",', app_js)
        self.assertIn('HISTORY_SORT_STORAGE_KEY="kanban-history-sort"', app_js)
        self.assertIn('params.set("sort",state.historySort)', app_js)
        self.assertIn('state.searchCursor=null', app_js)
        self.assertIn('role","menuitemradio', app_js)
        self.assertIn('historySort: "archived_desc"', state_js)
        self.assertIn('`创建：${fullTimestamp(card.created_at)} | 更新：${fullTimestamp(card.updated_at)}`', app_js)
        self.assertIn('String(date.getSeconds()).padStart(2, "0")', dates_js)
        self.assertIn(".history-sort-menu", self.css)
        self.assertIn(".result-times", self.css)

    def test_focus_and_responsive_css_exists(self):
        self.assertIn(":focus-visible", self.css)
        self.assertIn("@media (max-width: 720px)", self.css)
        self.assertIn("prefers-reduced-motion", self.css)


if __name__ == "__main__":
    unittest.main()

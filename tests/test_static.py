import re
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
        settings = next(attrs for _, attrs in self.parser.elements if attrs.get("id") == "settings-button")
        self.assertEqual(settings.get("title"), "设置")
        self.assertEqual(settings.get("aria-label"), "设置")
        self.assertEqual(settings.get("aria-controls"), "view-settings")
        self.assertIn('id="add-column-button"', self.html)

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

    def test_theme_settings_and_six_themes_exist(self):
        theme_init_js = (ROOT / "static" / "theme-init.js").read_text(encoding="utf-8")
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        themes = ("sea-salt-blue", "douban-green", "swiss-mono", "warm-paper", "liquid-glass", "deep-sea-night")
        legacy_themes = (
            "cloud-blue", "navy-blue", "aurora-blue", "douban-classic",
            "douban-modern", "office", "dark-tech",
        )
        self.assertIn('id="view-settings"', self.html)
        self.assertIn('id="theme-options"', self.html)
        self.assertNotIn('id="theme-button"', self.html)
        self.assertNotIn('id="theme-menu"', self.html)
        self.assertIn('data-theme="sea-salt-blue"', self.html)
        for theme, name in zip(themes, ("海盐蓝", "豆瓣绿", "瑞士黑白", "奶油陶土", "液态玻璃", "深海夜")):
            self.assertIn(f'value="{theme}"', self.html)
            self.assertIn(f'data-theme-value="{theme}"', self.html)
            self.assertIn(name, self.html)
            self.assertIn(f'"{theme}"', theme_init_js)
            self.assertIn(f'"{theme}"', app_js)
            self.assertEqual(len(re.findall(rf':root\[data-theme="{re.escape(theme)}"\]\s*\{{', self.css)), 1)
        for legacy_theme in legacy_themes:
            self.assertNotIn(legacy_theme, self.html)
            self.assertNotIn(f'data-theme="{legacy_theme}"', self.css)
        expected_migrations = {
            "cloud-blue": "sea-salt-blue",
            "navy-blue": "sea-salt-blue",
            "aurora-blue": "sea-salt-blue",
            "douban-classic": "douban-green",
            "douban-modern": "douban-green",
            "office": "swiss-mono",
            "dark-tech": "deep-sea-night",
        }
        for old_theme, new_theme in expected_migrations.items():
            with self.subTest(old_theme=old_theme, new_theme=new_theme):
                mapping_pattern = rf'["\']{re.escape(old_theme)}["\']\s*:\s*["\']{re.escape(new_theme)}["\']'
                self.assertRegex(theme_init_js, mapping_pattern)
                self.assertRegex(app_js, mapping_pattern)

        compact_css = "".join(self.css.split())
        for theme in themes:
            block = re.search(rf':root\[data-theme="{re.escape(theme)}"\]\s*\{{([^}}]+)\}}', self.css, re.S)
            self.assertIsNotNone(block)
            declarations = block.group(1)
            for token in ("--bg", "--board-bg", "--col-bg", "--text", "--text-light", "--border", "--primary"):
                self.assertRegex(declarations, rf'{re.escape(token)}\s*:')
        self.assertIn("background:var(--bg)", compact_css)
        self.assertIn("color:var(--text)", compact_css)
        self.assertIn("border", compact_css)
        self.assertIn("var(--border)", compact_css)
        self.assertIn(':root[data-theme="sea-salt-blue"]body', compact_css)
        self.assertIn(':root[data-theme="douban-green"].topbar', compact_css)
        self.assertIn(':root[data-theme="swiss-mono"]', compact_css)
        self.assertIn("font-family:", compact_css[compact_css.index(':root[data-theme="swiss-mono"]'):])
        self.assertIn(':root[data-theme="warm-paper"]body', compact_css)
        self.assertIn("linear-gradient", compact_css[compact_css.index(':root[data-theme="warm-paper"]body'):])
        liquid_glass_start = compact_css.index(':root[data-theme="liquid-glass"]')
        liquid_glass_end = compact_css.find(':root[data-theme="deep-sea-night"]', liquid_glass_start)
        liquid_glass_tokens = compact_css[liquid_glass_start:liquid_glass_end]
        for color in ("red", "yellow", "green", "blue", "purple"):
            self.assertIn(f"--rt-fg-{color}:", liquid_glass_tokens)
            self.assertIn(f"--rt-bg-{color}:", liquid_glass_tokens)
            self.assertIn(f"--rt-bg-{color}-text:", liquid_glass_tokens)
        self.assertIn(':root[data-theme="liquid-glass"]body', compact_css)
        self.assertIn("radial-gradient", compact_css[compact_css.index(':root[data-theme="liquid-glass"]body'):])
        self.assertIn('@supports((backdrop-filter:blur(1px))or(-webkit-backdrop-filter:blur(1px)))', compact_css)
        self.assertIn('-webkit-backdrop-filter:blur(27px)saturate(185%)', compact_css)
        self.assertIn('backdrop-filter:blur(27px)saturate(185%)', compact_css)
        self.assertIn('@media(prefers-contrast:more)', compact_css)
        self.assertIn('@media(forced-colors:active)', compact_css)
        self.assertIn(':root[data-theme="liquid-glass"].modal{background:rgba(254,254,254,.98);-webkit-backdrop-filter:none;backdrop-filter:none;}', compact_css)
        glass_card_rule = re.search(r':root\[data-theme="liquid-glass"\]\s+\.card,([^\{]+)\{([^}]+)\}', self.css, re.S)
        self.assertIsNotNone(glass_card_rule)
        self.assertNotIn("backdrop-filter", glass_card_rule.group(2))
        deep_sea_block = compact_css[compact_css.index(':root[data-theme="deep-sea-night"]'):]
        self.assertIn('color-scheme:dark', deep_sea_block)
        self.assertIn(':root[data-theme="deep-sea-night"].card', deep_sea_block)

    def test_card_modal_field_order_and_styled_confirmations(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        expected = ["card-title", "card-description", "card-labels", "card-due", "card-priority", "attachment-field", "card-column", "card-move-controls"]
        positions = [self.html.index(f'id="{value}"') for value in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("confirm(", app_js)
        self.assertNotIn("prompt(", app_js)
        self.assertNotIn("alert(", app_js)
        self.assertIn("取消新建卡片？", app_js)
        self.assertIn("放弃未保存的修改？", app_js)
        self.assertIn('title=editing?"重命名列":"新增列"', app_js)
        self.assertIn('primaryClass:"ghost",focus:"cancel",opener', app_js)
        self.assertIn("setupButtonTooltips", app_js)

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
        self.assertIn('fetch("/api/backup")', app_js)
        self.assertIn("response.blob()", app_js)
        self.assertIn("downloadBlob", app_js)
        self.assertIn("INCOMPLETE_BACKUP", app_js)
        self.assertNotIn('download("/api/backup")', app_js)

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
        self.assertIn("history-filter-field date-control", self.html)
        self.assertIn('id="search-from-clear"', self.html)
        self.assertIn('id="search-to-clear"', self.html)
        self.assertIn("history-date-trigger", self.html)
        self.assertIn("input[type=time]", self.css)
        self.assertIn(".date-input-shell", self.css)
        self.assertIn(".history-date-shell", self.css)
        self.assertIn("syncHistoryDateControl", dates_js)
        self.assertIn("clearHistoryDate", dates_js)
        self.assertIn("起始日期不能晚于结束日期", app_js)
        self.assertIn("calendarDayNumber", dates_js)
        self.assertIn("openDateTimePicker", dates_js)
        self.assertIn('role", "dialog', dates_js)
        self.assertIn('event.key === "Escape"', dates_js)
        self.assertIn("dateAllowed", dates_js)
        self.assertNotIn("showPicker", dates_js)
        self.assertIn("syncCardDateControl", dates_js)
        self.assertIn("clearCardDate", dates_js)
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
        self.assertNotIn('description.textContent=stripHtml(card.description)', app_js)
        self.assertIn(".result-descp,.result-descdiv{margin:0;min-height:1.5em;}", compact_css)
        self.assertIn(".result-descul,.result-descol{padding-left:24px;margin:4px0;white-space:normal;}", compact_css)
        self.assertIn('document.getElementById("card-description").innerHTML=card.description||""', app_js)
        self.assertIn('description:document.getElementById("card-description").innerHTML', app_js)

    def test_rich_text_color_controls_are_safe_and_accessible(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        compact_css = "".join(self.css.split())
        for element_id in ("text-color-button", "text-color-menu", "highlight-color-button", "highlight-color-menu"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertIn('aria-haspopup="menu"', self.html)
        self.assertIn('data-color-kind="fg"', self.html)
        self.assertIn('data-color-kind="bg"', self.html)
        self.assertNotIn('type="color"', self.html)
        self.assertNotIn("foreColor", app_js)
        self.assertNotIn("hiliteColor", app_js)
        self.assertNotIn("backColor", app_js)
        self.assertIn("RICH_TEXT_COLORS", app_js)
        self.assertIn("请先选择文字", app_js)
        self.assertIn('event.key==="Escape"', app_js)
        for color_class in ("rt-fg-red", "rt-fg-yellow", "rt-fg-green", "rt-fg-blue", "rt-fg-purple", "rt-bg-red", "rt-bg-yellow", "rt-bg-green", "rt-bg-blue", "rt-bg-purple"):
            self.assertIn(color_class, self.html)
            self.assertIn(color_class, self.css)
        self.assertIn(".editor.rt-fg-red,.card-desc.rt-fg-red,.result-desc.rt-fg-red", compact_css)
        for color in ("red", "yellow", "green", "blue", "purple"):
            self.assertIn(f"--rt-fg-{color}:", compact_css)
            self.assertIn(f"--rt-bg-{color}:", compact_css)
            self.assertIn(f"--rt-bg-{color}-text:", compact_css)
            background_rule = re.search(rf'\.rt-bg-{color}\s*\{{([^}}]+)\}}', self.css, re.S)
            self.assertIsNotNone(background_rule)
            declarations = "".join(background_rule.group(1).split())
            self.assertIn(f"background:var(--rt-bg-{color})", declarations)
            self.assertIn(f"color:var(--rt-bg-{color}-text)", declarations)
        deep_sea_start = compact_css.index(':root[data-theme="deep-sea-night"]')
        deep_sea_end = compact_css.find(':root[data-theme="', deep_sea_start + 1)
        deep_sea_tokens = compact_css[deep_sea_start:deep_sea_end if deep_sea_end >= 0 else len(compact_css)]
        for color in ("red", "yellow", "green", "blue", "purple"):
            self.assertIn(f"--rt-fg-{color}:", deep_sea_tokens)
            self.assertIn(f"--rt-bg-{color}:", deep_sea_tokens)
            self.assertIn(f"--rt-bg-{color}-text:", deep_sea_tokens)
        self.assertNotIn("--rt-fg-red:#ff0000", compact_css)
        self.assertNotIn("--rt-fg-yellow:#ffff00", compact_css)
        self.assertNotIn("--rt-bg-red:#ff0000", compact_css)
        self.assertNotIn("--rt-bg-yellow:#ffff00", compact_css)
        self.assertNotIn("rt-fg-amber", self.html + self.css + app_js)
        self.assertNotIn("rt-bg-clear", self.html + self.css + app_js)
        self.assertIn("splitRangeBoundaries", app_js)
        self.assertIn("removeColorFromFragment", app_js)
        self.assertIn("normalizeColorDom", app_js)
        self.assertNotIn('padding-inline:.08em', compact_css)

    def test_archive_and_permanent_delete_contract(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        ui_js = (ROOT / "static" / "js" / "ui.js").read_text(encoding="utf-8")
        compact_css = "".join(self.css.split())
        self.assertIn('id="card-archive-button"', self.html)
        self.assertIn('id="card-permanent-delete-button"', self.html)
        self.assertIn('d="M4 7h16v13H4zM3 4h18v3H3zM9 11h6"', self.html)
        self.assertIn("archiveCurrentCard", app_js)
        self.assertNotIn("deleteCurrentCard", app_js)
        self.assertIn('`/api/cards/${card.id}/permanent`', app_js)
        self.assertIn("permanentlyDeleteCard(card,{fromHistory:true,unsaved:false})", app_js)
        self.assertIn("附件：${count} 个", app_js)
        self.assertIn("未保存修改", app_js)
        self.assertIn("此操作不可撤销", app_js)
        self.assertIn('focus:"cancel"', app_js)
        self.assertIn('focus === "cancel"', ui_js)
        self.assertIn("history-permanent-delete", app_js)
        self.assertIn('remove.innerHTML=\'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 11v6m6-6v6M8 7l1-3h6l1 3m2 0-1 14H7L6 7"/></svg>\'', app_js)
        self.assertIn('remove.title="永久删除卡片"', app_js)
        self.assertNotIn('remove.textContent="永久删除"', app_js)
        self.assertIn('primaryClass:"ghost"', app_js)
        permanent_delete = app_js[app_js.index("function permanentDeleteMessage"):app_js.index("async function permanentlyDeleteCard")]
        self.assertNotIn('danger:true', permanent_delete)
        self.assertIn('primaryButton.className = primaryClass ||', ui_js)
        self.assertNotIn("opacity:0", compact_css[compact_css.index(".history-card-actions.history-permanent-delete{"):compact_css.index(".history-card-actions.history-permanent-delete{") + 500])
        self.assertIn('if(!card.archived){const actions=document.createElement("div");actions.className="result-actions history-card-actions"', app_js)

    def test_svg_icon_contract_replaces_legacy_emoji(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        markup = self.html + app_js
        for class_name in ("svg-icon", "column-action-icon", "card-meta-icon"):
            self.assertIn(class_name, markup)
        self.assertRegex(markup, r'<svg[^>]+class=["\'][^"\']*(?:svg-icon|column-action-icon|card-meta-icon)')
        for legacy_icon in ("📎", "📅", "✎", "🗑"):
            self.assertNotIn(legacy_icon, markup)

    def test_settings_and_card_description_preference_contract(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        state_js = (ROOT / "static" / "js" / "state.js").read_text(encoding="utf-8")
        compact_css = "".join(self.css.split())
        for element_id in ("settings-button", "view-settings", "settings-back-button", "board-display-title", "appearance-title", "data-management-title"):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertLess(self.html.index('id="view-settings"'), self.html.index('id="import-button"'))
        for value in ("none", "one-line", "two-lines", "full"):
            self.assertIn(f'value="{value}"', self.html)
        self.assertIn('value="two-lines" checked', self.html)
        self.assertIn('cardDescriptionDisplay: "two-lines"', state_js)
        self.assertIn('CARD_DESCRIPTION_STORAGE_KEY="kanban-card-description-display"', app_js)
        self.assertIn("CARD_DESCRIPTION_OPTIONS.has(value)", app_js)
        self.assertIn("localStorage.getItem(CARD_DESCRIPTION_STORAGE_KEY)", app_js)
        self.assertIn("localStorage.setItem(CARD_DESCRIPTION_STORAGE_KEY,state.cardDescriptionDisplay)", app_js)
        self.assertRegex(app_js, r'CARD_DESCRIPTION_OPTIONS\.has\(value\)\?value:"two-lines"')
        self.assertRegex(app_js, r'catch\(_\)\{state\.cardDescriptionDisplay="two-lines"\}')
        self.assertLess(app_js.index("loadCardDescriptionPreference()"), app_js.index("refresh(true)"))
        self.assertIn('state.cardDescriptionDisplay!=="none"', app_js)
        self.assertIn('description.classList.add(state.cardDescriptionDisplay)', app_js)
        self.assertIn(".card-desc.one-line,.card-desc.two-lines{overflow:hidden;}", compact_css)
        self.assertIn(".card-desc.one-line{max-height:1.45em;}", compact_css)
        self.assertIn(".card-desc.two-lines{max-height:2.9em;}", compact_css)
        self.assertIn(".settings-view{", compact_css)
        self.assertIn(".data-actions{", compact_css)

    def test_history_restore_uses_matched_active_column(self):
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        self.assertIn("state.columns.find(column=>column.id===card.restore_column_id)", app_js)
        self.assertIn("restoreCard(card,card.restore_column_id||undefined)", app_js)
        self.assertNotIn("button.onclick=()=>restoreCard(card);", app_js)

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
        self.assertIn(".board-view{", compact_css)
        self.assertIn("display:flex", compact_css[compact_css.index(".board-view{"):compact_css.index(".board-view{") + 300])
        self.assertIn("flex-direction:column", compact_css[compact_css.index(".board-view{"):compact_css.index(".board-view{") + 300])
        self.assertIn(".board{display:flex;", compact_css)
        self.assertIn("flex:1", compact_css[compact_css.index(".board{display:flex;"):compact_css.index(".board{display:flex;") + 300])
        self.assertIn("min-height:0", compact_css[compact_css.index(".board{display:flex;"):compact_css.index(".board{display:flex;") + 300])
        self.assertNotIn(".filter-reorder-hint{flex:0 0 100%", compact_css)
        self.assertNotRegex(compact_css, r"\.board\{[^}]*height:calc\(")

    def test_application_owned_picker_contract(self):
        dates_js = (ROOT / "static" / "js" / "dates.js").read_text(encoding="utf-8")
        app_js = (ROOT / "static" / "kanban.js").read_text(encoding="utf-8")
        compact_css = "".join(self.css.split())
        self.assertIn("input.disabled || input.readOnly", dates_js)
        self.assertIn("openDateTimePicker", dates_js)
        self.assertNotIn("showPicker", dates_js)
        for label in ("今天", "现在", "清除", "取消", "确定"):
            self.assertIn(f'"{label}"', dates_js)
        self.assertIn('event.key === "Escape"', dates_js)
        self.assertIn('event.key !== "Tab"', dates_js)
        self.assertIn("dateAllowed", dates_js)
        self.assertIn("input.min", dates_js)
        self.assertIn("input.max", dates_js)
        self.assertIn("dispatchPickerInput", dates_js)
        self.assertIn("event.currentTarget", app_js)
        self.assertIn(".owned-picker-overlay{position:fixed", compact_css)
        self.assertIn(".owned-picker-day[aria-selected=\"true\"]", compact_css)

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

    def test_history_filter_layout_contract(self):
        ordered_ids = (
            "search-q",
            "search-priority",
            "search-from",
            "search-to",
            "search-created-from",
            "search-created-to",
            "search-updated-from",
            "search-updated-to",
            "search-all",
            "history-sort-button",
        )
        positions = [self.html.index(f'id="{element_id}"') for element_id in ordered_ids]
        self.assertEqual(positions, sorted(positions))
        for label in (
            "关键词",
            "优先级",
            "截止日期从",
            "截止日期到",
            "创建日期从",
            "创建日期到",
            "更新日期从",
            "更新日期到",
        ):
            self.assertIn(f"<span>{label}</span>", self.html)
        self.assertIn("history-filter-field", self.html)
        self.assertNotIn("history-date-stack", self.html)
        self.assertIn(".search-panel { display:flex; flex-wrap:wrap; align-items:flex-end", self.css)
        self.assertIn(".search-panel .date-control { display:flex; flex:0 1 190px; flex-direction:column", self.css)
        self.assertIn("min-width:180px", self.css)
        self.assertIn(':root[data-theme="liquid-glass"] .history-view .search-panel', self.css)
        self.assertIn("z-index:40", self.css)
        self.assertIn(':root[data-theme="deep-sea-night"] .history-sort-button', self.css)
        self.assertIn(':root[data-theme="deep-sea-night"] .history-sort-menu', self.css)
        self.assertIn("@media (max-width:380px)", self.css)

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

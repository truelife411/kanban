/* ===========================================================================
 * 看板系统前端逻辑 —— 纯原生 JS，零第三方依赖
 * =========================================================================== */

// 全局状态
let state = {
    columns: [],        // [{id,name,position}]
    cards: [],          // 当前活跃卡片 [{id,column_id,title,...}]
    currentCardId: null,// 当前编辑的卡片 id（null=新建）
    currentCardCol: null// 新建卡片时所在的列
};

// 拖拽状态
let drag = {
    cardId: null,
    cardEl: null,
    sourceColId: null,
    placeholder: null
};

// 列拖拽状态（独立于卡片拖拽，避免互相干扰）
let colDrag = {
    colId: null,
    colEl: null
};

// ============ API 封装 ============
async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
    }
    const res = await fetch(url, opts);
    const text = await res.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch (e) { data = { error: text }; }
    if (!res.ok) throw new Error(data.error || ("请求失败 (" + res.status + ")"));
    return data;
}

// ============ Toast 提示 ============
function toast(msg, isError) {
    const t = document.getElementById("toast");
    t.textContent = msg;
    t.className = "toast" + (isError ? " error" : "");
    t.hidden = false;
    clearTimeout(t._timer);
    t._timer = setTimeout(() => { t.hidden = true; }, 2500);
}

// ============ 富文本编辑器 ============
// 绑定工具栏按钮 -> execCommand
// 关键：工具栏用 mousedown + preventDefault，保持编辑器原有焦点和选区不被夺走，
// 然后直接执行命令。不要手动 focus()，否则选区会丢失。
function setupEditorToolbar() {
    const toolbar = document.getElementById("editor-toolbar");
    if (!toolbar) return;
    toolbar.addEventListener("mousedown", (e) => {
        const btn = e.target.closest("button[data-cmd]");
        if (!btn) return;
        e.preventDefault();   // 阻止按钮夺走编辑区焦点，选区得以保留
        const cmd = btn.dataset.cmd;
        document.execCommand(cmd, false, null);
    });
}

// HTML -> 纯文本（用于卡片列表预览，去掉所有标签，保留换行）
function stripHtml(html) {
    if (!html) return "";
    const tmp = document.createElement("div");
    tmp.innerHTML = html;
    // <br> 和块级元素结尾转为换行，保持可读性
    tmp.querySelectorAll("br").forEach(br => br.replaceWith("\n"));
    let text = tmp.innerText || tmp.textContent || "";
    return text.replace(/\n{3,}/g, "\n\n").trim();
}

// ============ 视图切换 ============
function showView(view) {
    const board = document.getElementById("view-board");
    const history = document.getElementById("view-history");
    const btnB = document.getElementById("btn-board");
    const btnH = document.getElementById("btn-history");
    if (view === "board") {
        board.hidden = false; history.hidden = true;
        btnB.classList.add("active"); btnH.classList.remove("active");
    } else {
        board.hidden = true; history.hidden = false;
        btnB.classList.remove("active"); btnH.classList.add("active");
    }
}

// ============ 数据加载与渲染 ============
async function refresh() {
    try {
        const [cols, cards] = await Promise.all([
            api("GET", "/api/columns"),
            api("GET", "/api/cards")
        ]);
        state.columns = cols;
        state.cards = cards;
        renderBoard();
    } catch (e) {
        toast("加载失败：" + e.message, true);
    }
}

function renderBoard() {
    const board = document.getElementById("board");
    board.innerHTML = "";

    state.columns
        .slice()
        .sort((a, b) => a.position - b.position)
        .forEach(col => {
            board.appendChild(renderColumn(col));
        });

    // 末尾的"新增列"按钮
    const addBtn = document.createElement("button");
    addBtn.className = "add-column-btn";
    addBtn.textContent = "+ 新增列";
    addBtn.onclick = addColumn;
    board.appendChild(addBtn);
}

function renderColumn(col) {
    const colEl = document.createElement("div");
    colEl.className = "column";
    colEl.dataset.colId = col.id;

    // 列头（同时作为拖拽手柄）
    const header = document.createElement("div");
    header.className = "column-header";
    header.draggable = true;
    header.dataset.colId = col.id;

    const cards = state.cards.filter(c => c.column_id === col.id);
    const title = document.createElement("div");
    title.className = "column-title";
    title.textContent = col.name;

    const count = document.createElement("span");
    count.className = "column-count";
    count.textContent = cards.length;

    const actions = document.createElement("div");
    actions.className = "column-actions";
    const renameBtn = document.createElement("button");
    renameBtn.textContent = "✎";
    renameBtn.title = "重命名";
    renameBtn.onclick = (e) => { e.stopPropagation(); renameColumn(col); };
    const delBtn = document.createElement("button");
    delBtn.textContent = "🗑";
    delBtn.title = "删除列(卡片归档)";
    delBtn.onclick = (e) => { e.stopPropagation(); deleteColumn(col); };
    actions.appendChild(renameBtn);
    actions.appendChild(delBtn);

    header.appendChild(title);
    header.appendChild(count);
    header.appendChild(actions);
    colEl.appendChild(header);

    // 卡片列表（拖放目标）
    const list = document.createElement("div");
    list.className = "card-list";
    list.dataset.colId = col.id;
    cards
        .slice()
        .sort((a, b) => a.position - b.position)
        .forEach(card => list.appendChild(renderCard(card)));
    colEl.appendChild(list);

    // 新增卡片按钮
    const addCardBtn = document.createElement("button");
    addCardBtn.className = "add-card-btn";
    addCardBtn.textContent = "+ 添加卡片";
    addCardBtn.onclick = () => openNewCard(col.id);
    colEl.appendChild(addCardBtn);

    // ----- 卡片拖放（列内排序 + 跨列移动）-----
    setupColumnDropZone(colEl, list);

    // ----- 列头拖拽（改变列顺序）-----
    header.addEventListener("dragstart", onColumnDragStart);
    header.addEventListener("dragend", onColumnDragEnd);

    return colEl;
}

function renderCard(card) {
    const el = document.createElement("div");
    el.className = "card priority-" + card.priority;
    el.dataset.cardId = card.id;
    el.draggable = true;

    const titleEl = document.createElement("div");
    titleEl.className = "card-title";
    titleEl.textContent = card.title;
    el.appendChild(titleEl);

    // 描述（首页卡片直接按 HTML 渲染，显示列表/粗体等格式；设最大高度避免撑爆）
    if (card.description) {
        const descEl = document.createElement("div");
        descEl.className = "card-desc";
        descEl.innerHTML = card.description;
        el.appendChild(descEl);
    }

    // 标签
    if (card.labels) {
        const labels = card.labels.split(",").map(s => s.trim()).filter(Boolean);
        if (labels.length) {
            const wrap = document.createElement("div");
            wrap.className = "card-labels";
            labels.forEach(l => {
                const tag = document.createElement("span");
                tag.className = "card-label";
                tag.textContent = l;
                wrap.appendChild(tag);
            });
            el.appendChild(wrap);
        }
    }

    // 底部：优先级 + 截止日期
    const footer = document.createElement("div");
    footer.className = "card-footer";

    const dot = document.createElement("span");
    dot.className = "priority-dot " + card.priority;
    dot.title = "优先级：" + priorityLabel(card.priority);
    footer.appendChild(dot);

    if (card.due_date) {
        const due = document.createElement("span");
        due.className = "card-due " + dueClass(card.due_date);
        due.innerHTML = dueDisplay(card.due_date);
        footer.appendChild(due);
    }
    el.appendChild(footer);

    // 点击编辑
    el.onclick = () => openEditCard(card.id);

    // 拖拽事件
    el.addEventListener("dragstart", onCardDragStart);
    el.addEventListener("dragend", onCardDragEnd);

    return el;
}

function priorityLabel(p) {
    return p === "high" ? "高" : p === "low" ? "低" : "中";
}

function dueClass(dueDate) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const due = new Date(dueDate);
    due.setHours(0, 0, 0, 0);
    const diff = (due - today) / 86400000;
    if (diff < 0) return "overdue";
    if (diff <= 2) return "soon";
    return "";
}

// 拆分 due_date -> { date, time }，兼容纯日期 / 带时间 / 空
function splitDue(value) {
    if (!value) return { date: "", time: "" };
    const parts = value.trim().split(/\s+/);
    return { date: parts[0] || "", time: parts[1] || "" };
}

// 合并日期+时间为单个 due_date 字符串；无日期则视为无截止
function joinDue(date, time) {
    if (!date) return "";            // 没日期 = 无截止
    return time ? (date + " " + time) : date;
}

// 截止日期展示文案（带时间就显示时间）
function dueDisplay(dueDate) {
    const parts = dueDate.trim().split(/\s+/);
    if (parts.length >= 2) return "📅 " + parts[0] + " " + parts[1];
    return "📅 " + parts[0];
}

// ============ 拖拽逻辑 ============
function onCardDragStart(e) {
    drag.cardId = Number(e.currentTarget.dataset.cardId);
    drag.cardEl = e.currentTarget;
    drag.sourceColId = Number(e.currentTarget.closest(".card-list").dataset.colId);
    e.currentTarget.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    // Firefox 需要设置 dataTransfer 才能触发拖拽
    e.dataTransfer.setData("text/plain", String(drag.cardId));
}

function onCardDragEnd(e) {
    e.currentTarget.classList.remove("dragging");
    // 清理占位符和高亮
    document.querySelectorAll(".drop-placeholder").forEach(el => el.remove());
    document.querySelectorAll(".drag-over").forEach(el => el.classList.remove("drag-over"));
    drag.cardId = null;
    drag.cardEl = null;
    drag.sourceColId = null;
}

// ============ 列拖拽逻辑（改变列的顺序）============
function onColumnDragStart(e) {
    // 避免点击列头上的按钮时误触发拖拽
    if (e.target.closest(".column-actions")) {
        e.preventDefault();
        return;
    }
    const colEl = e.currentTarget.closest(".column");
    colDrag.colId = Number(e.currentTarget.dataset.colId);
    colDrag.colEl = colEl;
    colEl.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", "col:" + colDrag.colId);
}

function onColumnDragEnd(e) {
    if (colDrag.colEl) colDrag.colEl.classList.remove("dragging");
    document.querySelectorAll(".board").forEach(b => b.classList.remove("drag-over"));
    colDrag.colId = null;
    colDrag.colEl = null;
}

// 在 board 容器上监听列的拖放（水平方向重排）
function setupBoardColumnDrop() {
    const board = document.getElementById("board");

    board.addEventListener("dragover", (e) => {
        if (colDrag.colId == null) return;   // 只处理列拖拽
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        const after = getColumnDragAfter(board, e.clientX);
        // 临时重排 DOM：把被拖列插到计算出的位置（不刷新数据，纯视觉）
        if (colDrag.colEl) {
            if (after == null) {
                board.appendChild(colDrag.colEl);
            } else if (after !== colDrag.colEl) {
                board.insertBefore(colDrag.colEl, after);
            }
        }
    });

    board.addEventListener("drop", async (e) => {
        if (colDrag.colId == null) return;
        e.preventDefault();
        // 收集当前 DOM 顺序（忽略最后的"新增列"按钮），提交给后端
        const ids = [...board.querySelectorAll(".column")]
            .map(el => Number(el.dataset.colId));
        try {
            await api("POST", "/api/columns/reorder", { ids });
            await refresh();
        } catch (err) {
            toast("列排序失败：" + err.message, true);
            refresh();
        }
    });
}

// 找出鼠标 X 坐标之后应插入的列元素
function getColumnDragAfter(board, x) {
    const cols = [...board.querySelectorAll(".column:not(.dragging)")];
    return cols.reduce((closest, child) => {
        const box = child.getBoundingClientRect();
        const offset = x - box.left - box.width / 2;
        if (offset < 0 && offset > closest.offset) {
            return { offset: offset, element: child };
        }
        return closest;
    }, { offset: Number.NEGATIVE_INFINITY }).element;
}

function setupColumnDropZone(colEl, listEl) {
    // dragover：计算插入位置，显示占位符
    listEl.addEventListener("dragover", (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        colEl.classList.add("drag-over");
        const afterEl = getDragAfterElement(listEl, e.clientY);
        // 移除已有占位符
        let ph = listEl.querySelector(".drop-placeholder");
        if (!ph) {
            ph = document.createElement("div");
            ph.className = "drop-placeholder";
        }
        if (afterEl == null) {
            listEl.appendChild(ph);
        } else {
            listEl.insertBefore(ph, afterEl);
        }
    });

    listEl.addEventListener("dragleave", (e) => {
        // 仅当离开整个列才取消高亮
        if (!colEl.contains(e.relatedTarget)) {
            colEl.classList.remove("drag-over");
        }
    });

    listEl.addEventListener("drop", async (e) => {
        e.preventDefault();
        colEl.classList.remove("drag-over");
        const targetColId = Number(listEl.dataset.colId);
        const ph = listEl.querySelector(".drop-placeholder");
        // 计算目标 position（占位符之前的卡片数量）
        let position = 0;
        if (ph) {
            let node = ph.previousElementSibling;
            while (node) {
                if (node.classList && node.classList.contains("card") && !node.classList.contains("dragging")) {
                    position++;
                }
                node = node.previousElementSibling;
            }
        }
        ph && ph.remove();

        if (drag.cardId == null) return;
        const cardId = drag.cardId;
        const srcCol = drag.sourceColId;

        // 如果位置没变（同列同位置），不请求
        const card = state.cards.find(c => c.id === cardId);
        if (card && card.column_id === targetColId) {
            // 同列：判断 position 是否真的变化
            const colCards = state.cards.filter(c => c.column_id === targetColId).sort((a,b)=>a.position-b.position);
            if (colCards[position] && colCards[position].id === cardId) return;
            // 移到末尾且本来就是最后一个
            if (position >= colCards.length && colCards[colCards.length-1].id === cardId) return;
        }

        try {
            await api("PUT", `/api/cards/${cardId}/move`, {
                column_id: targetColId,
                position: position
            });
            await refresh();
        } catch (err) {
            toast("移动失败：" + err.message, true);
            refresh();
        }
    });
}

// 找出鼠标 Y 坐标之后应插入的元素（返回 null 表示插到末尾）
function getDragAfterElement(container, y) {
    const cards = [...container.querySelectorAll(".card:not(.dragging)")];
    return cards.reduce((closest, child) => {
        const box = child.getBoundingClientRect();
        const offset = y - box.top - box.height / 2;
        if (offset < 0 && offset > closest.offset) {
            return { offset: offset, element: child };
        }
        return closest;
    }, { offset: Number.NEGATIVE_INFINITY }).element;
}

// ============ 卡片 CRUD ============
function openNewCard(colId) {
    state.currentCardId = null;
    state.currentCardCol = colId;
    document.getElementById("modal-title-text").textContent = "新建卡片";
    document.getElementById("card-title").value = "";
    document.getElementById("card-description").innerHTML = "";
    document.getElementById("card-labels").value = "";
    document.getElementById("card-due").value = "";
    document.getElementById("card-due-time").value = "";
    document.getElementById("card-priority").value = "medium";
    document.getElementById("card-meta").textContent = "";
    document.getElementById("card-modal").hidden = false;
    setTimeout(() => document.getElementById("card-title").focus(), 50);
}

function openEditCard(cardId) {
    const card = state.cards.find(c => c.id === cardId);
    if (!card) return;
    state.currentCardId = cardId;
    state.currentCardCol = card.column_id;
    document.getElementById("modal-title-text").textContent = "编辑卡片";
    document.getElementById("card-title").value = card.title;
    document.getElementById("card-description").innerHTML = card.description || "";
    document.getElementById("card-labels").value = card.labels || "";
    // due_date 可能是 "YYYY-MM-DD" 或 "YYYY-MM-DD HH:MM"，拆分填入日期/时间框
    const dueParts = splitDue(card.due_date || "");
    document.getElementById("card-due").value = dueParts.date;
    document.getElementById("card-due-time").value = dueParts.time;
    document.getElementById("card-priority").value = card.priority;
    document.getElementById("card-meta").textContent =
        `创建于 ${card.created_at}　更新于 ${card.updated_at}`;
    document.getElementById("card-modal").hidden = false;
}

function closeCardModal() {
    document.getElementById("card-modal").hidden = true;
}

async function saveCard() {
    const title = document.getElementById("card-title").value.trim();
    if (!title) { toast("标题不能为空", true); return; }
    // 合并日期+时间：有日期才谈截止；时间可选
    const dueDate = document.getElementById("card-due").value;
    const dueTime = document.getElementById("card-due-time").value;
    const dueValue = joinDue(dueDate, dueTime);
    const payload = {
        column_id: state.currentCardCol,
        title,
        description: document.getElementById("card-description").innerHTML.trim(),
        labels: document.getElementById("card-labels").value.trim(),
        due_date: dueValue,
        priority: document.getElementById("card-priority").value
    };
    try {
        if (state.currentCardId == null) {
            await api("POST", "/api/cards", payload);
            toast("已创建");
        } else {
            await api("PUT", `/api/cards/${state.currentCardId}`, payload);
            toast("已保存");
        }
        closeCardModal();
        await refresh();
    } catch (e) {
        toast("保存失败：" + e.message, true);
    }
}

async function deleteCurrentCard() {
    if (state.currentCardId == null) { closeCardModal(); return; }
    if (!confirm("确定归档（删除）此卡片？可在「历史」中恢复。")) return;
    try {
        await api("DELETE", `/api/cards/${state.currentCardId}`);
        toast("已归档");
        closeCardModal();
        await refresh();
    } catch (e) {
        toast("删除失败：" + e.message, true);
    }
}

// ============ 列 CRUD ============
function addColumn() {
    document.getElementById("column-name").value = "";
    document.getElementById("column-modal").hidden = false;
    setTimeout(() => document.getElementById("column-name").focus(), 50);
}

function closeColumnModal() {
    document.getElementById("column-modal").hidden = true;
}

async function saveColumn() {
    const name = document.getElementById("column-name").value.trim();
    if (!name) { toast("列名不能为空", true); return; }
    try {
        await api("POST", "/api/columns", { name });
        closeColumnModal();
        await refresh();
    } catch (e) {
        toast("创建失败：" + e.message, true);
    }
}

async function renameColumn(col) {
    const name = prompt("新列名：", col.name);
    if (name == null) return;
    const trimmed = name.trim();
    if (!trimmed) { toast("列名不能为空", true); return; }
    try {
        await api("PUT", `/api/columns/${col.id}`, { name: trimmed });
        await refresh();
    } catch (e) {
        toast("重命名失败：" + e.message, true);
    }
}

async function deleteColumn(col) {
    if (!confirm(`确定删除列「${col.name}」？该列下的卡片将被归档（保留历史）。`)) return;
    try {
        await api("DELETE", `/api/columns/${col.id}`);
        await refresh();
    } catch (e) {
        toast("删除失败：" + e.message, true);
    }
}

// ============ 历史搜索 ============
async function doSearch() {
    const q = document.getElementById("search-q").value.trim();
    const priority = document.getElementById("search-priority").value;
    const from = document.getElementById("search-from").value;
    const to = document.getElementById("search-to").value;
    const all = document.getElementById("search-all").checked ? 1 : 0;

    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (priority) params.set("priority", priority);
    if (from) params.set("from", from);
    if (to) params.set("to", to);
    if (all) params.set("all", 1);

    try {
        const results = await api("GET", "/api/search?" + params.toString());
        const meta = document.getElementById("search-meta");
        const box = document.getElementById("search-results");
        meta.textContent = `找到 ${results.length} 条结果`;
        box.innerHTML = "";
        if (results.length === 0) {
            box.innerHTML = '<div style="color:#5e6c84;text-align:center;padding:32px;">无匹配结果</div>';
            return;
        }
        const colMap = {};
        state.columns.forEach(c => colMap[c.id] = c.name);
        results.forEach(card => {
            box.appendChild(renderResult(card, colMap));
        });
    } catch (e) {
        toast("搜索失败：" + e.message, true);
    }
}

function renderResult(card, colMap) {
    const el = document.createElement("div");
    el.className = "result-item priority-" + card.priority;

    const title = document.createElement("div");
    title.className = "result-title";
    title.textContent = card.title + (card.archived ? "  [已归档]" : "");
    el.appendChild(title);

    if (card.description) {
        const preview = stripHtml(card.description);
        if (preview) {
            const desc = document.createElement("div");
            desc.className = "result-desc";
            desc.textContent = preview;
            el.appendChild(desc);
        }
    }

    const meta = document.createElement("div");
    meta.className = "result-meta";
    const parts = [];
    parts.push("优先级：" + priorityLabel(card.priority));
    if (card.labels) parts.push("标签：" + card.labels);
    if (card.due_date) parts.push("截止：" + card.due_date);
    if (colMap[card.column_id]) parts.push("原列：" + colMap[card.column_id]);
    parts.push("更新：" + card.updated_at);
    meta.textContent = parts.join("　|　");
    el.appendChild(meta);

    if (card.archived) {
        const actions = document.createElement("div");
        actions.className = "result-actions";
        const btn = document.createElement("button");
        btn.textContent = "↩ 恢复到看板";
        btn.onclick = () => restoreCard(card.id);
        actions.appendChild(btn);
        el.appendChild(actions);
    }
    return el;
}

async function restoreCard(cardId) {
    try {
        await api("POST", `/api/cards/${cardId}/restore`);
        toast("已恢复");
        await doSearch();
        await refresh();
    } catch (e) {
        toast("恢复失败：" + e.message, true);
    }
}

function resetSearch() {
    document.getElementById("search-q").value = "";
    document.getElementById("search-priority").value = "";
    document.getElementById("search-from").value = "";
    document.getElementById("search-to").value = "";
    document.getElementById("search-all").checked = false;
    document.getElementById("search-results").innerHTML = "";
    document.getElementById("search-meta").textContent = "";
}

// ============ 导出 / 备份 ============
function exportJson() {
    // 直接触发浏览器下载（后端已带 Content-Disposition）
    const a = document.createElement("a");
    a.href = "/api/export";
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("正在导出 JSON…");
}

function backupDb() {
    const a = document.createElement("a");
    a.href = "/api/backup";
    a.download = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("正在下载数据库备份…");
}

// ============ 初始化 ============
document.addEventListener("DOMContentLoaded", () => {
    refresh();
    setupBoardColumnDrop();
    setupEditorToolbar();
    // 回车保存卡片
    document.getElementById("card-title").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && e.ctrlKey) saveCard();
    });
    // ESC 关闭弹窗
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            if (!document.getElementById("card-modal").hidden) closeCardModal();
            if (!document.getElementById("column-modal").hidden) closeColumnModal();
        }
    });
});

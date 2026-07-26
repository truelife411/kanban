export function localDateKey(date = new Date()) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

export function dueDatePart(value) { return (value || "").trim().slice(0, 10); }
export function isToday(card) { return dueDatePart(card.due_date) === localDateKey(); }
export function isTomorrow(card) { const t=new Date(); t.setDate(t.getDate()+1); return dueDatePart(card.due_date) === localDateKey(t); }

export function isOverdue(card, referenceDate) {
    const now = referenceDate instanceof Date ? referenceDate : new Date();
    const value = (card.due_date || "").trim();
    if (!value) return false;
    if (value.length === 16) {
        const [date, clock] = value.split(" ");
        const [year, month, day] = date.split("-").map(Number);
        const [hour, minute] = clock.split(":").map(Number);
        return new Date(year, month - 1, day, hour, minute).getTime() < now.getTime();
    }
    return dueDatePart(value) < localDateKey(now);
}

export function calendarDayNumber(value) {
    const parts = dueDatePart(value).split("-").map(Number);
    return parts.length === 3 && parts.every(Number.isFinite) ? Date.UTC(parts[0], parts[1] - 1, parts[2]) / 86400000 : null;
}

export function dueClass(value) {
    if (isOverdue({ due_date: value })) return "overdue";
    const target = calendarDayNumber(value);
    const today = calendarDayNumber(localDateKey());
    return target != null && today != null && target >= today && target - today <= 2 ? "soon" : "";
}

export function splitDue(value) {
    const parts = (value || "").trim().split(/\s+/);
    return { date: parts[0] || "", time: parts[1] || "" };
}

export function joinDue(date, time) { return date ? date + (time ? ` ${time}` : "") : ""; }
export function dueDisplay(value) { return value.trim(); }

export function dueSortTimestamp(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}):(\d{2}))?$/.exec((value || "").trim());
    if (!match) return null;
    const year = Number(match[1]), month = Number(match[2]), day = Number(match[3]);
    const hour = match[4] == null ? 23 : Number(match[4]), minute = match[5] == null ? 59 : Number(match[5]);
    const date = new Date(year, month - 1, day, hour, minute);
    if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day || date.getHours() !== hour || date.getMinutes() !== minute) return null;
    return date.getTime();
}

export function parseLocalTimestamp(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec((value || "").trim());
    if (!match) return null;
    const parts = match.slice(1).map(Number);
    const date = new Date(parts[0], parts[1] - 1, parts[2], parts[3], parts[4], parts[5]);
    if (date.getFullYear() !== parts[0] || date.getMonth() !== parts[1] - 1 || date.getDate() !== parts[2] || date.getHours() !== parts[3] || date.getMinutes() !== parts[4] || date.getSeconds() !== parts[5]) return null;
    return date;
}

export function fullTimestamp(value) {
    const date = parseLocalTimestamp(value);
    if (!date) return value || "未知";
    return `${localDateKey(date)} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}:${String(date.getSeconds()).padStart(2, "0")}`;
}

export function compactCreatedTime(value, referenceDate = new Date()) {
    const date = parseLocalTimestamp(value);
    if (!date) return "未知";
    if (date.getFullYear() === referenceDate.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日`;
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

export function relativeTimestamp(value, referenceDate = new Date()) {
    const date = parseLocalTimestamp(value);
    if (!date) return "未知";
    const seconds = Math.max(0, Math.floor((referenceDate.getTime() - date.getTime()) / 1000));
    if (seconds < 60) return "刚刚";
    if (seconds < 3600) return `${Math.floor(seconds / 60)}分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}小时前`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}天前`;
    return compactCreatedTime(value, referenceDate);
}

let pickerState = null;

function parseDateValue(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!match) return null;
    const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    return localDateKey(date) === value ? date : null;
}

function dateAllowed(value, input) {
    return Boolean(value) && (!input.min || value >= input.min) && (!input.max || value <= input.max);
}

function dispatchPickerInput(input) {
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
}

function closeOwnedPicker(restoreFocus) {
    if (!pickerState) return;
    const state = pickerState;
    pickerState = null;
    state.overlay.remove();
    if (restoreFocus && state.opener && state.opener.isConnected) state.opener.focus();
}

function pickerButton(text, className, handler) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    if (className) button.className = className;
    button.addEventListener("click", handler);
    return button;
}

function buildPickerFrame(input, opener, title) {
    closeOwnedPicker(false);
    const overlay = document.createElement("div");
    overlay.className = "owned-picker-overlay";
    const dialog = document.createElement("div");
    dialog.className = "owned-picker";
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    const heading = document.createElement("h3");
    heading.id = `owned-picker-title-${Date.now()}`;
    heading.textContent = title;
    dialog.setAttribute("aria-labelledby", heading.id);
    const body = document.createElement("div");
    body.className = "owned-picker-body";
    const actions = document.createElement("div");
    actions.className = "owned-picker-actions";
    dialog.append(heading, body, actions);
    overlay.appendChild(dialog);
    document.body.appendChild(overlay);
    pickerState = { overlay, dialog, input, opener, body, actions };
    overlay.addEventListener("mousedown", event => { if (event.target === overlay) event.preventDefault(); });
    overlay.addEventListener("click", event => { if (event.target === overlay) closeOwnedPicker(true); });
    dialog.addEventListener("keydown", event => {
        if (event.key === "Escape") { event.preventDefault(); closeOwnedPicker(true); return; }
        if (event.key !== "Tab") return;
        const items = [...dialog.querySelectorAll("button:not([disabled]),select:not([disabled])")].filter(node => !node.hidden);
        if (!items.length) return;
        const first = items[0], last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    });
    return pickerState;
}

function renderDateCalendar(state) {
    const input = state.input, selected = state.selected ? localDateKey(state.selected) : "";
    state.monthLabel.textContent = `${state.viewDate.getFullYear()}年${state.viewDate.getMonth() + 1}月`;
    state.grid.querySelectorAll("button").forEach(node => node.remove());
    const first = new Date(state.viewDate.getFullYear(), state.viewDate.getMonth(), 1);
    const start = new Date(first.getFullYear(), first.getMonth(), 1 - first.getDay());
    for (let index = 0; index < 42; index += 1) {
        const date = new Date(start.getFullYear(), start.getMonth(), start.getDate() + index);
        const value = localDateKey(date), button = document.createElement("button");
        button.type = "button";
        button.className = "owned-picker-day";
        button.textContent = String(date.getDate());
        button.dataset.date = value;
        button.setAttribute("aria-label", `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`);
        button.setAttribute("aria-selected", String(value === selected));
        if (date.getMonth() !== state.viewDate.getMonth()) button.classList.add("outside-month");
        if (value === localDateKey()) button.classList.add("today");
        button.disabled = !dateAllowed(value, input);
        button.addEventListener("click", () => { state.selected = date; renderDateCalendar(state); button.focus(); });
        button.addEventListener("keydown", event => {
            const offsets = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
            let next = null;
            if (Object.prototype.hasOwnProperty.call(offsets, event.key)) next = new Date(date.getFullYear(), date.getMonth(), date.getDate() + offsets[event.key]);
            else if (event.key === "Home") next = new Date(date.getFullYear(), date.getMonth(), date.getDate() - date.getDay());
            else if (event.key === "End") next = new Date(date.getFullYear(), date.getMonth(), date.getDate() + 6 - date.getDay());
            else if (event.key === "PageUp" || event.key === "PageDown") next = new Date(date.getFullYear(), date.getMonth() + (event.key === "PageUp" ? -1 : 1), date.getDate());
            else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); button.click(); return; }
            if (!next) return;
            event.preventDefault();
            const nextValue = localDateKey(next);
            if (!dateAllowed(nextValue, input)) return;
            state.viewDate = new Date(next.getFullYear(), next.getMonth(), 1);
            renderDateCalendar(state);
            requestAnimationFrame(() => state.grid.querySelector(`[data-date="${nextValue}"]`)?.focus());
        });
        state.grid.appendChild(button);
    }
}

function openDatePicker(input, opener) {
    const state = buildPickerFrame(input, opener, input.getAttribute("aria-label") || "选择日期");
    const initial = parseDateValue(input.value) || new Date();
    state.selected = parseDateValue(input.value);
    state.viewDate = new Date(initial.getFullYear(), initial.getMonth(), 1);
    const header = document.createElement("div");
    header.className = "owned-picker-calendar-header";
    const previous = pickerButton("‹", "owned-picker-nav", () => { state.viewDate.setMonth(state.viewDate.getMonth() - 1); renderDateCalendar(state); });
    previous.setAttribute("aria-label", "上个月");
    state.monthLabel = document.createElement("strong");
    state.monthLabel.setAttribute("aria-live", "polite");
    const next = pickerButton("›", "owned-picker-nav", () => { state.viewDate.setMonth(state.viewDate.getMonth() + 1); renderDateCalendar(state); });
    next.setAttribute("aria-label", "下个月");
    header.append(previous, state.monthLabel, next);
    const weekdays = document.createElement("div");
    weekdays.className = "owned-picker-weekdays";
    for (const day of ["日", "一", "二", "三", "四", "五", "六"]) { const span = document.createElement("span"); span.textContent = day; weekdays.appendChild(span); }
    state.grid = document.createElement("div");
    state.grid.className = "owned-picker-grid";
    state.grid.setAttribute("role", "grid");
    state.body.append(header, weekdays, state.grid);
    const today = localDateKey();
    const todayButton = pickerButton("今天", "ghost", () => { if (dateAllowed(today, input)) { state.selected = new Date(); state.viewDate = new Date(new Date().getFullYear(), new Date().getMonth(), 1); renderDateCalendar(state); } });
    todayButton.disabled = !dateAllowed(today, input);
    const clear = pickerButton("清除", "ghost", () => { input.value = ""; dispatchPickerInput(input); closeOwnedPicker(true); });
    const cancel = pickerButton("取消", "ghost", () => closeOwnedPicker(true));
    const confirm = pickerButton("确定", "primary", () => { if (!state.selected) return; input.value = localDateKey(state.selected); dispatchPickerInput(input); closeOwnedPicker(true); });
    state.actions.append(todayButton, clear, cancel, confirm);
    renderDateCalendar(state);
    requestAnimationFrame(() => {
        const target = state.grid.querySelector(`[data-date="${input.value || today}"]:not([disabled])`) || state.grid.querySelector("button:not([disabled])");
        if (target) target.focus(); else cancel.focus();
    });
}

function timeAllowed(value, input) {
    return Boolean(value) && (!input.min || value >= input.min) && (!input.max || value <= input.max);
}

function openTimePicker(input, opener) {
    const state = buildPickerFrame(input, opener, input.getAttribute("aria-label") || "选择时间");
    const match = /^(\d{2}):(\d{2})$/.exec(input.value || "");
    const now = new Date();
    const fields = document.createElement("div");
    fields.className = "owned-time-fields";
    const hour = document.createElement("select"), minute = document.createElement("select");
    hour.setAttribute("aria-label", "小时"); minute.setAttribute("aria-label", "分钟");
    for (let value = 0; value < 24; value += 1) { const option = document.createElement("option"); option.value = String(value).padStart(2, "0"); option.textContent = option.value; hour.appendChild(option); }
    for (let value = 0; value < 60; value += 1) { const option = document.createElement("option"); option.value = String(value).padStart(2, "0"); option.textContent = option.value; minute.appendChild(option); }
    hour.value = match ? match[1] : String(now.getHours()).padStart(2, "0");
    minute.value = match ? match[2] : String(now.getMinutes()).padStart(2, "0");
    const separator = document.createElement("span"); separator.textContent = ":"; separator.setAttribute("aria-hidden", "true");
    fields.append(hour, separator, minute); state.body.appendChild(fields);
    const setNow = pickerButton("现在", "ghost", () => { const current = new Date(), value = `${String(current.getHours()).padStart(2, "0")}:${String(current.getMinutes()).padStart(2, "0")}`; if (timeAllowed(value, input)) { hour.value = value.slice(0, 2); minute.value = value.slice(3); } });
    setNow.disabled = !timeAllowed(`${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`, input);
    const clear = pickerButton("清除", "ghost", () => { input.value = ""; dispatchPickerInput(input); closeOwnedPicker(true); });
    const cancel = pickerButton("取消", "ghost", () => closeOwnedPicker(true));
    const confirm = pickerButton("确定", "primary", () => { const value = `${hour.value}:${minute.value}`; if (!timeAllowed(value, input)) return; input.value = value; dispatchPickerInput(input); closeOwnedPicker(true); });
    state.actions.append(setNow, clear, cancel, confirm);
    requestAnimationFrame(() => hour.focus());
}

export function openDateTimePicker(id, opener) {
    const input = document.getElementById(id);
    if (!input || input.disabled || input.readOnly) return;
    const trigger = opener || document.activeElement || input;
    if (input.type === "time") openTimePicker(input, trigger);
    else openDatePicker(input, trigger);
}

function syncShell(input, shellSelector, clearId, className) {
    const shell = input?.closest(shellSelector);
    const clear = document.getElementById(clearId);
    if (!input || !shell || !clear) return;
    const hasValue = Boolean(input.value);
    shell.classList.toggle(className, hasValue);
    clear.hidden = !hasValue;
}

export function syncCardDateControl() { syncShell(document.getElementById("card-due"), ".date-shell-calendar", "card-date-clear", "has-date"); }
export function syncCardTimeControl() { syncShell(document.getElementById("card-due-time"), ".date-shell-time", "card-time-clear", "has-time"); }
export function clearCardDate() { const input = document.getElementById("card-due"); input.value = ""; syncCardDateControl(); input.focus(); }
export function clearCardTime() { const input = document.getElementById("card-due-time"); input.value = ""; syncCardTimeControl(); input.focus(); }

export function syncHistoryDateControl(changed) {
    const groups = [
        { key: "from", pair: "to", fromId: "search-from", toId: "search-to" },
        { key: "created_from", pair: "created_to", fromId: "search-created-from", toId: "search-created-to" },
        { key: "updated_from", pair: "updated_to", fromId: "search-updated-from", toId: "search-updated-to" },
    ];
    for (const group of groups) {
        const from = document.getElementById(group.fromId);
        const to = document.getElementById(group.toId);
        for (const [key, input] of [[group.key, from], [group.pair, to]]) syncShell(input, ".history-date-shell", `search-${key}-clear`.replace(/_/g, "-"), "has-date");
        to.min = from.value || "";
        from.max = to.value || "";
        if (changed === group.key && from.value && to.value && from.value > to.value) to.value = from.value;
        if (changed === group.pair && from.value && to.value && from.value > to.value) from.value = to.value;
    }
}

export function clearHistoryDate(which) {
    const id = `search-${which.replace(/_/g, "-")}`;
    const input = document.getElementById(id);
    input.value = "";
    syncHistoryDateControl(which);
    input.focus();
}

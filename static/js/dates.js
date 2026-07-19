export function localDateKey(date = new Date()) {
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

export function dueDatePart(value) { return (value || "").trim().slice(0, 10); }
export function isToday(card) { return dueDatePart(card.due_date) === localDateKey(); }

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
export function dueDisplay(value) { return `📅 ${value.trim()}`; }

export function openNativePicker(id) {
    const input = document.getElementById(id);
    if (!input) return;
    input.focus();
    if (typeof input.showPicker === "function") {
        try { input.showPicker(); } catch (_) { input.click(); }
    } else input.click();
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
    const from = document.getElementById("search-from");
    const to = document.getElementById("search-to");
    for (const [key, input] of [["from", from], ["to", to]]) syncShell(input, ".history-date-shell", `search-${key}-clear`, "has-date");
    to.min = from.value || "";
    from.max = to.value || "";
    if (changed === "from" && from.value && to.value && from.value > to.value) to.value = from.value;
    if (changed === "to" && from.value && to.value && from.value > to.value) from.value = to.value;
}

export function clearHistoryDate(which) {
    const input = document.getElementById(`search-${which}`);
    input.value = "";
    syncHistoryDateControl(which);
    input.focus();
}

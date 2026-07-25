export function setupButtonTooltips() {
    const tooltip = document.createElement("div");
    tooltip.id = "button-tooltip";
    tooltip.className = "button-tooltip";
    tooltip.setAttribute("role", "tooltip");
    tooltip.hidden = true;
    document.body.appendChild(tooltip);
    let activeButton = null;
    let previousDescribedBy = null;
    let showTimer = null;

    const normalizedText = value => (value || "").replace(/\s+/g, " ").trim();
    const visibleButtonText = button => normalizedText(button.innerText);
    const migrateTitle = button => {
        const title = normalizedText(button.getAttribute("title"));
        if (title) {
            if (!button.dataset.tooltip) button.dataset.tooltip = title;
            button.removeAttribute("title");
        }
    };
    document.querySelectorAll("button[title]").forEach(migrateTitle);
    const tooltipText = button => {
        if (!button || button.disabled) return "";
        migrateTitle(button);
        const text = normalizedText(button.dataset.tooltip) || normalizedText(button.getAttribute("aria-label"));
        return text && text !== visibleButtonText(button) ? text : "";
    };
    const hide = () => {
        clearTimeout(showTimer);
        showTimer = null;
        if (activeButton) {
            if (previousDescribedBy) activeButton.setAttribute("aria-describedby", previousDescribedBy);
            else activeButton.removeAttribute("aria-describedby");
        }
        activeButton = null;
        previousDescribedBy = null;
        tooltip.hidden = true;
    };
    const position = button => {
        const box = button.getBoundingClientRect();
        const halfWidth = tooltip.offsetWidth / 2;
        const left = Math.min(Math.max(box.left + box.width / 2, halfWidth + 12), window.innerWidth - halfWidth - 12);
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${box.bottom + 8}px`;
        tooltip.classList.toggle("above", box.bottom + tooltip.offsetHeight + 16 > window.innerHeight);
        if (tooltip.classList.contains("above")) tooltip.style.top = `${box.top - 8}px`;
    };
    const show = (button, immediate = false) => {
        const text = tooltipText(button);
        if (!text) { hide(); return; }
        hide();
        activeButton = button;
        previousDescribedBy = button.getAttribute("aria-describedby");
        showTimer = setTimeout(() => {
            if (activeButton !== button || !button.isConnected) return;
            tooltip.textContent = text;
            tooltip.hidden = false;
            button.setAttribute("aria-describedby", [previousDescribedBy, tooltip.id].filter(Boolean).join(" "));
            position(button);
        }, immediate ? 0 : 500);
    };
    document.addEventListener("pointerover", event => {
        const button = event.target.closest("button");
        if (!button || button.contains(event.relatedTarget)) return;
        show(button);
    });
    document.addEventListener("pointerout", event => {
        const button = event.target.closest("button");
        if (button && !button.contains(event.relatedTarget)) hide();
    });
    document.addEventListener("focusin", event => {
        const button = event.target.closest("button");
        if (button) show(button, true);
    });
    document.addEventListener("focusout", event => {
        if (event.target.closest("button")) hide();
    });
    document.addEventListener("pointerdown", hide, true);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
}

export function clearChildren(node) {
    if (typeof node.replaceChildren === "function") node.replaceChildren();
    else while (node.firstChild) node.removeChild(node.firstChild);
}

export function toast(message, options = {}) {
    if (typeof options === "boolean") options = { error: options };
    const box = document.getElementById("toast");
    clearChildren(box);
    const text = document.createElement("span");
    text.textContent = message;
    box.appendChild(text);
    if (options.actionText && options.onAction) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "toast-action";
        button.textContent = options.actionText;
        button.addEventListener("click", async () => {
            button.disabled = true;
            try { await options.onAction(); } finally { box.hidden = true; }
        });
        box.appendChild(button);
    }
    box.className = `toast${options.error ? " error" : ""}`;
    box.setAttribute("role", options.error ? "alert" : "status");
    box.hidden = false;
    clearTimeout(box._timer);
    box._timer = setTimeout(() => { box.hidden = true; }, options.duration || 2500);
}

export function announce(message) {
    const region = document.getElementById("live-region");
    region.textContent = "";
    setTimeout(() => { region.textContent = message; }, 10);
}

export function formatBytes(value) {
    if (value < 1024) return `${value} B`;
    if (value < 1048576) return `${(value / 1024).toFixed(value < 10240 ? 1 : 0)} KB`;
    if (value < 1073741824) return `${(value / 1048576).toFixed(1)} MB`;
    return `${(value / 1073741824).toFixed(1)} GB`;
}

export function setBusy(button, busy, label) {
    if (!button) return;
    if (busy && !button.dataset.originalText) button.dataset.originalText = button.textContent;
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
    button.textContent = busy ? label : button.dataset.originalText || button.textContent;
}

export function openModal(id, state, opener, focusId) {
    state.modalOpener = opener || document.activeElement;
    document.getElementById(id).hidden = false;
    document.querySelectorAll(".topbar, main").forEach(node => { node.inert = true; });
    requestAnimationFrame(() => document.getElementById(focusId)?.focus());
}

export function hideModal(id, state) {
    document.getElementById(id).hidden = true;
    document.querySelectorAll(".topbar, main").forEach(node => { node.inert = false; });
    const opener = state.modalOpener;
    state.modalOpener = null;
    if (opener?.isConnected) opener.focus();
}

export function chooseAction({ title, message, primary, secondary, danger = false, primaryClass, focus = "primary", opener, restoreFocus = true }) {
    return new Promise(resolve => {
        const overlay = document.getElementById("choice-modal");
        const restoreFocusTo = opener || document.activeElement;
        document.getElementById("choice-title").textContent = title;
        document.getElementById("choice-message").textContent = message;
        const primaryButton = document.getElementById("choice-primary");
        const secondaryButton = document.getElementById("choice-secondary");
        const cancelButton = document.getElementById("choice-cancel");
        primaryButton.textContent = primary;
        secondaryButton.textContent = secondary;
        primaryButton.className = primaryClass || (danger ? "danger-action" : "primary");
        secondaryButton.hidden = !secondary;
        const inertTargets = [...document.querySelectorAll(".topbar, main, .modal-overlay:not([hidden]), .owned-picker-overlay:not([hidden])")].filter(node => node !== overlay);
        const inertState = inertTargets.map(node => [node, node.inert]);
        overlay.hidden = false;
        inertState.forEach(([node]) => { node.inert = true; });
        const finish = value => {
            overlay.hidden = true;
            inertState.forEach(([node, inert]) => { node.inert = inert; });
            primaryButton.onclick = null;
            secondaryButton.onclick = null;
            cancelButton.onclick = null;
            primaryButton.className = "primary";
            secondaryButton.hidden = false;
            if (restoreFocus && restoreFocusTo?.isConnected) restoreFocusTo.focus();
            resolve(value);
        };
        primaryButton.onclick = () => finish("primary");
        secondaryButton.onclick = () => finish("secondary");
        cancelButton.onclick = () => finish("cancel");
        requestAnimationFrame(() => (focus === "cancel" ? cancelButton : primaryButton).focus());
    });
}

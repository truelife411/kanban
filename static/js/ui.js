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

export function chooseAction({ title, message, primary, secondary }) {
    return new Promise(resolve => {
        const overlay = document.getElementById("choice-modal");
        document.getElementById("choice-title").textContent = title;
        document.getElementById("choice-message").textContent = message;
        const primaryButton = document.getElementById("choice-primary");
        const secondaryButton = document.getElementById("choice-secondary");
        primaryButton.textContent = primary;
        secondaryButton.textContent = secondary;
        overlay.hidden = false;
        const finish = value => {
            overlay.hidden = true;
            primaryButton.onclick = null;
            secondaryButton.onclick = null;
            document.getElementById("choice-cancel").onclick = null;
            resolve(value);
        };
        primaryButton.onclick = () => finish("primary");
        secondaryButton.onclick = () => finish("secondary");
        document.getElementById("choice-cancel").onclick = () => finish("cancel");
        primaryButton.focus();
    });
}

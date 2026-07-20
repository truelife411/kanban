(function () {
    try {
        var theme = localStorage.getItem("kanban-theme");
        if (["douban-classic", "dark-tech", "office", "cloud-blue", "aurora-blue"].indexOf(theme) >= 0) {
            document.documentElement.setAttribute("data-theme", theme);
        }
    } catch (_) {}
}());

(function () {
    var defaultTheme = "mint";
    var themes = ["mint", "douban-green", "swiss-mono", "sea-salt-blue", "oat", "pearl", "liquid-glass", "deep-sea-night", "aurora", "aurora-glass", "pixel-arcade", "forest-night", "mist-pine-night", "ink-wash"];
    var legacyThemes = {
        "cloud-blue": "sea-salt-blue",
        "navy-blue": "sea-salt-blue",
        "aurora-blue": "sea-salt-blue",
        "douban-classic": "douban-green",
        "douban-modern": "douban-green",
        "office": "swiss-mono",
        "dark-tech": "deep-sea-night"
    };
    try {
        var stored = localStorage.getItem("kanban-theme");
        var theme = legacyThemes[stored] || stored;
        if (themes.indexOf(theme) < 0) theme = defaultTheme;
        document.documentElement.setAttribute("data-theme", theme);
        if (theme !== stored) localStorage.setItem("kanban-theme", theme);
    } catch (_) {
        document.documentElement.setAttribute("data-theme", defaultTheme);
    }
}());

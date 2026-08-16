(function () {
    // 主题单一数据源:设置页选项、主题校验、legacy 映射均从此处读取。
    var themes = [
        { value: "mint", label: "薄荷绿", swatch: "theme-swatch-mint" },
        { value: "douban-green", label: "豆瓣绿", swatch: "theme-swatch-douban" },
        { value: "swiss-mono", label: "黑白", swatch: "theme-swatch-swiss" },
        { value: "sea-salt-blue", label: "海盐蓝", swatch: "theme-swatch-sea" },
        { value: "oat", label: "燕麦", swatch: "theme-swatch-oat" },
        { value: "pearl", label: "珍珠", swatch: "theme-swatch-pearl" },
        { value: "liquid-glass", label: "玻璃", swatch: "theme-swatch-glass" },
        { value: "deep-sea-night", label: "深海夜", swatch: "theme-swatch-night" },
        { value: "aurora", label: "极光", swatch: "theme-swatch-aurora" },
        { value: "aurora-glass", label: "极光玻璃", swatch: "theme-swatch-aurora-glass" },
        { value: "pixel-arcade", label: "像素街机", swatch: "theme-swatch-pixel-arcade" },
        { value: "forest-night", label: "森林夜", swatch: "theme-swatch-forest-night" },
        { value: "mist-pine-night", label: "雾凇夜", swatch: "theme-swatch-mist-pine-night" },
        { value: "ink-wash", label: "水墨山水", swatch: "theme-swatch-ink-wash" }
    ];
    var legacyThemes = {
        "cloud-blue": "sea-salt-blue",
        "navy-blue": "sea-salt-blue",
        "aurora-blue": "sea-salt-blue",
        "douban-classic": "douban-green",
        "douban-modern": "douban-green",
        "office": "swiss-mono",
        "dark-tech": "deep-sea-night"
    };
    window.__KANBAN_THEMES__ = themes;
    window.__KANBAN_LEGACY_THEMES__ = legacyThemes;
    var defaultTheme = "mint";
    var values = {};
    for (var i = 0; i < themes.length; i++) values[themes[i].value] = true;
    try {
        var stored = localStorage.getItem("kanban-theme");
        var theme = legacyThemes[stored] || stored;
        if (!values[theme]) theme = defaultTheme;
        document.documentElement.setAttribute("data-theme", theme);
        if (theme !== stored) localStorage.setItem("kanban-theme", theme);
    } catch (_) {
        document.documentElement.setAttribute("data-theme", defaultTheme);
    }
}());

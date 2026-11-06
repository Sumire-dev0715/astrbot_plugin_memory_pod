"""管理页的样式：文字对比度、不用透明度淡化文字、窄屏规则。

对比度按 WCAG 的相对亮度公式算，口径是「同一条规则里同时写了文字色和底色」的组合，
浅色、深色两套变量各算一遍，普通文字要 ≥ 4.5:1。
透明度会把文字色和底色混在一起拉低对比度（审查里量到过选中芯片的数量只有 3.3:1），
所以 style.css 里除了"禁用的按钮"和"关闭图标"之外，不允许用 opacity 淡化东西。
"""

import re
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "pages" / "memory"
CSS = (PAGE / "style.css").read_text("utf-8")


def _block(selector: str) -> str:
    start = CSS.index(selector + " {")
    return CSS[start : CSS.index("\n}", start)]


def _vars(text: str) -> dict[str, str]:
    return {name: value.strip() for name, value in re.findall(r"--([a-z0-9-]+):\s*([^;]+);", text)}


LIGHT = _vars(_block(":root"))
DARK = {**LIGHT, **_vars(_block(':root[data-theme="dark"]'))}


def _rgb(color: str, theme: dict[str, str]):
    color = color.strip()
    var = re.fullmatch(r"var\(--([a-z0-9-]+)\)", color)
    if var:
        return _rgb(theme[var.group(1)], theme)
    hexa = re.fullmatch(r"#([0-9a-fA-F]{6})", color)
    if hexa:
        return tuple(int(hexa.group(1)[i : i + 2], 16) for i in (0, 2, 4))
    short = re.fullmatch(r"#([0-9a-fA-F]{3})", color)
    if short:
        return tuple(int(ch * 2, 16) for ch in short.group(1))
    return None


def _luminance(rgb) -> float:
    def channel(value: int) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ratio(a, b) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _rules() -> list[tuple[str, str]]:
    # 嵌在 @media 里的规则也会被匹配到（外层的 @media 行因为含有内层的花括号不会匹配）
    return [(" ".join(sel.split()), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS)]


def _declared(body: str):
    color = re.search(r"(?:^|[;\s])color:\s*([^;]+);", body)
    background = re.search(r"background(?:-color)?:\s*([^;]+);", body)
    return (color.group(1) if color else None), (background.group(1) if background else None)


def test_text_and_background_pairs_in_one_rule_meet_aa_in_both_themes():
    dark_prefix = ':root[data-theme="dark"] '
    pairs = []
    dark_overrides = {}
    for selector, body in _rules():
        color, background = _declared(body)
        if selector.startswith(dark_prefix):
            # 深色主题下单独改了颜色的规则，覆盖同名的基础规则
            dark_overrides[selector[len(dark_prefix) :]] = (color, background)
        elif color and background:
            pairs.append((selector, color, background))
    assert len(pairs) > 25, "应当能解析出相当多的颜色组合"
    failures = []
    for name, theme in (("light", LIGHT), ("dark", DARK)):
        for selector, color, background in pairs:
            if name == "dark" and selector in dark_overrides:
                over_color, over_background = dark_overrides[selector]
                color, background = over_color or color, over_background or background
            fg, bg = _rgb(color, theme), _rgb(background, theme)
            if fg is None or bg is None:
                continue
            ratio = _ratio(fg, bg)
            if ratio < 4.5:
                failures.append((name, round(ratio, 2), selector, color, background))
    assert not failures, failures


def test_dimmed_text_pairs_that_replaced_opacity_meet_aa():
    """选中芯片的数量、已撤销的整理记录：原来靠 opacity 淡化，现在用明确的颜色。"""
    explicit = [
        ("accent", "accent-soft"),  # 选中芯片里的数量，和标签同色
        ("text-3", "surface-2"),  # 未选中芯片里的数量、已撤销记录里的标签
        ("text-2", "surface"),  # 已撤销记录里的文字
        ("warn", "warn-soft"),  # 「已撤销于」标签
    ]
    for name, theme in (("light", LIGHT), ("dark", DARK)):
        for fg, bg in explicit:
            ratio = _ratio(_rgb(f"var(--{fg})", theme), _rgb(f"var(--{bg})", theme))
            assert ratio >= 4.5, (name, fg, bg, round(ratio, 2))


def test_opacity_is_not_used_to_dim_text():
    allowed = {
        # 禁用的控件不要求对比度（WCAG 1.4.3）
        '.btn:disabled,\n.btn[aria-disabled="true"]',
        # 关闭图标是图形不是文字，仍在 3:1 以上
        ".toast-close",
        ".toast-close:hover",
    }
    used = set()
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        selector, body = match.group(1).strip(), match.group(2)
        if re.search(r"(^|[;\s])opacity:", body):
            used.add(selector)
    assert used <= allowed, sorted(used - allowed)
    # 这两条原来用透明度，现在不能再有
    for selector in (".chip-count", ".run.is-undone"):
        for sel, body in _rules():
            if sel.startswith(selector):
                assert "opacity" not in body, sel

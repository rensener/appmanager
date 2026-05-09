"""格式化工具 — 大小、时间等人类可读的转换。

这些函数纯靠计算，不涉及系统调用，速度极快。
"""


def format_size(size_kb: int) -> str:
    """将 KB 转为人类可读的大小字符串。

    阈值说明：
      1024 * 1024 = 1048576 KB = 1 GB  → 显示 "x.x GB"
      1024          = 1024 KB = 1 MB    → 显示 "x.x MB"
      > 0                               → 显示 "x KB"
      0                                 → 返回空字符串（不显示大小）

    为什么保留一位小数？
      整数不够精确。如 12.3 MB vs 12 MB，差 300 KB。
      一位小数足够（磁盘大小本来就有误差）。

    为什么 >= 1024*1024 而不是 > 1024*1023？
      边界值 1048576 KB 正好是 1.0 GB，用 >= 更直观。
    """
    if size_kb >= 1024 * 1024:
        return f"{size_kb / (1024 * 1024):.1f} GB"
    elif size_kb >= 1024:
        return f"{size_kb / 1024:.1f} MB"
    elif size_kb > 0:
        return f"{size_kb} KB"
    return ""


def format_count(n: int, singular: str, plural: str = "") -> str:
    """根据数量返回带单复数形式的字符串。

    例如：
      format_count(1, "个包")   → "1 个包"
      format_count(3, "个包")   → "3 个包"
      format_count(1, "child", "children") → "1 child"
      format_count(3, "child", "children") → "3 children"

    当前代码中未大量使用，预留给后续国际化/美化。
    """
    if n == 1:
        return f"{n} {singular}"
    return f"{n} {plural or singular + 's'}"

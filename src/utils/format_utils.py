"""格式化工具 — 大小、时间等格式化。"""


def format_size(size_kb: int) -> str:
    """将 KB 转为人类可读的大小字符串。"""
    if size_kb >= 1024 * 1024:
        return f"{size_kb / (1024 * 1024):.1f} GB"
    elif size_kb >= 1024:
        return f"{size_kb / 1024:.1f} MB"
    elif size_kb > 0:
        return f"{size_kb} KB"
    return ""


def format_count(n: int, singular: str, plural: str = "") -> str:
    """根据数量返回单复数形式。"""
    if n == 1:
        return f"{n} {singular}"
    return f"{n} {plural or singular + 's'}"

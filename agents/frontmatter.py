"""Simple YAML-like frontmatter parser."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FrontmatterResult:
    meta: dict[str, str] = field(default_factory=dict)
    body: str = ""


def parse_frontmatter(content: str) -> FrontmatterResult:
    """把一段文字拆成：开头 --- 里的配置 + 后面的正文。"""
    lines = content.split("\n")

    # 第一行不是 ---，说明没有文头，整段都当正文
    if not lines or lines[0].strip() != "---":
        return FrontmatterResult(body=content)

    # 找第二个 ---（文头结束位置）
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    # 找不到结束符，也整段当正文
    if end_idx == -1:
        return FrontmatterResult(body=content)

    # 两个 --- 之间的行：name: xxx  -> 放进 meta 字典
    meta: dict[str, str] = {}
    for line in lines[1:end_idx]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key:
            meta[key] = value.strip()

    # 第二个 --- 后面：正文
    body = "\n".join(lines[end_idx + 1 :]).strip()
    return FrontmatterResult(meta=meta, body=body)


def format_frontmatter(meta: dict[str, str], body: str) -> str:
    """反过来：字典 + 正文 -> 拼回带 --- 的整段文字。"""
    parts = ["---"]
    for key, value in meta.items():
        parts.append(f"{key}: {value}")
    parts.extend(["---", "", body])
    return "\n".join(parts)

"""Shared helpers for provider serialization of multimodal message content."""

from __future__ import annotations

from collections.abc import Sequence

from run_agent_core.messages import ImageContent, TextContent

NON_VISION_USER_IMAGE_PLACEHOLDER = (
    "(image omitted: current model does not support image input; image contents are "
    "unavailable—do not infer or describe them)"
)
NON_VISION_TOOL_IMAGE_PLACEHOLDER = (
    "(tool image omitted: current model does not support image input; image contents are "
    "unavailable—do not infer or describe them; ask the user to switch to a vision-capable model)"
)


def text_and_images(
    content: str | Sequence[TextContent | ImageContent],
    *,
    supports_images: bool,
    image_placeholder: str,
) -> tuple[str, list[ImageContent]]:
    """Return visible text and sendable images, downgrading unsupported images."""
    if isinstance(content, str):
        return content, []

    text = "".join(block.text for block in content if isinstance(block, TextContent))
    images = [block for block in content if isinstance(block, ImageContent)]
    if supports_images:
        return text, images
    if images:
        text = f"{text}\n{image_placeholder}" if text else image_placeholder
    return text, []

"""Create a safe Bedrock inspection rendition without altering source artwork."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from math import sqrt

from PIL import Image

BEDROCK_MAX_IMAGE_BYTES = 3_700_000
BEDROCK_MAX_IMAGE_SIDE = 8_000


@dataclass(frozen=True)
class BedrockImage:
    content: bytes
    width: int
    height: int
    source_width: int
    source_height: int
    transparency_composited: bool


def prepare_bedrock_image(
    content: bytes,
    *,
    max_bytes: int = BEDROCK_MAX_IMAGE_BYTES,
    max_side: int = BEDROCK_MAX_IMAGE_SIDE,
) -> BedrockImage:
    """Return a PNG within Converse limits while retaining the untouched source elsewhere."""

    with Image.open(BytesIO(content)) as opened:
        opened.load()
        source_width, source_height = opened.size
        image = opened.convert("RGBA")

    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    alpha = image.getchannel("A")
    transparency_composited = alpha.getextrema()[0] < 255
    if transparency_composited:
        background = _checkerboard(image.size)
        background.paste(image, mask=alpha)
        rendition = background
    else:
        rendition = image.convert("RGB")

    encoded = _encode_png(rendition)
    while len(encoded) > max_bytes and min(rendition.size) > 1:
        ratio = min(0.9, sqrt(max_bytes / len(encoded)) * 0.9)
        next_size = (
            max(1, int(rendition.width * ratio)),
            max(1, int(rendition.height * ratio)),
        )
        if next_size == rendition.size:
            next_size = (max(1, rendition.width - 1), max(1, rendition.height - 1))
        rendition = rendition.resize(next_size, Image.Resampling.LANCZOS)
        encoded = _encode_png(rendition)

    if len(encoded) > max_bytes:
        raise ValueError("Artwork cannot be rendered within the Bedrock image-size limit")

    return BedrockImage(
        content=encoded,
        width=rendition.width,
        height=rendition.height,
        source_width=source_width,
        source_height=source_height,
        transparency_composited=transparency_composited,
    )


def _checkerboard(size: tuple[int, int], tile_size: int = 32) -> Image.Image:
    background = Image.new("RGB", size, (210, 210, 210))
    dark = Image.new("RGB", (tile_size, tile_size), (160, 160, 160))
    for y in range(0, size[1], tile_size):
        for x in range(0, size[0], tile_size):
            if (x // tile_size + y // tile_size) % 2:
                background.paste(dark, (x, y))
    return background


def _encode_png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", optimize=True, compress_level=9)
    return output.getvalue()

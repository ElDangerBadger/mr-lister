from __future__ import annotations

from io import BytesIO

from PIL import Image

from mr_lister.intelligence.images import prepare_bedrock_image


def png_bytes(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_large_source_gets_a_separate_bounded_inspection_rendition() -> None:
    source = Image.effect_noise((512, 384), 96).convert("RGB")
    original = png_bytes(source)

    rendition = prepare_bedrock_image(original, max_bytes=12_000, max_side=128)

    assert original == png_bytes(source)
    assert len(rendition.content) <= 12_000
    assert rendition.width <= 128
    assert rendition.height <= 128
    assert rendition.source_width == 512
    assert rendition.source_height == 384


def test_transparent_artwork_is_composited_for_visual_inspection() -> None:
    source = Image.new("RGBA", (8, 8), (255, 255, 255, 0))
    source.putpixel((4, 4), (255, 255, 255, 255))

    rendition = prepare_bedrock_image(png_bytes(source))

    with Image.open(BytesIO(rendition.content)) as result:
        assert result.mode == "RGB"
        assert result.getpixel((4, 4)) == (255, 255, 255)
        assert result.getpixel((0, 0)) != (255, 255, 255)
    assert rendition.transparency_composited is True

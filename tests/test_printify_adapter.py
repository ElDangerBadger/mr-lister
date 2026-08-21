from __future__ import annotations

from hashlib import sha256

import pytest

from mr_lister.production.adapter import PrintifyProductionAdapter
from mr_lister.production.printify import (
    PrintifyAuthenticationError,
    PrintifyUploadedImage,
)
from mr_lister.workflow.errors import ProductionConfigurationError
from mr_lister.workflow.models import ArtworkInput


class RecordingClient:
    MAX_BASE64_SOURCE_BYTES = 5 * 1024 * 1024

    def __init__(self) -> None:
        self.received: bytes | None = None

    def upload_artwork_contents(self, *, file_name, content_type, content):
        self.received = content
        return PrintifyUploadedImage(
            image_id="image-svg",
            file_name=file_name,
            width=100,
            height=100,
            size_bytes=len(content),
            mime_type=content_type,
        )


def test_adapter_preserves_svg_source_bytes_for_printify() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'
    artwork = ArtworkInput.model_construct(
        filename="source.svg",
        content_type="image/svg+xml",
        content_sha256=sha256(svg).hexdigest(),
        size_bytes=len(svg),
    )
    client = RecordingClient()
    adapter = PrintifyProductionAdapter(client=client, shop_id=42)

    image_id = adapter.upload_artwork(job_id="job-1", artwork=artwork, content=svg)

    assert image_id == "image-svg"
    assert client.received == svg


def test_adapter_maps_printify_auth_failure_to_terminal_configuration_error() -> None:
    class RejectingClient(RecordingClient):
        def upload_artwork_contents(self, **_request):
            raise PrintifyAuthenticationError("provider details")

    png = b"\x89PNG\r\n\x1a\nfixture"
    artwork = ArtworkInput(
        filename="source.png",
        content_type="image/png",
        content_sha256=sha256(png).hexdigest(),
        size_bytes=len(png),
    )
    adapter = PrintifyProductionAdapter(client=RejectingClient(), shop_id=42)

    with pytest.raises(ProductionConfigurationError) as raised:
        adapter.upload_artwork(job_id="job-1", artwork=artwork, content=png)

    assert "provider details" not in str(raised.value)

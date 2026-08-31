import { afterEach, describe, expect, it, vi } from "vitest";
import {
  type DecodedPngEvidence,
  prepareArtworkForUpload,
  proportionalRasterDimensions,
  sanitizeSvgForRasterization,
  type SanitizedSvg,
  UploadValidationError,
  validateAndHashPng,
} from "../src/upload/direct-upload";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const PNG_FIXTURES = {
  landscape: "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAEUlEQVR4nGOU8MhhYGBg+A8ABlEBzdPZ5QUAAAAASUVORK5CYII=",
  portrait: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAACCAYAAACZgbYnAAAAEklEQVR4nGOQ8MhhYGJgYPgPAAcfAc7s1TIuAAAAAElFTkSuQmCC",
  square: "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAGklEQVR4nGOU8MhhYGBgcGBhYGA4wMDAYA8AEzMCEeYQEi8AAAAASUVORK5CYII=",
  opaque: "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAEUlEQVR4nGOU8Mj5z8DAwAAACk0BzZOHfWQAAAAASUVORK5CYII=",
  transparent: "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAADklEQVR4nGOU8MhhAAEABVIAzg6/E7gAAAAASUVORK5CYII=",
} as const;

describe("artwork preparation", () => {
  it.each([
    ["landscape", 2, 1],
    ["portrait", 1, 2],
    ["square", 2, 2],
  ] as const)("preserves and accepts decoded %s mixed-alpha PNG bytes", async (fixture, width, height) => {
    const png = pngFile(fixture, `${fixture}.png`);
    const original = new Uint8Array(await png.arrayBuffer());

    const prepared = await prepareArtworkForUpload(png);

    expect(prepared).toEqual({ file: png, sourceFormat: "png" });
    expect(prepared.file).toBe(png);
    expect(new Uint8Array(await prepared.file.arrayBuffer())).toEqual(original);
    expect(await validateAndHashPng(
      prepared.file,
      decoder({ width, height, hasVisiblePixels: true }),
    )).toMatch(/^[a-f0-9]{64}$/u);
  });

  it("sanitizes SVG and exposes only a converted PNG to the upload path", async () => {
    const source = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200"><defs><linearGradient id="g"/></defs><rect width="400" height="200" fill="url(#g)"/></svg>';
    const svg = fileWithText(source, "wide-art.svg", "image/svg+xml");
    const converted = pngFile("square", "wide-art.png");
    const rasterize = vi.fn<(svg: SanitizedSvg, outputName: string, lastModified: number) => Promise<File>>()
      .mockResolvedValue(converted);

    const prepared = await prepareArtworkForUpload(svg, rasterize);

    expect(prepared).toEqual({ file: converted, sourceFormat: "svg" });
    expect(rasterize).toHaveBeenCalledTimes(1);
    const call = rasterize.mock.calls[0];
    expect(call?.[0].sourceWidth).toBe(400);
    expect(call?.[0].sourceHeight).toBe(200);
    expect(call?.[0].markup).toContain("<svg");
    expect(call?.[1]).toBe("wide-art.png");
    expect(call?.[2]).toBe(svg.lastModified);
  });

  it.each([
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script>alert(1)</script></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><image href="https://example.test/a.png"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect onclick="alert(1)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><style>@import "https://example.test/a.css";</style></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect style="fill: red"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect fill="u\\72l(https://example.test/x.svg#p)"/></svg>',
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><?unsafe href="https://example.test"?><rect/></svg>',
    '<!DOCTYPE svg><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>',
  ])("rejects active or externally referenced SVG content", (source) => {
    expect(() => sanitizeSvgForRasterization(source)).toThrow(UploadValidationError);
  });

  it("requires bounded SVG geometry", () => {
    expect(() => sanitizeSvgForRasterization('<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>'))
      .toThrow("requires a positive viewBox or pixel dimensions");
    expect(sanitizeSvgForRasterization('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 100"/>'))
      .toMatchObject({ sourceWidth: 1, sourceHeight: 100 });
    expect(() => sanitizeSvgForRasterization('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30000 30000"/>'))
      .toThrow("requires positive dimensions");
  });

  it.each([
    [400, 200, 4096, true, { width: 4096, height: 2048 }],
    [200, 400, 4096, true, { width: 2048, height: 4096 }],
    [400, 400, 4096, true, { width: 4096, height: 4096 }],
    [3000, 1500, 4096, false, { width: 3000, height: 1500 }],
  ] as const)(
    "uses a proportional edge-to-edge canvas for %sx%s artwork",
    (width, height, longestSide, allowUpscale, expected) => {
      expect(proportionalRasterDimensions(width, height, longestSide, allowUpscale))
        .toEqual(expected);
    },
  );

  it("bounds SVG path and reference complexity before rendering", () => {
    const path = `M0 0${"L1 1".repeat(20_001)}`;
    expect(() => sanitizeSvgForRasterization(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="${path}"/></svg>`))
      .toThrow("path data is too complex");
    expect(() => sanitizeSvgForRasterization('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><use href="#shape"/></svg>'))
      .toThrow("use elements are not supported");
    expect(() => sanitizeSvgForRasterization('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><marker id="m"><circle r="1"/></marker></svg>'))
      .toThrow("marker elements are not supported");
    expect(() => sanitizeSvgForRasterization('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><text>unbounded glyph work</text></svg>'))
      .toThrow("text elements are not supported");
  });

  it("does not accept mismatched extensions and media types", async () => {
    const svgNamedPng = fileWithText('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"/>', "art.png", "image/svg+xml");
    await expect(prepareArtworkForUpload(svgNamedPng)).rejects.toThrow("Choose PNG, compatible SVG, or JPEG");
    await expect(prepareArtworkForUpload(fileWithBytes(
      jpegBytes(3, 2),
      "art.jpg",
      "image/png",
    ))).rejects.toThrow("Choose PNG, compatible SVG, or JPEG");
  });

  it("preserves an OS-unspecified PNG for exact validation", async () => {
    const png = fileWithBytes(fixtureBytes("landscape"), "art.png", "");

    await expect(prepareArtworkForUpload(png)).resolves.toEqual({ file: png, sourceFormat: "png" });
  });

  it("accepts visible opaque PNG artwork", async () => {
    await expect(validateAndHashPng(
      pngFile("opaque", "opaque.png"),
      decoder({ width: 2, height: 1, hasVisiblePixels: true }),
    )).resolves.toMatch(/^[a-f0-9]{64}$/u);
  });

  it("rejects fully transparent PNG artwork with no visible pixel", async () => {
    await expect(validateAndHashPng(
      pngFile("transparent", "transparent.png"),
      decoder({ width: 2, height: 1, hasVisiblePixels: false }),
    )).rejects.toThrow("at least one visible pixel");
  });

  it.each([
    ["photo.jpg", "image/jpeg"],
    ["photo.jpeg", "image/jpeg"],
    ["photo.jpg", ""],
  ])("validates and normalizes %s JPEG artwork before upload", async (name, mediaType) => {
    const jpeg = fileWithBytes(jpegBytes(3, 2), name, mediaType);
    const converted = pngFile("landscape", "photo.png");
    const rasterizeJpeg = vi.fn().mockResolvedValue(converted);

    const prepared = await prepareArtworkForUpload(jpeg, undefined, rasterizeJpeg);

    expect(prepared).toEqual({ file: converted, sourceFormat: "jpeg" });
    expect(rasterizeJpeg).toHaveBeenCalledWith(
      jpeg,
      "photo.png",
      jpeg.lastModified,
      { width: 3, height: 2 },
    );
  });

  it("rejects a JPEG with an invalid signature before browser decode", async () => {
    const rasterizeJpeg = vi.fn();
    await expect(prepareArtworkForUpload(
      fileWithBytes(new Uint8Array([0, 1, 2, 3]), "photo.jpg", "image/jpeg"),
      undefined,
      rasterizeJpeg,
    )).rejects.toThrow("valid JPEG signature");
    expect(rasterizeJpeg).not.toHaveBeenCalled();
  });

  it("rejects excessive JPEG header dimensions before browser decode", async () => {
    const rasterizeJpeg = vi.fn();
    await expect(prepareArtworkForUpload(
      fileWithBytes(jpegBytes(20_001, 1), "photo.jpeg", "image/jpeg"),
      undefined,
      rasterizeJpeg,
    )).rejects.toThrow("JPEG dimensions exceed");
    expect(rasterizeJpeg).not.toHaveBeenCalled();
  });

  it("uses browser-oriented JPEG dimensions on an edge-to-edge PNG canvas", async () => {
    const jpeg = fileWithBytes(jpegBytes(3, 2), "oriented.jpg", "image/jpeg");
    const close = vi.fn();
    const decoded = { width: 2, height: 3, close } as unknown as ImageBitmap;
    const decode = vi.fn().mockResolvedValue(decoded);
    vi.stubGlobal("createImageBitmap", decode);
    const clearRect = vi.fn();
    const drawImage = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      clearRect,
      drawImage,
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => {
      const bytes = fixtureBytes("portrait");
      const buffer = new ArrayBuffer(bytes.byteLength);
      new Uint8Array(buffer).set(bytes);
      callback(new Blob([buffer], { type: "image/png" }));
    });

    const prepared = await prepareArtworkForUpload(jpeg);

    expect(decode).toHaveBeenCalledWith(jpeg, { imageOrientation: "from-image" });
    expect(clearRect).toHaveBeenCalledWith(0, 0, 2, 3);
    expect(drawImage).toHaveBeenCalledWith(decoded, 0, 0, 2, 3);
    expect(close).toHaveBeenCalledTimes(1);
    expect(prepared.sourceFormat).toBe("jpeg");
    expect(prepared.file).toMatchObject({ name: "oriented.png", type: "image/png" });
  });

  it("rejects a decoded image whose dimensions disagree with IHDR", async () => {
    await expect(validateAndHashPng(
      pngFile("landscape", "art.png"),
      decoder({ width: 1, height: 2, hasVisiblePixels: true }),
    )).rejects.toThrow("dimensions do not match");
  });

  it("rejects a corrupt decode after accepting a structurally valid header", async () => {
    const decode = vi.fn().mockRejectedValue(new DOMException("decode failed", "EncodingError"));

    await expect(validateAndHashPng(pngFile("landscape", "art.png"), decode))
      .rejects.toThrow("corrupt or could not be decoded");
  });

  it("rejects corrupt and excessive IHDR dimensions before decoding", async () => {
    const corrupted = fixtureBytes("landscape");
    corrupted[20] = (corrupted[20] ?? 0) ^ 1;
    const decode = decoder({ width: 2, height: 1, hasVisiblePixels: true });
    await expect(validateAndHashPng(fileWithBytes(corrupted, "corrupt.png", "image/png"), decode))
      .rejects.toThrow("valid PNG header");
    expect(decode).not.toHaveBeenCalled();

    const excessive = withIhdrDimensions(fixtureBytes("landscape"), 20_001, 1);
    await expect(validateAndHashPng(fileWithBytes(excessive, "wide.png", "image/png"), decode))
      .rejects.toThrow("dimensions exceed");
    expect(decode).not.toHaveBeenCalled();
  });

  it("accepts an OS-unspecified media type after exact PNG decoding", async () => {
    const png = fileWithBytes(fixtureBytes("landscape"), "art.png", "");
    const prepared = await prepareArtworkForUpload(png);

    await expect(validateAndHashPng(
      prepared.file,
      decoder({ width: 2, height: 1, hasVisiblePixels: true }),
    )).resolves.toMatch(/^[a-f0-9]{64}$/u);
    expect(prepared).toEqual({ file: png, sourceFormat: "png" });
  });
});

function decoder(evidence: DecodedPngEvidence) {
  return vi.fn().mockResolvedValue(evidence);
}

function pngFile(fixture: keyof typeof PNG_FIXTURES, name: string): File {
  return fileWithBytes(fixtureBytes(fixture), name, "image/png");
}

function fixtureBytes(fixture: keyof typeof PNG_FIXTURES): Uint8Array {
  return Uint8Array.from(atob(PNG_FIXTURES[fixture]), (character) => character.charCodeAt(0));
}

function jpegBytes(width: number, height: number): Uint8Array {
  return new Uint8Array([
    0xff, 0xd8,
    0xff, 0xc0, 0x00, 0x11, 0x08,
    (height >>> 8) & 0xff, height & 0xff,
    (width >>> 8) & 0xff, width & 0xff,
    0x03,
    0x01, 0x11, 0x00,
    0x02, 0x11, 0x00,
    0x03, 0x11, 0x00,
    0xff, 0xd9,
  ]);
}

function withIhdrDimensions(bytes: Uint8Array, width: number, height: number): Uint8Array {
  const updated = bytes.slice();
  const view = new DataView(updated.buffer, updated.byteOffset, updated.byteLength);
  view.setUint32(16, width, false);
  view.setUint32(20, height, false);
  view.setUint32(29, pngCrc32(updated.subarray(12, 29)), false);
  return updated;
}

function pngCrc32(bytes: Uint8Array): number {
  let checksum = 0xffffffff;
  for (const byte of bytes) {
    checksum ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      checksum = (checksum >>> 1) ^ (checksum & 1 ? 0xedb88320 : 0);
    }
  }
  return (checksum ^ 0xffffffff) >>> 0;
}

function fileWithText(source: string, name: string, type: string): File {
  return fileWithBytes(new TextEncoder().encode(source), name, type);
}

function fileWithBytes(bytes: Uint8Array, name: string, type: string): File {
  const buffer = new ArrayBuffer(bytes.byteLength);
  new Uint8Array(buffer).set(bytes);
  const file = new File([buffer], name, { type, lastModified: 123 });
  Object.defineProperty(file, "arrayBuffer", {
    value: () => Promise.resolve(buffer.slice(0)),
  });
  return file;
}

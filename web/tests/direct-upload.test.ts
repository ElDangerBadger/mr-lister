import { describe, expect, it, vi } from "vitest";
import {
  prepareArtworkForUpload,
  sanitizeSvgForRasterization,
  type SanitizedSvg,
  UploadValidationError,
  validateAndHashPng,
} from "../src/upload/direct-upload";

const PNG_BYTES = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 1]);

describe("artwork preparation", () => {
  it("preserves an accepted PNG before hashing", async () => {
    const png = fileWithBytes(PNG_BYTES, "art.png", "image/png");

    const prepared = await prepareArtworkForUpload(png);

    expect(prepared).toEqual({ file: png, sourceFormat: "png" });
    expect(await validateAndHashPng(prepared.file)).toMatch(/^[a-f0-9]{64}$/u);
  });

  it("sanitizes SVG and exposes only a converted PNG to the upload path", async () => {
    const source = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 200"><defs><linearGradient id="g"/></defs><rect width="400" height="200" fill="url(#g)"/></svg>';
    const svg = fileWithText(source, "wide-art.svg", "image/svg+xml");
    const converted = fileWithBytes(PNG_BYTES, "wide-art.png", "image/png");
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
    expect(() => sanitizeSvgForRasterization('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 100"/>'))
      .toThrow("too narrow or wide");
    expect(() => sanitizeSvgForRasterization('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 30000 30000"/>'))
      .toThrow("requires positive dimensions");
  });

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
    await expect(prepareArtworkForUpload(svgNamedPng)).rejects.toThrow("Choose PNG or SVG");
  });
});

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

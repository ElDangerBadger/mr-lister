import type { UploadResponse } from "../contracts";

export const MAX_ARTWORK_BYTES = 5 * 1024 * 1024;
const PNG_SIGNATURE = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
const PNG_HEADER_BYTES = 33;
const MAX_ARTWORK_DIMENSION = 20_000;
const MAX_ARTWORK_PIXELS = 100_000_000;
const PNG_ALPHA_SCAN_TILE = 512;
const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const SVG_RASTER_SIZES = [4096, 3072, 2048, 1024] as const;
const SVG_RENDER_TIMEOUT_MS = 10_000;
const MAX_SVG_ELEMENTS = 5_000;
const MAX_SVG_ATTRIBUTE_CHARACTERS = 1_000_000;
const MAX_SVG_PATH_CHARACTERS = 100_000;
const MAX_SVG_PATH_COMMANDS = 20_000;
const MAX_SVG_POINT_CHARACTERS = 100_000;
const MAX_SVG_POINT_COORDINATES = 40_000;
const MAX_SVG_INTRINSIC_DIMENSION = 4_096;
const FORBIDDEN_SVG_ELEMENTS = new Set([
  "a",
  "animate",
  "animatecolor",
  "animatemotion",
  "animatetransform",
  "audio",
  "clippath",
  "discard",
  "embed",
  "feimage",
  "filter",
  "foreignobject",
  "iframe",
  "image",
  "link",
  "marker",
  "mask",
  "object",
  "pattern",
  "script",
  "set",
  "source",
  "style",
  "text",
  "textpath",
  "tspan",
  "use",
  "video",
]);

export interface PreparedArtwork {
  file: File;
  sourceFormat: "png" | "svg";
}

export interface SanitizedSvg {
  markup: string;
  sourceWidth: number;
  sourceHeight: number;
}

type SvgRasterizer = (svg: SanitizedSvg, outputName: string, lastModified: number) => Promise<File>;

export interface DecodedPngEvidence {
  width: number;
  height: number;
  hasVisiblePixels: boolean;
  hasTransparency: boolean;
}

type PngDecoder = (file: File) => Promise<DecodedPngEvidence>;

export async function prepareArtworkForUpload(
  file: File,
  rasterize: SvgRasterizer = rasterizeSvgToPng,
): Promise<PreparedArtwork> {
  const lowerName = file.name.toLocaleLowerCase("en-US");
  if ((file.type === "image/png" || file.type === "") && lowerName.endsWith(".png")) {
    return { file, sourceFormat: "png" };
  }
  if ((file.type === "image/svg+xml" || file.type === "") && lowerName.endsWith(".svg")) {
    if (file.size < 1 || file.size > MAX_ARTWORK_BYTES) {
      throw new UploadValidationError("SVG artwork must be non-empty and no larger than 5 MB.");
    }
    const bytes = new Uint8Array(await file.arrayBuffer());
    let source: string;
    try {
      source = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch {
      throw new UploadValidationError("The selected SVG is not valid UTF-8 text.");
    }
    const sanitized = sanitizeSvgForRasterization(source);
    const outputName = `${file.name.slice(0, -4)}.png`;
    const converted = await rasterize(sanitized, outputName, file.lastModified);
    return { file: converted, sourceFormat: "svg" };
  }
  throw new UploadValidationError("Choose PNG or SVG artwork files.");
}

export function sanitizeSvgForRasterization(source: string): SanitizedSvg {
  if (source.length < 1 || source.length > MAX_ARTWORK_BYTES) {
    throw new UploadValidationError("SVG artwork is outside the supported size limit.");
  }
  const lowerSource = source.toLocaleLowerCase("en-US");
  if (lowerSource.includes("<!doctype") || lowerSource.includes("<!entity")) {
    throw new UploadValidationError("SVG declarations and entities are not allowed.");
  }
  const document = new DOMParser().parseFromString(source, "image/svg+xml");
  if (document.querySelector("parsererror") !== null) {
    throw new UploadValidationError("The selected SVG contains invalid XML.");
  }
  const root = document.documentElement;
  if (root.localName.toLocaleLowerCase("en-US") !== "svg" || root.namespaceURI !== SVG_NAMESPACE) {
    throw new UploadValidationError("The selected file is not a standard SVG document.");
  }
  const processingInstructions = document.createTreeWalker(
    root,
    NodeFilter.SHOW_PROCESSING_INSTRUCTION,
  );
  if (processingInstructions.nextNode() !== null) {
    throw new UploadValidationError("SVG processing instructions are not allowed.");
  }
  const elements = [root, ...root.querySelectorAll("*")];
  if (elements.length > MAX_SVG_ELEMENTS) {
    throw new UploadValidationError("SVG artwork is too complex to prepare safely.");
  }
  let attributeCharacters = 0;
  let pathCharacters = 0;
  let pathCommands = 0;
  let pointCharacters = 0;
  let pointCoordinates = 0;
  for (const element of elements) {
    const localName = element.localName.toLocaleLowerCase("en-US");
    if (FORBIDDEN_SVG_ELEMENTS.has(localName) || localName.startsWith("fe")) {
      throw new UploadValidationError(`SVG ${localName} elements are not supported.`);
    }
    for (const attribute of [...element.attributes]) {
      const name = attribute.localName.toLocaleLowerCase("en-US");
      const value = attribute.value.trim();
      attributeCharacters += attribute.name.length + attribute.value.length;
      if (attributeCharacters > MAX_SVG_ATTRIBUTE_CHARACTERS) {
        throw new UploadValidationError("SVG artwork is too complex to prepare safely.");
      }
      if (attribute.value.includes("\\")) {
        throw new UploadValidationError("SVG escaped attribute values are not supported.");
      }
      if (localName === "path" && name === "d") {
        pathCharacters += attribute.value.length;
        pathCommands += attribute.value.match(/[AaCcHhLlMmQqSsTtVvZz]/gu)?.length ?? 0;
        if (pathCharacters > MAX_SVG_PATH_CHARACTERS || pathCommands > MAX_SVG_PATH_COMMANDS) {
          throw new UploadValidationError("SVG path data is too complex to prepare safely.");
        }
      }
      if ((localName === "polygon" || localName === "polyline") && name === "points") {
        pointCharacters += attribute.value.length;
        pointCoordinates += attribute.value.match(/[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?/gu)?.length ?? 0;
        if (pointCharacters > MAX_SVG_POINT_CHARACTERS
          || pointCoordinates > MAX_SVG_POINT_COORDINATES) {
          throw new UploadValidationError("SVG point data is too complex to prepare safely.");
        }
      }
      if (name.startsWith("on") || name === "src" || name === "base" || name === "style") {
        throw new UploadValidationError("SVG scripts and external resources are not allowed.");
      }
      if ((name === "href" && value !== "" && !isInternalSvgReference(value))
        || containsUnsafeSvgUrl(value)) {
        throw new UploadValidationError("SVG external resource references are not allowed.");
      }
    }
  }

  const viewBox = parseSvgViewBox(root.getAttribute("viewBox"));
  const sourceWidth = viewBox?.width ?? parseSvgLength(root.getAttribute("width"));
  const sourceHeight = viewBox?.height ?? parseSvgLength(root.getAttribute("height"));
  if (sourceWidth === null || sourceHeight === null) {
    throw new UploadValidationError("SVG artwork requires a positive viewBox or pixel dimensions.");
  }
  const aspectRatio = sourceWidth / sourceHeight;
  if (aspectRatio < 0.02 || aspectRatio > 50) {
    throw new UploadValidationError("SVG artwork dimensions are too narrow or wide to prepare.");
  }
  if (viewBox === null) root.setAttribute("viewBox", `0 0 ${sourceWidth} ${sourceHeight}`);
  const intrinsicScale = Math.min(
    1,
    MAX_SVG_INTRINSIC_DIMENSION / sourceWidth,
    MAX_SVG_INTRINSIC_DIMENSION / sourceHeight,
  );
  root.setAttribute("width", String(sourceWidth * intrinsicScale));
  root.setAttribute("height", String(sourceHeight * intrinsicScale));
  root.setAttribute("preserveAspectRatio", "xMidYMid meet");
  return {
    markup: new XMLSerializer().serializeToString(root),
    sourceWidth,
    sourceHeight,
  };
}

export async function validateAndHashPng(
  file: File,
  decode: PngDecoder = decodePng,
): Promise<string> {
  const lowerName = file.name.toLocaleLowerCase("en-US");
  if ((file.type !== "image/png" && file.type !== "")
    || !lowerName.endsWith(".png")
    || file.name !== file.name.trim()
    || /[\u0000-\u001f\u007f/\\]/u.test(file.name)) {
    throw new UploadValidationError("Choose a PNG artwork file.");
  }
  if (file.size < PNG_HEADER_BYTES || file.size > MAX_ARTWORK_BYTES) {
    throw new UploadValidationError("Artwork must be a non-empty PNG no larger than 5 MB.");
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (bytes.byteLength !== file.size) {
    throw new UploadValidationError("The selected PNG could not be read completely.");
  }
  const header = parsePngHeader(bytes);
  if (header.width > MAX_ARTWORK_DIMENSION
    || header.height > MAX_ARTWORK_DIMENSION
    || header.width * header.height > MAX_ARTWORK_PIXELS) {
    throw new UploadValidationError("PNG dimensions exceed the supported artwork limit.");
  }
  let decoded: DecodedPngEvidence;
  try {
    decoded = await decode(file);
  } catch (error) {
    if (error instanceof UploadValidationError) throw error;
    throw new UploadValidationError("The selected PNG is corrupt or could not be decoded.");
  }
  if (decoded.width !== header.width || decoded.height !== header.height) {
    throw new UploadValidationError("The decoded PNG dimensions do not match its header.");
  }
  if (!decoded.hasVisiblePixels || !decoded.hasTransparency) {
    throw new UploadValidationError(
      "PNG artwork must contain visible artwork and a transparent background.",
    );
  }
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function parsePngHeader(bytes: Uint8Array): { width: number; height: number } {
  if (bytes.byteLength < PNG_HEADER_BYTES
    || !PNG_SIGNATURE.every((expected, index) => bytes[index] === expected)
    || readUint32(bytes, 8) !== 13
    || String.fromCharCode(...bytes.subarray(12, 16)) !== "IHDR"
    || pngCrc32(bytes.subarray(12, 29)) !== readUint32(bytes, 29)) {
    throw new UploadValidationError("The selected file does not have a valid PNG header.");
  }
  const width = readUint32(bytes, 16);
  const height = readUint32(bytes, 20);
  if (width < 1 || height < 1) {
    throw new UploadValidationError("PNG artwork requires positive dimensions.");
  }
  return { width, height };
}

function readUint32(bytes: Uint8Array, offset: number): number {
  return new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint32(offset, false);
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

async function decodePng(file: File): Promise<DecodedPngEvidence> {
  let image: ImageBitmap;
  try {
    image = await createImageBitmap(file);
  } catch {
    throw new UploadValidationError("The selected PNG is corrupt or could not be decoded.");
  }
  try {
    const canvas = document.createElement("canvas");
    canvas.width = PNG_ALPHA_SCAN_TILE;
    canvas.height = PNG_ALPHA_SCAN_TILE;
    const context = canvas.getContext("2d", { alpha: true, willReadFrequently: true });
    if (context === null) {
      throw new UploadValidationError("This browser cannot inspect PNG transparency.");
    }
    let hasVisiblePixels = false;
    let hasTransparency = false;
    for (let y = 0; y < image.height && !(hasVisiblePixels && hasTransparency); y += PNG_ALPHA_SCAN_TILE) {
      const tileHeight = Math.min(PNG_ALPHA_SCAN_TILE, image.height - y);
      for (let x = 0; x < image.width && !(hasVisiblePixels && hasTransparency); x += PNG_ALPHA_SCAN_TILE) {
        const tileWidth = Math.min(PNG_ALPHA_SCAN_TILE, image.width - x);
        context.clearRect(0, 0, PNG_ALPHA_SCAN_TILE, PNG_ALPHA_SCAN_TILE);
        context.drawImage(image, x, y, tileWidth, tileHeight, 0, 0, tileWidth, tileHeight);
        const pixels = context.getImageData(0, 0, tileWidth, tileHeight).data;
        for (let index = 3; index < pixels.length; index += 4) {
          const alpha = pixels[index];
          if (alpha === undefined) continue;
          if (alpha > 0) hasVisiblePixels = true;
          if (alpha < 255) hasTransparency = true;
          if (hasVisiblePixels && hasTransparency) break;
        }
      }
    }
    return { width: image.width, height: image.height, hasVisiblePixels, hasTransparency };
  } catch (error) {
    if (error instanceof UploadValidationError) throw error;
    throw new UploadValidationError("The selected PNG is corrupt or could not be decoded.");
  } finally {
    image.close();
  }
}

async function rasterizeSvgToPng(
  svg: SanitizedSvg,
  outputName: string,
  lastModified: number,
): Promise<File> {
  const loaded = await loadSvgImage(svg.markup);
  try {
    for (const size of SVG_RASTER_SIZES) {
      const canvas = document.createElement("canvas");
      canvas.width = size;
      canvas.height = size;
      const context = canvas.getContext("2d", { alpha: true });
      if (context === null) {
        throw new UploadValidationError("This browser cannot prepare SVG artwork.");
      }
      context.clearRect(0, 0, size, size);
      const margin = Math.max(2, Math.round(size * 0.02));
      const available = size - margin * 2;
      const scale = Math.min(available / svg.sourceWidth, available / svg.sourceHeight);
      const width = svg.sourceWidth * scale;
      const height = svg.sourceHeight * scale;
      if (![scale, width, height].every(Number.isFinite) || width <= 0 || height <= 0) {
        throw new UploadValidationError("SVG artwork dimensions cannot be rendered safely.");
      }
      context.drawImage(loaded.image, (size - width) / 2, (size - height) / 2, width, height);
      const blob = await canvasPng(canvas);
      if (blob.size <= MAX_ARTWORK_BYTES) {
        return new File([blob], outputName, {
          type: "image/png",
          lastModified,
        });
      }
    }
  } finally {
    loaded.image.src = "";
    URL.revokeObjectURL(loaded.url);
  }
  throw new UploadValidationError("The converted SVG is too detailed to fit the 5 MB artwork limit.");
}

function loadSvgImage(markup: string): Promise<{ image: HTMLImageElement; url: string }> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    const url = URL.createObjectURL(new Blob([markup], { type: "image/svg+xml" }));
    let settled = false;
    const finish = (result: "load" | "error" | "timeout") => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      image.removeEventListener("load", loaded);
      image.removeEventListener("error", failed);
      if (result === "load") {
        resolve({ image, url });
        return;
      }
      image.src = "";
      URL.revokeObjectURL(url);
      reject(new UploadValidationError(
        result === "timeout"
          ? "SVG conversion timed out before any upload began."
          : "The selected SVG could not be rendered safely.",
      ));
    };
    const loaded = () => finish("load");
    const failed = () => finish("error");
    const timeout = window.setTimeout(() => finish("timeout"), SVG_RENDER_TIMEOUT_MS);
    image.decoding = "async";
    image.addEventListener("load", loaded, { once: true });
    image.addEventListener("error", failed, { once: true });
    image.src = url;
  });
}

function canvasPng(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timeout = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new UploadValidationError("SVG conversion timed out before any upload began."));
    }, SVG_RENDER_TIMEOUT_MS);
    canvas.toBlob((blob) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeout);
      if (blob === null || blob.type !== "image/png" || blob.size < PNG_SIGNATURE.length) {
        reject(new UploadValidationError("The browser could not convert this SVG to PNG."));
      } else {
        resolve(blob);
      }
    }, "image/png");
  });
}

function parseSvgViewBox(value: string | null): { width: number; height: number } | null {
  if (value === null) return null;
  const components = value.trim().split(/[\s,]+/u).map(Number);
  if (components.length !== 4 || components.some((component) => !Number.isFinite(component))) {
    throw new UploadValidationError("SVG artwork has an invalid viewBox.");
  }
  const width = components[2];
  const height = components[3];
  if (width === undefined
    || height === undefined
    || width < 0.01
    || height < 0.01
    || width > 20_000
    || height > 20_000) {
    throw new UploadValidationError("SVG artwork requires positive dimensions.");
  }
  return { width, height };
}

function parseSvgLength(value: string | null): number | null {
  if (value === null) return null;
  const match = /^([0-9]+(?:\.[0-9]+)?)(?:px)?$/u.exec(value.trim());
  if (match?.[1] === undefined) return null;
  const parsed = Number(match[1]);
  return Number.isFinite(parsed) && parsed > 0 && parsed <= 20_000 ? parsed : null;
}

function isInternalSvgReference(value: string): boolean {
  return /^#[A-Za-z_][A-Za-z0-9_.:-]*$/u.test(value);
}

function containsUnsafeSvgUrl(value: string): boolean {
  const normalized = value.trim().toLocaleLowerCase("en-US");
  if (normalized.includes("javascript:") || normalized.includes("@import")) return true;
  for (const match of value.matchAll(/url\(([^)]*)\)/giu)) {
    const target = (match[1] ?? "").trim().replace(/^(['"])(.*)\1$/u, "$2");
    if (!isInternalSvgReference(target)) return true;
  }
  return false;
}

export function uploadToAuthorizedS3(
  file: File,
  authorization: NonNullable<UploadResponse["authorization"]>,
  onProgress: (percent: number) => void,
  signal: AbortSignal,
  xhrFactory: () => XMLHttpRequest = () => new XMLHttpRequest(),
): Promise<void> {
  if (signal.aborted) return Promise.reject(new DOMException("Upload cancelled", "AbortError"));
  if (authorization.content_sha256.length !== 64 || authorization.size_bytes !== file.size) {
    return Promise.reject(new Error("Upload authorization does not match the selected file."));
  }
  return new Promise((resolve, reject) => {
    const xhr = xhrFactory();
    const abort = () => xhr.abort();
    signal.addEventListener("abort", abort, { once: true });
    xhr.open("POST", authorization.url, true);
    xhr.withCredentials = false;
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.min(100, Math.round((event.loaded / event.total) * 100)));
      }
    });
    xhr.addEventListener("load", () => {
      signal.removeEventListener("abort", abort);
      if (xhr.status === 200 || xhr.status === 201 || xhr.status === 204) resolve();
      else reject(new Error("The artwork transfer was not accepted."));
    });
    xhr.addEventListener("error", () => {
      signal.removeEventListener("abort", abort);
      reject(new Error("The artwork transfer was interrupted."));
    });
    xhr.addEventListener("abort", () => {
      signal.removeEventListener("abort", abort);
      reject(new DOMException("Upload cancelled", "AbortError"));
    });
    const form = new FormData();
    for (const [name, value] of Object.entries(authorization.form_fields)) form.append(name, value);
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

export class UploadValidationError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UploadValidationError";
  }
}

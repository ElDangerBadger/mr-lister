import type { UploadResponse } from "../contracts";

export const MAX_ARTWORK_BYTES = 5 * 1024 * 1024;
const PNG_SIGNATURE = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);

export async function validateAndHashPng(file: File): Promise<string> {
  if (file.type !== "image/png" || !file.name.toLocaleLowerCase("en-US").endsWith(".png")) {
    throw new UploadValidationError("Choose a PNG artwork file.");
  }
  if (file.size < PNG_SIGNATURE.length || file.size > MAX_ARTWORK_BYTES) {
    throw new UploadValidationError("Artwork must be a non-empty PNG no larger than 5 MB.");
  }
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (!PNG_SIGNATURE.every((expected, index) => bytes[index] === expected)) {
    throw new UploadValidationError("The selected file does not have a valid PNG signature.");
  }
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
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

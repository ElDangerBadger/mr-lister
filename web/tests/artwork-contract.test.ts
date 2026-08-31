import { describe, expect, it } from "vitest";
import artworkContract from "../../contracts/artwork/phase6.0.0.json";
import {
  ARTWORK_FILE_INPUT_ACCEPT,
  SUPPORTED_ARTWORK_SOURCE_EXTENSIONS,
  SUPPORTED_ARTWORK_SOURCE_MEDIA_TYPES,
} from "../src/upload/direct-upload";
import { MAX_BATCH_FILES } from "../src/upload/upload-context";

describe("frozen Phase 6 artwork input contract", () => {
  it("keeps browser source formats and cardinality aligned with the frozen contract", () => {
    const extensions = artworkContract.source_formats.flatMap((format) => format.extensions);
    const mediaTypes = artworkContract.source_formats.flatMap((format) => format.media_types);

    expect([...SUPPORTED_ARTWORK_SOURCE_EXTENSIONS]).toEqual(extensions);
    expect([...SUPPORTED_ARTWORK_SOURCE_MEDIA_TYPES]).toEqual(mediaTypes);
    expect(ARTWORK_FILE_INPUT_ACCEPT.split(",")).toEqual([...mediaTypes, ...extensions]);
    expect(MAX_BATCH_FILES).toBe(artworkContract.submission.max_files);
    expect(artworkContract.submission).toMatchObject({
      min_files: 1,
      common_ingestion_path: true,
      job_cardinality: "one_independent_job_per_file",
    });
    expect(artworkContract.canonical_artwork).toMatchObject({
      format: "png",
      content_type: "image/png",
      normalization_boundary: "browser_before_upload_intent",
    });
  });
});

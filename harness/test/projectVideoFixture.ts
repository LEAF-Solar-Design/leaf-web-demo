/** Entirely synthetic red JPEG and transcript. No original/evaluator material. */
import { canonicalVideo, videoHash, type VideoIdentity, type ProjectVideo } from "../src/agent/projectVideoInspection.js";
export const transcriptId = "99999999-9999-4999-8999-999999999999";
export const transcriptBytes = Buffer.from("Synthetic transcript.\n");
export const jpeg = Buffer.from("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAMABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK+ZP3E//Z", "base64");

export function videoFixture(identity: VideoIdentity) {
  const movie = Buffer.from("Synthetic movie identity");
  const manifest: ProjectVideo["manifest"] = {
    schema: "leaf.project-video-frames.v1", policy: "full-decode-first-last-two-second-v1", ...identity,
    original: { material_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", sha256: videoHash(movie), size_bytes: movie.length },
    transcripts: [{ material_id: transcriptId, sha256: videoHash(transcriptBytes), size_bytes: transcriptBytes.length }],
    width: 16, height: 12, duration_ms: 1000, start_ms: 0, stream_index: 0, decoded_frame_count: 2, time_base: [1, 1000],
    frames: [0, 1000].map((timestamp_ms, index) => ({
      frame_id: "frame-" + String(index + 1).padStart(3, "0"), pts: timestamp_ms, timestamp_ms,
      width: 16, height: 12, sha256: videoHash(jpeg), size_bytes: jpeg.length,
    })),
  };
  const video: ProjectVideo = {
    manifest, manifest_sha256: videoHash(canonicalVideo(manifest)),
    images: manifest.frames.map(row => ({ frame_id: row.frame_id, data: jpeg.toString("base64") })),
  };
  const notes = { summary: "A synthetic red frame.", observations: manifest.frames.map(row => ({
    frame_id: row.frame_id, description: "A red rectangle is visible.",
  })) };
  return { video, notes, materials: new Map([[transcriptId + ".txt", transcriptBytes]]),
    report: Buffer.from(canonicalVideo({ schema: "leaf.project-video-observations.v1",
      manifest, manifest_sha256: video.manifest_sha256, ...notes }) + "\n") };
}

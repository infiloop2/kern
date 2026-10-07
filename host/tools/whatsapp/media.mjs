// Private Python-to-child handoff. No URLs or agent-controlled paths.
import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";

export function mediaContent(media, caption, stateDir) {
  const limits = { "image/jpeg": 5000000, "image/png": 5000000, "video/mp4": 16000000 };
  const maximum = limits[media?.media_type];
  if (!maximum || !Number.isInteger(media.size_bytes) || media.size_bytes < 512 || media.size_bytes > maximum
      || typeof media.path !== "string" || path.dirname(media.path) !== path.resolve(stateDir)
      || !/^send-[A-Za-z0-9_-]+$/.test(path.basename(media.path))) {
    throw new Error("WhatsApp private media reference is invalid.");
  }
  const fd = fs.openSync(media.path, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
  let bytes;
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.size !== media.size_bytes) throw new Error("WhatsApp media bytes changed after approval.");
    // Read exactly the approved size: never an unbounded read of a growing file.
    bytes = Buffer.alloc(media.size_bytes);
    let offset = 0;
    while (offset < bytes.length) {
      const count = fs.readSync(fd, bytes, offset, bytes.length - offset, offset);
      if (!count) throw new Error("WhatsApp media bytes changed after approval.");
      offset += count;
    }
    if (fs.fstatSync(fd).size !== media.size_bytes
        || createHash("sha256").update(bytes).digest("hex") !== media.sha256) {
      throw new Error("WhatsApp media bytes changed after approval.");
    }
  } finally {
    fs.closeSync(fd);
  }
  const key = media.media_type.startsWith("image/") ? "image" : "video";
  // Avoid an optional ffmpeg dependency: omit a generated video thumbnail.
  return { [key]: bytes, mimetype: media.media_type, caption,
    ...(key === "video" ? { jpegThumbnail: Buffer.alloc(0) } : {}) };
}

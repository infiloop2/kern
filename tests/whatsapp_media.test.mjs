import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { mediaContent } from "../host/tools/whatsapp/media.mjs";

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const modules = process.env.KERN_HOST_NODE_MODULES || path.join(repo, "host/npm/node_modules");
const { prepareWAMessageMedia } = await import(pathToFileURL(path.join(modules, "baileys/lib/index.js")));

function fixture(run) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "kern-wa-media-"));
  const file = path.join(root, "send-fixture");
  const bytes = Buffer.alloc(512, 1);
  fs.writeFileSync(file, bytes, { mode: 0o600 });
  const media = { path: file, media_type: "video/mp4", size_bytes: bytes.length,
    sha256: createHash("sha256").update(bytes).digest("hex") };
  return Promise.resolve().then(() => run(root, file, bytes, media))
    .finally(() => fs.rmSync(root, { recursive: true, force: true }));
}

test("private handoff yields exact native content and caption", () => fixture((root, file, bytes, media) => {
  for (const type of ["image/jpeg", "image/png", "video/mp4"]) {
    const content = mediaContent({ ...media, media_type: type }, "Exact 📷 caption", root);
    assert.equal(content.caption, "Exact 📷 caption");
    assert.equal(content.mimetype, type);
    assert.deepEqual(content[type.startsWith("image") ? "image" : "video"], bytes);
    assert.equal(content.text, undefined);
    fs.writeFileSync(file, Buffer.alloc(512, 2));
    assert.deepEqual(content[type.startsWith("image") ? "image" : "video"], bytes);
    fs.writeFileSync(file, bytes);
  }
}));

test("rejects external paths, URLs, symlinks, formats, sizes, and changed bytes", () => fixture((root, file, bytes, media) => {
  const link = path.join(root, "send-link");
  fs.symlinkSync(file, link);
  for (const change of [{ path: "/etc/passwd" }, { path: "https://example.test/v.mp4" },
    { path: link }, { media_type: "video/quicktime" }, { size_bytes: 16000001 },
    { media_type: "image/png", size_bytes: 5000001 }, { size_bytes: 511 },
    { size_bytes: 513 }, { sha256: "0".repeat(64) }]) {
    assert.throws(() => mediaContent({ ...media, ...change }, "", root));
  }
  fs.writeFileSync(file, Buffer.alloc(512, 2));
  assert.throws(() => mediaContent(media, "", root), /changed after approval/);
}));

test("locked Baileys produces native encrypted image/video messages without network or ffmpeg", () => fixture(async (root, file, bytes, media) => {
  // Valid PNG padded to the staging minimum; trailing bytes do not change its pixels.
  const png = Buffer.concat([Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aLc0AAAAASUVORK5CYII=", "base64"), Buffer.alloc(512)]);
  for (const [mime, data, key] of [["image/png", png, "imageMessage"], ["video/mp4", bytes, "videoMessage"]]) {
    fs.writeFileSync(file, data);
    const content = mediaContent({ ...media, media_type: mime, size_bytes: data.length,
      sha256: createHash("sha256").update(data).digest("hex") }, "Approved caption", root);
    let uploads = 0;
    const message = await prepareWAMessageMedia(content, {
      mediaUploadTimeoutMs: 180000,
      upload: async (encryptedPath, options) => {
        uploads++;
        const encrypted = fs.readFileSync(encryptedPath);
        assert.notDeepEqual(encrypted, data);
        assert.equal(options.mediaType, key === "imageMessage" ? "image" : "video");
        assert.equal(options.timeoutMs, 180000);
        return { mediaUrl: "https://example.invalid/approved", directPath: "/approved" };
      },
    });
    assert.equal(uploads, 1);
    assert.equal(message[key].caption, "Approved caption");
    assert.equal(message[key].mimetype, mime);
    assert.deepEqual(Buffer.from(message[key].fileSha256), createHash("sha256").update(data).digest());
  }
}));

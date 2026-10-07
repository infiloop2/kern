# WhatsApp linked-device messages

Enable WhatsApp under Home > Integrations and link the existing phone account
using the operator-only QR flow. This change needs a host redeploy, but no new
API key, OAuth scope, account, public hostname, package, or reconnect solely
because media support was added. It retains the private Baileys child and
single-recipient approval flow; it does not switch to Meta's Cloud API.

## Staging and sending

Use the existing `stage_image` or `stage_video` MCP action with
`for_tool: "whatsapp"`, then pass the returned asset id to `send_message`:

```json
{"path":"/output/frame.png","for_tool":"whatsapp"}
```

```json
{"recipient":"+447700900123","media_asset_id":"<returned image_asset_id>","text":"Exact caption"}
```

`stage_video` works the same way and returns `video_asset_id`. Both kinds use
the single `media_asset_id` send parameter. There is no separate media send
action, album, group send, remote-source URL, document, audio, sticker, or bulk
recipient input. Existing `{"recipient": "…", "text": "…"}` sends still work.

| Action/input | Contract |
| --- | --- |
| `stage_image.path` | Required nonempty Files path such as `/output/frame.png`, or absolute path under agent home. Regular file, no final-component symlink. JPEG (`.jpg`/`.jpeg`) or PNG (`.png`), 512–5,000,000 **bytes** inclusive. |
| `stage_video.path` | Same path rules; MP4 (`.mp4`), 512–16,000,000 **bytes** inclusive. MOV is unsupported for WhatsApp. |
| Staging `for_tool` | Required; the existing enum gains `whatsapp`. Destination must be enabled. Exactly `path` and `for_tool` are accepted; unknown fields fail. |
| `send_message.recipient` | Required single E.164 number matching `^\+[1-9][0-9]{1,14}$`. No list or group id. WhatsApp registration is checked only during approved execution. |
| `send_message.text` | Optional string, defaults to `""`. At most 4,096 Unicode **characters**, not UTF-8 bytes. For text-only sends it must be nonblank. With media it is the exact caption and can be empty. No trimming or rewriting. |
| `send_message.media_asset_id` | Optional nonempty string identifying one live, WhatsApp-owned staged image/video. Explicit null, arrays, expired ids, and other tools' ids fail. Omitting it means text-only. |

The shim validates path, file type and size before sending bytes over the private
tools Unix socket. The tools service independently checks enablement, extension,
type, byte count, and container signature before finalizing staging. Filenames
are bounded to 255 UTF-8 bytes and reject control characters. Asset ids are
opaque, tool-scoped references; the store accepts only its existing
32–128-character `[A-Za-z0-9_-]` grammar. Staged bytes are not model context.
The file signature check identifies the container, not codecs, full decoding,
or playback. Use H.264 video with AAC audio in MP4; prepare incompatible media
before staging. Sending does not transcode or generate a video thumbnail.

Staging runs directly, is local and free of provider calls, and creates no
approval. It retains the shared quotas (20 staged files, 1 GB total). Assets
expire after 26 hours, are swept hourly, and are discarded on tools-service
restart. Keep the original workspace file; never store an asset id as durable
App state. Editing the original after staging cannot replace the private copy.

## Approval and execution

Every `send_message` queues the existing operator approval. Its immutable
payload binds linked account id/label, recipient, exact text/caption and, for
media, asset id, filename, MIME type, byte count, SHA-256, and expiry. The summary
names the attachment. Bulk decisions submit WhatsApp approvals one at a time,
so slow sends do not wait behind earlier sends inside the admin proxy timeout.
Unrelated tools can still run independently. View exact request shows full
caption and metadata plus
an authenticated image/video preview. The UI uses the shared cookie-and-CSRF
blob fetch helper, then a local blob URL allowed by the admin CSP. Preview blobs
are released on close, rerender or page exit; the full file is fetched before
playback. The private preview API serves only the file in that
pending WhatsApp approval, supports byte ranges, and stops resolving after a
decision, expiry, deletion, or service restart. It creates no public capability.
Denied/pending approvals send no media to WhatsApp.

Approved execution rechecks connection/account, live asset ownership and the
entire approved metadata snapshot. Python reads a bounded stream into a private
mode-0600, host-generated handoff file and verifies exact size, SHA-256 and
signature before the send RPC. The child accepts only this private filename,
rejects symlinks, bounds its read, verifies the hash again, and holds a buffer
with the approved bytes. After checking recipient registration it rechecks
the active socket/account before calling Baileys with native image/video
content and the exact caption. No agent-controlled path or URL reaches Baileys.

The temporary handoff copy is removed after success or failure; a replacement
child startup removes copies left by a killed process. The original staged
asset follows the shared expiry policy. Successful execution reports the
message id. Ordinary gateway requests retain their 40-second deadline; media
sends get a bounded 240-second RPC budget and a 180-second Baileys upload
timeout. The tools service's 300-second stop allowance lets an in-flight media
request unwind before cgroup termination. The lifecycle lock, one attempt per
approval, and unknown-outcome/no-automatic-retry behavior remain.
If a send might have happened, inspect the chat before approving another attempt.

`send_message` remains an approval-gated action and therefore uses structural
validation plus approval of the exact outbound copy, rather than the public
request-parameter text guard. This exception applies to approved recipient and
text/caption, as for existing text sends; it does not authorize remote asset
URLs or unchecked source paths. Direct read guard declarations are unchanged.

## Data and verification boundaries

After approval, WhatsApp receives the selected recipient, image/video bytes,
caption and linked-device traffic. No publicly accessible Kern media URL is
needed or permitted for WhatsApp assets. The bounded chat cache continues to
retain only text/captions and metadata, never received media bytes. Approval
and audit records retain the exact bounded request/metadata under the existing
host policy; workspace originals remain until deleted separately. WhatsApp's
retention and recipient copies are outside Kern's cleanup control.

No paid read or per-send provider API charge is introduced or recorded. Native
uploads still disclose approved media to WhatsApp and consume bandwidth. The
existing unofficial-protocol account and availability limitations apply.

Automated checks cover private staging through the real Unix service, scope and
expiry failures, exact approvals, account changes, bounded hash-checked handoff,
cleanup/no-retry behavior, authenticated preview/ranges, and browser review.
The optional Node check, `node --test tests/whatsapp_media.test.mjs`, exercises
the locked Baileys media preparation with a fake upload callback after the
`host/npm` dependencies are installed; it makes no provider request and is not
run by CI. These checks do not prove a real send,
recipient-side playback, or deployment. A live send requires a separate exact
operator-approved test after redeploy.

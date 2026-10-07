# X images and direct messages

The X package manifest owns action inputs, the operator connection guide and
exact data policies. This reference supplements that guide with implementation
and verification boundaries. Browser X actions remain text-only.

`post_tweet` accepts ordered private `stage_image(for_tool=twitter)` IDs for
1–4 JPEG, PNG or WebP images, at most 5,000,000 bytes each, or the existing
single staged MP4/MOV video. No mixed media, caller URLs or provider media IDs.
Preparation snapshots each asset's ID, filename, MIME type, size and SHA256
alongside the exact account, text and optional reply/quote target. No upload
runs before approval. Execution validates all image metadata and digests and
captures their bounded bytes before any outbound upload. It uploads each image
once to `POST /2/media/upload` with base64 `media` and `tweet_image` category,
then attaches the confirmed numeric IDs to one `POST /2/tweets`. Errors or
unready image processing stop publication. The existing video chunk/processing
flow is preserved: 4 MiB chunks, a shared five-minute upload/processing deadline
and at most 30 status checks, with the existing 200 MB staging bound. No public
asset URLs are created.

`list_dm_events` reads one explicit page from `GET /2/dm_events`.
`read_dm_conversation` reads one page from the participant or conversation
`dm_events` endpoint. Each request is authenticated as the selected account;
no app-only bearer-token fallback. Sender and participant identifiers are
requested via `expansions`, with expanded user resources included in accounting. Pages contain 1–100 events (default 20).
A returned `next_token` is passed unchanged as `pagination_token` for the same
account and target. There is no implicit page loop. X documents only up to 30
days of available events. An empty page or missing token never proves a complete
inbox or historic archive. Results retain event/conversation IDs, timestamps,
event types, sender and returned participant IDs, direction relative to the
connected account, text and media keys. Participant IDs are bounded to 256
with an explicit truncation flag. Missing sender information gives
`unknown` direction; returned participants are not a complete roster. Text is
bounded to 10,000 characters with an explicit truncation flag. No DM media is
downloaded. Provider errors or malformed pages are surfaced rather than silently
presented as complete reads. All JSON result objects have closed typed schemas.

`send_dm` queues one exact text message (up to 10,000 characters) to a numeric
recipient resolved by public lookup or an existing conversation ID (15–19 digits, or two 1–19 digit user IDs joined by a
hyphen). The approval
binds sender ID, exact target and exact text including whitespace. Recipient
lookup confirms a numeric user's existence; it does not prove messaging
eligibility. Execution revalidates the authenticated sender and required scopes,
then makes one `POST /2/dm_conversations/with/{id}/messages` or
`POST /2/dm_conversations/{id}/messages`. Confirmation requires both provider
`dm_event_id` and `dm_conversation_id`; an existing target conversation must
match. Errors and ambiguous responses are terminal. Inspect history before a
new approval; no automatic retries, media sends, group management or campaigns.
The host approval executor already consumes decisions once.

New OAuth connections request `media.write`, `dm.read` and `dm.write` alongside
existing tweet/user/offline scopes. Legacy accounts retain public reads and
text posting; missing feature scopes give a reconnect error without clearing a
valid connection. Scopes alone do not prove live API access: the developer app
needs the Read, write, and direct messages app permission, endpoint eligibility
and credits, and X may deny recipients under privacy
or account restrictions. Quote posting remains subject to X Enterprise access.

The host ledger records published standard DM event read pricing ($0.010 per
resource, daily app/resource deduplication) and successful DM submission responses ($0.015), including ambiguous 2xx
confirmations without a deduplication ID when complete confirmation is unavailable.
DM reads do not assume the owned-resource discount. X publishes no separate
media-upload rate; none is fabricated or mapped to the metadata rate. Existing
post-cost handling is unchanged. No costs are added to action results.

Official references checked 2026-10-07:

- [Create Posts](https://docs.x.com/x-api/posts/create-post): up to four photos,
  one video or one animated GIF; quote posts require Enterprise.
- [Media upload](https://docs.x.com/x-api/media/upload-media) and
  [best practices](https://docs.x.com/x-api/media/quickstart/best-practices):
  base64 JSON upload, media.write, formats and 5 MB image limit.
- [DM lookup](https://docs.x.com/x-api/direct-messages/lookup/introduction),
  [endpoint](https://docs.x.com/x-api/direct-messages/get-dm-events) and
  [quickstart](https://docs.x.com/x-api/direct-messages/lookup/quickstart):
  retention, bounded pagination and user-context scopes.
- [DM management](https://docs.x.com/x-api/direct-messages/manage/introduction)
  and [participant send](https://docs.x.com/x-api/direct-messages/create-dm-message-by-participant-id):
  text sends, scopes and returned identifiers.
- [Pricing](https://docs.x.com/x-api/getting-started/pricing): resource rates and
  credit requirements. Developer Console access remains the live authority.

Targeted tests mock every X boundary and verify validation, scope/account
binding, no upload/send before approval, ordered image digests, terminal
provider failures and pagination. Socket tests cover image-staging enablement
and tool isolation; the focused Chromium approval smoke checks exact text and
safe digest rendering on mobile. These checks establish host contracts, not
live account eligibility or a completed publication. No live post or DM was
made to validate this feature.

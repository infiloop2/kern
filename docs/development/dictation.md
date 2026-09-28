# English dictation

Chat and App Chat share a microphone control. Click it, allow microphone
access, and speak. Short phrases append to the editable prompt. The mic shows
a muted green pulse while recording and a green spinner while finishing.
Pause releases the microphone; Resume reacquires it. Done ends capture.
There is no automatic Send.

Done allows up to five seconds for outstanding text to arrive. After that,
the prompt settles and the mic shows an amber warning. Click the mic to Retry,
or Discard audio to keep only the text already transcribed. A late response
can be saved for Retry, but cannot change the settled prompt. Retry also has
a five-second insertion window and can use that saved response immediately.
Send waits until pending audio for this conversation is resolved or discarded.

## Recovery and browser storage

Drafts remain browser-local and keyed by conversation, as before. This feature
assumes one active Kern tab per browser; it adds no cross-tab coordination or
server draft synchronization. Different browsers have independent drafts.

The browser saves pending PCM audio in IndexedDB, checkpointing an active
phrase about once per second. A crash can lose audio since the last checkpoint.
Text and a phrase receipt are committed together to the existing local draft
before deleting audio, preventing duplicate insertion if that deletion fails.
Reload never retries automatically: open the original conversation and click
the amber mic. Clearing browser storage removes this recovery copy.

Navigation, microphone disconnection, and suspended capture stop recording.
Navigation leaves untranscribed audio attached to its original conversation
for explicit Retry. Other conversations remain usable. Failed storage writes
retain the full segment in memory while the page remains open; a reload can
only restore the last successful checkpoint. Discard deletes pending audio
and retains already transcribed text.

HTTPS (or localhost), microphone permission, AudioWorklet, and browser storage
are required. Processing state has accessible labels and a spinner; warnings
have a badge, explanatory text and recovery actions, not color alone.

## Host and latency

Recognition uses CPU int8 faster-whisper base.en, English forced. The model
loads at service startup and stays resident. Before opening the microphone,
the browser checks the operator-only GET /v1/dictation/ready endpoint. A loading
or unavailable model fails promptly, with a retry message. Transcription
requests never initiate model loading.

The operator-only POST /v1/dictation/transcribe accepts at most twelve seconds
of mono 16 kHz PCM16 as base64 JSON. The admin service uses a peer-authenticated
Unix socket. The isolated worker permits one inference at a time, rejects
concurrent inference as busy, and keeps readiness responsive during inference.
Systemd starts it at boot and restarts failures, with 2 GiB memory and two-core
CPU limits. Deployment waits for model readiness and fails if it never becomes
ready. Audio and transcripts are never written to host disk or logs.

Weights are revision- and SHA-256-pinned. Provisioning downloads roughly
148 MB from the public Kern release `model-faster-whisper-base.en-1`.
Bootstrap verifies the files against SHA-256 digests pinned in source;
inference uses local files without network access.
The service is installed through normal Kern deployment.

Silence ends a phrase; continuous speech is split after eight seconds.
Recognition must keep up with speech for a smooth experience. Keeping the
model resident removes repeated loading, but does not guarantee inference
speed. A September 2026 comparison in the agent's resource-limited environment
on a two-logical-CPU host used the four-second public speech fixture after
warming each model. Two serial small.en inferences took 8.18 and 8.65 seconds;
base.en took 1.54 and 1.49 seconds. Two concurrent base.en inferences with one
thread each took 3.30 seconds overall, versus 3.04 seconds for the serial pair
with two threads. This small shared-host sample does not establish production
throughput or accent accuracy; the base model also changed the fixture's
"ask" to "asked". Keep serial inference and measure real speech after deployment.
CPU load, speech and hardware affect latency. The browser pauses capture at roughly
one minute of backlog. Review technical words and phrase boundaries before
sending; test real speech on the intended host before relying on long dictation.

### Host diagnostics

Dictation uses the existing structured journald reporter and Host diagnostics
panel; the collector already includes both admin and transcription services.
`admin_api.dictation` records failed requests and requests taking at least three
seconds. It includes the socket request duration, timeout, operation and a
fixed failure category (busy, model not ready, timeout, invalid response,
worker failure or transport failure).

`transcription.inference` records failed inference and inference taking at least
three seconds, including audio duration, inference wall time, process CPU time,
model and thread count. CPU time sums CPU consumed across the worker's threads;
it is not a percentage or a CloudWatch measurement. A random request ID links
the admin and worker observations when both are recorded. Model-load failures
are recorded under `transcription.startup`. Error details contain exception
class names only; never exception messages, audio or transcripts.

Each component/outcome warns at most once per minute, independently of other
outcomes. These are sampled anomalies, not exhaustive per-request traces or
an exact failure count. Reporting happens after inference capacity is released,
on a daemon thread admitted by the rate limit, so a slow journald writer cannot
hold up the browser reply. Reporter thread-start failure drops the observation.
The existing admin stall diagnostic also names the two dictation routes.

Compare request time with inference time to locate host-side delays. Browser
chunk formation, IndexedDB persistence, pending-chunk queue time and the browser
network path are outside these timers. A large wall/CPU difference suggests
waiting or contention but does not alone identify its cause. The five-second
finish window described above remains a separate browser deadline.

## Verification

Run `tests/scripts/test test_transcription test_deploy` for transport,
readiness, validation and deployment contracts. `tests/smoke-ui/dictation_smokes.py`
runs in the workspace browser suite. It covers microphone startup, editing,
five-second timeout, late results, reload, navigation, retry, discard and App
Chat. Recognition is mocked there; those tests do not establish model accuracy
or production throughput. The shared AWS/Lima live smoke additionally checks
unauthenticated access, malformed/oversized input, actual speech recognition
using a small public-domain fixture, readiness during inference, and that the
worker remains resident. It records latency without treating synthetic speech
as an accent-accuracy benchmark.

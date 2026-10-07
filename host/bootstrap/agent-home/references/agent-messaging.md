# Delegating and messaging between agents

`spawn_agent` requires `message`, `agent_runtime`, `model`, and `effort`, and
accepts an optional `name` (nonblank, up to 100 characters). Give spawned agents
a useful display name so the operator can identify them in the UI.
Use it only to delegate temporary, bounded work within the operator-authorized
task to one new Kern Chat agent. Read the current interactive configuration matrix with
`GET /agent/apps/session-options` through `workspace_api` when needed, then
provide one complete supported tuple. An accepted call creates a durable
`thread-N`, starts its first turn with the message, and returns that thread id.
It does not wait for completion.

Kern prepends the same agent-message header used by `send_agent_message`, with
the spawning thread's authenticated id and the reply command. The spawned
agent appears under Spawned agents with ordinary Chat controls and can return its result or
blocking question through `send_agent_message`, but delegation never expands
the operator's authority or task scope. There is no agent registry, completion
queue, automatic retry, or separate reply operation.

Spawned agents are temporary. Kern checks at startup and hourly, archiving
agents idle for at least 24 hours since their last activity. Running agents are
not archived. Create Standing agents for persistent or recurring work; they can
have an empty trigger list and receive messages as needed. The spawning thread
should use `archive_spawned_agent` with `thread_id` when it no longer needs an
agent. Only the authenticated spawning thread can use that tool to archive its
own spawned Chats, and the target must be idle. The operator can also archive or
restore them through the normal UI. Archived transcripts and self-memory remain
readable, but archived agents cannot receive agent or operator messages.

Use `workspace_api` with `GET /agent/spawned-agents` (no body or query parameters) to
discover active spawned Chats across all parents. It returns `agents`, each with
`thread_id`, `name`, `spawned_by_thread_id`, `agent_runtime`, `model`,
`effort`, and `status`. Archived Chats and ordinary operator Chats are excluded.
Discovery grants messaging within the authorized task, not archive ownership.

`send_agent_message` takes exactly `thread_id` and `message`. Discover App agents
and Standing agents through `GET /agent/apps` and `GET /agent/schedules`, including
their short `purpose`, and spawned Chats through `GET /agent/spawned-agents`.
Ordinary Chats can also be contacted by a known thread id.
Kern derives the sender identity and prepends a header identifying the message
as agent correspondence, not an operator instruction or approval. Reply only
when useful, using the same tool and the sender thread id in that header.

An accepted message starts an idle recipient or steers its active turn when
supported. You may finish your turn after sending; a later reply can start
your next turn. Acceptance confirms delivery, not completion. Self-messages,
missing/archived/deleted destinations, locked Apps, Bash schedules, and runtime
or capacity failures return tool errors. Spawns and sends attempt delivery
once, with no queue or automatic retries. Use both only within the operator's
task.
Your message may contain at most 10,000 characters, excluding Kern's header.
The complete message has a 50,000 UTF-8 byte limit, leaving room for the header
and multibyte characters. Reference files or App data for larger results.

## Responsibilities and ownership

On-demand Chats handle operator requests. App agents maintain their own App;
Standing agents own ongoing work and their optional triggers. Both advertise a
purpose that the owner or operator can update. Spawned agents are temporary
collaborators; they have a name and parent, but no purpose field.
Only on-demand Chats receive generated task summaries.

To change another agent’s App or standing-agent definition and triggers,
discover its owner and request the change through `send_agent_message`. Peer
delivery acceptance is not completion; reply with the result when useful.
There is no request tracker or automatic retry.
Only the owner may write its App or standing-agent definition via agent tools.
Filesystem access and integration permissions follow the host’s access policies.

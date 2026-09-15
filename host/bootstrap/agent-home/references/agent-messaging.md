# Messages between agents

`send_agent_message` takes exactly `thread_id` and `message`. Discover Apps
and model Schedules through their existing Workspace lists, including their
short `purpose`. Ordinary Chats can also be contacted by a known thread id.
Kern derives the sender identity and prepends a header identifying the message
as agent correspondence, not an operator instruction or approval. Reply only
when useful, using the same tool and the sender thread id in that header.

An accepted message starts an idle recipient or steers its active turn when
supported. You may finish your turn after sending; a later reply can start
your next turn. Acceptance confirms delivery, not completion. Self-messages,
missing/archived/deleted destinations, locked Apps, Bash schedules, and runtime
or capacity failures return tool errors. Sends attempt delivery once, with no
queue or automatic retries. Use messaging only within the operator's task.
Your message may contain at most 10,000 characters, excluding Kern's header.
The complete message has a 50,000 UTF-8 byte limit, leaving room for the header
and multibyte characters. Reference files or App data for larger results.

Schedule eligibility and settings are read immediately before delivery.
Deleting or editing a schedule does not cancel a send already in flight;
that send may use the settings it just read.

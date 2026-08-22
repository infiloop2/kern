from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


class AgentChatRichTextTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is required for the UI renderer test")
    def test_markdown_renderer_escapes_html_and_rejects_javascript_links(self) -> None:
        renderer = Path("host/runtime/workspace/chat/ui/rich_text.js").resolve()
        script = (
            f"const rich = require({json.dumps(str(renderer))});"
            "process.stdout.write(rich.renderMarkdown("
            "process.argv[1]));"
        )
        source = (
            "# Status\n\n**done** and `safe`\n\n"
            "<img src=x onerror=alert(1)>\n\n"
            "[bad](javascript:alert(1)) [copy](https://example.com) "
            "[github](https://github.com/infiversehq/kern) "
            "[reply](https://x.com/intent/tweet?in_reply_to=9001&text=Prepared%20reply)\n\n"
            "```js\nconst x = 1;\n```"
        )
        rendered = subprocess.run(
            ["node", "-e", script, source],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn("<h1>Status</h1>", rendered)
        self.assertIn("<strong>done</strong>", rendered)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertIn('data-copy-href="https://example.com"', rendered)
        self.assertIn(
            '<a class="md-open-link" href="https://github.com/infiversehq/kern" '
            'title="https://github.com/infiversehq/kern" target="_blank" '
            'rel="noopener noreferrer">github</a>',
            rendered,
        )
        self.assertIn(
            '<a class="md-open-link" href="https://x.com/intent/tweet?in_reply_to=9001&amp;text=Prepared%20reply" '
            'title="https://x.com/intent/tweet?in_reply_to=9001&amp;text=Prepared%20reply" '
            'target="_blank" rel="noopener noreferrer">reply</a>',
            rendered,
        )
        self.assertIn('class="md-copy"', rendered)

    @unittest.skipUnless(shutil.which("node"), "node is required for the UI renderer test")
    def test_only_hardcoded_safe_navigation_hosts_open_directly(self) -> None:
        renderer = Path("host/runtime/workspace/chat/ui/rich_text.js").resolve()
        script = (
            f"const rich = require({json.dumps(str(renderer))});"
            "process.stdout.write(JSON.stringify(process.argv.slice(1).map(value => "
            "rich.safeNavigationHref(value))));"
        )
        values = [
            "https://github.com/infiversehq/kern/pull/264",
            "https://www.instagram.com/reel/ABC123/",
            "https://www.linkedin.com/posts/alice_agents-123",
            "https://polymarket.com/event/example-market",
            "https://calendar.google.com/calendar/u/0/r/eventedit/abc",
            "https://www.google.com/calendar/event?eid=YWJj&ctz=UTC",
            "https://x.com/alice/status/9001",
            "https://x.com/intent/tweet?in_reply_to=9001&text=Looks%20good",
            "https://twitter.com/messages/compose?recipient_id=123456789&text=Prepared%20DM",
            "https://x.com/intent/tweet?in_reply_to=not-numeric&url=https%3A%2F%2Fexample.com",
            "https://twitter.com/messages/compose?recipient_id=not-numeric&extra=value",
            "https://docs.byteplus.com/en/docs/ModelArk/1361424",
            "https://runwayml.com/privacy-policy",
            "https://example.com/not-trusted",
            "http://github.com/infiversehq/kern",
            "https://github.com.evil.example/infiversehq/kern",
            "https://user@github.com/infiversehq/kern",
            "https://github.com:444/infiversehq/kern",
            "https://github.com/login/oauth/authorize?client_id=attacker",
            "https://www.instagram.com/oauth/authorize?client_id=attacker",
            "https://www.linkedin.com/oauth/v2/authorization?client_id=attacker",
            "https://x.com/i/oauth2/authorize?client_id=attacker",
            "https://www.google.com/search?q=not-calendar",
            "https://www.google.com/calendar/event?eid=abc&continue=https%3A%2F%2Fevil.example",
            "http://x.com/intent/tweet?in_reply_to=9001&text=no",
            "https://x.com.evil.example/intent/tweet?in_reply_to=9001&text=no",
        ]
        rendered = json.loads(subprocess.run(
            ["node", "-e", script, *values],
            check=True,
            capture_output=True,
            text=True,
        ).stdout)

        self.assertEqual(rendered, values[:13] + [""] * (len(values) - 13))

    @unittest.skipUnless(shutil.which("node"), "node is required for the UI renderer test")
    def test_agent_home_markdown_links_open_workspace_files(self) -> None:
        renderer = Path("host/runtime/workspace/chat/ui/rich_text.js").resolve()
        script = (
            f"const rich = require({json.dumps(str(renderer))});"
            "process.stdout.write(rich.renderMarkdown(process.argv[1]));"
        )
        source = (
            "[app.py](/mnt/kern-agent/agent-home/project/app.py:12) "
            "[report](</mnt/kern-agent/agent-home/My Project/report.md:3>) "
            "[outside](/etc/passwd) "
            "[escape](/mnt/kern-agent/agent-home/../admin/secret.txt)"
        )
        rendered = subprocess.run(
            ["node", "-e", script, source],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn(
            'class="md-open-file" data-file-path="/project/app.py:12" '
            'data-fallback-path="/project/app.py"',
            rendered,
        )
        self.assertIn(
            'class="md-open-file" data-file-path="/My Project/report.md:3" '
            'data-fallback-path="/My Project/report.md"',
            rendered,
        )
        self.assertNotIn('data-file-path="/etc/passwd"', rendered)
        self.assertNotIn("secret.txt", rendered)

        numeric_name = subprocess.run(
            [
                "node", "-e", script,
                "[run](/mnt/kern-agent/agent-home/reports/run:2026)",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn('data-file-path="/reports/run:2026"', numeric_name)
        self.assertIn('data-fallback-path="/reports/run"', numeric_name)

    @unittest.skipUnless(shutil.which("node"), "node is required for the UI renderer test")
    def test_appended_details_accumulate_while_plain_details_replace(self) -> None:
        # Streamed reasoning carries one chunk per event so the stored trace
        # stays linear; the live card has to put them back together. The
        # completed record sends the whole trace without the marker and
        # replaces, so the two paths must not double it up.
        renderer = Path("host/runtime/workspace/chat/ui/rich_text.js").resolve()
        script = (
            f"const rich = require({json.dumps(str(renderer))});"
            "const event = (seq, phase, detail, append_detail=false) => ({"
            "seq, thread_id: 'thread-1', event_type: 'thread.activity', payload: {activity: {"
            "activity_id: 'reasoning', kind: 'reasoning', phase, title: 'Reasoning',"
            "detail, append_detail}}});"
            "const streamed = rich.compactActivityEvents(["
            "event(1, 'started', 'Think'), event(2, 'started', ' harder', true)]);"
            "const finished = rich.compactActivityEvents(["
            "event(1, 'started', 'Think'), event(2, 'started', ' harder', true),"
            "event(3, 'completed', 'Think harder')]);"
            "process.stdout.write(JSON.stringify({"
            "streamedLength: streamed.length,"
            "streamed: streamed[0].payload.activity.detail,"
            "finished: finished[0].payload.activity.detail,"
            "phase: finished[0].payload.activity.phase"
            "}));"
        )
        compared = json.loads(subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        ).stdout)

        self.assertEqual(compared, {
            "streamedLength": 1,
            "streamed": "Think harder",
            "finished": "Think harder",
            "phase": "completed",
        })

    @unittest.skipUnless(shutil.which("node"), "node is required for the UI renderer test")
    def test_activity_deltas_compact_to_one_bounded_snapshot(self) -> None:
        renderer = Path("host/runtime/workspace/chat/ui/rich_text.js").resolve()
        script = (
            f"const rich = require({json.dumps(str(renderer))});"
            "const event = (seq, phase, output, append_output=false) => ({"
            "seq, thread_id: 'thread-1', event_type: 'thread.activity', payload: {activity: {"
            "activity_id: 'command-1', kind: 'command', phase, title: seq === 1 ? 'npm test' : 'Command output',"
            "output, append_output}}});"
            "const compacted = rich.compactActivityEvents(["
            "event(1, 'started', 'one'), event(2, 'started', ' two', true),"
            "event(3, 'completed', 'done')]);"
            "process.stdout.write(JSON.stringify({"
            "length: compacted.length,"
            "title: compacted[0].payload.activity.title,"
            "phase: compacted[0].payload.activity.phase,"
            "output: compacted[0].payload.activity.output,"
            "clipped: rich.clipUtf8('😀'.repeat(600000)).endsWith('\\n… (truncated)')"
            "}));"
        )
        compared = json.loads(subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        ).stdout)

        self.assertEqual(compared, {
            "length": 1,
            "title": "npm test",
            "phase": "completed",
            "output": "done",
            "clipped": True,
        })

    @unittest.skipUnless(shutil.which("node"), "node is required for the UI renderer test")
    def test_activity_compaction_preserves_first_sequence_across_merges(self) -> None:
        renderer = Path("host/runtime/workspace/chat/ui/rich_text.js").resolve()
        script = (
            f"const rich = require({json.dumps(str(renderer))});"
            "const activity = (seq, phase) => ({"
            "seq, thread_id: 'thread-1', event_type: 'thread.activity', payload: {activity: {"
            "activity_id: 'command-1', kind: 'command', phase, title: 'npm test'}}});"
            "const message = seq => ({"
            "seq, thread_id: 'thread-1', event_type: 'thread.message', payload: {message: {role: 'user'}}});"
            "const first = rich.compactActivityEvents(["
            "activity(10, 'started'), message(11), activity(12, 'completed')]);"
            "const second = rich.compactActivityEvents(["
            "...first, message(13), activity(14, 'completed')].sort((a, b) => a.seq - b.seq));"
            "process.stdout.write(JSON.stringify(second.map(event => ({"
            "seq: event.seq, type: event.event_type, phase: event.payload.activity?.phase"
            "}))));"
        )
        compacted = json.loads(subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        ).stdout)

        self.assertEqual(compacted, [
            {"seq": 10, "type": "thread.activity", "phase": "completed"},
            {"seq": 11, "type": "thread.message"},
            {"seq": 13, "type": "thread.message"},
        ])

    @unittest.skipUnless(shutil.which("node"), "node is required for the UI renderer test")
    def test_activity_compaction_never_merges_host_scoped_provider_ids(self) -> None:
        # The host prefixes provider ids with the private execution number, so
        # a later process reusing command-1 cannot overwrite old activity.
        renderer = Path("host/runtime/workspace/chat/ui/rich_text.js").resolve()
        script = (
            f"const rich = require({json.dumps(str(renderer))});"
            "const activity = (seq, scope, phase, title) => ({"
            "seq, thread_id: 'thread-1', event_type: 'thread.activity', payload: {activity: {"
            "activity_id: `${scope}:command-1`, kind: 'command', phase, title}}});"
            "const compacted = rich.compactActivityEvents(["
            "activity(2, 1, 'started', 'npm test'), activity(3, 1, 'completed', 'npm test'),"
            "activity(5, 2, 'started', 'npm run lint')]);"
            "process.stdout.write(JSON.stringify(compacted"
            ".filter(event => event.event_type === 'thread.activity')"
            ".map(event => ({seq: event.seq, phase: event.payload.activity.phase,"
            " title: event.payload.activity.title}))));"
        )
        compacted = json.loads(subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        ).stdout)

        self.assertEqual(compacted, [
            {"seq": 2, "phase": "completed", "title": "npm test"},
            {"seq": 5, "phase": "started", "title": "npm run lint"},
        ])

    @unittest.skipUnless(shutil.which("node"), "node is required for the UI renderer test")
    def test_deeply_nested_blockquotes_do_not_overflow_the_stack(self) -> None:
        renderer = Path("host/runtime/workspace/chat/ui/rich_text.js").resolve()
        script = (
            f"const rich = require({json.dumps(str(renderer))});"
            "process.stdout.write(rich.renderMarkdown(process.argv[1]));"
        )
        rendered = subprocess.run(
            ["node", "-e", script, ">" * 10_000 + " bounded"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

        self.assertIn("bounded", rendered)
        self.assertLessEqual(rendered.count("<blockquote>"), 17)


if __name__ == "__main__":
    unittest.main()

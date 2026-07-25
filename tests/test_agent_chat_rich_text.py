from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest


class AgentChatRichTextTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is required for the UI renderer test")
    def test_markdown_renderer_escapes_html_and_rejects_javascript_links(self) -> None:
        renderer = Path("host/apps/agent_chat/ui/rich_text.js").resolve()
        script = (
            f"const rich = require({json.dumps(str(renderer))});"
            "process.stdout.write(rich.renderMarkdown("
            "process.argv[1]));"
        )
        source = (
            "# Status\n\n**done** and `safe`\n\n"
            "<img src=x onerror=alert(1)>\n\n"
            "[bad](javascript:alert(1)) [good](https://example.com)\n\n"
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
        self.assertNotIn("<a ", rendered)
        self.assertIn('class="md-copy"', rendered)

    @unittest.skipUnless(shutil.which("node"), "node is required for the UI renderer test")
    def test_activity_deltas_compact_to_one_bounded_snapshot(self) -> None:
        renderer = Path("host/apps/agent_chat/ui/rich_text.js").resolve()
        script = (
            f"const rich = require({json.dumps(str(renderer))});"
            "const event = (seq, phase, output, append_output=false) => ({"
            "seq, task_id: 'task_1', event_type: 'task.activity', payload: {activity: {"
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
    def test_deeply_nested_blockquotes_do_not_overflow_the_stack(self) -> None:
        renderer = Path("host/apps/agent_chat/ui/rich_text.js").resolve()
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

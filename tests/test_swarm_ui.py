"""Swarm reflects runtime failures separately from pending approvals."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), 'node is required')
class SwarmLayoutTests(unittest.TestCase):
    def test_layout_includes_disconnected_agents_without_overlap(self) -> None:
        source = (Path(__file__).parents[1] / 'host/runtime/admin_api/admin_ui/swarm_layout.js').read_bytes()
        module = 'data:text/javascript;base64,' + base64.b64encode(source).decode()
        script = f"""
            import {{layoutAgents, rankAgents}} from {json.dumps(module)};
            const agents = Array.from({{length: 100}}, (_, i) => ({{thread_id: `thread-${{i+1}}`}}));
            const edges = [{{sender_thread_id: 'thread-1', target_thread_id: 'thread-30', count: 8}}];
            const layout = layoutAgents(agents, edges);
            if (layout.positions.size !== 100) throw Error('lost agents');
            const positions = [...layout.positions.values()];
            for (const [i, a] of positions.entries()) {{
                if (a.x < 0 || a.y < 0 || a.x + 220 > layout.width || a.y + 216 > layout.height) throw Error('outside map');
                for (const b of positions.slice(i + 1)) {{
                    if (Math.abs(a.x - b.x) < 220 && Math.abs(a.y - b.y) < 216) throw Error('overlap');
                }}
            }}
            // Large disconnected catalogs exercise the bounded relaxation path.
            for (const count of [500, 1000]) {{
                const disconnected = Array.from({{length: count}}, (_, i) => ({{thread_id: `thread-${{i+1}}`}}));
                const spread = layoutAgents(disconnected, []);
                const places = [...spread.positions.values()];
                for (const [i, a] of places.entries()) {{
                    if (!Number.isFinite(a.x + a.y) || a.x + 220 > spread.width || a.y + 216 > spread.height) throw Error('invalid expanded map');
                    for (const b of places.slice(i + 1)) {{
                        if (Math.abs(a.x - b.x) < 220 && Math.abs(a.y - b.y) < 216) throw Error(`overlap at ${{count}} agents`);
                    }}
                }}
            }}
            const again = layoutAgents([...agents].reverse(), edges);
            if (JSON.stringify([...again.positions]) !== JSON.stringify([...layout.positions])) throw Error('unstable ordering');
            // Operator attention dominates even an extremely busy autonomous helper.
            const sample = ['operator', 'lead', 'helper', 'quiet', 'peer'].map(thread_id => ({{thread_id}}));
            const metrics = {{
                lead: {{operator_messages: 20, agent_peers: 1, total_tokens: 100}},
                helper: {{operator_messages: 0, agent_peers: 100, total_tokens: 1e12}},
                peer: {{operator_messages: 0, agent_peers: 2, total_tokens: null}},
            }};
            const scores = rankAgents(sample, metrics);
            if (!(scores.get('lead') > scores.get('helper') && scores.get('helper') > scores.get('peer') && scores.get('peer') > scores.get('quiet'))) throw Error('wrong importance order');
            if (scores.get('quiet') !== 0 || scores.has('operator')) throw Error('missing telemetry / operator score');
            const ranked = layoutAgents(sample, [], metrics);
            for (const [a, score] of scores) {{
                if (ranked.positions.get('operator').y + 216 > ranked.positions.get(a).y) throw Error('operator not above agents');
                for (const [b, other] of scores) {{
                    if (score > other && ranked.positions.get(a).y >= ranked.positions.get(b).y) throw Error('hierarchy inverted');
                }}
            }}
            if (Math.abs(ranked.positions.get('operator').x + 110 - ranked.width / 2) > 1e-6) throw Error('operator not centred');
            const archived = rankAgents(sample, {{...metrics, archived: {{operator_messages: 1e9, agent_peers: 1e9, total_tokens: 1e20}}}});
            if (JSON.stringify([...scores]) !== JSON.stringify([...archived])) throw Error('archived metrics affect ranking');
            const reverse = layoutAgents([...sample].reverse(), [], metrics);
            if (JSON.stringify([...ranked.positions]) !== JSON.stringify([...reverse.positions])) throw Error('unstable rank ties');
            // A strong collaborator pulls horizontal placement closer, without
            // changing the ranking or dropping disconnected nodes.
            const peers = Array.from({{length: 9}}, (_, i) => ({{thread_id: `a${{i}}`}}));
            const plain = layoutAgents(peers, []);
            for (const target of ['a6', 'a7', 'a8']) {{
                const connected = layoutAgents(peers, [{{sender_thread_id:'a0', target_thread_id:target, count:100}}]);
                const gap = map => Math.abs(map.positions.get('a0').x - map.positions.get(target).x);
                if (gap(connected) >= gap(plain)) throw Error('collaborators did not move closer');
            }}
            if (layoutAgents([], []).positions.size) throw Error('empty map');
            const catalog = Array.from({{length: 10000}}, (_, i) => ({{thread_id: `thread-${{i+1}}`}}));
            const dense = Array.from({{length: 500}}, (_, i) => ({{sender_thread_id: `thread-${{i+1}}`, target_thread_id: `thread-${{i+20}}`, count: i+1}}));
            const activity = Object.fromEntries(catalog.map((agent, i) => [agent.thread_id,
                {{operator_messages: i % 41, agent_peers: i % 17, total_tokens: i * 1234}}]));
            const started = performance.now();
            if (layoutAgents(catalog, dense, activity).positions.size !== 10000) throw Error('large catalog lost agents');
            if (performance.now() - started > 5000) throw Error('large catalog blocks UI');
        """
        subprocess.run(['node', '--input-type=module', '-e', script], check=True)

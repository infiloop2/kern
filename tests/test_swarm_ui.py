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
            import {{layoutAgents}} from {json.dumps(module)};
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
            const distance = (map, a, b) => Math.hypot(map.get(a).x - map.get(b).x, map.get(a).y - map.get(b).y);
            const small = ['operator', 'a', 'b', 'c'].map(thread_id => ({{thread_id}}));
            const weighted = layoutAgents(small, [
                {{sender_thread_id:'operator', target_thread_id:'a', count:100}},
                {{sender_thread_id:'operator', target_thread_id:'b', count:1}},
            ]).positions;
            if (distance(weighted, 'operator', 'a') >= distance(weighted, 'operator', 'b') * .85) throw Error('strength does not affect distance');
            if (new Set(positions.map(p => p.x)).size < 80) throw Error('grid layout');
            if (layoutAgents([], []).positions.size) throw Error('empty map');
            const catalog = Array.from({{length: 10000}}, (_, i) => ({{thread_id: `thread-${{i+1}}`}}));
            const started = performance.now();
            if (layoutAgents(catalog, edges).positions.size !== 10000) throw Error('large catalog lost agents');
            if (performance.now() - started > 5000) throw Error('large catalog blocks UI');
        """
        subprocess.run(['node', '--input-type=module', '-e', script], check=True)

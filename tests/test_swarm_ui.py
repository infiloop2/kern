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
            import {{layoutAgents, rankAgents, footprint}} from {json.dumps(module)};
            const overlap = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
            const inside = (map, point) => {{
                const box = footprint(point);
                return box.left >= 0 && box.top >= 0 && box.right <= map.width && box.bottom <= map.height;
            }};
            const distance = (map, id) => Math.hypot(map.positions.get(id).x - map.center.x, map.positions.get(id).y - map.center.y);
            const agents = Array.from({{length: 100}}, (_, i) => ({{thread_id: `thread-${{i+1}}`}}));
            const edges = [{{sender_thread_id: 'thread-1', target_thread_id: 'thread-30', count: 8}}];
            const layout = layoutAgents(agents, edges);
            if (layout.positions.size !== 100) throw Error('lost agents');
            const positions = [...layout.positions.values()];
            for (const [i, a] of positions.entries()) {{
                if (!inside(layout, a)) throw Error('outside map');
                for (const b of positions.slice(i + 1)) if (overlap(footprint(a), footprint(b))) throw Error('overlap');
            }}
            // Large disconnected catalogs exercise the bounded placement path.
            for (const count of [500, 1000]) {{
                const disconnected = Array.from({{length: count}}, (_, i) => ({{thread_id: `thread-${{i+1}}`}}));
                const spread = layoutAgents(disconnected, []);
                const places = [...spread.positions.values()];
                for (const [i, a] of places.entries()) {{
                    if (!Number.isFinite(a.x + a.y) || !inside(spread, a)) throw Error('invalid expanded map');
                    for (const b of places.slice(i + 1)) if (overlap(footprint(a), footprint(b))) throw Error(`overlap at ${{count}} agents`);
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
            // More involved agents orbit strictly closer to the operator and are never smaller.
            const ranked = layoutAgents(sample, [], metrics);
            const operator = ranked.positions.get('operator');
            if (operator.x !== ranked.center.x || operator.y !== ranked.center.y) throw Error('operator not at the centre');
            if (Math.abs(operator.x - ranked.width / 2) > 1e-6 || Math.abs(operator.y - ranked.height / 2) > 1e-6) throw Error('operator not centred');
            for (const [a, score] of scores) {{
                if (overlap(footprint(operator), footprint(ranked.positions.get(a)))) throw Error('agent overlaps operator');
                for (const [b, other] of scores) {{
                    if (score > other && distance(ranked, a) >= distance(ranked, b)) throw Error('hierarchy inverted');
                    if (score > other && ranked.positions.get(a).r < ranked.positions.get(b).r) throw Error('size inverted');
                }}
            }}
            if (ranked.rings.map(ring => ring.label).join() !== 'Core,Active,Occasional,Quiet') throw Error(`wrong tiers ${{ranked.rings.map(ring => ring.label)}}`);
            if (ranked.rings.some((ring, i) => i && ring.radius <= ranked.rings[i - 1].radius)) throw Error('rings out of order');
            const archived = rankAgents(sample, {{...metrics, archived: {{operator_messages: 1e9, agent_peers: 1e9, total_tokens: 1e20}}}});
            if (JSON.stringify([...scores]) !== JSON.stringify([...archived])) throw Error('archived metrics affect ranking');
            const reverse = layoutAgents([...sample].reverse(), [], metrics);
            if (JSON.stringify([...ranked.positions]) !== JSON.stringify([...reverse.positions])) throw Error('unstable rank ties');
            // Host deliveries have a fixed sender node, outside agent ranking.
            const withHost = [...sample, {{thread_id: 'kern-host'}}];
            const hostMetrics = {{...metrics, 'kern-host': {{operator_messages: 1e9, agent_peers: 1e9, total_tokens: 1e20}}}};
            if (JSON.stringify([...scores]) !== JSON.stringify([...rankAgents(withHost, hostMetrics)])) throw Error('host affects ranking');
            const hostMap = layoutAgents(withHost, [{{sender_thread_id: 'kern-host', target_thread_id: 'helper', count: 10}}], hostMetrics);
            if (hostMap.positions.size !== withHost.length) throw Error('host or agents missing');
            const host = hostMap.positions.get('kern-host'), hub = hostMap.positions.get('operator');
            if (hub.y !== host.y) throw Error('senders not side by side');
            if (overlap(footprint(hub), footprint(host))) throw Error('host overlaps operator');
            for (const id of scores.keys()) {{
                if (overlap(footprint(host), footprint(hostMap.positions.get(id)))) throw Error('host overlaps agents');
            }}
            if (Math.abs(hub.x - hostMap.width / 2) > 1e-6) throw Error('operator not centred with host');
            for (const agents of [[{{thread_id:'kern-host'}}], [{{thread_id:'operator'}}, {{thread_id:'kern-host'}}]]) {{
                const map = layoutAgents(agents, []);
                if (map.positions.size !== agents.length) throw Error('empty swarm loses senders');
                for (const point of map.positions.values()) if (!inside(map, point)) throw Error('sender outside map');
                if (map.positions.has('operator')) {{
                    if (map.positions.get('operator').y !== map.positions.get('kern-host').y) throw Error('empty swarm separates senders');
                    if (Math.abs(map.positions.get('operator').x - map.width / 2) > 1e-6) throw Error('empty swarm operator not centred');
                }}
            }}
            // A strong collaborator pulls placement closer, without changing
            // the ranking or dropping disconnected nodes.
            const peers = Array.from({{length: 9}}, (_, i) => ({{thread_id: `a${{i}}`}}));
            const plain = layoutAgents(peers, []);
            for (const target of ['a6', 'a7', 'a8']) {{
                const connected = layoutAgents(peers, [{{sender_thread_id:'a0', target_thread_id:target, count:100}}]);
                const gap = map => Math.hypot(map.positions.get('a0').x - map.positions.get(target).x, map.positions.get('a0').y - map.positions.get(target).y);
                if (gap(connected) >= gap(plain)) throw Error('collaborators did not move closer');
                if (connected.positions.size !== peers.length) throw Error('collaboration lost agents');
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

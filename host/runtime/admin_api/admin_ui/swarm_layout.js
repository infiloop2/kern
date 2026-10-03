// Deterministic weighted springs: frequent collaborators settle closer together.
// Status is deliberately absent, so working/idle transitions never move agents.
function neighboringPairs(points, size, visit) {
  const cells = new Map();
  points.forEach((point, a) => {
    const x = Math.floor(point.x / size), y = Math.floor(point.y / size);
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        for (const b of cells.get(`${x + dx}:${y + dy}`) || []) visit(a, b);
      }
    }
    const key = `${x}:${y}`;
    if (!cells.has(key)) cells.set(key, []);
    cells.get(key).push(a);
  });
}
export function layoutAgents(agents, interactions) {
  const ids = agents.map(agent => agent.thread_id).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  if (!ids.length) return { positions: new Map(), width: 340, height: 336 };
  const index = new Map(ids.map((id, i) => [id, i]));
  const pairs = new Map();
  for (const edge of interactions) {
    const a = index.get(edge.sender_thread_id), b = index.get(edge.target_thread_id);
    if (a === undefined || b === undefined || a === b) continue;
    const key = `${Math.min(a, b)}:${Math.max(a, b)}`;
    pairs.set(key, (pairs.get(key) || 0) + edge.count);
  }
  const links = [...pairs].sort(([a], [b]) => a.localeCompare(b)).map(([key, count]) => {
    const [a, b] = key.split(':').map(Number);
    const strength = Math.log2(1 + count);
    return { a, b, length: 320 + 550 / (1 + strength), pull: .018 + .008 * strength };
  });
  const points = ids.map((id, i) => {
    const angle = i * Math.PI * (3 - Math.sqrt(5));
    const radius = 220 * Math.sqrt(i);
    return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
  });
  // Preserve the detailed small-swarm layout; large catalogs use nearby cells
  // and at most 80,000 node relaxation visits instead of repeated all-pairs work.
  const steps = points.length > 150 ? Math.max(4, Math.min(80, Math.floor(80000 / points.length))) : 500;
  for (let step = 0; step < steps; step++) {
    const forces = points.map(p => ({ x: -p.x * .01, y: -p.y * .01 }));
    const repel = (a, b) => {
      const dx = points[b].x - points[a].x, dy = points[b].y - points[a].y;
      const distance = Math.hypot(dx, dy) || 1;
      const force = 140000 / (distance * distance);
      const x = dx / distance * force, y = dy / distance * force;
      forces[a].x -= x; forces[a].y -= y;
      forces[b].x += x; forces[b].y += y;
    };
    if (points.length > 150) neighboringPairs(points, 640, repel);
    else for (let a = 0; a < points.length; a++) {
      for (let b = a + 1; b < points.length; b++) repel(a, b);
    }
    for (const link of links) {
      const dx = points[link.b].x - points[link.a].x, dy = points[link.b].y - points[link.a].y;
      const distance = Math.hypot(dx, dy) || 1;
      const force = (distance - link.length) * link.pull;
      const x = dx / distance * force, y = dy / distance * force;
      forces[link.a].x += x; forces[link.a].y += y;
      forces[link.b].x -= x; forces[link.b].y -= y;
    }
    const limit = 25 * (1 - step / (steps * 1.2));
    points.forEach((p, i) => {
      const magnitude = Math.hypot(forces[i].x, forces[i].y) || 1;
      const multiplier = Math.min(1, limit / magnitude);
      p.x += forces[i].x * multiplier; p.y += forces[i].y * multiplier;
    });
  }
  // Separate whole character/label footprints, including disconnected nodes.
  for (let step = 0; step < 120; step++) {
    let moved = false;
    neighboringPairs(points, 320, (a, b) => {
      const dx = points[b].x - points[a].x, dy = points[b].y - points[a].y;
      const distance = Math.hypot(dx, dy) || 1;
      if (distance >= 320) return;
      const amount = (320 - distance) / 2 + .01;
      const x = dx / distance * amount, y = dy / distance * amount;
      points[a].x -= x; points[a].y -= y;
      points[b].x += x; points[b].y += y;
      moved = true;
    });
    if (!moved) break;
  }
  // Bounded relaxation can leave small footprint overlaps. Expand uniformly
  // just enough to clear them while preserving the relative cluster geometry.
  let spacing = 1;
  neighboringPairs(points, 320, (a, b) => {
    const dx = Math.abs(points[b].x - points[a].x), dy = Math.abs(points[b].y - points[a].y);
    spacing = Math.max(spacing, Math.min(220.01 / dx, 216.01 / dy));
  });
  if (spacing > 1) points.forEach(p => { p.x *= spacing; p.y *= spacing; });
  const minX = Math.min(...points.map(p => p.x)), minY = Math.min(...points.map(p => p.y));
  const positions = new Map(ids.map((id, i) => [id, { x: points[i].x - minX + 60, y: points[i].y - minY + 60 }]));
  return { positions, width: Math.max(...points.map(p => p.x)) - minX + 340, height: Math.max(...points.map(p => p.y)) - minY + 336 };
}

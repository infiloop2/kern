const SIGNALS = [["operator_messages", .6], ["agent_peers", .25], ["total_tokens", .15]];
const compareIds = new Intl.Collator("en", { numeric: true }).compare;

// Scores describe weekly involvement, not authority. Normalize only against
// visible identities; an archived agent must not set the scale for this map.
export function rankAgents(agents, metrics = {}) {
  const ids = agents.map(agent => agent.thread_id).filter(id => id !== "operator");
  const value = (id, field) => Math.log1p(Math.max(0, metrics[id]?.[field] || 0));
  const maxima = SIGNALS.map(([field]) => ids.reduce((max, id) => Math.max(max, value(id, field)), 0));
  return new Map(ids.map(id => [id, SIGNALS.reduce((score, [field, weight], i) =>
    score + (maxima[i] ? weight * value(id, field) / maxima[i] : 0), 0)]));
}

export function layoutAgents(agents, interactions, metrics = {}) {
  const scores = rankAgents(agents, metrics);
  const ids = [...scores.keys()].sort((a, b) => scores.get(b) - scores.get(a) || compareIds(a, b));
  const hasOperator = agents.some(agent => agent.thread_id === "operator");
  if (!agents.length) return { positions: new Map(), width: 340, height: 336 };

  // Rank determines vertical placement. Compact rows keep even a large idle
  // catalog usable; score offsets within each row preserve the ranking order.
  const capacity = Math.max(2, Math.ceil(Math.sqrt(ids.length * 2)));
  const rows = [];
  for (let i = 0; i < ids.length;) {
    const size = Math.min(capacity, 3 + rows.length * 2);
    rows.push(ids.slice(i, i + size)); i += size;
  }
  const positions = new Map();
  if (hasOperator) positions.set("operator", { x: 0, y: 0 });
  const placeRow = (row, rowIndex) => row.forEach((id, column) => {
    positions.set(id, { x: (column - (row.length - 1) / 2) * 260,
      y: (rowIndex + (hasOperator ? 1 : 0)) * 280 + (1 - scores.get(id)) * 60 });
  });
  rows.forEach(placeRow);

  // Weighted horizontal barycentres bring collaborators together without
  // allowing message traffic to override the vertical involvement hierarchy.
  const neighbors = new Map(agents.map(agent => [agent.thread_id, new Map()]));
  const pairs = new Map();
  for (const edge of interactions) {
    const a = edge.sender_thread_id, b = edge.target_thread_id;
    if (a === b || !neighbors.has(a) || !neighbors.has(b) || edge.count <= 0) continue;
    const key = JSON.stringify([a, b].sort(compareIds));
    pairs.set(key, (pairs.get(key) || 0) + edge.count);
  }
  for (const [key, count] of [...pairs].sort(([a], [b]) => compareIds(a, b))) {
    const [a, b] = JSON.parse(key), weight = Math.log1p(count);
    neighbors.get(a).set(b, weight); neighbors.get(b).set(a, weight);
  }
  const strengths = new Map(ids.map(id => [id,
    [...neighbors.get(id).values()].reduce((sum, value) => sum + value, 0)]));
  // Bounded passes and at most 500 display links; no all-pairs physics.
  for (let pass = 0; pass < 12; pass++) {
    rows.forEach(row => {
      const targets = new Map();
      for (const id of row) {
        let sum = positions.get(id).x, weight = 1;
        for (const [peer, strength] of neighbors.get(id)) {
          sum += positions.get(peer).x * strength; weight += strength;
        }
        targets.set(id, sum / weight);
      }
      // Each row sees the updated rows above it, preventing linked peers on
      // opposite sides from repeatedly swapping places between passes.
      const slots = row.map((_, column) => (column - (row.length - 1) / 2) * 260);
      const connected = row.filter(id => neighbors.get(id).size)
        .sort((a, b) => strengths.get(b) - strengths.get(a) || compareIds(a, b));
      // Give connected agents the nearest free slot to their collaborators.
      // Unconnected agents fill remaining slots in their existing order instead
      // of blocking the centre and cancelling every attraction during packing.
      for (const id of connected) {
        let best = 0;
        for (let i = 1; i < slots.length; i++) {
          if (Math.abs(slots[i] - targets.get(id)) < Math.abs(slots[best] - targets.get(id))) best = i;
        }
        positions.get(id).x = slots.splice(best, 1)[0];
      }
      const disconnected = row.filter(id => !neighbors.get(id).size)
        .sort((a, b) => positions.get(a).x - positions.get(b).x || compareIds(a, b));
      disconnected.forEach((id, i) => { positions.get(id).x = slots[i]; });
    });
  }

  // All rows are centred on the operator, whose card sits alone above them.
  let minX = 0, maxX = 0, maxY = 0;
  for (const point of positions.values()) {
    minX = Math.min(minX, point.x); maxX = Math.max(maxX, point.x); maxY = Math.max(maxY, point.y);
  }
  const width = maxX - minX + 340;
  for (const point of positions.values()) { point.x += 60 - minX; point.y += 60; }
  // Return canonical order, independent of API ordering or status changes.
  return { positions: new Map([...positions].sort(([a], [b]) => compareIds(a, b))), width, height: maxY + 336 };
}

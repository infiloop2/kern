const SIGNALS = [["operator_messages", .6], ["agent_peers", .25], ["total_tokens", .15]];
const compareIds = new Intl.Collator("en", { numeric: true }).compare;

// Scores describe weekly involvement, not authority. Normalize only against
// visible identities; an archived agent must not set the scale for this map.
export function rankAgents(agents, metrics = {}) {
  const ids = agents.map(agent => agent.thread_id).filter(id => !["operator", "kern-host"].includes(id));
  const value = (id, field) => Math.log1p(Math.max(0, metrics[id]?.[field] || 0));
  const maxima = SIGNALS.map(([field]) => ids.reduce((max, id) => Math.max(max, value(id, field)), 0));
  return new Map(ids.map(id => [id, SIGNALS.reduce((score, [field, weight], i) =>
    score + (maxima[i] ? weight * value(id, field) / maxima[i] : 0), 0)]));
}

// Involvement tiers drawn as orbit rings. The last tier always matches.
export const TIERS = [[.5, "Core"], [.15, "Active"], [1e-9, "Occasional"], [-Infinity, "Quiet"]];
export const tierOf = score => TIERS.find(([min]) => score >= min)[1];

// Orbs grow with involvement; every node reserves a name label beneath it.
const LABEL_HALF_WIDTH = 80, LABEL_HEIGHT = 44, ORB_GAP = 8;
const INNER_RADIUS = 170, SPACING = 104, CELL = 180, MARGIN = 60;
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
export function orbRadius(id, score = 0) {
  if (id === "operator") return 46;
  if (id === "kern-host") return 34;
  return Math.round(28 + 18 * score);
}
export function footprint(point) {
  return { left: point.x - LABEL_HALF_WIDTH, right: point.x + LABEL_HALF_WIDTH,
    top: point.y - point.r - ORB_GAP, bottom: point.y + point.r + LABEL_HEIGHT };
}
const overlaps = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;

// Uniform buckets keep each collision check local, even for huge catalogs.
function collisionGrid() {
  const cells = new Map();
  const keys = box => {
    const out = [];
    for (let x = Math.floor(box.left / CELL); x <= Math.floor(box.right / CELL); x++) {
      for (let y = Math.floor(box.top / CELL); y <= Math.floor(box.bottom / CELL); y++) out.push((x + 32768) * 65536 + y + 32768);
    }
    return out;
  };
  return {
    hits: box => keys(box).some(key => cells.get(key)?.some(other => overlaps(box, other))),
    add: box => { for (const key of keys(box)) cells.has(key) ? cells.get(key).push(box) : cells.set(key, [box]); },
  };
}

// The operator sits at the centre. Each agent's distance from it grows
// strictly with involvement rank, so more involved agents always orbit closer
// and appear larger. Angles follow a golden-angle spiral, bent towards the
// agents each one messages most, so collaborators gather into neighbourhoods.
export function layoutAgents(agents, interactions, metrics = {}) {
  if (!agents.length) return { positions: new Map(), width: 340, height: 336, center: { x: 170, y: 168 }, rings: [] };
  const scores = rankAgents(agents, metrics);
  const ids = [...scores.keys()].sort((a, b) => scores.get(b) - scores.get(a) || compareIds(a, b));
  const hasOperator = agents.some(agent => agent.thread_id === "operator");
  const hasHost = agents.some(agent => agent.thread_id === "kern-host");
  const fixed = new Map();
  if (hasOperator) fixed.set("operator", { x: 0, y: 0, r: orbRadius("operator") });
  if (hasHost) fixed.set("kern-host", { x: hasOperator ? 240 : 0, y: 0, r: orbRadius("kern-host") });

  // Weighted links between ranked agents decide which angle each one prefers.
  // Operator and host links do not pull: both already sit at the centre.
  const neighbors = new Map(ids.map(id => [id, new Map()]));
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

  // Later passes let lower-ranked collaborators pull on agents placed before
  // them. Each pass is greedy and collision-checked; no all-pairs physics.
  let positions = new Map(), angles = new Map(), radii = new Map();
  for (let pass = 0; pass < 3; pass++) {
    const grid = collisionGrid();
    positions = new Map(fixed);
    for (const point of fixed.values()) grid.add(footprint(point));
    const placed = new Map();
    let radius = 0;
    ids.forEach((id, rank) => {
      const own = angles.get(id) ?? rank * GOLDEN_ANGLE - Math.PI / 2;
      let sx = Math.cos(own), sy = Math.sin(own);
      for (const [peer, weight] of neighbors.get(id)) {
        const angle = pass ? angles.get(peer) : placed.get(peer);
        if (angle === undefined) continue;
        sx += weight * Math.cos(angle); sy += weight * Math.sin(angle);
      }
      const preferred = Math.hypot(sx, sy) > 1e-6 ? Math.atan2(sy, sx) : own;
      const r = orbRadius(id, scores.get(id));
      radius = Math.max(radius + 1, INNER_RADIUS + SPACING * Math.sqrt(rank));
      for (;;) {
        // Search outward from the preferred angle in ~40px arc steps; only a
        // full ring pushes this and every later agent further out.
        const step = Math.min(Math.PI / 12, 40 / radius);
        let point = null;
        for (let k = 0; k * step <= Math.PI && !point; k++) {
          for (const sign of k ? [1, -1] : [1]) {
            const angle = preferred + sign * k * step;
            const candidate = { x: radius * Math.cos(angle), y: radius * Math.sin(angle), r };
            if (!grid.hits(footprint(candidate))) { point = candidate; placed.set(id, angle); break; }
          }
        }
        if (point) { positions.set(id, point); grid.add(footprint(point)); radii.set(id, radius); break; }
        radius += 24;
      }
    });
    angles = placed;
  }

  // Rings separate involvement tiers; the outermost encloses the swarm.
  const rings = [];
  let previous = null;
  for (const id of ids) {
    const tier = tierOf(scores.get(id));
    if (previous && previous.tier !== tier) rings.push({ radius: (previous.radius + radii.get(id)) / 2, label: previous.tier });
    previous = { tier, radius: radii.get(id) };
  }
  if (previous) rings.push({ radius: previous.radius + 110, label: previous.tier });
  // Each ring label sits where its ring passes furthest from any agent.
  const agentsOnly = ids.map(id => positions.get(id)).concat([...fixed.values()]);
  for (const ring of rings) {
    const nearby = agentsOnly.filter(point => Math.abs(Math.hypot(point.x, point.y) - ring.radius) <= 200);
    let best = -Infinity;
    for (let step = 0; step < 48; step++) {
      const angle = -Math.PI / 2 + step * Math.PI / 24;
      const x = ring.radius * Math.cos(angle), y = ring.radius * Math.sin(angle);
      let clearance = Infinity;
      for (const point of nearby) {
        clearance = Math.min(clearance, Math.hypot((point.x - x) / 1.6, point.y + point.r / 2 - y));
      }
      if (clearance > best + 1) { best = clearance; ring.angle = angle; }
    }
  }

  // Extents are symmetric so the operator stays at the centre of the map.
  let extentX = 0, extentY = 0;
  for (const point of positions.values()) {
    const box = footprint(point);
    extentX = Math.max(extentX, -box.left, box.right); extentY = Math.max(extentY, -box.top, box.bottom);
  }
  const center = { x: extentX + MARGIN, y: extentY + MARGIN };
  for (const point of positions.values()) { point.x += center.x; point.y += center.y; }
  // Return canonical order, independent of API ordering or status changes.
  return { positions: new Map([...positions].sort(([a], [b]) => compareIds(a, b))),
    width: 2 * center.x, height: 2 * center.y, center, rings };
}

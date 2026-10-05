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

// Involvement tiers are used in details. The last tier always matches.
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

// Uniform buckets keep each collision check local, even for huge catalogs.
function collisionGrid() {
  const cells = new Map();
  return {
    hits: (x, y, r) => {
      const left = x - LABEL_HALF_WIDTH, right = x + LABEL_HALF_WIDTH;
      const top = y - r - ORB_GAP, bottom = y + r + LABEL_HEIGHT;
      for (let cx = Math.floor(left / CELL); cx <= Math.floor(right / CELL); cx++) {
        for (let cy = Math.floor(top / CELL); cy <= Math.floor(bottom / CELL); cy++) {
          const bucket = cells.get((cx + 32768) * 65536 + cy + 32768);
          if (!bucket) continue;
          for (const other of bucket) if (left < other.right && other.left < right && top < other.bottom && other.top < bottom) return true;
        }
      }
      return false;
    },
    add: point => {
      const box = footprint(point);
      for (let cx = Math.floor(box.left / CELL); cx <= Math.floor(box.right / CELL); cx++) {
        for (let cy = Math.floor(box.top / CELL); cy <= Math.floor(box.bottom / CELL); cy++) {
          const key = (cx + 32768) * 65536 + cy + 32768;
          if (cells.has(key)) cells.get(key).push(box); else cells.set(key, [box]);
        }
      }
    },
  };
}

// Settle springs offline: the UI renders only the resulting coordinates.
// Ranking seeds a deterministic spiral; involvement adds a gentle centre pull.
export function layoutAgents(agents, interactions, metrics = {}, previous = null) {
  if (!agents.length) return { positions: new Map(), width: 340, height: 336, center: { x: 170, y: 168 } };
  const scores = rankAgents(agents, metrics);
  const ids = [...scores.keys()].sort((a, b) => scores.get(b) - scores.get(a) || compareIds(a, b));
  const hasOperator = agents.some(agent => agent.thread_id === "operator");
  const hasHost = agents.some(agent => agent.thread_id === "kern-host");
  const fixed = new Map();
  if (hasOperator) fixed.set("operator", { x: 0, y: 0, r: orbRadius("operator") });
  if (hasHost) fixed.set("kern-host", { x: hasOperator ? 240 : 0, y: 0, r: orbRadius("kern-host") });
  // Previous coordinates include the map margin. Translate them back to the
  // operator origin before simulation; camera compensation is handled by UI.
  const origin = previous?.get("operator") || previous?.get("kern-host");
  const density = Math.sqrt(Math.max(1, ids.length / 200));
  const seedSpacing = ids.length >= 500 ? 156 : SPACING;
  const nodes = ids.map((id, rank) => {
    const old = origin && previous.get(id);
    const radius = INNER_RADIUS + seedSpacing * Math.sqrt(rank);
    const angle = rank * GOLDEN_ANGLE - Math.PI / 2;
    const x = old ? old.x - origin.x : radius * Math.cos(angle);
    const y = old ? old.y - origin.y : radius * Math.sin(angle);
    return { id, order: rank, gravity: (.0005 + .004 * scores.get(id)) / density, x, y, r: orbRadius(id, scores.get(id)), anchor: old ? { x, y } : null, peers: new Set() };
  });
  const index = new Map(nodes.map((point, i) => [point.id, i]));
  // Canonical pairs sum both directions. Synthetic sender links never spring.
  const pairs = new Map();
  for (const edge of interactions) {
    const a = edge.sender_thread_id, b = edge.target_thread_id;
    if (a === b || !index.has(a) || !index.has(b) || edge.count <= 0) continue;
    const key = JSON.stringify([a, b].sort(compareIds));
    pairs.set(key, (pairs.get(key) || 0) + edge.count);
  }
  const links = [...pairs].sort(([a], [b]) => compareIds(a, b)).map(([key, count]) => {
    const [a, b] = JSON.parse(key);
    return { a: nodes[index.get(a)], b: nodes[index.get(b)], weight: Math.log1p(count) };
  });
  const maxWeight = Math.max(1, ...links.map(link => link.weight));
  for (const link of links) { link.weight /= maxWeight; link.a.peers.add(link.b); link.b.peers.add(link.a); }
  const fixedPoints = [...fixed.values()], count = nodes.length;
  const iterations = Math.max(24, Math.min(60, Math.ceil(40000 / Math.max(1, count))));
  const timeStep = 200 / iterations, cooling = Math.pow(.978, timeStep);
  let alpha = previous ? .3 : 1;
  // Reuse numeric buffers throughout settling. The head grid includes the
  // maximum possible travel, so neither buckets nor force objects allocate
  // inside the iteration loop. Each node's displacement is capped below.
  const x = new Float64Array(count + fixedPoints.length), y = new Float64Array(x.length);
  const fx = new Float64Array(count), fy = new Float64Array(count);
  const radii = new Float64Array(x.length), gravity = new Float64Array(count);
  let seedExtentX = 0, seedExtentY = 0;
  nodes.concat(fixedPoints).forEach((point, i) => {
    x[i] = point.x; y[i] = point.y; radii[i] = point.r;
    if (i < count) gravity[i] = point.gravity;
    seedExtentX = Math.max(seedExtentX, Math.abs(point.x)); seedExtentY = Math.max(seedExtentY, Math.abs(point.y));
  });
  const travel = 24 * alpha * timeStep / (1 - cooling), cellSize = 340;
  const ox = Math.ceil((seedExtentX + travel) / cellSize) + 1, oy = Math.ceil((seedExtentY + travel) / cellSize) + 1;
  const columns = ox * 2 + 1, rows = oy * 2 + 1;
  const head = new Int32Array(columns * rows), next = new Int32Array(count);
  const offsets = [1, rows - 1, rows, rows + 1];
  const endsA = Int32Array.from(links, link => link.a.order), endsB = Int32Array.from(links, link => link.b.order);
  const strengths = Float64Array.from(links, link => (.025 + .22 * link.weight) / Math.sqrt(Math.min(link.a.peers.size, link.b.peers.size)));
  const peers = nodes.map(point => new Set([...point.peers].map(other => other.order)));
  const repel = (a, b) => {
    const dx = x[a] - x[b], dy = y[a] - y[b], ax = Math.abs(dx), ay = Math.abs(dy);
    if (ax >= 340 || ay >= 260) return;
    const related = peers[a].size && peers[a].has(b);
    const rx = related ? 170 : 340, ry = related ? radii[a] + radii[b] + LABEL_HEIGHT + ORB_GAP : 260;
    if (ax >= rx || ay >= ry) return;
    const length = ax + ay, force = (1 - Math.max(ax / rx, ay / ry)) * 100;
    const px = (length ? dx / length : Math.cos(a * GOLDEN_ANGLE)) * force;
    const py = (length ? dy / length : Math.sin(a * GOLDEN_ANGLE)) * force;
    fx[a] += px; fy[a] += py;
    if (b < count) { fx[b] -= px; fy[b] -= py; }
  };
  for (let iteration = 0; iteration < iterations; iteration++, alpha *= cooling) {
    head.fill(-1);
    for (let i = 0; i < count; i++) {
      fx[i] = -x[i] * gravity[i]; fy[i] = -y[i] * gravity[i];
      const anchor = nodes[i].anchor;
      if (anchor) { fx[i] += (anchor.x - x[i]) * .15; fy[i] += (anchor.y - y[i]) * .15; }
      const cell = (Math.floor(x[i] / cellSize) + ox) * rows + Math.floor(y[i] / cellSize) + oy;
      next[i] = head[cell]; head[cell] = i;
    }
    for (let link = 0; link < links.length; link++) {
      const a = endsA[link], b = endsB[link], dx = x[b] - x[a], dy = y[b] - y[a];
      const distance = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (distance - 170) / distance * strengths[link];
      fx[a] += dx * force; fy[a] += dy * force; fx[b] -= dx * force; fy[b] -= dy * force;
    }
    // Visit each neighboring bucket pair once. Repulsion stays local even
    // for disconnected catalogs; synthetic senders remain fixed.
    for (let cell = 0; cell < head.length; cell++) {
      const first = head[cell];
      if (first < 0) continue;
      for (let a = first; a >= 0; a = next[a]) for (let b = next[a]; b >= 0; b = next[b]) repel(a, b);
      for (const offset of offsets) {
        const neighbor = head[cell + offset] ?? -1;
        for (let a = first; a >= 0; a = next[a]) for (let b = neighbor; b >= 0; b = next[b]) repel(a, b);
      }
    }
    for (let i = 0; i < count; i++) {
      for (let fixed = count; fixed < x.length; fixed++) repel(i, fixed);
      const squared = fx[i] * fx[i] + fy[i] * fy[i];
      const step = alpha * timeStep * (squared > 576 ? 24 / Math.sqrt(squared) : 1);
      x[i] += fx[i] * step; y[i] += fy[i] * step;
      const anchor = nodes[i].anchor;
      if (anchor) {
        const dx = x[i] - anchor.x, dy = y[i] - anchor.y, distance = Math.sqrt(dx * dx + dy * dy);
        if (distance > 20) { x[i] = anchor.x + dx * 20 / distance; y[i] = anchor.y + dy * 20 / distance; }
      }
    }
  }
  nodes.forEach((point, i) => { point.x = x[i]; point.y = y[i]; });
  // Guarantee room for names, in rank order. Search around each simulated
  // position instead of projecting it onto an involvement ring.
  const positions = new Map(fixed), grid = collisionGrid();
  for (const point of fixedPoints) grid.add(point);
  for (const simulated of nodes) {
    const { x, y, r } = simulated;
    let point = { x, y, r };
    if (grid.hits(point.x, point.y, point.r) && simulated.anchor) point = { ...simulated.anchor, r };
    for (let radius = 80; grid.hits(point.x, point.y, point.r); radius += 80) {
      const step = Math.min(Math.PI / 6, 120 / radius);
      let found = false;
      for (let k = 0; k * step < Math.PI * 2; k++) {
        const angle = index.get(simulated.id) * GOLDEN_ANGLE + k * step;
        const cx = x + radius * Math.cos(angle), cy = y + radius * Math.sin(angle);
        if (!grid.hits(cx, cy, r)) { point = { x: cx, y: cy, r }; found = true; break; }
      }
      if (found) break;
    }
    positions.set(simulated.id, point); grid.add(point);
  }
  // Symmetric extents keep the operator in the centre of the map.
  let extentX = 0, extentY = 0;
  for (const point of positions.values()) {
    const box = footprint(point);
    extentX = Math.max(extentX, -box.left, box.right); extentY = Math.max(extentY, -box.top, box.bottom);
  }
  const center = { x: extentX + MARGIN, y: extentY + MARGIN };
  for (const point of positions.values()) { point.x += center.x; point.y += center.y; }
  return { positions: new Map([...positions].sort(([a], [b]) => compareIds(a, b))),
    width: 2 * center.x, height: 2 * center.y, center };
}

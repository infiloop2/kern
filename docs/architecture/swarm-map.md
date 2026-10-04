# Swarm map

The map is a constellation. The operator sits at its centre, with Kern host
beside it on the right at the same height. All active Chat, App, Standing and
Spawned agents remain visible, including idle and disconnected agents; Bash
schedules are excluded. Each agent type is a distinct robot that keeps its body
as runtime state changes: Apps are retro monitors on a stand, Standing agents
are owls with a clock belly, Spawned agents are small propeller drones, and
On-demand chats are capsule bots with an antenna. Orbs are tinted by type and a
legend shows each robot. Eyes, props and a status halo show state: a spinning
halo and working prop (code lines, turning clock hands, spinning rotor or a
typing bubble) for Working, an amber halo and `?` badge for pending approvals,
and a red halo with crossed-out eyes and `!` for errors. Idle agents sleep. Hover
or keyboard focus shows the type and task or purpose.

## Automatic involvement hierarchy

The last seven UTC dates determine placement, using three signals:

- 60% direct accepted operator messages received (automated triggers excluded).
- 25% distinct other agents contacted in either direction. Reciprocal traffic
  counts as one peer; operator and self-links do not count toward breadth.
- 15% known tokens processed, summed across the four non-overlapping usage
  buckets described in [Token analytics](token-analytics.md). This includes
  cache reads and writes without counting them again as plain input.

For each signal, take `log(1 + count)` and divide by the largest transformed
value among the agents on the map. An absent or all-zero signal contributes
zero; its weight is not redistributed. Sum the three weighted values. Operator
attention can contribute 60 points, while breadth and tokens together contribute
at most 40. This is an involvement heuristic, not reporting authority or a
measurement of output quality. Changing the active roster changes normalization.

Metrics aggregate all recorded weekly pairs in SQL before the drawing limit.
A peer can count even if it has since been archived. Token accounting uses the
existing turn's latest measurement date, including its known partial buckets;
missing measurements are not reconstructed. Details distinguish unavailable,
partial, and measured zero token counts and show the current score and inputs.

Agents are sorted by score; ties use immutable IDs. Each agent's distance from
the operator grows strictly with rank (`170 + 104 * sqrt(rank)` px, pushed out
only when a ring is full), so a more involved agent always orbits closer. Its orb
also grows with score. Faint dashed rings separate the involvement tiers and are
labelled Core (score at least 0.5), Active (at least 0.15), Occasional (above 0)
and Quiet. Angles follow a golden-angle spiral, bent towards the weighted
circular mean of each agent's linked collaborators using log-scaled, combined
bidirectional message counts. Operator and host links do not bend angles. Three
greedy passes place agents and let lower-ranked collaborators pull on earlier
ones. A uniform collision grid keeps orb and name footprints apart, so placement
needs no all-pairs physics. Runtime status has no influence on placement.

Initial load, a changed agent roster, and **Arrange** compute placement.
Refreshing counts, status, metrics or the set of links preserves positions;
**Arrange** applies the latest ranking and fits the map. Details always show
the latest available metrics, which may differ from the last arrangement.

## Connection display and failures

At most 500 directed message links are drawn, chosen by weekly count. This
bounds browser work and clutter; it never omits agents or limits ranking
metrics. Small, fixed-size arrows show direction, thickness and opacity show
volume, and selection shows exact counts. Link colour follows the sender: gold
for operator messages, slate for Kern host deliveries, and teal between agents.
Moving pulses run along links whose sender or target is working now, faster for
busier links. Selecting an agent fades agents outside its direct neighbourhood.
Collaborator grouping uses these displayed links.

The map is an unbounded canvas. Drag anywhere, including on an agent, to pan
past every agent into open space. A press counts as a click unless it moves
more than 4 px. Scroll or a trackpad pans; Ctrl/⌘ + scroll, trackpad pinch and
two-finger touch pinch zoom around the pointer. One finger drags on touch. With
the map focused, arrow keys pan, `+`/`−` zoom and `0` fits everything. The dot
grid moves with the camera. A minimap shows every agent and the current view,
widens to include the view when it leaves the swarm, and moves the view on click
or drag. Fit all, 100% and Arrange sit on the map. Large swarms open on the
inner orbits at a readable size. Below 40% zoom, idle agents without approvals
hide their names. Focusing an off-screen agent with the keyboard glides it into
view, and the focused link keeps focus when a refresh redraws the map. Status
halos, pulses and glides are disabled when reduced motion is requested.

The counts/metrics request is optional. A failure keeps the agent map usable
with the last successful connections and metrics and a visible stale-data
notice. On an initial failure, agents have neutral scores and details say
metrics are unavailable. After recovery, Arrange applies the recovered ranking.
No additional telemetry storage, migration, provider request or inference is
needed. SQL aggregates counts and usage without reading conversation bodies.

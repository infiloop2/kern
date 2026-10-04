# Swarm map

The operator sits at the top centre, with Kern host beside it on the right at
the same height. All active Chat, App, Standing and Spawned
agents remain visible, including idle and disconnected agents; Bash schedules
are excluded. Characters retain their type, task or purpose, and runtime status.
Each agent type has a small head detail within the shared character style:
an app window, an on-demand speech bubble, a standing-agent clock, or spawned
branching nodes. The detail stays the same as the agent's runtime state changes.

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

Agents are sorted by score into compact rows that widen below the operator;
higher scores sit higher, including within a row. Ties use immutable IDs.
Twelve bounded horizontal ordering passes bring linked agents closer using
log-scaled, combined bidirectional message counts. Complete character/label
footprints stay separated. Runtime status has no influence on placement.

Initial load, a changed agent roster, and **Arrange** compute placement.
Refreshing counts, status, metrics or the set of links preserves positions;
**Arrange** applies the latest ranking and fits the map. Details always show
the latest available metrics, which may differ from the last arrangement.

## Connection display and failures

At most 500 directed message links are drawn, chosen by weekly count. This
bounds browser work and clutter; it never omits agents or limits ranking
metrics. Small, fixed-size arrows show direction, thickness shows volume, and
selection shows exact counts. Horizontal grouping uses these displayed links.

Drag the map background to pan with a mouse or pen. Clicking agents and
connections still opens their details. Touch swipes, scrollbars and keyboard
scrolling remain available, along with zoom and Fit all.

The counts/metrics request is optional. A failure keeps the agent map usable
with the last successful connections and metrics and a visible stale-data
notice. On an initial failure, agents have neutral scores and details say
metrics are unavailable. After recovery, Arrange applies the recovered ranking.
No additional telemetry storage, migration, provider request or inference is
needed. SQL aggregates counts and usage without reading conversation bodies.

// Runtime state comes from Kern; human attention is a separate AI assessment.
export const SWARM_POSES = Object.freeze({ BUSY: "busy", FAILED: "failed", IDLE: "idle", NEEDS_HUMAN: "needs-human" });
export function poseForAgent(agent) {
  if (agent?.state === "busy") return SWARM_POSES.BUSY;
  if (agent?.state === "failed") return SWARM_POSES.FAILED;
  return agent?.needs_human === true ? SWARM_POSES.NEEDS_HUMAN : SWARM_POSES.IDLE;
}

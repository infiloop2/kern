// Runtime state wins; pending native approvals determine human attention.
export const SWARM_POSES = Object.freeze({ BUSY: "busy", FAILED: "failed", IDLE: "idle", NEEDS_HUMAN: "needs-human" });
export function poseForAgent(agent) {
  if (agent?.state === "busy") return SWARM_POSES.BUSY;
  if (agent?.state === "failed") return SWARM_POSES.FAILED;
  return agent?.pending_approval_count > 0 ? SWARM_POSES.NEEDS_HUMAN : SWARM_POSES.IDLE;
}

"""One fal account for H3 Max generation and Topaz enhancement."""
from dataclasses import replace
from typing import cast

from host.tools.fal_ai import h3max, topaz
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject
from host.tools.manifest import (
    ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, SetupStep,
    ToolManifest, validated_input,
)
from host.tools.results import ActionFailed, ActionResult, ApprovalResult
from host.tools.shared import outputs

TASK_INPUT = outputs.obj({"task_id": outputs.text("Unchanged task id from generate_video or upscale_image/video.")}, ["task_id"])
POLL_OUTPUT = outputs.obj({
    **cast(JSONObject, h3max.GET_TASK_OUTPUT_SCHEMA["properties"]),
    **cast(JSONObject, topaz.TASK_OUTPUT["properties"]),
}, ["task_id", "task_status", "message"])


def task_action(name: str) -> ActionSpec:
    source = next(a for a in (topaz.MANIFEST.actions if name == "save_image" else h3max.MANIFEST.actions) if a.id == name)
    return replace(source,
        description=("Poll an H3 Max or Topaz task. H3 Max returns video_url; Topaz returns output_url and output_kind. Save successful results promptly." if name == "get_task" else
                     f"Save a completed falAI {name.removeprefix('save_')} into the private workspace, up to 200 MB."),
        data_policy="Sends the validated task id to the pinned fal queue. Saving streams the authoritative result URL into a private workspace file; it does not publish anything.",
        input_schema=TASK_INPUT,
        output_schema=POLL_OUTPUT if name == "get_task" else {},
        input_protections={"task_id": validated_input("H3 Max text_/image_/reference_ or Topaz topaz_image_/topaz_video_ prefix followed by UUID.")},
    )


MANIFEST = ToolManifest(
    tool_id="fal_ai", display_name="falAI", connection="enable_only", reports_cost=True,
    description="Generate native-audio video with H3 Max and enhance image keyframes or videos with Topaz, using one fal account.",
    actions=(h3max.MANIFEST.actions[0], topaz.enhancement_action("image"), topaz.enhancement_action("video"),
             task_action("get_task"), task_action("save_image"), task_action("save_video")),
    config=(ConfigRequirement(key="FAL_API_KEY", description="API-scoped fal key shared by H3 Max and Topaz. Configure anew when replacing the retired H3 Max integration."),),
    setup_steps=(
        SetupStep(title="Create a fal API key", description="Fund the intended fal account and create an API-scoped key. H3 Max and Topaz spend the same fal balance. A Topaz desktop license does not cover these calls.", link_url="https://fal.ai/dashboard/keys", link_label="Open fal API keys"),
        SetupStep(title="Configure falAI", description="Save FAL_API_KEY and enable falAI. This replaces the H3 Max integration; its old key and enabled state are not migrated. Connect once to use both H3 Max and Topaz.", show_config=True),
        SetupStep(title="Check billing", description="Kern calculates published charges for simple H3 Max generation only. Reference-mode H3 Max and Topaz costs are not tracked. Consult fal billing; missing cost records do not mean free processing.", link_url="https://fal.ai/dashboard/billing", link_label="Open fal billing"),
    ),
    protections=tuple(dict.fromkeys((*h3max.MANIFEST.protections, *topaz.MANIFEST.protections))),
    technical_details=tuple(dict.fromkeys((*h3max.MANIFEST.technical_details, *topaz.MANIFEST.technical_details))),
    data_summary=DataSummary(cards=(
        DataSummaryCard(title="What leaves this host", description="H3 Max sends prompts, settings and selected staged media bytes. Topaz sends staged image/video bytes and enhancement settings. Uploads use generic filenames; workspace paths and caller-supplied URLs are not accepted. Prompts pass the host parameter guard."),
        DataSummaryCard(title="Where it can go", description="fal queue and storage services, H3 Max and Topaz processing, and their infrastructure providers. Selected files upload to fal storage before processing.", links=(DataSummaryLink(label="fal models", url="https://fal.ai/models"), DataSummaryLink(label="Topaz on fal", url="https://fal.ai/topaz"))),
        DataSummaryCard(title="What providers can do with it", description="fal and its model providers process media under their terms and privacy policies. No extra training-use or confidentiality guarantee is made. H3 Max retains its existing setting that disables fal's optional safety checker; provider policies still apply.", links=(DataSummaryLink(label="fal privacy", url="https://fal.ai/privacy"), DataSummaryLink(label="Topaz privacy", url="https://www.topazlabs.com/privacy-policy"))),
        DataSummaryCard(title="How long it is retained", description="Requests disable fal request-history storage and request a 24-hour media lifetime. Provider media URLs are accessible to anyone holding them until expiry. This does not erase provider billing records. Saved workspace files remain until removed.", links=(DataSummaryLink(label="fal retention", url="https://fal.ai/docs/documentation/model-apis/media-expiration"),)),
    )),
    agent_notes="generate_video uses H3 Max; upscale_image and upscale_video use Topaz. One FAL_API_KEY config. " + h3max.MANIFEST.agent_notes + " " + topaz.MANIFEST.agent_notes,
)


class FalAITool:
    manifest = MANIFEST
    credentials = None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action not in {a.id for a in MANIFEST.actions}:
            return ActionFailed("Unsupported falAI action.")
        task_id = tool_input.get("task_id")
        if action in {"upscale_image", "upscale_video", "save_image"} or (
            action in {"get_task", "save_video"} and isinstance(task_id, str) and task_id.startswith("topaz_")
        ):
            return topaz.TopazTool().execute(action, tool_input, api)
        return h3max.H3MaxTool().execute(action, tool_input, api)

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        return ActionFailed("falAI has no approval-gated actions.")


BUNDLED_TOOL = FalAITool()

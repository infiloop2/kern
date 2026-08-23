// Operator-facing catalog for network integrations that are built into the
// host rather than supplied by tool manifests. The compact integration card
// and the full Home detail page render this same content.

export const MANAGED_INTEGRATIONS = {
  openai: {
    label: "OpenAI",
    summary: "Connect your OpenAI subscription and let your agent use Codex for tasks and cached web search.",
    protections: [
      "The linked OpenAI account is pinned. Authenticated traffic for another account is denied until you explicitly disconnect and log in again.",
      "Live browsing and remote tool servers are blocked. Codex can use only OpenAI's cached web search.",
    ],
    setupSteps: [
      { title: "Enable OpenAI", description: "On Home, open OpenAI under Integrations and choose Enable." },
      { title: "Start the Codex login", description: "In Account, choose Start Codex login. In the OpenAI browser sign-in, use the subscription you want this host to use and enter the displayed device code to complete sign-in." },
      { title: "Verify the linked account", description: "Return to Kern and wait for the row to show connected with the expected email or account id. That identity is now the operator-approved account anchor." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Assume any host data available to Codex can go to OpenAI, including prompts, conversation history, workspace files and diffs, tool inputs, and tool results.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "OpenAI", text: "Everything the agent sends goes to OpenAI's services under the linked account." },
            { label: "Service providers", text: "OpenAI shares selected content onward with its trusted service providers for safety and data annotation, so data can leave OpenAI itself." },
            { label: "Web search", text: "Cached web search keeps the search query and surrounding context within OpenAI; no external site is contacted for the request." },
          ],
          links: [],
        },
        {
          title: "What OpenAI can do with it",
          description: "This guide assumes a personal ChatGPT/Codex OAuth subscription. One account setting, Improve the model for everyone, controls training use.",
          points: [
            { label: "Before connecting", text: "Turn off Improve the model for everyone in ChatGPT Settings > Data Controls. While it is on, OpenAI may use new conversations and Codex content to improve its models; once off, OpenAI says new conversations are not used for model training. The setting changes training use, not retention." },
            { label: "Either way", text: "Limited reviewers may access content for abuse or security investigations, support, or legal matters." },
          ],
          links: [
            { url: "https://help.openai.com/en/articles/7730893-chatgpt-data-usage-for-model-training", label: "OpenAI Data Controls instructions" },
            { url: "https://help.openai.com/en/articles/7039943", label: "OpenAI consumer data usage FAQ" },
            { url: "https://openai.com/policies/privacy-policy/", label: "OpenAI Privacy Policy" },
          ],
        },
        {
          title: "How long OpenAI retains it",
          description: "Codex chats and their content remain saved until you delete them.",
          points: [
            { label: "After deletion", text: "OpenAI schedules permanent deletion within 30 days unless data was de-identified, disassociated from your account, or must be kept for security or legal reasons." },
          ],
          links: [
            { url: "https://help.openai.com/en/articles/20001333-how-to-archive-and-delete-codex-chats-in-the-chatgpt-app", label: "OpenAI Codex retention and deletion" },
          ],
        },
      ],
    },
    capabilities: [
      { name: "Codex model access", description: "Runs Codex tasks through the models and usage limits available to the linked OpenAI subscription." },
      { name: "Cached web search", description: "Lets Codex search OpenAI's existing index or cache. Kern denies request forms that would let OpenAI fetch live external pages for the request." },
    ],
    controls: [
      "The proxy fails closed when the account pin or request body cannot be checked.",
    ],
    networkScope: [
      ["api.openai.com", "POST; pinned-account and external-URL request guards"],
      ["auth.openai.com", "GET and POST for the operator login flow"],
      ["chatgpt.com", "GET and POST; pinned-account and external-URL request guards"],
    ],
  },
  claude: {
    label: "Claude",
    summary: "Connect your Anthropic subscription and let your agent use Claude Code for tasks. Web search is optional and off by default.",
    protections: [
      "The linked Anthropic account and OAuth token are pinned. Credentials for another account are denied until you explicitly disconnect and log in again.",
      "Web search is off by default. When you enable it, the query and surrounding context reach Anthropic's server-side search, which may use search partners and retrieve source pages outside Kern's boundary. Server-side web fetch, code execution, and remote tool servers stay blocked at the proxy regardless; the agent's own web fetch runs on this host and can reach only Kern's allowed domains.",
    ],
    setupSteps: [
      { title: "Enable Claude", description: "On Home, open Claude under Integrations and choose Enable." },
      { title: "Start the Claude Code login", description: "In Account, choose Start Claude Code login. Follow the displayed Anthropic OAuth flow and paste the authorization result when prompted." },
      { title: "Verify the linked account", description: "Wait for the row to show connected with the expected Anthropic identity. Kern validates the token live before reporting the runtime active." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Assume any host data available to Claude Code can go to Anthropic, including prompts, conversation history, workspace files and diffs, tool inputs, and tool results.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "Anthropic", text: "Everything the agent sends goes to Anthropic's services under the linked account, with service providers used to operate Claude." },
            { label: "Search partners (only if web search is enabled)", text: "With web search enabled, the query may go to Anthropic's search partners and Anthropic may retrieve source pages, outside Kern's network boundary. Anthropic does not name which third-party search providers it uses. With web search off (the default), nothing leaves for search." },
          ],
          links: [],
        },
        {
          title: "What Anthropic can do with it",
          description: "This guide assumes a personal Claude Free, Pro, or Max OAuth subscription used with Claude Code. One account setting, Help Improve Claude, controls training use.",
          points: [
            { label: "Before connecting", text: "Turn off Help Improve Claude in Claude Settings > Privacy. While it is on, Anthropic may use new personal chats and Claude Code sessions to improve Claude; once off, past and new chats or coding sessions are not used for future model training, though training already underway is unaffected." },
            { label: "Regardless", text: "Safety-flagged conversations may still be analyzed for policy enforcement and to improve Anthropic's safeguards." },
          ],
          links: [
            { url: "https://privacy.claude.com/en/articles/12109829-how-do-i-change-my-model-improvement-privacy-settings", label: "Anthropic model improvement setting instructions" },
            { url: "https://privacy.claude.com/en/articles/10023580-is-my-data-used-for-model-training", label: "Anthropic consumer training policy" },
          ],
        },
        {
          title: "How long Anthropic retains it",
          description: "Personal conversations remain until you delete them; Anthropic says deletion removes them from history immediately and from backend storage within 30 days.",
          points: [
            { label: "Covered Models", text: "Anthropic designates its most capable models, including Fable 5, as Covered Models with an extra safety measure: prompts and outputs are kept for 30 days on every plan, even with model improvement off. After 30 days they are deleted automatically unless a safety investigation or legal obligation requires longer." },
            { label: "Safety flags", text: "Anthropic may retain flagged inputs and outputs for up to 2 years and trust-and-safety classification scores for up to 7 years." },
            { label: "Feedback and de-identified data", text: "Feedback may be kept for 5 years; anonymized or de-identified data may be kept longer." },
          ],
          links: [
            { url: "https://privacy.anthropic.com/en/articles/10023548-how-long-do-you-store-my-data", label: "Anthropic consumer retention policy" },
            { url: "https://support.claude.com/en/articles/15425695-covered-models", label: "Anthropic Covered Models retention" },
          ],
        },
      ],
    },
    capabilities: [
      { name: "Claude Code model access", description: "Runs Claude Code tasks through the models and usage limits available to the linked Anthropic subscription." },
      {
        name: "Web search (optional, off by default)",
        description: "Off unless you enable it for the Claude integration. When on, Anthropic runs the search server-side: the query and surrounding context leave to Anthropic and its search partners — Anthropic does not name which third-party search providers it uses.",
        linkUrl: "https://support.claude.com/en/articles/10684626-enable-and-use-web-search",
        linkLabel: "Anthropic web search documentation",
      },
    ],
    controls: [
      "A token rotation is re-attested to Anthropic and must still match the operator-approved account.",
    ],
    networkScope: [
      ["api.anthropic.com", "GET and POST; pinned-account, OAuth-token, and server-side web-tool guards"],
      ["platform.claude.com", "GET and POST only for the Claude OAuth endpoints"],
    ],
  },
  xai: {
    label: "Grok",
    summary: "Run Grok Build chats and tasks through your Grok subscription, with X search enabled. Grok's server-side web search is not available on this host.",
    protections: [
      "The linked xAI account is pinned. Traffic naming another account, or a credential that does not claim the linked one, is denied until you explicitly disconnect and log in again.",
      "Only the subscription chat proxy is opened. xAI's metered developer API stays blocked, so inference draws on your Grok subscription's usage pool instead of billing an xAI console credit balance.",
      "The server-side tool allowlist is fixed to shapes that stay on xAI/X infrastructure: X search with X-only filters, text-to-image generation with no external input, and a bare reserved video-generation declaration. Web search, code execution, collections search, hosted browsing, remote tool servers, unknown tools, and media declarations carrying extra inputs stay blocked. Session sync to xAI is blocked too, so conversation state stays on this host.",
      "Web search is blocked because Grok's cannot be narrowed. It searches and opens live pages as one capability, and xAI's servers do the fetching — so an allowed search could pull a model-chosen URL, carrying arbitrary agent-chosen data in its parameters, without that request ever passing this host's network policy. Grok answers from what it already knows plus what the agent reads locally.",
    ],
    setupSteps: [
      { title: "Enable Grok", description: "On Home, open Grok under Integrations and choose Enable." },
      { title: "Start the Grok login", description: "In Account, choose Start Grok login. Open the displayed URL, sign in with the account holding the Grok subscription, and confirm the device code." },
      { title: "Verify the linked account", description: "Return to Kern and wait for the row to show connected with the expected email or account id. That identity is now the operator-approved account anchor." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Assume any host data available to Grok Build can go to xAI, including prompts, conversation history, workspace files and diffs, tool inputs, and tool results.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "xAI", text: "Everything the agent sends goes to xAI's services under the linked account." },
            { label: "X search", text: "The host sends the query and surrounding conversation only to xAI's pinned subscription chat proxy. xAI runs X search on its servers against X posts, users, threads, and X-hosted media; this host does not contact x.com or a third-party search provider." },
            { label: "Media generation", text: "The proxy admits only text-to-image generation and the bare reserved video-generation declaration; external-input fields and unknown options fail closed. It does not open api.x.ai, imgen.x.ai, or vidgen.x.ai. Grok Build 1.0.5 does not yet expose these media tools, and xAI currently documents video generation only on its separate metered developer API, so this is a narrow policy allowance rather than a working media workflow today." },
            { label: "Nowhere else", text: "Web search and browsing remain blocked, so xAI cannot fetch arbitrary pages through Grok's hosted tools. Remote MCP, hosted code, collections search, and unknown hosted tools remain blocked too." },
          ],
          links: [],
        },
        {
          title: "What xAI can do with it",
          description: "Kern uses the Grok Build coding-agent path. Its coding-data and team ZDR controls are separate from the consumer controls for Grok.com and Grok on X.",
          points: [
            {
              label: "For Kern and Grok Build",
              content: [
                "This is the relevant account setting for Kern. Open Settings with /privacy and choose Opt out under Coding data, retention, and training. xAI says opting out prevents coding data such as prompts, traces, and metrics from being retained and used for training or product improvement. It is account-backed rather than a config.toml key; Kern displays its observed state next to the connected account when Grok reports it. On team accounts only a team admin can change it. For team settings, open the ",
                { url: "https://console.x.ai/", label: "xAI Console" },
                " as a team admin. Team ZDR is stronger: it prevents prompt, code, and response persistence at the inference layer when the Grok CLI login belongs to that team. While ZDR is on, the coding-data choice cannot be changed.",
              ],
            },
            {
              label: "What Kern enforces locally",
              text: "Kern pins Grok product telemetry and trace upload off in root-owned requirements, and its network proxy blocks trace, storage, session-sync, workspace-sync, feedback, and bundle-upload routes. Those controls prevent separate client-side uploads but do not rewrite the xAI account choice, so confirm that the connected-account row says coding-data opt-out active (or use /privacy).",
            },
            {
              label: "For the xAI developer API",
              text: "This is not the path Kern opens. xAI says API inputs and outputs are not used for training without explicit permission even when ZDR is off; by default they may still be retained for up to 30 days for abuse auditing. ZDR removes that default content retention.",
            },
            {
              label: "For the Grok app and Grok on X",
              content: [
                "These are separate consumer data paths and their toggles do not change Grok Build or team ZDR. xAI's consumer terms allow conversations to be used to train its models by default, and paid tiers are not exempt. ",
                { url: "https://grok.com/?_s=data", label: "Grok.com data controls" },
                " control whether content and interactions from new Grok web and mobile-app conversations are used for training. Separately, ",
                { url: "https://x.com/settings/grok_settings", label: "X Grok settings" },
                " control whether X can share your public X data — including public posts and profile metadata — plus your interactions, inputs, and results with Grok on X for training and fine-tuning. Turn off both settings if you use both products. Opting out applies to future data, not data already collected.",
              ],
            },
          ],
          links: [
            { url: "https://docs.x.ai/build/modes-and-commands#core-tui-commands", label: "Grok Build /privacy documentation" },
            { url: "https://docs.x.ai/developers/faq/security#does-xai-train-on-customers-api-requests", label: "xAI API training and retention" },
            { url: "https://docs.x.ai/build/enterprise#privacy--data-lifecycle", label: "Grok Build privacy and ZDR" },
            { url: "https://x.ai/legal/faq#how-do-i-select-whether-my-content-is-used-for-model-training", label: "Grok training opt-out instructions" },
            { url: "https://x.ai/legal/privacy-policy", label: "xAI Privacy Policy" },
            { url: "https://x.ai/legal/subprocessor-list", label: "xAI subprocessor list" },
            { url: "https://x.ai/privacy-portal", label: "xAI privacy portal (access and deletion)" },
          ],
        },
        {
          title: "How long xAI retains it",
          description: "xAI does not publish a specific retention period for Grok conversation data, and opting out of training changes how data is used rather than whether it is kept.",
          points: [
            { label: "If retention matters to you", text: "Zero Data Retention is the setting that addresses it, and it is a team-admin control rather than a per-user one. Access and deletion requests go through xAI's privacy portal." },
          ],
          links: [
            { url: "https://console.x.ai/", label: "Open xAI Console team settings" },
            { url: "https://docs.x.ai/build/enterprise#privacy--data-lifecycle", label: "Grok Build privacy and ZDR" },
          ],
        },
      ],
    },
    capabilities: [
      { name: "Grok Build runtime", description: "Creates and resumes Grok Build sessions for Chat, Apps, and Schedules, streams messages and activity, accepts live steering, and exposes the connected subscription's usage when xAI reports it." },
      {
        name: "X search",
        description: "Allows Grok's hosted x_search tool. The request goes only to the pinned xAI chat proxy; xAI executes keyword, semantic, user, and thread search against X data on its servers.",
        linkUrl: "https://docs.x.ai/developers/tools/x-search",
        linkLabel: "xAI X search documentation",
      },
      {
        name: "xAI-hosted media declarations",
        description: "Allows only image_generation with action generate and a bare video_generation declaration through the chat proxy; external inputs and unknown options fail closed. Grok Build 1.0.5 does not emit either declaration, and xAI currently exposes video generation through the blocked metered developer API, so media generation is not yet usable from the Grok runtime.",
        linkUrl: "https://docs.x.ai/developers/tools/image-generation",
        linkLabel: "xAI image generation tool documentation",
      },
      {
        name: "Web search (not available)",
        description: "Grok's server-side web search is blocked, and there is no setting that turns it on. It cannot be narrowed: searching and opening live pages are one capability, and xAI's servers do the fetching, so an allowed search could pull a model-chosen URL, carrying arbitrary agent-chosen data in its parameters, without that request ever passing this host's network policy. Grok answers from what it already knows plus what the agent reads locally, and the agent's own tools reach only your allowed domains.",
        linkUrl: "https://docs.x.ai/developers/tools/web-search",
        linkLabel: "xAI web search documentation",
      },
    ],
    controls: [
      "The proxy fails closed when the account pin or request body cannot be checked.",
    ],
    networkScope: [
      ["auth.x.ai", "GET and POST for the operator login flow"],
      ["cli-chat-proxy.grok.com", "GET and POST; pinned-account, OAuth-token, and server-side tool guards"],
    ],
  },
  bedrock: {
    label: "Hermes (AWS Bedrock)",
    summary: "Connect your AWS account and let Hermes run tasks through Bedrock in your own account.",
    protections: [
      "Inference happens through AWS Bedrock in your own AWS account, which provides maximal data privacy from model providers: the model provider never receives your traffic, and AWS states Bedrock does not store prompts or completions after serving the response, does not share them with model providers, and does not use them to train models.",
      "The agent process never receives your AWS credential. Hermes signs with fixed dummy values that carry no AWS capability; this host's proxy re-signs allowed requests with your connected IAM key stored encrypted in the host database. Presigned query-string auth and temporary session credentials are denied.",
      "Only the configured region's Bedrock model APIs are reachable. The Bedrock control plane, other AWS services, and other regions stay blocked, so the key's blast radius on this host is inference only.",
    ],
    setupSteps: [
      {
        title: "Create a dedicated IAM user",
        description: "In AWS IAM, create one user for Kern's Hermes Bedrock connection. Attach this policy, then create a long-term access key. Temporary session credentials are not supported.",
        code: `{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream"
    ],
    "Resource": "*"
  }]
}`,
        linkUrl: "https://docs.aws.amazon.com/bedrock/latest/userguide/security_iam_id-based-policy-examples.html",
        linkLabel: "View AWS's Bedrock IAM policy examples",
      },
      {
        title: "Request model access in Bedrock",
        description: "In the Bedrock console's Model access page, enable the models you plan to use in your region.",
        linkUrl: "https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html",
        linkLabel: "View AWS's model access guide",
      },
      { title: "Connect AWS", description: "On Home, open Hermes (AWS Bedrock), paste the access key id and secret access key, choose the region matching your model access, and connect them together." },
      { title: "Enable Bedrock", description: "Choose Enable. Hermes becomes available." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Assume any host data available to the harness can go to your AWS account, including prompts, conversation history, workspace files and diffs, tool inputs, and tool results.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "Your AWS account only", text: "Everything the agent sends goes to the Bedrock runtime endpoint in this integration's configured region of your own AWS account. Cross-region inference profiles (us. prefixed models) may route between US regions of AWS's own infrastructure." },
            { label: "Model providers", text: "Never. Bedrock serves the models from AWS-hosted copies; the model providers do not receive your prompts or outputs." },
          ],
          links: [],
        },
        {
          title: "What AWS can do with it",
          description: "AWS treats Bedrock inputs and outputs as your content.",
          points: [
            { label: "No training, no sharing", text: "AWS states it does not use Bedrock prompts or completions to train models and does not share them with model providers or other customers." },
            { label: "Optional features change this", text: "Model invocation logging and Guardrails are off unless you enable them in your account; enabling them stores or processes request data under your own AWS configuration." },
          ],
          links: [
            { url: "https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html", label: "AWS Bedrock data protection" },
            { url: "https://aws.amazon.com/bedrock/faqs/", label: "AWS Bedrock FAQs" },
          ],
        },
        {
          title: "How long AWS retains it",
          description: "AWS states Bedrock does not store or log prompts and completions after processing the request; nothing is retained unless you enable invocation logging into your own account.",
          points: [],
          links: [
            { url: "https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html", label: "AWS Bedrock data protection" },
          ],
        },
      ],
    },
    capabilities: [
      { name: "Hermes runtime", description: "Runs Hermes on DeepSeek, Qwen, and Kimi models through one guarded Bedrock connection." },
      { name: "Live usage estimate", description: "Provider details show a month-to-date estimate computed live by this host from the token usage AWS reports in each response and priced at the on-demand catalog rates. AWS bills authoritatively." },
    ],
    controls: [
      "Changing the access key requires the operator to paste and validate the replacement.",
      "One Bedrock Enable/Disable control governs Hermes and its credential, region, account, and network boundary.",
      "The host-side AWS check (STS identity attestation) runs from the host itself and is not reachable by the agent.",
    ],
    networkScope: [
      ["bedrock-runtime.<region>.amazonaws.com", "POST only, Bedrock model API paths only, re-signed by the proxy with this integration's connected access key; only its configured region"],
    ],
  },
  github: {
    label: "GitHub",
    summary: "Connect GitHub and let your agent read repositories and write only to the repositories you choose.",
    protections: [
      "GitHub credentials never reach the agent: the host injects the working token into requests at the proxy and strips any credential the agent sends, so the token is never returned to or read by the agent.",
      "Reads can reach any public repository and private repositories visible to the credential; writes work only for the repositories you configure.",
      "Repository administration, GraphQL, Git LFS uploads, and other write paths that could reach beyond the configured repositories stay denied.",
      "Direct Git pushes to main are blocked by default. Push a feature branch and merge it through a pull request, or explicitly disable the protection in Settings.",
      "Keep approval for `.github` pushes enabled. Workflow changes can make GitHub Actions run arbitrary code with network access and repository credentials.",
      "Search/read query values pass the host parameter guard: values shaped like a secret, credential, or sensitive identifier are denied before the request is sent. Request headers are not inspected; the agent's Authorization is replaced with the host-held token and User-Agent with a fixed host value.",
      "GitHub Actions Azure downloads are limited to GitHub's documented productionresultssa0 through productionresultssa19 storage accounts; path and query values take the parameter guard.",
    ],
    setupSteps: [
      { title: "Choose a credential mode", description: "Use a fine-grained personal access token for the simplest personal setup. Use a GitHub App when you want repository installation scope and short-lived minted tokens." },
      { title: "Create a fine-grained token", description: "In GitHub Settings > Developer settings > Personal access tokens > Fine-grained tokens, choose Generate new token. Select the resource owner and only the repositories this host should reach. Grant Contents read/write for Git pushes, Metadata read, and only the additional repository permissions required by the REST actions you intend to use.", linkUrl: "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens", linkLabel: "View GitHub's fine-grained token guide" },
      { title: "Or create and install a GitHub App", description: "In GitHub Settings > Developer settings > GitHub Apps, create an app with only the repository permissions your workflow needs. Install it on the selected repositories, note the App ID and installation ID, then generate and download a private key. Kern uses those values to mint short-lived installation tokens.", linkUrl: "https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app", linkLabel: "View GitHub's app registration guide" },
      { title: "Store the credential", description: "Enable GitHub, expand the row, select the credential type, enter its values, and choose Set credential. Stored secret values are never read back into the UI." },
      { title: "Add write repositories", description: "Under Write repositories, add each owner/repository that may receive a push or mutating REST API call. Repositories not listed remain read-only." },
      { title: "Keep direct main pushes blocked", description: "Kern enables Block direct pushes to main by default. Agents can still push feature branches and merge pull requests, but the proxy rejects a Git push transaction that updates refs/heads/main." },
      { title: "Keep .github push approval enabled", description: "Kern enables Require approval for .github pushes when GitHub is first turned on. Keep it enabled so workflow and other .github path changes are held for an operator decision; GitHub Actions workflows can execute arbitrary code with network access and repository credentials." },
    ],
    capabilities: [
      { name: "Git and REST reads", description: "Clone, fetch, inspect releases and raw files, and use read-only GitHub REST endpoints wherever the credential has access." },
      { name: "Scoped Git and REST writes", description: "Push and call mutating repository REST endpoints only for configured write repositories." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Any data on this host can be written to a repository on the write list, so assume GitHub can receive anything the agent can read here. Reads send repository paths, query parameters, and request headers; GitHub may log that metadata whether or not the requested repository exists. Read query values first pass the host parameter guard (see Technical notes), which denies secret- or credential-shaped values before the request is sent. Headers are forwarded as the client sent them, except the agent's Authorization, which is replaced with the host-held token, and User-Agent, which is replaced with a fixed host value.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "Write repositories", text: "Apart from public repositories and GitHub Actions (below), data can go only to the repositories on your write list; in a private repository it is visible only to that repository's collaborators." },
            { label: "Public repositories", text: "Everything pushed to a public write repository is exposed to the entire internet." },
            { label: "GitHub Actions", text: "A push changing a .github path can start workflow runs, which execute code with network access and can send repository data anywhere. Kern holds .github pushes for your approval by default." },
          ],
          links: [],
        },
        {
          title: "What GitHub can do with it",
          description: "GitHub processes pushed content and account, repository, and usage data under its Privacy Statement; who else can see pushed data is set by the repository's visibility and organization settings.",
          links: [
            { label: "GitHub General Privacy Statement", url: "https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement" },
            { label: "GitHub App permissions", url: "https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app" },
          ],
        },
        {
          title: "How long GitHub retains it",
          description: "Repository content remains until it is changed or deleted, and public content may be copied or forked by anyone while it is visible. GitHub keeps account data while the account is active and as needed for contracts, legal obligations, disputes, or enforcement.",
          links: [
            { label: "GitHub General Privacy Statement", url: "https://docs.github.com/en/site-policy/privacy-policies/github-general-privacy-statement" },
          ],
        },
      ],
    },
    controls: [
      "Parameter guard: agent-authored read query values are checked against deterministic rules for secrets, credentials, personal identifiers, and encoded payloads; a match denies the request before it is sent. Repository paths and revision identifiers are exempt. Request headers are not inspected: the agent's Authorization is replaced with the host-held token and User-Agent with a fixed host value, and the rest are forwarded as the client sent them.",
      "Azure Blob downloads: only GitHub's documented productionresultssa0 through productionresultssa19 storage accounts are eligible. The SAS signature is shape-validated and neutralized, then the path and other query values take the parameter guard.",
      "Disabling GitHub clears the write-repository list; the independently stored credential can remain staged or be cleared separately.",
    ],
    networkScope: [
      ["github.com", "GET, HEAD, and fetch for any visible repository; push only to write repositories; LFS uploads denied"],
      ["api.github.com", "GET and HEAD broadly; repository REST writes only for write repositories; GraphQL denied; administration denied"],
      ["uploads.github.com", "Release-asset uploads only for write repositories"],
      ["codeload.github.com", "GET and HEAD for any visible repository archive"],
      ["raw.githubusercontent.com", "GET and HEAD for any visible repository path"],
      ["objects.githubusercontent.com", "GET and HEAD for signed download URLs only"],
      ["github-cloud.githubusercontent.com", "GET and HEAD for signed download URLs only"],
      ["release-assets.githubusercontent.com", "GET and HEAD for signed release-asset URLs only"],
      ["results-receiver.actions.githubusercontent.com", "GET and HEAD for GitHub Actions result downloads; provider-signed URL only"],
      ["productionresultssa{0..19}.blob.core.windows.net", "GET and HEAD for GitHub Actions logs, summaries, artifacts, and caches; exactly one validated Azure SAS signature required"],
    ],
  },
  python_packages: {
    label: "Python packages",
    summary: "Lets your agent discover and install public Python packages from PyPI.",
    protections: [
      "Access is read-only and limited to the public PyPI index, package metadata, and distribution download paths.",
      "Package publishing and arbitrary requests to PyPI or the download host remain denied.",
      "Index and metadata reads on pypi.org pass the host parameter guard: anything shaped like a secret, credential, or sensitive identifier is denied before the request is sent. Credential headers are removed and User-Agent is replaced with a fixed host value; other headers are forwarded as sent, because PyPI reflects none of them back. The bulk download host files.pythonhosted.org is exempt — its distribution download URL paths are NOT scanned by the parameter guard.",
    ],
    setupSteps: [
      { title: "Enable Python packages", description: "On Home, open Python Packages under Integrations and choose Enable. pip and compatible package clients can then resolve and download public distributions." },
    ],
    capabilities: [
      { name: "Package discovery", description: "Reads the PyPI simple index and package JSON metadata." },
      { name: "Distribution downloads", description: "Downloads wheels and source archives from PyPI's package file host." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Package names and versions, the files requested, and the request headers your package client sends — these are forwarded as sent, so treat any header the client adds as visible to the registry. Two are changed: credential headers (Authorization, Cookie) are removed, and User-Agent is replaced with a fixed value identifying this host rather than your client. Standard web request metadata (source IP, request time) is visible as it is for any HTTPS request. Nothing else on this host is sent. PyPI reflects no header back, so the control that matters here is on the requested package name.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "PyPI", text: "Requests go to PyPI, run by the Python Software Foundation with infrastructure providers including AWS and Fastly." },
            { label: "Public download dataset", text: "Each package download is recorded in a public statistics dataset: the package, file, client tool, and an approximate location derived from the source IP. Plain metadata lookups are not included." },
            { label: "Nonexistent packages", text: "A request for a package name that does not exist still reaches PyPI and its request logs like any other request, so requested-name text is itself data sent to PyPI. It does not enter the public dataset." },
          ],
          links: [
            { label: "PyPI public download dataset", url: "https://docs.pypi.org/api/bigquery/" },
          ],
        },
        {
          title: "What PyPI can do with it",
          description: "PyPI uses request logs to operate and secure the index; it does not sell them or use them for advertising. PyPI says its retained download logs contain no IP addresses.",
          links: [
            { label: "PyPI Privacy Notice", url: "https://policies.python.org/pypi.org/Privacy-Notice/" },
          ],
        },
        {
          title: "How long PyPI retains it",
          description: "PyPI does not publish a fixed retention period for ordinary request logs. Entries in the public download dataset remain available indefinitely, but they identify only the package and download context, not you.",
          links: [
            { label: "PyPI Privacy Notice", url: "https://policies.python.org/pypi.org/Privacy-Notice/" },
          ],
        },
      ],
    },
    controls: [
      "Parameter guard: requested package names and URL values are checked against deterministic rules for secrets, credentials, personal identifiers, and encoded payloads; a match denies the request before it is sent. Headers are not scanned; credential headers are removed and User-Agent is replaced with a fixed host value. Distribution download URL paths on files.pythonhosted.org are exempt and are not scanned.",
    ],
    networkScope: [
      ["pypi.org", "GET and HEAD only under /simple and /pypi/<package>/json"],
      ["files.pythonhosted.org", "GET and HEAD only under /packages"],
    ],
  },
  npm_packages: {
    label: "NPM Packages",
    summary: "Lets your agent discover and install public JavaScript packages and download Node.js releases.",
    protections: [
      "Registry and Node.js distribution access is read-only; npm publishing and arbitrary Node.js website paths remain denied.",
      "Only public registry data and release files are available through this integration.",
      "Requested package names and URL values pass the host parameter guard: anything shaped like a secret, credential, or sensitive identifier is denied before the request is sent. Credential headers are removed and User-Agent is replaced with a fixed host value; other headers are forwarded as sent, because the registry reflects none of them back. The bulk URL paths are exempt — registry.npmjs.org carries no path guards, and every npm tarball path (any path containing `/-/`) is NOT scanned by the parameter guard.",
    ],
    setupSteps: [
      { title: "Enable NPM Packages", description: "On Home, open NPM Packages under Integrations and choose Enable. npm and compatible package clients can then resolve and download public packages and Node.js distributions." },
    ],
    capabilities: [
      { name: "npm registry reads", description: "Reads public package metadata and tarballs through registry.npmjs.org." },
      { name: "Node.js downloads", description: "Downloads published Node.js distributions from the official /dist path." },
    ],
    dataSummary: {
      items: [
        {
          title: "What leaves this host",
          description: "Package names and versions, the files requested, and the request headers your package client sends — these are forwarded as sent, so treat any header the client adds as visible to the registry. Two are changed: credential headers (Authorization, Cookie) are removed, and User-Agent is replaced with a fixed value identifying this host rather than your client. Standard web request metadata (source IP, request time) is visible as it is for any HTTPS request. Nothing else on this host is sent. The registry reflects no header back, so the control that matters here is on the requested package name.",
          links: [],
        },
        {
          title: "Where it can go",
          points: [
            { label: "npm registry", text: "Package requests go to npm's registry, operated by GitHub, which stores registry-use information in the United States." },
            { label: "nodejs.org", text: "Node.js downloads go to the OpenJS Foundation's website infrastructure." },
            { label: "Public counts", text: "Only aggregate per-package download counts are published; they contain nothing about you or this host." },
            { label: "Nonexistent packages", text: "A request for a package name that does not exist still reaches the registry and its request logs like any other request, so requested-name text is itself data sent to npm. Nothing about it is published." },
          ],
          links: [],
        },
        {
          title: "What npm and OpenJS can do with it",
          description: "npm uses registry request logs to operate and secure the registry. OpenJS processes nodejs.org download request metadata the same way under its website Privacy Policy.",
          links: [
            { label: "npm Privacy Policy", url: "https://docs.npmjs.com/policies/privacy/" },
            { label: "OpenJS Foundation Privacy Policy", url: "https://openjsf.org/privacy" },
          ],
        },
        {
          title: "How long npm and OpenJS retain it",
          description: "Neither policy states one fixed retention period for ordinary registry and download request logs. Aggregate download counts remain public, but they contain nothing about you or this host.",
          links: [
            { label: "npm public-registry terms", url: "https://docs.npmjs.com/policies/open-source-terms/" },
            { label: "OpenJS Foundation Privacy Policy", url: "https://openjsf.org/privacy" },
          ],
        },
      ],
    },
    controls: [
      "Parameter guard: requested package names and URL values are checked against deterministic rules for secrets, credentials, personal identifiers, and encoded payloads; a match denies the request before it is sent. Headers are not scanned; credential headers are removed and User-Agent is replaced with a fixed host value. npm tarball URL paths (any path containing /-/) are exempt and are not scanned.",
    ],
    networkScope: [
      ["registry.npmjs.org", "GET and HEAD only"],
      ["nodejs.org", "GET and HEAD only under /dist"],
    ],
  },
};

export const CUSTOM_DOMAIN_GUIDE = {
  id: "custom_domain",
  label: "Custom Domain Access",
  summary: "Creates an explicit network rule for a domain that is not covered by a managed integration or bundled tool.",
  protections: [
    "Every request must match the configured domain, method, and any path guards. Anything outside the rule is denied and recorded in the network audit log.",
    "Nothing inside the request is inspected: no header checks, no URL parameter guard, no body scanning. The domain, method and path rule is the whole boundary, so adding a domain here means trusting that destination with anything the agent can send it.",
    "Managed-integration domains are reserved, so a custom rule cannot bypass their account, repository, or request-body protections.",
  ],
  setupSteps: [
    { title: "Identify the narrow boundary", description: "Decide the smallest exact domain, method set, and path surface the workflow needs. Prefer an exact API host over a wildcard." },
    { title: "Add the rule", description: "Expand Custom Domain Access, enter the domain, comma-separated HTTP methods, and optional path regexes one per line, then choose Add domain rule." },
    { title: "Verify in the audit log", description: "Run the intended request and inspect Network audit log. A denial gives a dedicated reason; widen only the specific boundary the real request proves necessary." },
  ],
  capabilities: [
    { name: "Custom HTTPS access", description: "Allows agent traffic to operator-selected third-party API or download hosts within the rule." },
  ],
  dataSummary: {
    items: [
      {
        title: "What leaves this host",
        description: "The configured service receives the complete HTTPS request: hostname, path, query parameters, method, headers, cookies or authorization values, body, and source network metadata. Any host data the agent places in a request can go to that service, and none of it is inspected — the host checks only that the domain, method and path match your rule. Add a domain here only if you trust that destination with your data.",
        links: [],
      },
      {
        title: "Where it can go",
        description: "Directly to the configured domain, and from there wherever that service's own terms allow. Kern applies no redaction and holds no contract limiting onward sharing.",
        links: [],
      },
      {
        title: "What the third party can do with it",
        description: "Kern adds only the configured network boundary. It provides no provider contract, field redaction, OAuth isolation, or data-use promise; the service's own current terms control what it does with the data.",
        links: [],
      },
      {
        title: "How long the third party retains it",
        description: "Kern does not know the configured service's retention or deletion practices. Check that service's current policy before sending personal, confidential, regulated, or credential-bearing data.",
        links: [],
      },
    ],
  },
  controls: [
    "No content inspection: the domain, method and path rule is enforced and nothing else. URL values, headers, cookies, credentials and the request body are all forwarded as the agent sent them. Managed integrations are guarded because their clients and destinations are known; a custom domain is not, so trusting the destination is the decision you are making here.",
    "Rules validate structurally and publish atomically; an invalid replacement leaves the active policy unchanged.",
  ],
  networkScope: [],
};

export function integrationInfo(name) {
  return MANAGED_INTEGRATIONS[name];
}

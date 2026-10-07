-- migrate:up
-- Google Search is a new opt-in tool. Do not transfer keys or enablement.
DELETE FROM tool_config WHERE tool_id = 'linkedin_discovery';
DELETE FROM enabled_tools WHERE tool_id = 'linkedin_discovery';
-- Retain historical tool events, action runs, approvals and costs under normal retention.

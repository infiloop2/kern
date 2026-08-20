-- Checklist progress is derived from live Workspace state, so the only thing
-- worth storing is the operator's decision to stop showing it. That decision is
-- about the host rather than about one browser.

-- migrate:up
SET LOCAL search_path TO public;

CREATE TABLE workspace_onboarding_dismissal (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON workspace_onboarding_dismissal TO "kern-workspace";

-- migrate:down
SET LOCAL search_path TO public;

DROP TABLE workspace_onboarding_dismissal;

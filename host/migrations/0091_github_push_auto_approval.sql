-- Share auto-approval audit/history with held GitHub pushes.
-- migrate:up
SET LOCAL search_path TO public;
ALTER TABLE auto_approval_reviews ALTER COLUMN approval_number DROP NOT NULL;
ALTER TABLE auto_approval_reviews ADD COLUMN push_id TEXT REFERENCES pending_pushes(id) ON DELETE CASCADE;
ALTER TABLE auto_approval_reviews ADD CHECK ((approval_number IS NULL) <> (push_id IS NULL));
CREATE UNIQUE INDEX auto_approval_reviews_push ON auto_approval_reviews (push_id) WHERE push_id IS NOT NULL;

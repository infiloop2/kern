-- migrate:up
CREATE TABLE xai_video_storage (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    bucket text NOT NULL,
    region text NOT NULL,
    access_key_id text NOT NULL,
    secret_access_key_encrypted text NOT NULL
);
GRANT SELECT ON xai_video_storage TO "kern-proxy";

-- migrate:down
DROP TABLE xai_video_storage;

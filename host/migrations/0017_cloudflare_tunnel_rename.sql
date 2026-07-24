-- Rename the Cloudflare operator connection mode from 'cloudflare_access' to
-- 'cloudflare_tunnel'. The admin login is now the authentication boundary and
-- the tunnel carries only transport and Cloudflare edge protection, so the old
-- name (which implied a Cloudflare Access login gate in front) no longer
-- describes the mode. Existing rows are migrated in place; the CHECK
-- constraints, which mirror host/config.py, are rebuilt for the new value.

-- migrate:up

ALTER TABLE operator_connections DROP CONSTRAINT operator_connections_mode_check;
ALTER TABLE operator_connections DROP CONSTRAINT operator_connections_check;
UPDATE operator_connections SET mode = 'cloudflare_tunnel' WHERE mode = 'cloudflare_access';
ALTER TABLE operator_connections
    ADD CONSTRAINT operator_connections_mode_check CHECK (mode IN ('ssh', 'cloudflare_tunnel'));
ALTER TABLE operator_connections
    ADD CONSTRAINT operator_connections_check CHECK (
        (mode = 'ssh' AND ssh_public_key IS NOT NULL AND hostname IS NULL AND tunnel_token IS NULL)
        OR (mode = 'cloudflare_tunnel' AND ssh_public_key IS NULL AND hostname IS NOT NULL AND tunnel_token IS NOT NULL)
    );

-- migrate:down

ALTER TABLE operator_connections DROP CONSTRAINT operator_connections_mode_check;
ALTER TABLE operator_connections DROP CONSTRAINT operator_connections_check;
UPDATE operator_connections SET mode = 'cloudflare_access' WHERE mode = 'cloudflare_tunnel';
ALTER TABLE operator_connections
    ADD CONSTRAINT operator_connections_mode_check CHECK (mode IN ('ssh', 'cloudflare_access'));
ALTER TABLE operator_connections
    ADD CONSTRAINT operator_connections_check CHECK (
        (mode = 'ssh' AND ssh_public_key IS NOT NULL AND hostname IS NULL AND tunnel_token IS NULL)
        OR (mode = 'cloudflare_access' AND ssh_public_key IS NULL AND hostname IS NOT NULL AND tunnel_token IS NOT NULL)
    );

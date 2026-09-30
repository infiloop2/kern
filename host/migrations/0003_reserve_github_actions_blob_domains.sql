-- migrate:up
-- Azure Blob storage backs GitHub Actions job summaries, logs, artifacts,
-- and caches. The GitHub managed integration now owns this apex, so remove
-- legacy custom-domain rules that target a host beneath it or use a broader
-- wildcard that would cover it. Foreign-key cascades remove method and path
-- rows belonging to each deleted custom domain.
DELETE FROM allowed_domains
WHERE
    domain = 'blob.core.windows.net'
    OR domain LIKE '%.blob.core.windows.net'
    OR (
        domain LIKE '*.%'
        AND 'blob.core.windows.net' LIKE '%.' || substring(domain FROM 3)
    );

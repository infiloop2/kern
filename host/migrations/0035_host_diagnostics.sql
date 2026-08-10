-- Broaden the operator host-error log into a two-severity diagnostics log.

-- migrate:up

ALTER TABLE host_errors RENAME TO host_diagnostics;
ALTER SEQUENCE host_errors_id_seq RENAME TO host_diagnostics_id_seq;
ALTER SEQUENCE host_errors_seq_seq RENAME TO host_diagnostics_seq_seq;
ALTER INDEX host_errors_pkey RENAME TO host_diagnostics_pkey;
ALTER INDEX host_errors_seq_key RENAME TO host_diagnostics_seq_key;
ALTER INDEX host_errors_fingerprint_last_seen_idx
    RENAME TO host_diagnostics_fingerprint_last_seen_idx;

ALTER TABLE host_diagnostics
    ADD COLUMN severity TEXT NOT NULL DEFAULT 'error'
        CHECK (severity IN ('error', 'warning'));
ALTER TABLE host_diagnostics ALTER COLUMN severity DROP DEFAULT;
ALTER TABLE host_diagnostics DROP CONSTRAINT host_errors_kind_check;
ALTER TABLE host_diagnostics
    ADD CONSTRAINT host_diagnostics_kind_check CHECK (
        kind IN (
            'unexpected_exception',
            'service_exit',
            'invariant_failure',
            'provider_failure',
            'unexpected_behavior'
        )
    );
ALTER TABLE host_diagnostics
    ADD CONSTRAINT host_diagnostics_context_size_check CHECK (
        octet_length(context::text) <= 4096
    );

-- migrate:down

DELETE FROM host_diagnostics
WHERE severity = 'warning'
   OR kind NOT IN ('unexpected_exception', 'service_exit', 'invariant_failure');
ALTER TABLE host_diagnostics DROP CONSTRAINT host_diagnostics_context_size_check;
ALTER TABLE host_diagnostics DROP CONSTRAINT host_diagnostics_kind_check;
ALTER TABLE host_diagnostics
    ADD CONSTRAINT host_errors_kind_check CHECK (
        kind IN ('unexpected_exception', 'service_exit', 'invariant_failure')
    );
ALTER TABLE host_diagnostics DROP COLUMN severity;

ALTER INDEX host_diagnostics_fingerprint_last_seen_idx
    RENAME TO host_errors_fingerprint_last_seen_idx;
ALTER INDEX host_diagnostics_seq_key RENAME TO host_errors_seq_key;
ALTER INDEX host_diagnostics_pkey RENAME TO host_errors_pkey;
ALTER SEQUENCE host_diagnostics_seq_seq RENAME TO host_errors_seq_seq;
ALTER SEQUENCE host_diagnostics_id_seq RENAME TO host_errors_id_seq;
ALTER TABLE host_diagnostics RENAME TO host_errors;

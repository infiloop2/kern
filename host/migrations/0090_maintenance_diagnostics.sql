-- migrate:up

ALTER TABLE host_diagnostics DROP CONSTRAINT host_diagnostics_severity_check;
ALTER TABLE host_diagnostics
    ADD CONSTRAINT host_diagnostics_severity_check
        CHECK (severity IN ('error', 'warning', 'info'));
ALTER TABLE host_diagnostics DROP CONSTRAINT host_diagnostics_kind_check;
ALTER TABLE host_diagnostics
    ADD CONSTRAINT host_diagnostics_kind_check CHECK (
        kind IN (
            'unexpected_exception',
            'service_exit',
            'invariant_failure',
            'provider_failure',
            'unexpected_behavior',
            'maintenance'
        )
    );

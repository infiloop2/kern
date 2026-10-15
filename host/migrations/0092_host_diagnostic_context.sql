-- Preserve bounded provider diagnostics, including JSON escaping overhead.
-- migrate:up
ALTER TABLE host_diagnostics DROP CONSTRAINT host_diagnostics_context_size_check;
ALTER TABLE host_diagnostics ADD CONSTRAINT host_diagnostics_context_size_check
    CHECK (octet_length(context::text) <= 65536);

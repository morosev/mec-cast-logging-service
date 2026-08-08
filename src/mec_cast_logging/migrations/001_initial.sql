-- Core log storage.

CREATE TABLE IF NOT EXISTS log_entries (
    id          BIGSERIAL PRIMARY KEY,
    "timestamp" TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    level       TEXT        NOT NULL,
    severity    SMALLINT    NOT NULL,
    service     TEXT        NOT NULL,
    host        TEXT,
    logger      TEXT,
    message     TEXT        NOT NULL,
    context     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    trace_id    TEXT,
    search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', message)) STORED,

    CONSTRAINT log_entries_level_check
        CHECK (level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL')),
    CONSTRAINT log_entries_context_is_object
        CHECK (jsonb_typeof(context) = 'object')
);

-- Primary read path: newest first, optionally narrowed. The trailing id keeps
-- keyset pagination stable when many rows share a timestamp.
CREATE INDEX IF NOT EXISTS log_entries_timestamp_idx
    ON log_entries ("timestamp" DESC, id DESC);

CREATE INDEX IF NOT EXISTS log_entries_service_timestamp_idx
    ON log_entries (service, "timestamp" DESC, id DESC);

CREATE INDEX IF NOT EXISTS log_entries_severity_timestamp_idx
    ON log_entries (severity, "timestamp" DESC, id DESC);

CREATE INDEX IF NOT EXISTS log_entries_trace_id_idx
    ON log_entries (trace_id, "timestamp" DESC)
    WHERE trace_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS log_entries_context_idx
    ON log_entries USING GIN (context jsonb_path_ops);

CREATE INDEX IF NOT EXISTS log_entries_search_idx
    ON log_entries USING GIN (search_vector);

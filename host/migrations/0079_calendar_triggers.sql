-- Calendar triggers share the scheduled agent's revision and persistent thread.
-- migrate:up
SET LOCAL search_path TO public;

CREATE FUNCTION pg_temp.calendar_triggers(
    old_cadence TEXT, minutes BIGINT, daily TEXT, prompt TEXT, anchor TEXT
) RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
    anchor_at TIMESTAMP := date_trunc('minute', anchor::timestamptz AT TIME ZONE 'UTC');
    result JSONB;
BEGIN
    IF old_cadence = 'daily' THEN
        RETURN jsonb_build_array(jsonb_build_object('type', 'daily',
            'times', jsonb_build_array(daily), 'prompt', prompt));
    ELSIF minutes <= 1440 AND 1440 % minutes = 0 AND 1440 / minutes <= 120 THEN
        -- Split daily occurrences into groups of at most 24 times.
        SELECT jsonb_agg(jsonb_build_object('type', 'daily', 'times', times, 'prompt', prompt) ORDER BY bucket)
        INTO result FROM (
            SELECT n / 24 AS bucket, jsonb_agg(time ORDER BY time) AS times FROM (
                SELECT row_number() OVER (ORDER BY time) - 1 AS n, time FROM (
                    SELECT to_char(anchor_at + i * interval '1 minute', 'HH24:MI') AS time
                    FROM generate_series(0::bigint, 1440 - minutes, minutes) AS i
                ) AS clock_times
            ) AS numbered GROUP BY n / 24
        ) AS grouped;
    ELSIF 10080 % minutes = 0 AND 10080 / minutes <= 5 THEN
        -- Longer intervals use up to five evenly spaced weekly occurrences.
        SELECT jsonb_agg(jsonb_build_object('type', 'weekly',
            'days', jsonb_build_array((ARRAY['mon','tue','wed','thu','fri','sat','sun'])[extract(isodow FROM due)::integer]),
            'time', to_char(due, 'HH24:MI'), 'prompt', prompt) ORDER BY due)
        INTO result FROM (
            SELECT anchor_at + i * interval '1 minute' AS due
            FROM generate_series(0::bigint, 10080 - minutes, minutes) AS i
        ) AS occurrences;
    ELSE
        -- Irregular intervals and cadences exceeding the five-trigger limit
        -- fall back to once daily at their existing next-firing UTC time.
        RETURN jsonb_build_array(jsonb_build_object('type', 'daily',
            'times', jsonb_build_array(to_char(anchor_at, 'HH24:MI')), 'prompt', prompt));
    END IF;
    RETURN result;
END;
$$;

ALTER TABLE schedules ADD COLUMN triggers JSONB;
ALTER TABLE schedule_revisions ADD COLUMN triggers JSONB;
UPDATE schedules SET triggers = pg_temp.calendar_triggers(cadence, interval_minutes, daily_time, message, next_run_at);
-- History did not retain firing anchors. Use the schedule's retained anchor for
-- each revision, preserving its own cadence, prompt, metadata and deletion flag.
UPDATE schedule_revisions AS history SET triggers = pg_temp.calendar_triggers(
    history.cadence, history.interval_minutes, history.daily_time, history.message, schedule.next_run_at
) FROM schedules AS schedule WHERE history.schedule_id = schedule.id;
DROP FUNCTION pg_temp.calendar_triggers(TEXT, BIGINT, TEXT, TEXT, TEXT);

ALTER TABLE schedules
    ALTER COLUMN triggers SET NOT NULL,
    ALTER COLUMN next_run_at DROP NOT NULL,
    ADD CONSTRAINT schedules_triggers_check CHECK (jsonb_typeof(triggers) = 'array' AND jsonb_array_length(triggers) <= 5),
    DROP COLUMN message, DROP COLUMN cadence, DROP COLUMN interval_minutes, DROP COLUMN daily_time;
ALTER TABLE schedule_revisions
    ALTER COLUMN triggers SET NOT NULL,
    ADD CONSTRAINT schedule_revisions_triggers_check CHECK (jsonb_typeof(triggers) = 'array' AND jsonb_array_length(triggers) <= 5),
    DROP COLUMN message, DROP COLUMN cadence, DROP COLUMN interval_minutes, DROP COLUMN daily_time;

-- Preserve the next occurrence, rounded down to the new minute precision.
UPDATE schedules SET next_run_at = to_char(next_run_at::timestamptz AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:00"Z"');

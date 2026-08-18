-- Per-organisation upload control, managed from the admin console.
-- Disabling uploads leaves analysis and benchmarking available (view-only).

ALTER TABLE client ADD COLUMN uploads_enabled INTEGER NOT NULL DEFAULT 1;

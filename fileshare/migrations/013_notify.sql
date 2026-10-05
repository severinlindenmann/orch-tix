-- Per-ticket phone notifications (default OFF). The server sends a push for a mirrored ticket only while its
-- `notify` is 1. notify_phone_rev counts the changes the phone made, so a desktop push built before it saw them
-- cannot undo them. spaces.notify_messages: agent messages that name no ticket notify only while it is 1.
-- Existing mirrors and spaces start at 0: after this migration nothing pushes until the owner turns it on.
ALTER TABLE tickets ADD COLUMN notify INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tickets ADD COLUMN notify_phone_rev INTEGER NOT NULL DEFAULT 0;
ALTER TABLE spaces ADD COLUMN notify_messages INTEGER NOT NULL DEFAULT 0;

-- QA #55: which browser session sent a phone decision, so the "handled" push after it is applied can say it was this
-- phone (and not be sent to that phone at all).
ALTER TABLE decisions ADD COLUMN session_hash TEXT;

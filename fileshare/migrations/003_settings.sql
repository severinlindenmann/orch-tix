-- Encrypted settings (spec §15). One row at most, and the server holds only ciphertext under the MK.
CREATE TABLE settings (
  id           INTEGER PRIMARY KEY CHECK (id = 1),
  enc_settings TEXT NOT NULL,
  rev          INTEGER NOT NULL,
  updated_at   TEXT NOT NULL
)

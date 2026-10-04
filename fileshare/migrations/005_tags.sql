-- Tags (spec §19). Cleartext labels, like project: the server can filter by them, never read content.
CREATE TABLE file_tags (
  file_n INTEGER NOT NULL REFERENCES files(n),
  tag    TEXT NOT NULL,
  PRIMARY KEY (file_n, tag)
);
CREATE INDEX file_tags_tag ON file_tags(tag, file_n)

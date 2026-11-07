CREATE TABLE threads (
  id TEXT PRIMARY KEY,
  title TEXT,
  updated_at INTEGER,
  cwd TEXT,
  model TEXT,
  archived INTEGER,
  rollout_path TEXT,
  tokens_used INTEGER
);
INSERT INTO threads (id,title,updated_at,cwd,model,archived,rollout_path,tokens_used)
VALUES ('thread-1','Example chat',1999950,'/work/example','gpt-6.1-sol',0,'',100);

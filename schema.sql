PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS build_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  report_json TEXT
);

CREATE TABLE IF NOT EXISTS knowledge_bases (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  vehicle_series TEXT,
  scene TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  relative_path TEXT NOT NULL UNIQUE,
  file_name TEXT NOT NULL,
  extension TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  modified_at TEXT NOT NULL,
  logical_key TEXT NOT NULL,
  version TEXT,
  effective_date TEXT,
  scene TEXT NOT NULL,
  vehicle_tags TEXT NOT NULL,
  energy_tags TEXT NOT NULL,
  status TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_scene ON documents(scene);
CREATE INDEX IF NOT EXISTS idx_documents_enabled ON documents(enabled);
CREATE INDEX IF NOT EXISTS idx_documents_logical_key ON documents(logical_key);

CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id),
  ordinal INTEGER NOT NULL,
  source_locator TEXT NOT NULL,
  content TEXT NOT NULL,
  search_terms TEXT NOT NULL,
  vehicle_tags TEXT NOT NULL,
  scene TEXT NOT NULL,
  energy_tags TEXT NOT NULL,
  token_count INTEGER NOT NULL,
  UNIQUE(document_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_scene ON chunks(scene);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  search_terms,
  content,
  tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS vector_meta (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunk_vectors (
  chunk_id TEXT PRIMARY KEY REFERENCES chunks(id),
  vector_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  name TEXT NOT NULL,
  code TEXT,
  description TEXT,
  source_path TEXT,
  source_locator TEXT,
  confidence TEXT,
  review_status TEXT
);

CREATE INDEX IF NOT EXISTS idx_entities_label ON entities(label);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);

CREATE TABLE IF NOT EXISTS triples (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  subject_label TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object_id TEXT NOT NULL,
  object TEXT NOT NULL,
  object_label TEXT NOT NULL,
  source_path TEXT,
  source_locator TEXT,
  confidence TEXT,
  review_status TEXT
);

CREATE INDEX IF NOT EXISTS idx_triples_subject ON triples(subject_id);
CREATE INDEX IF NOT EXISTS idx_triples_object ON triples(object_id);
CREATE INDEX IF NOT EXISTS idx_triples_predicate ON triples(predicate);

CREATE VIRTUAL TABLE IF NOT EXISTS triples_fts USING fts5(
  triple_id UNINDEXED,
  search_terms,
  tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  account TEXT,
  vehicle_id TEXT,
  vehicle_series TEXT,
  scene TEXT,
  pending_question TEXT DEFAULT '',
  diagnostic_topic TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  citations_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT,
  message_id INTEGER,
  account TEXT,
  vehicle_id TEXT,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  rating TEXT NOT NULL,
  comment TEXT DEFAULT '',
  vehicle_series TEXT,
  scene TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS query_intents (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  scene TEXT DEFAULT '',
  description TEXT DEFAULT '',
  example_question TEXT DEFAULT '',
  sort_order INTEGER NOT NULL DEFAULT 0,
  enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS vin_records (
  vin TEXT PRIMARY KEY,
  vehicle_type TEXT DEFAULT '',
  chassis_no TEXT DEFAULT '',
  emission_type TEXT DEFAULT '',
  vehicle_series TEXT DEFAULT '',
  fuel_type TEXT DEFAULT '',
  announcement_model TEXT DEFAULT '',
  factory_model_code TEXT DEFAULT '',
  rear_axle TEXT DEFAULT '',
  tire_spec TEXT DEFAULT '',
  engine_type TEXT DEFAULT '',
  engine_model TEXT DEFAULT '',
  transmission_model TEXT DEFAULT '',
  offline_time TEXT DEFAULT '',
  vehicle_note TEXT DEFAULT '',
  engine_name TEXT DEFAULT '',
  device_app_version TEXT DEFAULT '',
  mcu_version TEXT DEFAULT '',
  sim_match TEXT DEFAULT '',
  updated_at TEXT NOT NULL
);

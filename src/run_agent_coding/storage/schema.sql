
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL
);

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    cwd TEXT NOT NULL,
    title TEXT,
    model TEXT NOT NULL,
    provider_name TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    last_seq INTEGER NOT NULL DEFAULT 0 CHECK(last_seq >= 0),
    generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
    owner_id TEXT,
    active_run_id TEXT,
    owner_expires_at REAL,
    owner_active INTEGER NOT NULL DEFAULT 0 CHECK(owner_active IN (0, 1)),
    recovery_required INTEGER NOT NULL DEFAULT 0 CHECK(recovery_required IN (0, 1)),
    active_branch_id TEXT NOT NULL DEFAULT 'main'
);
CREATE INDEX sessions_project_updated ON sessions(project_id, updated_at DESC);
CREATE INDEX sessions_principal_updated ON sessions(principal_id, updated_at DESC);

CREATE TABLE branches (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    branch_id TEXT NOT NULL,
    parent_branch_id TEXT,
    fork_entry_id TEXT,
    head_id TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY(session_id, branch_id),
    FOREIGN KEY(session_id, parent_branch_id) REFERENCES branches(session_id, branch_id),
    FOREIGN KEY(session_id, fork_entry_id) REFERENCES entries(session_id, entry_id),
    FOREIGN KEY(session_id, head_id) REFERENCES entries(session_id, entry_id)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE entries (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    entry_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK(seq > 0),
    parent_id TEXT,
    origin_branch_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    body_json TEXT NOT NULL CHECK(json_valid(body_json)),
    PRIMARY KEY(session_id, entry_id),
    UNIQUE(session_id, seq),
    FOREIGN KEY(session_id, origin_branch_id) REFERENCES branches(session_id, branch_id),
    FOREIGN KEY(session_id, parent_id) REFERENCES entries(session_id, entry_id)
);
CREATE INDEX entries_run ON entries(run_id, seq);

CREATE TABLE executions (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    branch_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','cancelled','interrupted','outcome_unknown')),
    started_at REAL NOT NULL,
    finished_at REAL,
    head_id TEXT,
    watermark INTEGER,
    outcome_json TEXT CHECK(outcome_json IS NULL OR json_valid(outcome_json)),
    error TEXT,
    snapshot_id TEXT REFERENCES context_snapshots(snapshot_id),
    FOREIGN KEY(session_id, branch_id) REFERENCES branches(session_id, branch_id)
);

CREATE TABLE execution_revocations (
    run_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    requested_at REAL NOT NULL
);

CREATE TABLE context_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    branch_id TEXT NOT NULL,
    head_id TEXT,
    watermark INTEGER NOT NULL CHECK(watermark >= 0),
    builder_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at REAL NOT NULL,
    FOREIGN KEY(session_id, branch_id) REFERENCES branches(session_id, branch_id),
    FOREIGN KEY(session_id, head_id) REFERENCES entries(session_id, entry_id)
);
CREATE INDEX snapshots_branch ON context_snapshots(session_id, branch_id, watermark DESC);
CREATE INDEX snapshots_run ON context_snapshots(run_id, created_at);

CREATE TABLE snapshot_blocks (
    digest TEXT PRIMARY KEY,
    body_json TEXT NOT NULL CHECK(json_valid(body_json))
);

CREATE TABLE extension_owners (
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    source_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    PRIMARY KEY(session_id, source_id)
);

CREATE TABLE extension_state (
    source_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    value_json TEXT NOT NULL CHECK(json_valid(value_json)),
    PRIMARY KEY(source_id, scope, key)
);

CREATE TABLE resources (
    source_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    head_version TEXT,
    PRIMARY KEY(source_id, scope, resource_key),
    FOREIGN KEY(source_id, scope, resource_key, head_version)
        REFERENCES resource_versions(source_id, scope, resource_key, version)
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE resource_versions (
    source_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    version TEXT NOT NULL,
    parent_version TEXT,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at REAL NOT NULL,
    PRIMARY KEY(source_id, scope, resource_key, version),
    FOREIGN KEY(source_id, scope, resource_key) REFERENCES resources(source_id, scope, resource_key),
    FOREIGN KEY(source_id, scope, resource_key, parent_version)
        REFERENCES resource_versions(source_id, scope, resource_key, version)
);

CREATE TABLE resource_publications (
    publication_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    previous_version TEXT,
    version TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    session_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(source_id, scope, resource_key, version)
        REFERENCES resource_versions(source_id, scope, resource_key, version)
);

CREATE TABLE artifacts (
    digest TEXT PRIMARY KEY,
    size INTEGER NOT NULL CHECK(size >= 0),
    created_at REAL NOT NULL
);
CREATE TABLE artifact_refs (
    owner_kind TEXT NOT NULL,
    owner_key TEXT NOT NULL,
    digest TEXT NOT NULL REFERENCES artifacts(digest),
    PRIMARY KEY(owner_kind, owner_key, digest)
);

CREATE TABLE host_metadata (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL CHECK(json_valid(value_json))
);

CREATE TABLE observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    stream TEXT NOT NULL,
    body_json TEXT NOT NULL CHECK(json_valid(body_json)),
    created_at REAL NOT NULL
);
CREATE INDEX observations_stream_seq ON observations(stream, seq);

CREATE TABLE observation_health (
    sink_id TEXT PRIMARY KEY,
    dropped INTEGER NOT NULL CHECK(dropped >= 0),
    failed INTEGER NOT NULL CHECK(failed >= 0),
    updated_at REAL NOT NULL
);

CREATE TABLE extension_tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    source_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    generation TEXT NOT NULL,
    handler TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    snapshot_id TEXT,
    origin_kind TEXT NOT NULL DEFAULT 'user'
        CHECK(origin_kind IN ('user','review','evaluation','naming')),
    status TEXT NOT NULL CHECK(status IN ('queued','running','cancelling','cancelled','succeeded','failed','interrupted')),
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    error TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX extension_tasks_owner ON extension_tasks(session_id,source_id,owner_id,generation,status);

CREATE TABLE managed_processes (
    process_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    run_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('launching','running','exited','launch_failed')),
    intent_json TEXT NOT NULL CHECK(json_valid(intent_json)),
    native_json TEXT CHECK(native_json IS NULL OR json_valid(native_json)),
    outcome_json TEXT CHECK(outcome_json IS NULL OR json_valid(outcome_json)),
    updated_at REAL NOT NULL
);
CREATE INDEX managed_processes_run ON managed_processes(run_id,status);

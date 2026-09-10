CREATE TABLE gateway_owner (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    owner_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    accepting INTEGER NOT NULL CHECK(accepting IN (0,1)),
    dispatch_seq INTEGER NOT NULL DEFAULT 0,
    next_lane TEXT NOT NULL DEFAULT 'foreground'
);

CREATE TABLE gateway_routes (
    route_key TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    epoch INTEGER NOT NULL CHECK(epoch>=1),
    destination_json TEXT NOT NULL CHECK(json_valid(destination_json)),
    pending_new INTEGER NOT NULL DEFAULT 0 CHECK(pending_new IN (0,1))
);

CREATE TABLE gateway_workspaces (
    workspace_id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('available','leased','quarantined')),
    run_id TEXT,
    reason TEXT
);

CREATE TABLE gateway_tasks (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL UNIQUE,
    route_key TEXT NOT NULL REFERENCES gateway_routes(route_key),
    principal_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    origin_session_id TEXT NOT NULL REFERENCES sessions(session_id),
    conversation_epoch INTEGER NOT NULL,
    source_head_id TEXT,
    lane TEXT NOT NULL CHECK(lane IN ('foreground','background')),
    workspace_id TEXT NOT NULL REFERENCES gateway_workspaces(workspace_id),
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
    destination_json TEXT NOT NULL CHECK(json_valid(destination_json)),
    status TEXT NOT NULL CHECK(status IN ('queued','steering','consumed','running','cancelling','cancelled','succeeded','failed','interrupted','outcome_unknown','blocked')),
    target_run_id TEXT REFERENCES gateway_attempts(run_id),
    consumed_entry_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    run_id TEXT,
    output TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE INDEX gateway_tasks_ready ON gateway_tasks(status,lane,session_id,seq);
CREATE INDEX gateway_tasks_principal ON gateway_tasks(principal_id,status,lane);
CREATE INDEX gateway_tasks_steering ON gateway_tasks(target_run_id,status,seq);

CREATE TABLE gateway_session_order (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
    last_dispatched INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE gateway_controls (
    control_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    route_key TEXT NOT NULL,
    command TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('waiting','completed')),
    response_json TEXT NOT NULL CHECK(json_valid(response_json)),
    created_at REAL NOT NULL
);

CREATE TABLE gateway_inbox (
    adapter_instance_id TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    task_id TEXT REFERENCES gateway_tasks(task_id),
    control_id TEXT REFERENCES gateway_controls(control_id),
    receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
    created_at REAL NOT NULL,
    PRIMARY KEY(adapter_instance_id,source_message_id),
    CHECK((task_id IS NULL) != (control_id IS NULL))
);

CREATE TABLE gateway_attempts (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES gateway_tasks(task_id),
    attempt INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    owner_generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    released INTEGER NOT NULL DEFAULT 0 CHECK(released IN (0,1)),
    started_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE(task_id,attempt)
);
CREATE INDEX gateway_attempts_active ON gateway_attempts(released,owner_id);

CREATE TABLE gateway_outbox (
    delivery_id TEXT PRIMARY KEY,
    task_id TEXT REFERENCES gateway_tasks(task_id),
    control_id TEXT REFERENCES gateway_controls(control_id),
    kind TEXT NOT NULL CHECK(kind IN ('accepted','result','control')),
    destination_json TEXT NOT NULL CHECK(json_valid(destination_json)),
    content_json TEXT NOT NULL CHECK(json_valid(content_json)),
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','sending','sent','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    claimed_by TEXT,
    claim_generation INTEGER,
    receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
    error TEXT,
    created_at REAL NOT NULL,
    sent_at REAL,
    UNIQUE(task_id,kind),
    UNIQUE(control_id,kind),
    CHECK((task_id IS NULL) != (control_id IS NULL))
);
CREATE INDEX gateway_outbox_ready ON gateway_outbox(status,next_attempt_at);

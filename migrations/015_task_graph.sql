-- ============================================================================
-- Migration 015: Domain-Agnostic Task Graph Layer (P0)
--
-- Adds a goal-oriented task decomposition layer on top of the existing work
-- market. A run can now carry one or more task_graphs; each graph decomposes a
-- high-level goal into subtasks (DAG nodes) with explicit dependencies,
-- acceptance criteria, tool allowlists and per-node budgets. Nodes are
-- published into the existing agent_tasks market, so claim/signals/controller
-- keep working unchanged; the graph layer only gates *which* nodes become
-- claimable (dependency-gated publishing) and evaluates acceptance on
-- completion.
--
-- Also adds a receipt-verified evidence chain (task_evidence), ported from the
-- reverselibrary swarm: evidence refs must appear verbatim in a completed tool
-- call's request/response before they count.
--
-- This layer is deliberately domain-agnostic: role/task_type/tool_allowlist
-- are plain strings; nothing here knows about IDA, binaries or web scanners.
-- ============================================================================

-- ── agent_tasks: extend with graph affiliation + acceptance bookkeeping ──

ALTER TABLE agent_tasks ADD COLUMN graph_id TEXT;
ALTER TABLE agent_tasks ADD COLUMN task_key TEXT;
ALTER TABLE agent_tasks ADD COLUMN depends_on_keys TEXT DEFAULT '[]';
ALTER TABLE agent_tasks ADD COLUMN acceptance_criteria TEXT DEFAULT '[]';
ALTER TABLE agent_tasks ADD COLUMN tool_allowlist TEXT DEFAULT '[]';
ALTER TABLE agent_tasks ADD COLUMN acceptance_status TEXT
    DEFAULT 'pending' CHECK (acceptance_status IN ('pending', 'accepted', 'rejected'));

-- ── task_graphs: one row per goal-level task graph ──

CREATE TABLE IF NOT EXISTS task_graphs (
    graph_id        TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    goal            TEXT NOT NULL,
    strategy        TEXT NOT NULL DEFAULT 'deterministic',
    domain          TEXT NOT NULL DEFAULT 'general',
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'completed', 'cancelled')),
    total_nodes     INTEGER NOT NULL DEFAULT 0,
    completed_nodes INTEGER NOT NULL DEFAULT 0,
    failed_nodes    INTEGER NOT NULL DEFAULT 0,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ── task_graph_nodes: DAG nodes (subtasks) of a graph ──

CREATE TABLE IF NOT EXISTS task_graph_nodes (
    node_id             TEXT PRIMARY KEY,
    graph_id            TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    task_key            TEXT NOT NULL,          -- unique within a graph
    goal                TEXT NOT NULL,
    phase               INTEGER NOT NULL DEFAULT 1,
    depends_on          TEXT NOT NULL DEFAULT '[]',   -- JSON array of task_keys
    priority            INTEGER NOT NULL DEFAULT 50,
    role                TEXT NOT NULL DEFAULT 'custom',
    task_type           TEXT NOT NULL DEFAULT 'analyze',
    tool_allowlist      TEXT NOT NULL DEFAULT '[]',   -- JSON array of tool ids
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',   -- JSON array of {metric,op,value,required}
    budget              TEXT NOT NULL DEFAULT '{}',   -- JSON {tokens,tool_calls,seconds}
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','published','running',
                                          'completed','failed','blocked','cancelled')),
    task_id             TEXT,                        -- agent_tasks.task_id once published
    metadata            TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (graph_id, task_key)
);

-- ── task_evidence: receipt-verified evidence chain ──

CREATE TABLE IF NOT EXISTS task_evidence (
    evidence_id      TEXT PRIMARY KEY,
    task_id          TEXT NOT NULL,             -- agent_tasks.task_id
    node_id          TEXT,
    graph_id         TEXT,
    run_id           TEXT NOT NULL,
    evidence_type    TEXT NOT NULL,
    ref              TEXT NOT NULL,
    receipt_id       TEXT,
    supports_metrics TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','verified','rejected')),
    verification_note TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT
);

-- ── indexes ──

CREATE INDEX IF NOT EXISTS idx_task_graphs_run ON task_graphs(run_id, status);
CREATE INDEX IF NOT EXISTS idx_tgn_graph ON task_graph_nodes(graph_id, status);
CREATE INDEX IF NOT EXISTS idx_tgn_run ON task_graph_nodes(run_id, status, role);
CREATE INDEX IF NOT EXISTS idx_tgn_task ON task_graph_nodes(task_id);
CREATE INDEX IF NOT EXISTS idx_te_task ON task_evidence(task_id, status);
CREATE INDEX IF NOT EXISTS idx_te_run ON task_evidence(run_id);
CREATE INDEX IF NOT EXISTS idx_at_graph ON agent_tasks(graph_id, status);

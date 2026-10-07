"""由 KB 的一致性备份及迁移事务调用。"""


def migrate(conn):
    statements = [
        """CREATE TABLE IF NOT EXISTS kb_parse_versions (
            parse_id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            config_json TEXT NOT NULL, status TEXT NOT NULL, lease TEXT NOT NULL DEFAULT '',
            page_count INTEGER NOT NULL, completed_pages INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS kb_pages (
            parse_id TEXT NOT NULL, page INTEGER NOT NULL, status TEXT NOT NULL,
            printed_page TEXT, width INTEGER NOT NULL, height INTEGER NOT NULL,
            coordinate_space TEXT NOT NULL, rotation INTEGER NOT NULL,
            raw_path TEXT NOT NULL, warnings_json TEXT NOT NULL, truncated INTEGER,
            PRIMARY KEY(parse_id,page))""",
        """CREATE TABLE IF NOT EXISTS kb_blocks (
            parse_id TEXT NOT NULL, block_id TEXT NOT NULL, page INTEGER NOT NULL,
            ordinal INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
            bbox_json TEXT NOT NULL, structure_json TEXT NOT NULL,
            section_id TEXT NOT NULL DEFAULT '', parent_id TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(parse_id,block_id))""",
        """CREATE TABLE IF NOT EXISTS kb_index_runs (
            run_id TEXT PRIMARY KEY, parse_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            config_json TEXT NOT NULL, status TEXT NOT NULL, chunk_count INTEGER NOT NULL DEFAULT 0,
            indexed_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS kb_chunks (
            run_id TEXT NOT NULL, chunk_idx INTEGER NOT NULL, kind TEXT NOT NULL,
            text TEXT NOT NULL, token_count INTEGER NOT NULL,
            PRIMARY KEY(run_id,chunk_idx))""",
        """CREATE TABLE IF NOT EXISTS kb_chunk_sources (
            run_id TEXT NOT NULL, chunk_idx INTEGER NOT NULL, parse_id TEXT NOT NULL,
            block_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
            row_start INTEGER, row_end INTEGER,
            PRIMARY KEY(run_id,chunk_idx,block_id,start))""",
        """CREATE TABLE IF NOT EXISTS kb_identifiers (
            parse_id TEXT NOT NULL, block_id TEXT NOT NULL, value TEXT NOT NULL,
            original TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
            PRIMARY KEY(parse_id,block_id,value,start))""",
        "CREATE INDEX IF NOT EXISTS idx_kb_parse_doc ON kb_parse_versions(doc_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_blocks_page ON kb_blocks(parse_id,page,ordinal)",
        "CREATE INDEX IF NOT EXISTS idx_kb_identifiers_value ON kb_identifiers(value,parse_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_sources_block ON kb_chunk_sources(parse_id,block_id)",
        """CREATE TABLE IF NOT EXISTS kb_structure_versions (
            structure_id TEXT PRIMARY KEY, parse_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            config_json TEXT NOT NULL, status TEXT NOT NULL, checkpoint_json TEXT NOT NULL DEFAULT '{}',
            quality_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            UNIQUE(parse_id,fingerprint))""",
        """CREATE TABLE IF NOT EXISTS kb_document_nodes (
            structure_id TEXT NOT NULL, node_id TEXT NOT NULL, parent_id TEXT NOT NULL,
            kind TEXT NOT NULL, ordinal INTEGER NOT NULL, depth INTEGER NOT NULL,
            title TEXT NOT NULL, preview TEXT NOT NULL, basis TEXT NOT NULL,
            start_order INTEGER NOT NULL, end_order INTEGER NOT NULL,
            page INTEGER NOT NULL, end_page INTEGER NOT NULL, attributes_json TEXT NOT NULL,
            PRIMARY KEY(structure_id,node_id))""",
        """CREATE TABLE IF NOT EXISTS kb_node_sources (
            structure_id TEXT NOT NULL, node_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            block_id TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
            PRIMARY KEY(structure_id,node_id,ordinal))""",
        """CREATE TABLE IF NOT EXISTS kb_node_relations (
            structure_id TEXT NOT NULL, source_id TEXT NOT NULL, target_id TEXT NOT NULL,
            kind TEXT NOT NULL, basis TEXT NOT NULL, evidence_json TEXT NOT NULL,
            PRIMARY KEY(structure_id,source_id,target_id,kind))""",
        "CREATE INDEX IF NOT EXISTS idx_kb_nodes_parent ON kb_document_nodes(structure_id,parent_id,ordinal)",
        "CREATE INDEX IF NOT EXISTS idx_kb_nodes_span ON kb_document_nodes(structure_id,start_order,end_order)",
        "CREATE INDEX IF NOT EXISTS idx_kb_node_source_block ON kb_node_sources(structure_id,block_id)",
        "CREATE INDEX IF NOT EXISTS idx_kb_relations_target ON kb_node_relations(structure_id,target_id,kind)",
    ]
    for statement in statements:
        conn.execute(statement)
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(kb_pages)')}
    for name, definition in {'error': "TEXT NOT NULL DEFAULT ''", 'attempts': 'INTEGER NOT NULL DEFAULT 0'}.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE kb_pages ADD COLUMN {name} {definition}')
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(kb_index_runs)')}
    if 'structure_id' not in columns:
        conn.execute("ALTER TABLE kb_index_runs ADD COLUMN structure_id TEXT NOT NULL DEFAULT ''")
    columns = {r['name'] for r in conn.execute('PRAGMA table_info(kb_chunks)')}
    if 'node_id' not in columns:
        conn.execute("ALTER TABLE kb_chunks ADD COLUMN node_id TEXT NOT NULL DEFAULT ''")

"""由 KB 的一致性备份及迁移事务调用。"""


def migrate(conn):
    """在既有迁移事务内创建或升级文档版本、页块、结构和索引检查点表。"""
    statements = [
        """CREATE TABLE IF NOT EXISTS kb_structure_publications (
            review_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, doc_id TEXT NOT NULL, parse_id TEXT NOT NULL,
            expected_run_id TEXT NOT NULL, result_hash TEXT NOT NULL, config_json TEXT NOT NULL,
            status TEXT NOT NULL, structure_id TEXT NOT NULL DEFAULT '', run_id TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS kb_structure_reviews (
            review_id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, doc_id TEXT NOT NULL,
            parse_id TEXT NOT NULL, structure_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            config_json TEXT NOT NULL, status TEXT NOT NULL, lease TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT '',
            checkpoint_json TEXT NOT NULL DEFAULT '{}', result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(doc_id, fingerprint))""",
        """CREATE TABLE IF NOT EXISTS kb_structure_review_batches (
            review_id TEXT NOT NULL, batch_no INTEGER NOT NULL, input_hash TEXT NOT NULL,
            input_json TEXT NOT NULL, output_json TEXT NOT NULL, metrics_json TEXT NOT NULL,
            PRIMARY KEY(review_id,batch_no))""",
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
            parse_id TEXT NOT NULL, block_id TEXT NOT NULL, page INTEGER,
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
            page INTEGER, end_page INTEGER, attributes_json TEXT NOT NULL,
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
        """CREATE TABLE IF NOT EXISTS kb_structure_blocks (
            structure_id TEXT NOT NULL, block_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            section_id TEXT NOT NULL, parent_id TEXT NOT NULL, structure_json TEXT NOT NULL,
            PRIMARY KEY(structure_id,block_id))""",
    ]
    for table in ('kb_blocks', 'kb_document_nodes'):
        columns = list(conn.execute(f'PRAGMA table_info({table})'))
        if any(r['name'] == 'page' and r['notnull'] for r in columns):
            create = next(s for s in statements if s.startswith(f'CREATE TABLE IF NOT EXISTS {table} ('))
            conn.execute(create.replace(f'IF NOT EXISTS {table}', f'{table}_text_migration'))
            names = ','.join(r['name'] for r in columns)
            conn.execute(f'INSERT INTO {table}_text_migration ({names}) SELECT {names} FROM {table}')
            if conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] != conn.execute(f'SELECT count(*) FROM {table}_text_migration').fetchone()[0]:
                raise ValueError('document_migration_count_mismatch')
            conn.execute(f'DROP TABLE {table}')
            conn.execute(f'ALTER TABLE {table}_text_migration RENAME TO {table}')
    for statement in statements:
        conn.execute(statement)
    review_columns={r['name'] for r in conn.execute('PRAGMA table_info(kb_structure_reviews)')}
    if 'request_id' not in review_columns:
        conn.execute("ALTER TABLE kb_structure_reviews ADD COLUMN request_id TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_kb_review_request ON kb_structure_reviews(doc_id,request_id) WHERE request_id!=''")
    conn.execute('''CREATE VIEW IF NOT EXISTS kb_resolved_blocks AS
        SELECT v.structure_id,b.parse_id,b.block_id,b.page,coalesce(p.ordinal,b.ordinal) AS ordinal,
            b.kind,b.text,b.bbox_json,coalesce(p.structure_json,b.structure_json) AS structure_json,
            coalesce(p.section_id,b.section_id) AS section_id,coalesce(p.parent_id,b.parent_id) AS parent_id
        FROM kb_structure_versions v JOIN kb_blocks b ON b.parse_id=v.parse_id
        LEFT JOIN kb_structure_blocks p ON p.structure_id=v.structure_id AND p.block_id=b.block_id
        UNION ALL SELECT '',parse_id,block_id,page,ordinal,kind,text,bbox_json,structure_json,section_id,parent_id
        FROM kb_blocks''')
    conn.execute('''CREATE TABLE IF NOT EXISTS kb_text_sources (
        parse_id TEXT PRIMARY KEY, text_path TEXT NOT NULL, text_sha256 TEXT NOT NULL,
        encoding TEXT NOT NULL, char_count INTEGER NOT NULL, line_starts_json TEXT NOT NULL)''')
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

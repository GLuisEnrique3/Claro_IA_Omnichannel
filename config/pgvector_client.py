import json
import math
import re
import psycopg2
import psycopg2.extras


def _table_name(collection_name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]", "_", collection_name)
    return f"db_{safe}"


class PgVectorCollection:
    def __init__(self, name: str, table: str, conn_factory):
        self.name = name
        self._table = table
        self._conn_factory = conn_factory

    def _conn(self):
        return self._conn_factory()

    @staticmethod
    def _where_sql(where):
        if not where:
            return "", []
        return "AND metadata @> %s::jsonb", [json.dumps(where)]

    def add(self, ids, documents, embeddings, metadatas):
        conn = self._conn()
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                f"""
                INSERT INTO {self._table} (id, document, embedding, metadata)
                VALUES (%s, %s, %s::vector, %s::jsonb)
                ON CONFLICT (id) DO UPDATE SET
                    document  = EXCLUDED.document,
                    embedding = EXCLUDED.embedding,
                    metadata  = EXCLUDED.metadata
                """,
                [
                    (i, d, json.dumps(e), json.dumps(m))
                    for i, d, e, m in zip(ids, documents, embeddings, metadatas)
                ],
                page_size=500,
            )
        conn.commit()

    def query(self, query_embeddings, n_results=3, where=None, include=None):
        if include is None:
            include = ["documents", "metadatas", "distances"]
        w_sql, w_params = self._where_sql(where)
        sql = f"""
            SELECT id, document, metadata,
                   (embedding <=> %s::vector) AS cosine_dist
            FROM {self._table}
            WHERE 1=1 {w_sql}
            ORDER BY cosine_dist ASC LIMIT %s
        """
        params = [json.dumps(query_embeddings[0])] + w_params + [n_results]
        conn = self._conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        ids_out, docs, metas, dists = [], [], [], []
        for row in rows:
            ids_out.append(row["id"])
            docs.append(row["document"])
            metas.append(dict(row["metadata"]))
            # Convierte distancia coseno → L2-equivalente para preservar fórmula sim_pct
            dists.append(math.sqrt(max(0.0, 2.0 * float(row["cosine_dist"]))))
        return {
            "ids":       [ids_out],
            "documents": [docs]  if "documents"  in include else [[]],
            "metadatas": [metas] if "metadatas"  in include else [[]],
            "distances": [dists] if "distances"  in include else [[]],
        }

    def get(self, where=None, include=None):
        if include is None:
            include = ["documents", "metadatas"]
        w_sql, w_params = self._where_sql(where)
        sql = f"""
            SELECT id, document, metadata
            FROM {self._table}
            WHERE 1=1 {w_sql}
            ORDER BY id
        """
        conn = self._conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, w_params)
            rows = cur.fetchall()
        return {
            "ids":       [r["id"] for r in rows],
            "documents": [r["document"] for r in rows] if "documents" in include else [],
            "metadatas": [dict(r["metadata"]) for r in rows] if "metadatas" in include else [],
        }

    def delete(self, ids):
        conn = self._conn()
        with conn.cursor() as cur:
            cur.execute(
                f"DELETE FROM {self._table} WHERE id = ANY(%s)",
                [ids],
            )
        conn.commit()

    def count(self):
        conn = self._conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {self._table}")
            return cur.fetchone()[0]


class PgVectorClient:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = None

    def _get_conn(self):
        try:
            if self._conn is None or self._conn.closed:
                raise Exception
            self._conn.cursor().execute("SELECT 1")
        except Exception:
            self._conn = psycopg2.connect(self._dsn, connect_timeout=10)
            self._conn.autocommit = False
        return self._conn

    def _create_table(self, table: str):
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id        TEXT         PRIMARY KEY,
                    document  TEXT         NOT NULL DEFAULT '',
                    embedding vector(384)  NOT NULL,
                    metadata  JSONB        NOT NULL DEFAULT '{{}}'
                )
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS {table}_emb
                ON {table} USING hnsw (embedding vector_cosine_ops)
            """)
            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS {table}_meta
                ON {table} USING GIN (metadata jsonb_path_ops)
            """)
        conn.commit()

    def _table_exists(self, table: str) -> bool:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = %s",
                [table],
            )
            return cur.fetchone() is not None

    def get_or_create_collection(self, name: str, **_) -> PgVectorCollection:
        table = _table_name(name)
        self._create_table(table)
        return PgVectorCollection(name, table, self._get_conn)

    def get_collection(self, name: str) -> PgVectorCollection:
        table = _table_name(name)
        if not self._table_exists(table):
            raise ValueError(
                f"Collection '{name}' no existe (tabla '{table}' no encontrada)."
            )
        return PgVectorCollection(name, table, self._get_conn)

    def delete_collection(self, name: str):
        table = _table_name(name)
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()

    def list_collections(self):
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name LIKE 'db_%' "
                "ORDER BY 1"
            )
            rows = cur.fetchall()

        class _Col:
            def __init__(self, n):
                self.name = n.replace("db_", "", 1)

        return [_Col(r[0]) for r in rows]

import math
import numpy as np
import chromadb

# Modelo de embeddings compartido con main.py — se carga una sola vez
_embed_model = None


def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embed_model


def _normalize_where(where: dict | None) -> dict | None:
    """Normaliza filtros simples {k: v} al formato explícito {k: {"$eq": v}} de ChromaDB."""
    if not where:
        return None
    if len(where) == 1:
        k, v = next(iter(where.items()))
        if k in ("$and", "$or") and isinstance(v, list):
            return {k: [_normalize_where(expr) for expr in v]}
    return {
        k: (v if isinstance(v, dict) else {"$eq": v})
        for k, v in where.items()
    }


class ChromaCollection:
    def __init__(self, collection):
        self._col = collection

    def add(self, ids, documents, embeddings, metadatas):
        self._col.add(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )

    def query(self, query_embeddings, n_results=3, where=None, include=None):
        if include is None:
            include = ["documents", "metadatas", "distances"]
        normalized_where = _normalize_where(where) if where else None

        # En ChromaDB 1.x, query() con where+HNSW lanza "Error finding id" porque
        # los embeddings están en el índice HNSW y el filtro de metadata los cruza
        # de forma incorrecta. Cuando hay filtro, obtenemos los docs via get() (SQLite)
        # y calculamos la similitud coseno re-encodeando con el mismo modelo.
        if normalized_where:
            return self._query_with_filter(query_embeddings, n_results, normalized_where, include)

        # Sin filtro: usar HNSW directamente, acotando n_results al tamaño real
        try:
            total = self._col.count()
            if total == 0:
                return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
            n_results = min(n_results, total)
        except Exception:
            pass

        result = self._col.query(
            query_embeddings=query_embeddings,
            n_results=n_results,
            include=include,
        )
        # Convierte distancia coseno ChromaDB [0,1] → L2-equivalente para la fórmula
        # sim_pct = (1 - dist²/2) * 100 que usa main.py
        if "distances" in include and result.get("distances"):
            result["distances"] = [
                [math.sqrt(max(0.0, 2.0 * d)) for d in row]
                for row in result["distances"]
            ]
        return result

    def _query_with_filter(self, query_embeddings, n_results, normalized_where, include):
        """
        Obtiene docs filtrados via get() (solo SQLite, sin HNSW), los re-encodea con
        all-MiniLM-L6-v2 y calcula similitud coseno manualmente.
        """
        filtered = self._col.get(
            where=normalized_where,
            include=["documents", "metadatas"],
        )
        ids = filtered.get("ids", [])
        total = len(ids)

        if total == 0:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        documents = filtered.get("documents") or []
        if not documents:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        # Re-encodear los documentos filtrados con el mismo modelo
        model = _get_embed_model()
        doc_embs = model.encode(
            documents,
            convert_to_numpy=True,
            show_progress_bar=False,
        ).astype(np.float32)

        q_emb = np.array(query_embeddings[0], dtype=np.float32)
        q_norm = q_emb / (np.linalg.norm(q_emb) + 1e-10)
        norms = np.linalg.norm(doc_embs, axis=1, keepdims=True)
        doc_norms = doc_embs / (norms + 1e-10)
        sims = doc_norms @ q_norm  # cosine similarity por documento

        k = min(n_results, total)
        top_idx = np.argsort(sims)[::-1][:k].tolist()

        result_ids = [ids[i] for i in top_idx]
        result_docs = [documents[i] for i in top_idx]
        result_metas = (
            [filtered["metadatas"][i] for i in top_idx]
            if filtered.get("metadatas") else [{}] * k
        )
        # dist = sqrt(2 * cosine_dist) = sqrt(2 * (1 - sim))
        # → sim_pct = (1 - dist²/2)*100 = (1 - (1-sim))*100 = sim*100  ✓
        result_dists = [
            math.sqrt(max(0.0, 2.0 * (1.0 - float(sims[i])))) for i in top_idx
        ]

        return {
            "ids": [result_ids],
            "documents": [result_docs],
            "metadatas": [result_metas],
            "distances": [result_dists],
        }

    def get(self, where=None, include=None):
        if include is None:
            include = ["documents", "metadatas"]
        kwargs = {"include": include}
        if where:
            kwargs["where"] = _normalize_where(where)
        return self._col.get(**kwargs)

    def delete(self, ids):
        self._col.delete(ids=ids)

    def count(self):
        return self._col.count()


class ChromaClient:
    def __init__(self, path: str):
        self._client = chromadb.PersistentClient(path=path)

    def get_or_create_collection(self, name: str, **_) -> ChromaCollection:
        col = self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=None,
        )
        return ChromaCollection(col)

    def get_collection(self, name: str) -> ChromaCollection:
        col = self._client.get_collection(
            name=name,
            embedding_function=None,
        )
        return ChromaCollection(col)

    def delete_collection(self, name: str):
        self._client.delete_collection(name=name)

    def list_collections(self):
        return self._client.list_collections()

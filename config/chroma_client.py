import math
import chromadb


def _normalize_where(where: dict | None) -> dict | None:
    """Normaliza filtros simples {k: v} al formato explícito {k: {"$eq": v}} de ChromaDB."""
    if not where:
        return None
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
        kwargs = {
            "query_embeddings": query_embeddings,
            "n_results": n_results,
            "include": include,
        }
        if where:
            kwargs["where"] = _normalize_where(where)
        result = self._col.query(**kwargs)
        # Convierte distancia coseno [0,2] → L2-equivalente para mantener la fórmula
        # sim_pct = (1 - dist²/2) * 100 que usa main.py
        if "distances" in include and result.get("distances"):
            result["distances"] = [
                [math.sqrt(max(0.0, 2.0 * d)) for d in row]
                for row in result["distances"]
            ]
        return result

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

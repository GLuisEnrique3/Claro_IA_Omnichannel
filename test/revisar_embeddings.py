import os
import sys
from dotenv import load_dotenv

# =========================================================
# CONFIG
# =========================================================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))
from config.pgvector_client import PgVectorClient

# =========================================================
# FUNCTION
# =========================================================

def inspect_collection(
    collection_name: str,
    show_preview: bool = False,
    preview_chars: int = 500
):

    client = PgVectorClient(dsn=os.getenv("PGVECTOR_DSN"))

    collections = [c.name for c in client.list_collections()]

    if collection_name not in collections:

        print(
            f"❌ Error: la colección "
            f"'{collection_name}' no existe."
        )

        return

    collection = client.get_collection(collection_name)

    data = collection.get()

    total_chunks = len(data["ids"])

    print("\n" + "=" * 80)
    print(f"COLECCIÓN: {collection_name}")
    print(f"TOTAL CHUNKS: {total_chunks}")
    print("=" * 80)

    for i, chunk_id in enumerate(data["ids"], start=1):

        metadata = data["metadatas"][i - 1]
        document = data["documents"][i - 1]

        filename = metadata.get("filename", "N/A")
        category = metadata.get("category", "N/A")
        carrier = metadata.get("carrier", "N/A")
        page = metadata.get("page", "N/A")
        chunk_index = metadata.get("chunk_index", "N/A")

        print(
            f"{i:04d} | "
            f"{chunk_id}"
        )

        print(f"   Archivo     : {filename}")
        print(f"   Categoría   : {category}")
        print(f"   Carrier     : {carrier}")
        print(f"   Página      : {page}")
        print(f"   Chunk Index : {chunk_index}")

        if show_preview:

            preview = (
                document[:preview_chars]
                .replace("\n", " ")
                .strip()
            )

            print("\n   Preview:")
            print(f"   {preview}")

        print("-" * 80)

    print("\n✅ Verificación completada.")

# =========================================================
# USAGE
# =========================================================

inspect_collection(
    collection_name="calendario_comisiones",
    show_preview=False
)

inspect_collection(
    collection_name="instructivos_contratos_aca",
    show_preview=True
)
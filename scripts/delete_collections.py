import os
import chromadb

# =========================================================
# CONFIG
# =========================================================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMA_PATH = os.path.join(BASE_DIR, "chroma_db")

COLLECTION_NAME = "instructivos_contratos_aca"

# =========================================================
# CONNECT
# =========================================================

client = chromadb.PersistentClient(path=CHROMA_PATH)

# =========================================================
# DELETE COLLECTION
# =========================================================

collections = [c.name for c in client.list_collections()]

if COLLECTION_NAME in collections:

    client.delete_collection(COLLECTION_NAME)

    print(f"✅ Colección eliminada: {COLLECTION_NAME}")

else:

    print(f"⚠️ La colección no existe: {COLLECTION_NAME}")
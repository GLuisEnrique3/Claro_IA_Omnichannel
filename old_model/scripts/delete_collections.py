import os
import sys
from dotenv import load_dotenv

# =========================================================
# CONFIG
# =========================================================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))
from config.chroma_client import ChromaClient

#COLLECTION_NAME = "instructivos_contratos_supplementary"

COLLECTIONS_TO_DELETE = [
    "bonus_compensation",
]

# =========================================================
# CONNECT
# =========================================================

client = ChromaClient(path=os.getenv("CHROMA_PATH", "./chroma_db"))

# =========================================================
# DELETE COLLECTION
# =========================================================

"""
collections = [c.name for c in client.list_collections()]
if COLLECTION_NAME in collections:

    client.delete_collection(COLLECTION_NAME)

    print(f"✅ Colección eliminada: {COLLECTION_NAME}")

else:

    print(f"⚠️ La colección no existe: {COLLECTION_NAME}")
"""

existing = [c.name for c in client.list_collections()]

for name in COLLECTIONS_TO_DELETE:
    if name in existing:
        client.delete_collection(name)
        print(f"✅ Eliminada: {name}")
    else:
        print(f"⚠️ No existe: {name}")


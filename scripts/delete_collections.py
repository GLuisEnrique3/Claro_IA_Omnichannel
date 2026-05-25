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

COLLECTION_NAME = "calendario_comisiones"

# =========================================================
# CONNECT
# =========================================================

client = ChromaClient(path=os.getenv("CHROMA_PATH", "./chroma_db"))

# =========================================================
# DELETE COLLECTION
# =========================================================

collections = [c.name for c in client.list_collections()]

if COLLECTION_NAME in collections:

    client.delete_collection(COLLECTION_NAME)

    print(f"✅ Colección eliminada: {COLLECTION_NAME}")

else:

    print(f"⚠️ La colección no existe: {COLLECTION_NAME}")
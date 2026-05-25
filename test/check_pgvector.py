import os
import sys
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))
from config.chroma_client import ChromaClient

# --- ChromaDB state ---
client = ChromaClient(path=os.getenv("CHROMA_PATH", "./chroma_db"))
collection = client.get_collection("calendario_comisiones")
data = collection.get()

# Mostrar los dos primeros chunks
print("=== PRIMER CHUNK ===")
if len(data['documents']) > 0:
    print(data['documents'][0])
    print(f"\nMetadata: {data['metadatas'][0]}")
else:
    print("No hay chunks disponibles")

print("\n=== SEGUNDO CHUNK ===")
if len(data['documents']) > 1:
    print(data['documents'][1])
    print(f"\nMetadata: {data['metadatas'][1]}")
else:
    print("No hay segundo chunk disponible")

# Información básica
print(f"\nTotal chunks en ChromaDB: {len(data['ids'])}")

"""
# --- PDF extractable text ---
print()
print("Texto extraible de los PDFs:")
for fname in os.listdir(PDF_DIR):
    if not fname.endswith(".pdf"):
        continue
    reader = PdfReader(os.path.join(PDF_DIR, fname))
    total_chars = sum(len((p.extract_text() or "").strip()) for p in reader.pages)
    print(f"  {fname}: {len(reader.pages)} pag, {total_chars} chars")

# --- Sample chunk ---
if data["documents"]:
    print()
    print("Primer chunk de muestra:")
    print(data["documents"][0][:300])
"""
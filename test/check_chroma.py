import os
import chromadb
from pypdf import PdfReader

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMA_PATH = os.path.join(BASE_DIR, "chroma_db")
PDF_DIR = os.path.join(BASE_DIR, "pdf")

# --- ChromaDB state ---
client = chromadb.PersistentClient(path=CHROMA_PATH)
collection = client.get_collection("documentos_normativos")
data = collection.get()
print(f"Total chunks en ChromaDB: {len(data['ids'])}")

counts = {}
for m in data["metadatas"]:
    key = (m.get("filename"), m.get("category"))
    counts[key] = counts.get(key, 0) + 1
for (f, c), n in counts.items():
    print(f"  archivo={f}  categoria={c}  chunks={n}")

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

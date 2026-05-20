import os
import chromadb

# CONFIG

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROMA_PATH = os.path.join(BASE_DIR, "chroma_db")
COLLECTION_NAME = "documentos_normativos"

FILENAME_TO_DELETE = "CM-IN-18 Contract Management with ALLIANT ACA.pdf"

# CONEXION

client = chromadb.PersistentClient(path=CHROMA_PATH)

collection = client.get_collection(COLLECTION_NAME)

# BUSCAR IDS DEL ARCHIVO

results = collection.get(
    where={"filename": FILENAME_TO_DELETE},
    include=["metadatas"]
)

ids_to_delete = results["ids"]

if not ids_to_delete:
    print(f"No se encontraron chunks para:")
    print(FILENAME_TO_DELETE)
    exit()

print(f"\nSe encontraron {len(ids_to_delete)} chunks")
print(f"Eliminando archivo:")
print(FILENAME_TO_DELETE)

# ELIMINAR

collection.delete(ids=ids_to_delete)

print("✅ Archivo eliminado de ChromaDB")
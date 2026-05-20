import os
import chromadb
from sentence_transformers import SentenceTransformer
from pypdf import PdfReader

# =========================================================
# CONFIG
# =========================================================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_DIR = os.path.join(BASE_DIR, "pdf")
CHROMA_PATH = os.path.join(BASE_DIR, "chroma_db")

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

CHUNK_SIZE = 500
CHUNK_OVERLAP = 100
MAX_CHARS_PER_CHUNK = 600

# =========================================================
# DOCUMENT CONFIGURATION
# =========================================================

DOCUMENTS = [
    {
        "filename": "Calendario Pago Comisiones.pdf",
        "collection": "calendario_comisiones",
        "chunking": "lines",
        "metadata": {
            "category": "calendario_comisiones"
        }
    },

    {
        "filename": "CM-IN-02 Instructivo Gestión de contratos con OSCAR ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "OSCAR",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-03 Instructivo Gestión de contratos con AMBETTER ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "AMBETTER",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-04 Instructivo Gestión de contratos con FLORIDA BLUE  ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "FLORIDA BLUE",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-05 Instructivo Gestión de contratos con MOLINA ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "MOLINA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-06 Instructivo Gestión de contratos con UNITED HEALTH CARE  ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "UNITED HEALTH CARE",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-07 Instructivo Gestión de contratos con CIGNA  ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "CIGNA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-08 Instructivo Gestión de contratos con FRIDAY ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "FRIDAY",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-09 Instructivo Gestión de contratos con AVMED ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "AVMED",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-10 Instructivo Gestión de contratos con BlueCross BlueShield Texas ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "BlueCross BlueShield Texas",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-11 Instructivo Gestión de contratos con BlueCross BlueShield NC, SC ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "BlueCross BlueShield NC SC",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-13 Instructivo Gestión de contratos con Anthem BlueCross BlueShield CA, IN, MO, OH ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "Anthem BlueCross BlueShield",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-14 Instructivo Gestión de contratos con AETNA ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "AETNA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-15 Instructivo Gestión de contratos con Amerihealth Caritas ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "Amerihealth Caritas",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-16 Instructivo Gestión de contratos con Caresource ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "Caresource",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-17 Instructivo Gestión de contratos con Ascension ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "Ascension",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-18 Contract Management with ALLIANT ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "ALLIANT",
            "category": "instructivos_aca"
        }
    },
]

# =========================================================
# PDF EXTRACTION
# =========================================================

def extract_pages(pdf_path: str) -> list[tuple[int, str]]:
    reader = PdfReader(pdf_path)

    pages = []

    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text()

        if text and text.strip():
            pages.append((i, text.strip()))

    return pages

# =========================================================
# CHUNKING METHODS
# =========================================================

def sliding_window(text: str) -> list[str]:
    chunks = []

    start = 0

    while start < len(text):

        end = start + CHUNK_SIZE

        chunk = text[start:end].strip()

        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break

        start = end - CHUNK_OVERLAP

    return chunks


def line_chunks(text: str) -> list[str]:

    lines = [l.strip() for l in text.split("\n") if l.strip()]

    chunks = []
    current = []
    current_len = 0

    for line in lines:

        if current_len + len(line) > MAX_CHARS_PER_CHUNK and current:

            chunks.append("\n".join(current))

            current = [line]
            current_len = len(line)

        else:
            current.append(line)
            current_len += len(line) + 1

    if current:
        chunks.append("\n".join(current))

    return chunks


def generate_chunks(text: str, strategy: str) -> list[str]:

    if strategy == "sliding":
        return sliding_window(text)

    elif strategy == "lines":
        return line_chunks(text)

    else:
        raise ValueError(f"Estrategia desconocida: {strategy}")

# =========================================================
# CHROMA
# =========================================================

chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)

model = SentenceTransformer(EMBEDDING_MODEL)

# =========================================================
# PROCESS DOCUMENTS
# =========================================================

for doc_config in DOCUMENTS:

    filename = doc_config["filename"]
    collection_name = doc_config["collection"]
    chunking_strategy = doc_config["chunking"]
    extra_metadata = doc_config["metadata"]

    print("=" * 80)
    print(f"Procesando configuración: {filename}")

    # -----------------------------------------------------
    # Collection
    # -----------------------------------------------------

    try:
        collection = chroma_client.get_collection(collection_name)

        print(
            f"Colección '{collection_name}' existente "
            f"({collection.count()} chunks)."
        )

    except Exception:

        collection = chroma_client.create_collection(collection_name)

        print(f"Colección '{collection_name}' creada.")

    # -----------------------------------------------------
    # Already indexed?
    # -----------------------------------------------------

    existing_data = collection.get(include=["metadatas"])

    indexed_files = {
        m["filename"]
        for m in existing_data["metadatas"]
    }

    if filename in indexed_files:
        print(f"⏭ Ya indexado: {filename}")
        continue

    # -----------------------------------------------------
    # PDF exists?
    # -----------------------------------------------------

    pdf_path = os.path.join(PDF_DIR, filename)

    if not os.path.exists(pdf_path):
        print(f"⚠ Archivo no encontrado: {filename}")
        continue

    # -----------------------------------------------------
    # Extract pages
    # -----------------------------------------------------

    pages = extract_pages(pdf_path)

    if not pages:
        print("⚠ PDF sin texto extraíble.")
        continue

    # -----------------------------------------------------
    # Generate chunks
    # -----------------------------------------------------

    ids = []
    documents = []
    embeddings = []
    metadatas = []

    doc_id_base = (
        filename
        .replace(".pdf", "")
        .lower()
        .replace(" ", "_")
    )

    total_chunks = 0

    for page_num, page_text in pages:

        chunks = generate_chunks(
            page_text,
            chunking_strategy
        )

        if not chunks:
            continue

        batch_embeddings = model.encode(
            chunks,
            show_progress_bar=False
        ).tolist()

        for chunk_idx, (chunk, embedding) in enumerate(
            zip(chunks, batch_embeddings)
        ):

            ids.append(
                f"{doc_id_base}_p{page_num}_c{chunk_idx}"
            )

            documents.append(chunk)

            embeddings.append(embedding)

            metadata = {
                "filename": filename,
                "page": page_num,
                "chunk_index": chunk_idx,
                **extra_metadata
            }

            metadatas.append(metadata)

            total_chunks += 1

    # -----------------------------------------------------
    # Insert into Chroma
    # -----------------------------------------------------

    collection.add(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas
    )

    print(
        f"✅ {len(pages)} páginas "
        f"→ {total_chunks} chunks"
    )

# =========================================================
# DONE
# =========================================================

print("\nProcesamiento completado.")
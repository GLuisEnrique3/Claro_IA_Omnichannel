import os
import re
import sys
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from pypdf import PdfReader

# =========================================================
# CONFIG
# =========================================================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_DIR = os.path.join(BASE_DIR, "pdf")

sys.path.insert(0, BASE_DIR)
load_dotenv(os.path.join(BASE_DIR, ".env"))
from config.chroma_client import ChromaClient

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

CHUNK_SIZE = 500
MAX_CHARS_PER_CHUNK = 600

# =========================================================
# DOCUMENT CONFIGURATION
# =========================================================

DOCUMENTS = [
    {
        "filename": "Calendario Pago de Liquidaciones 2026.pdf",
        "collection": "calendario_liquidaciones",
        "chunking": "lines",
        "metadata": {
            "category": "calendario_liquidaciones"
        }
    },
    {
        "filename": "Calendario Pago de Comisiones 2026.pdf",
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
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-03 Instructivo Gestión de contratos con AMBETTER ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "AMBETTER",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-04 Instructivo Gestión de contratos con FLORIDA BLUE  ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "FLORIDA BLUE",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-05 Instructivo Gestión de contratos con MOLINA ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "MOLINA",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-06 Instructivo Gestión de contratos con UNITED HEALTH CARE  ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "UNITED HEALTH CARE",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-07 Instructivo Gestión de contratos con CIGNA  ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "CIGNA",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-08 Instructivo Gestión de contratos con FRIDAY ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "FRIDAY",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-09 Instructivo Gestión de contratos con AVMED ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "AVMED",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-10 Instructivo Gestión de contratos con BlueCross BlueShield Texas ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "BlueCross BlueShield Texas",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-11 Instructivo Gestión de contratos con BlueCross BlueShield NC, SC ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "BlueCross BlueShield NC SC",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-13 Instructivo Gestión de contratos con Anthem BlueCross BlueShield CA, IN, MO, OH ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "Anthem BlueCross BlueShield",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-15 Instructivo Gestión de contratos con Amerihealth Caritas ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "Amerihealth Caritas",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-16 Instructivo Gestión de contratos con Caresource ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "Caresource",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-17 Instructivo Gestión de contratos con Ascension ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "Ascension",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },

    {
        "filename": "CM-IN-18 Contract Management with ALLIANT ACA.pdf",
        "collection": "instructivos_contratos_aca",
        "chunking": "sliding",
        "metadata": {
            "carrier": "ALLIANT",
            "line_of_business":"ACA",
            "category": "instructivos_aca"
        }
    },
    {
        "filename": "CM-IN-26 Instructivo Gestión de contratos con AVMED MEDICARE.pdf",
        "collection": "instructivos_contratos_medicare",
        "chunking": "sliding",
        "metadata": {
            "carrier": "AVMED",
            "line_of_business":"MEDICARE",
            "category": "instructivos_medicare"
        }
    },
    {
        "filename": "CM-IN-27 Instructivo Gestión de Contratos con UNITED HEALTH CARE MEDICARE.pdf",
        "collection": "instructivos_contratos_medicare",
        "chunking": "sliding",
        "metadata": {
            "carrier": "UNITED HEALTH CARE",
            "line_of_business":"MEDICARE",
            "category": "instructivos_medicare"
        }
    },
    {
        "filename": "CM-IN-28 Instructivo Gestión de contratos con AETNA MEDICARE.pdf",
        "collection": "instructivos_contratos_medicare",
        "chunking": "sliding",
        "metadata": {
            "carrier": "AETNA",
            "line_of_business":"MEDICARE",
            "category": "instructivos_medicare"
        }
    },

    {
        "filename": "CM-IN-29 Instructivo de Gestión de Contratos con HUMANA MEDICARE.pdf",
        "collection": "instructivos_contratos_medicare",
        "chunking": "sliding",
        "metadata": {
            "carrier": "HUMANA",
            "line_of_business":"MEDICARE",
            "category": "instructivos_medicare"
        }
    },
        {
        "filename": "CM-IN-30 Instructivo de Gestión de Contratos con SIMPLY MEDICARE.pdf",
        "collection": "instructivos_contratos_medicare",
        "chunking": "sliding",
        "metadata": {
            "carrier": "SIMPLY",
            "line_of_business":"MEDICARE",
            "category": "instructivos_medicare"
        }
    },
    {
        "filename": "CM-IN-31 Instructivo Gestión de contratos con OSCAR MEDICARE.pdf",
        "collection": "instructivos_contratos_medicare",
        "chunking": "sliding",
        "metadata": {
            "carrier": "OSCAR",
            "line_of_business":"MEDICARE",
            "category": "instructivos_medicare"
        }
    },
    {
        "filename": "CM-IN-34 Instructivo Gestión de contratos con WELLCARE MEDICARE.pdf",
        "collection": "instructivos_contratos_medicare",
        "chunking": "sliding",
        "metadata": {
            "carrier": "WELLCARE",
            "line_of_business":"MEDICARE",
            "category": "instructivos_medicare"
        }
    },
    {
        "filename": "CM-IN-21 Instructivo Gestión de contratos con National Life LIFE.pdf",
        "collection": "instructivos_contratos_life",
        "chunking": "sliding",
        "metadata": {
            "carrier": "NATIONAL LIFE",
            "line_of_business":"LIFE",
            "category": "instructivos_life"
        }
    },
    {
        "filename": "CM-IN-22 Instructivo Gestión de contratos con FORESTERS LIFE.pdf",
        "collection": "instructivos_contratos_life",
        "chunking": "sliding",
        "metadata": {
            "carrier": "FORESTERS",
            "line_of_business":"LIFE",
            "category": "instructivos_life"
        }
    },
    {
        "filename": "CM-IN-24 Instructivo Gestión de contratos con Ameritas LIFE.pdf",
        "collection": "instructivos_contratos_life",
        "chunking": "sliding",
        "metadata": {
            "carrier": "AMERITAS",
            "line_of_business":"LIFE",
            "category": "instructivos_life"
        }
    },
    {
        "filename": "CM-IN-25 Instructivo Gestión de contratos con ATHENE LIFE.pdf",
        "collection": "instructivos_contratos_life",
        "chunking": "sliding",
        "metadata": {
            "carrier": "ATHENE",
            "line_of_business":"LIFE",
            "category": "instructivos_life"
        }
    },
    {
        "filename": "CM-IN-35 Instructivo Gestión de contratos con AMERITAS (SUPPLEMENTARY).pdf",
        "collection": "instructivos_contratos_supplementary",
        "chunking": "sliding",
        "metadata": {
            "carrier": "AMERITAS",
            "line_of_business":"SUPPLEMENTARY",
            "category": "instructivos_supplementary"
        }
    },
    {
        "filename": "CM-IN-37 Instructivo Gestión de contratos con United Health One SUPPLEMENTARY.pdf",
        "collection": "instructivos_contratos_supplementary",
        "chunking": "sliding",
        "metadata": {
            "carrier": "UNITED HEALTH ONE",
            "line_of_business":"SUPPLEMENTARY",
            "category": "instructivos_supplementary"
        }
    },
    {
        "filename": "CM-IN-38 Instructivo Gestión de contratos con CIGNA (SUPPLEMENTARY).pdf",
        "collection": "instructivos_contratos_supplementary",
        "chunking": "sliding",
        "metadata": {
            "carrier": "CIGNA",
            "line_of_business":"SUPPLEMENTARY",
            "category": "instructivos_supplementary"
        }
    },
    {
        "filename": "Envíos de Plazos Estándar de Contratos.pdf",
        "collection": "documentos_normativos",
        "chunking": "sliding",
        "metadata": {
            "category": "flujos"
        }
    },
    {
        "filename": "Horario_Roles.pdf",
        "collection": "documentos_normativos",
        "chunking": "sliding",
        "metadata": {
            "category": "horarios"
        }
    },
     {
        "filename": "ARC_OFF_EXCHANGES_FAQ.pdf",
        "collection": "documentos_normativos",
        "chunking": "sliding",
        "metadata": {
            "category": "faq"
        }
    },
    {
        "filename": "CLARO_INSURANCE_FAQ.pdf",
        "collection": "documentos_normativos",
        "chunking": "sliding",
        "metadata": {
            "category": "claro insurance faq"
        }
    }
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

def sentence_chunks(text: str) -> list[str]:
    sentences = re.split(r'(?<=[.!?:])\s+', text.strip())
    sentences = [s.strip() for s in sentences if s.strip()]

    chunks, current, current_len = [], [], 0

    for sentence in sentences:
        if current_len + len(sentence) > CHUNK_SIZE and current:
            chunks.append(' '.join(current))
            current = [current[-1]]          # overlap: última oración al chunk siguiente
            current_len = len(current[0])
        current.append(sentence)
        current_len += len(sentence) + 1

    if current:
        chunks.append(' '.join(current))

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
        return sentence_chunks(text)

    elif strategy == "lines":
        return line_chunks(text)

    else:
        raise ValueError(f"Estrategia desconocida: {strategy}")

# =========================================================
# CHROMA
# =========================================================

chroma_client = ChromaClient(path=os.getenv("CHROMA_PATH", "./chroma_db"))

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

    collection = chroma_client.get_or_create_collection(collection_name)
    print(f"Colección '{collection_name}' lista ({collection.count()} chunks).")

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
    existing_ids = collection.get(where={"filename": filename})["ids"]
    if existing_ids:
        collection.delete(ids=existing_ids)
        print(f"🗑 Eliminados {len(existing_ids)} chunks viejos")

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


import pickle
from pathlib import Path

# =========================================================
# PATHS
# =========================================================

ROOT = Path(__file__).parent.parent

EMBEDDINGS_PATH = ROOT / "data" / "use_cases_embeddings.pkl"

# =========================================================
# LOAD FILE
# =========================================================

if not EMBEDDINGS_PATH.exists():

    print(f"❌ Archivo no encontrado:\n{EMBEDDINGS_PATH}")
    exit(1)

with open(EMBEDDINGS_PATH, "rb") as f:

    data = pickle.load(f)

# =========================================================
# SUMMARY
# =========================================================

print("\n" + "=" * 80)
print("USE CASE EMBEDDINGS")
print("=" * 80)

print(f"\n📦 Total use cases: {len(data)}\n")

# =========================================================
# SHOW CONTENT
# =========================================================

for i, (uc_id, item) in enumerate(data.items(), start=1):

    nombre = item.get("nombre", "N/A")
    tipo = item.get("tipo", "N/A")
    catalogo = item.get("catalogo", "N/A")

    embedding = item.get("embedding", [])

    pregunta = item.get("pregunta", {})

    semantic_examples = pregunta.get("semantic_examples", [])

    print("-" * 80)

    print(f"[{i:03d}] USE CASE ID : {uc_id}")
    print(f"      Nombre      : {nombre}")
    print(f"      Tipo        : {tipo}")
    print(f"      Catálogo    : {catalogo}")

    print(f"\n      Embedding Size : {len(embedding)}")

    preview = embedding[:10]

    print(f"      Vector Preview:")
    print(f"      {preview}")

    print(f"\n      Semantic Examples ({len(semantic_examples)}):")

    if semantic_examples:

        for ex_idx, example in enumerate(semantic_examples, start=1):

            print(f"         {ex_idx}. {example}")

    else:

        print("         Ninguno")

    print()

print("=" * 80)
print("✅ Revisión completada.")
print("=" * 80)
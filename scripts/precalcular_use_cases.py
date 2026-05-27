"""
Genera data/use_cases_embeddings.pkl a partir de data/use_cases.json.
Para cada caso de uso calcula un embedding centroide promediando el vector
del campo "texto" más los vectores de cada entrada en "semantic_examples".
Ejecutar una vez (y repetir cada vez que cambie use_cases.json):
    python scripts/precalcular_use_cases.py
"""
import json
import pickle
import sys
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).parent.parent
USE_CASES_PATH = ROOT / "data" / "use_cases.json"
OUTPUT_PATH    = ROOT / "data" / "use_cases_embeddings.pkl"

def main():
    with open(USE_CASES_PATH, encoding="utf-8") as f:
        use_cases_data = json.load(f)

    print("Cargando modelo de embeddings...")
    model = SentenceTransformer("all-MiniLM-L6-v2")

    result: dict = {}

    for catalogo_key, catalogo in use_cases_data["options"].items():
        for pregunta in catalogo["preguntas"]:
            uc_id = f"{catalogo_key}{pregunta['id']}"
            nombre = pregunta["texto"]
            examples = pregunta.get("semantic_examples", [])

            # Casos sin semantic_examples se excluyen del PKL de búsqueda semántica.
            # Siguen siendo accesibles vía "invoca" en flujos múltiples.
            if not examples:
                print(f"  {uc_id}: '{nombre}' — omitido (sin semantic_examples, solo invocable)")
                continue

            texts = [nombre] + examples
            embeddings = model.encode(texts, convert_to_numpy=True)
            centroid = np.mean(embeddings, axis=0)

            result[uc_id] = {
                "embedding": centroid.tolist(),
                "embeddings_list": embeddings.tolist(),
                "nombre": nombre,
                "tipo": pregunta.get("tipo", "sql"),
                "catalogo": catalogo_key,
                "pregunta": pregunta,
            }
            print(f"  {uc_id}: '{nombre}' — {len(texts)} texto(s) embebidos")

    with open(OUTPUT_PATH, "wb") as f:
        pickle.dump(result, f)

    print(f"\nGuardado: {OUTPUT_PATH}  ({len(result)} casos de uso)")

if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()

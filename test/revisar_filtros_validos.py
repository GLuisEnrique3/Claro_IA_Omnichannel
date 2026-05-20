import pickle
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import FILTROS_VALIDOS_PATH

def revisar_filtros():
    path = FILTROS_VALIDOS_PATH
    
    if not os.path.exists(path):
        print(f"❌ No se encontró: {path}")
        return
    
    with open(path, "rb") as f:
        filtros = pickle.load(f)
    
    print("=" * 50)
    print("Filtros válidos cargados:")
    print("=" * 50)
    
    for clave, valores in filtros.items():
        print(f"\n🔹 {clave}: {len(valores)} valores")
        print(f"   Ejemplos: {valores[:5]}")
    
    print("\nCarga exitosa!")

if __name__ == "__main__":
    revisar_filtros()
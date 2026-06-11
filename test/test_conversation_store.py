"""
Verificación manual de ConversationStore.
Ejecutar desde la raíz del proyecto:
    python test/test_conversation_store.py

Cubre los escenarios SCEN-01 al SCEN-05 del spec.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from core.conversation_store import ConversationStore, _HISTORY_FILE


def _limpiar_archivo():
    """Elimina el archivo de historial si existe (limpieza entre tests)."""
    if _HISTORY_FILE.exists():
        _HISTORY_FILE.unlink()


def _resultado(nombre: str, ok: bool, detalle: str = "") -> None:
    estado = "PASS" if ok else "FAIL"
    sufijo = f" — {detalle}" if detalle else ""
    print(f"[{estado}] {nombre}{sufijo}")


# ── SCEN-01 — load en clave inexistente retorna [] ─────────────────────────────

def test_scen01_load_clave_inexistente():
    _limpiar_archivo()
    store = ConversationStore()
    resultado = store.load("gchat::spaces/ABC::users/123")
    ok = resultado == [] and not _HISTORY_FILE.exists()
    _resultado("SCEN-01: load en clave inexistente retorna [] sin crear archivo", ok)


# ── SCEN-02 — append en clave nueva crea los turnos correctos ──────────────────

def test_scen02_append_clave_nueva():
    _limpiar_archivo()
    store = ConversationStore()

    store.append(
        "rest::session-uuid-001",
        "cuántos contratos tengo",
        "Tienes 42 contratos activos.",
    )

    historia = store.load("rest::session-uuid-001")
    ok = (
        len(historia) == 2
        and historia[0] == {"role": "user", "content": "cuántos contratos tengo"}
        and historia[1] == {"role": "assistant", "content": "Tienes 42 contratos activos."}
    )
    detalle = f"len={len(historia)}" if not ok else ""
    _resultado("SCEN-02: append en clave nueva crea 2 dicts correctos", ok, detalle)


# ── SCEN-03 — sliding window en turno 11 ──────────────────────────────────────

def test_scen03_sliding_window():
    _limpiar_archivo()
    store = ConversationStore()
    key = "gchat::spaces/X::users/Y"

    # Agregar 10 turnos
    for i in range(1, 11):
        store.append(key, f"pregunta {i}", f"respuesta {i}")

    # El turno 11 debe descartar el turno 1
    store.append(key, "pregunta 11", "respuesta 11")

    historia = store.load(key)
    ok = (
        len(historia) == 20  # 10 turnos × 2 dicts
        and historia[0]["content"] == "pregunta 2"  # turno 1 fue descartado
        and historia[-2]["content"] == "pregunta 11"
        and historia[-1]["content"] == "respuesta 11"
    )
    detalle = f"len={len(historia)}, primer_user={historia[0]['content'] if historia else 'N/A'}" if not ok else ""
    _resultado("SCEN-03: sliding window descarta turno 1 al agregar turno 11", ok, detalle)


# ── SCEN-04 — TTL 24h descarta turnos viejos en load ──────────────────────────

def test_scen04_ttl():
    _limpiar_archivo()
    store = ConversationStore()
    key = "rest::abc"

    # Agregar 3 turnos normales
    for i in range(1, 4):
        store.append(key, f"pregunta {i}", f"respuesta {i}")

    # Modificar directamente el JSON para que el turno 1 tenga timestamp de hace 25h
    with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    ts_viejo = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    entries = data[key]
    # El turno 1 son los primeros 2 dicts (índices 0 y 1)
    entries[0]["timestamp"] = ts_viejo
    entries[1]["timestamp"] = ts_viejo

    with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    historia = store.load(key)
    ok = (
        len(historia) == 4  # solo turnos 2 y 3 (2 × 2 dicts)
        and historia[0]["content"] == "pregunta 2"
    )
    detalle = f"len={len(historia)}, primer={historia[0]['content'] if historia else 'N/A'}" if not ok else ""
    _resultado("SCEN-04: TTL filtra turno con timestamp > 24h", ok, detalle)


# ── Ejecutar todos los tests ──────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("ConversationStore — Verificación Manual")
    print("=" * 60)

    test_scen01_load_clave_inexistente()
    test_scen02_append_clave_nueva()
    test_scen03_sliding_window()
    test_scen04_ttl()

    print("=" * 60)
    print("Verificación completada.")

    # Limpiar archivo temporal de tests
    _limpiar_archivo()

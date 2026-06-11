"""
Tests de core/conversation_store.py — historial conversacional sliding window.

AISLAMIENTO: cada test redirige _HISTORY_FILE a un archivo temporal vía
fixture autouse. NUNCA tocar data/conversation_history.json real — esta
máquina sirve tráfico real de Google Chat y una versión anterior de este
test borraba el historial de usuarios reales en cada corrida.

Ejecutar:
    pytest test/test_conversation_store.py -v
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.conversation_store as cs_mod
from core.conversation_store import ConversationStore


@pytest.fixture(autouse=True)
def archivo_aislado(tmp_path, monkeypatch):
    """Redirige el archivo de historial a tmp — jamás el real."""
    ruta = tmp_path / "conversation_history.json"
    monkeypatch.setattr(cs_mod, "_HISTORY_FILE", ruta)
    yield ruta


@pytest.fixture
def store():
    return ConversationStore()


class TestConversationStore:
    def test_load_clave_inexistente_retorna_vacio(self, store, archivo_aislado):
        assert store.load("gchat::spaces/ABC::users/123") == []
        assert not archivo_aislado.exists()

    def test_append_clave_nueva(self, store):
        key = "rest::session-uuid-001"
        store.append(key, "hola, qué cubre mi póliza?", "Tu póliza cubre...")

        historia = store.load(key)
        assert len(historia) == 2
        assert historia[0] == {"role": "user", "content": "hola, qué cubre mi póliza?"}
        assert historia[1]["role"] == "assistant"

    def test_contenido_truncado_a_200_chars(self, store):
        key = "gchat::spaces/X::users/Y"
        store.append(key, "x" * 500, "y" * 500)
        historia = store.load(key)
        assert len(historia[0]["content"]) == 200
        assert len(historia[1]["content"]) == 200

    def test_sliding_window_max_10_turnos(self, store):
        key = "gchat::spaces/X::users/Y"
        for i in range(13):
            store.append(key, f"pregunta {i}", f"respuesta {i}")

        historia = store.load(key)
        assert len(historia) == 20  # 10 turnos × 2 mensajes
        assert historia[0]["content"] == "pregunta 3"   # los 3 primeros se descartaron
        assert historia[-1]["content"] == "respuesta 12"

    def test_ttl_filtra_turnos_de_mas_de_24h(self, store, archivo_aislado):
        key = "rest::abc"
        for i in range(1, 4):
            store.append(key, f"pregunta {i}", f"respuesta {i}")

        # Envejecer el turno 1 directamente en el JSON (timestamp de hace 25h)
        with open(archivo_aislado, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts_viejo = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        data[key][0]["timestamp"] = ts_viejo
        data[key][1]["timestamp"] = ts_viejo
        with open(archivo_aislado, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        historia = store.load(key)
        assert len(historia) == 4  # solo turnos 2 y 3
        assert historia[0]["content"] == "pregunta 2"

    def test_claves_independientes(self, store):
        store.append("gchat::spaces/A::users/1", "pregunta A", "respuesta A")
        store.append("gchat::spaces/B::users/2", "pregunta B", "respuesta B")
        assert store.load("gchat::spaces/A::users/1")[0]["content"] == "pregunta A"
        assert store.load("gchat::spaces/B::users/2")[0]["content"] == "pregunta B"

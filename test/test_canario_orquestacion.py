"""
Test CANARIO — detecta cambios en la orquestación del CLI que Google Chat
replica a mano.

core/guided_flow.py re-implementa la orquestación conversacional de main.py
(la parte basada en input()/print() que no puede importarse). Si alguien
modifica esas funciones en main.py, la réplica NO se actualiza sola: este
test falla a propósito para que el cambio no pase silencioso.

QUÉ HACER SI ESTE TEST FALLA:

1. Revisa qué cambió en la función señalada de main.py:
       git diff main.py

2. Decide si el cambio afecta la conversación (textos, orden de prompts,
   keywords, nuevos pasos):
   - SÍ  → refleja el cambio en el archivo espejo indicado y en sus tests.
   - NO  (refactor interno, comentarios) → no hay nada que reflejar.

3. Actualiza el hash en HASHES_ESPERADOS con el valor que imprime el test
   (o ejecuta: python test/test_canario_orquestacion.py).

NO actualices el hash sin hacer el paso 2 — ese es exactamente el drift
que este test existe para evitar.
"""
import hashlib
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main as cli

# función de main.py → (hash de su código fuente, archivo espejo que la replica/parsea)
HASHES_ESPERADOS = {
    "_input":               ("7a28cb180dfedc18", "core/guided_flow.py (_procesar: keywords globales, saludo, escalar)"),
    "_input_sn":            ("e871b74914d3ccdd", "core/guided_flow.py (validación S/N en CONFIRM/RETRY/OTRA)"),
    "identificar_usuario":  ("b79fa2109c59cdc2", "core/guided_flow.py (_handle_ident_tipo, _handle_ident_valor)"),
    "ciclo_consultas":      ("da2fb08847d2f410", "core/guided_flow.py (_pipeline_consulta, _handle_confirm, _ejecutar_pendiente)"),
    "main":                 ("bb65e7d5aa2af899", "core/guided_flow.py (_completar_identificacion, iniciar_sesion)"),
    "ejecutar_rag":         ("8ac2e6032cbfcfa0", "core/guided_flow.py (_parse_rag_salida — formato de prints parseado)"),
    "ejecutar_multiple":    ("7a0db8adb93b656c", "core/guided_flow.py (_parse_resultado_multiple — formato de prints parseado)"),
    "_mostrar_instrucciones": ("f31a42b2ff0895e5", "adapters/chat_render.py (_transformar_instrucciones — formato de pantalla parseado)"),
}


def _hash_fuente(func) -> str:
    return hashlib.sha256(inspect.getsource(func).encode()).hexdigest()[:16]


@pytest.mark.parametrize("nombre", HASHES_ESPERADOS)
def test_orquestacion_sin_cambios(nombre):
    esperado, espejo = HASHES_ESPERADOS[nombre]
    actual = _hash_fuente(getattr(cli, nombre))
    assert actual == esperado, (
        f"\n\n⚠  main.py::{nombre}() CAMBIÓ y Google Chat replica/parsea esa función a mano.\n\n"
        f"   Espejo a revisar : {espejo}\n"
        f"   Hash esperado    : {esperado}\n"
        f"   Hash actual      : {actual}\n\n"
        f"   1. git diff main.py — revisa si el cambio afecta la conversación.\n"
        f"   2. Si afecta: actualiza el espejo y sus tests.\n"
        f"   3. Actualiza el hash en test/test_canario_orquestacion.py.\n"
        f"   NO actualices el hash sin el paso 2 (ver docstring del módulo).\n"
    )


if __name__ == "__main__":
    # Modo utilitario: imprime los hashes actuales para actualizar la tabla
    print("Hashes actuales de main.py:")
    for nombre in HASHES_ESPERADOS:
        print(f'    "{nombre}": "{_hash_fuente(getattr(cli, nombre))}",')

"""
Tests de paridad de core/guided_flow.py contra el flujo del CLI (main.py).

Las funciones pesadas (LLM, BigQuery, embeddings, Chroma) se mockean; lo que
se verifica es la ORQUESTACIÓN del motor de dos agentes: estados, textos y
transiciones (Agente 1 = seleccionar_caso_de_uso_llm, Agente 2 =
_interpretar_confirmacion).

Ejecutar:
    pytest test/test_guided_flow.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main as cli
import config.logger as logger_mod
from core import guided_flow, session_store as session_store_mod


SEP = "━" * 55


@pytest.fixture(autouse=True)
def entorno_aislado(tmp_path, monkeypatch):
    """Sesiones en archivo temporal, sin flush a BigQuery, loggers limpios."""
    monkeypatch.setattr(session_store_mod, "_SESSIONS_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(logger_mod, "_flush_bq", lambda q: None)
    monkeypatch.setattr(logger_mod, "LOG_DIR", tmp_path)
    guided_flow._loggers.clear()
    yield


@pytest.fixture
def sesion_lista(monkeypatch):
    """Sesión ya identificada como Representante de Agencia, lista para consultar."""
    key = "gchat::spaces/TEST::users/1"
    monkeypatch.setitem(cli.FILTROS_VALIDOS, "agency", ["Comfort Insurance"])
    monkeypatch.setattr(cli, "_buscar_semantico", lambda *a, **k: ("Comfort Insurance", 0.90))
    guided_flow.iniciar_sesion(key)
    guided_flow.step(key, "1")
    resultado = guided_flow.step(key, "comfort")
    assert "✅ Bienvenido" in resultado.texto
    return key


def _mock_agente1(monkeypatch, pregunta: dict | None, *, mensaje: str = "Entiendo que quieres X. ¿Es correcto?",
                  es_meta: bool = False, catalogo: str = "A", entidades: dict | None = None):
    """Mockea el Agente 1 (clasificación). use_case_entry=None si pregunta es None."""
    monkeypatch.setattr(cli, "reescribir_consulta", lambda hist, q: q)
    entry = None if pregunta is None else {"nombre": pregunta["texto"], "pregunta": pregunta, "catalogo": catalogo}
    monkeypatch.setattr(
        cli, "seleccionar_caso_de_uso_llm",
        lambda query, cats: (entry, mensaje, es_meta),
    )
    monkeypatch.setattr(cli, "extraer_entidades", lambda *a, **k: entidades or {})


def _mock_agente2(monkeypatch, *, confirmado: bool, query_ajustada: str | None = None):
    """Mockea el Agente 2 (interpretación de la confirmación libre)."""
    monkeypatch.setattr(
        cli, "_interpretar_confirmacion",
        lambda mensaje, respuesta: {"confirmado": confirmado, "query_ajustada": query_ajustada},
    )


PREGUNTA_SQL = {
    "id": "A1",
    "texto": "Contratos Activos",
    "tipo": "sql",
    "parametros": ["o.Carrier"],
}


# ── Inicio e identificación ────────────────────────────────────────────────────

class TestIdentificacion:
    def test_primer_mensaje_muestra_header_y_menu(self):
        resultado = guided_flow.step("gchat::s::u", "hola cualquier cosa")
        assert "SISTEMA DE CONSULTAS IA - BIENVENIDO" in resultado.texto
        assert "🔐 IDENTIFICACIÓN DE USUARIO" in resultado.texto
        assert "Seleccione su tipo de usuario:" in resultado.texto
        assert "  1. Representante de Agencia" in resultado.texto
        # Rol Management (tipo 3) eliminado: solo dos tipos de usuario.
        assert resultado.botones == [
            ("1. Representante de Agencia", "1"),
            ("2. Agente NPN", "2"),
        ]

    def test_opcion_invalida_repite_menu(self):
        key = "gchat::s::u"
        guided_flow.iniciar_sesion(key)
        resultado = guided_flow.step(key, "9")
        assert resultado.lineas[0] == "⚠  Opción no válida. Intente nuevamente."
        assert "Seleccione su tipo de usuario:" in resultado.texto

    def test_tipo_1_pide_agencia_y_valida_semanticamente(self, monkeypatch):
        key = "gchat::s::u"
        monkeypatch.setitem(cli.FILTROS_VALIDOS, "agency", ["Comfort Insurance"])
        monkeypatch.setattr(cli, "_buscar_semantico", lambda *a, **k: ("Comfort Insurance", 0.87))
        guided_flow.iniciar_sesion(key)

        resultado = guided_flow.step(key, "1")
        assert resultado.lineas == ["Ingrese el nombre de su agencia: "]

        resultado = guided_flow.step(key, "comfort")
        assert (
            '✅ Bienvenido, el sistema ha reconocido tu ingreso "Comfort Insurance", '
            "nivel de coincidencia 0.87"
        ) in resultado.texto

    def test_tipo_1_sin_coincidencia_reintenta(self, monkeypatch):
        key = "gchat::s::u"
        monkeypatch.setitem(cli.FILTROS_VALIDOS, "agency", ["Comfort Insurance"])
        monkeypatch.setattr(cli, "_buscar_semantico", lambda *a, **k: (None, 0.41))
        guided_flow.iniciar_sesion(key)
        guided_flow.step(key, "1")

        resultado = guided_flow.step(key, "xyz")
        assert "⚠  Sin coincidencia para 'xyz' (mejor similitud: 0.41, umbral: 0.65)." in resultado.texto
        assert resultado.lineas[-1] == "Ingrese el nombre de su agencia: "


# ── Pipeline de consulta (Agente 1) ─────────────────────────────────────────────

class TestPipelineConsulta:
    def test_caso_detectado_pide_confirmacion(self, sesion_lista, monkeypatch):
        _mock_agente1(monkeypatch, PREGUNTA_SQL, mensaje="Entiendo que quieres consultar Contratos Activos. ¿Es correcto?")
        resultado = guided_flow.step(sesion_lista, "cuantos contratos activos")

        assert "  Analizando su consulta..." in resultado.lineas
        # El mensaje de confirmación lo redacta el Agente 1 (lenguaje natural).
        assert any("¿Es correcto?" in l for l in resultado.lineas)
        assert resultado.botones == guided_flow._BOTON_CONFIRMAR
        session = guided_flow.session_store.load(sesion_lista)
        assert session["state"] == "CONFIRM"

    def test_pregunta_meta_responde_directo_sin_confirmar(self, sesion_lista, monkeypatch):
        _mock_agente1(monkeypatch, None, mensaje="Soy el asistente de Claro Insurance.", es_meta=True)
        resultado = guided_flow.step(sesion_lista, "quién eres")

        assert "Soy el asistente de Claro Insurance." in resultado.texto
        assert "¿Qué desea consultar hoy?" in resultado.texto
        # No hay nada que confirmar: vuelve directo a QUERY.
        session = guided_flow.session_store.load(sesion_lista)
        assert session["state"] == "QUERY"
        assert resultado.botones is None

    def test_caso_no_detectado_queda_en_confirm_para_afinar(self, sesion_lista, monkeypatch):
        # Agente 1 sin caso (use_case None): muestra el mensaje y espera respuesta
        # libre que el Agente 2 usará para reclasificar (no hay rama RETRY).
        _mock_agente1(monkeypatch, None, mensaje="No identifiqué un flujo. ¿Puedes dar más detalle?")
        resultado = guided_flow.step(sesion_lista, "asdf qwerty")

        assert "No identifiqué un flujo" in resultado.texto
        session = guided_flow.session_store.load(sesion_lista)
        assert session["state"] == "CONFIRM"
        # Sin caso ejecutable → sin botón de confirmación rápida.
        assert resultado.botones is None

    def test_filtros_requeridos_faltantes_pide_reformular(self, sesion_lista, monkeypatch):
        pregunta = dict(PREGUNTA_SQL, filtros_requeridos=["p.Policy_Number__c"], parametros=["p.Policy_Number__c"])
        _mock_agente1(monkeypatch, pregunta)
        _mock_agente2(monkeypatch, confirmado=True)
        guided_flow.step(sesion_lista, "detalle de comisiones")
        # Confirma → al ejecutar detecta que faltan filtros requeridos
        resultado = guided_flow.step(sesion_lista, "sí")

        assert "  Esta consulta requiere identificar los siguientes filtros/entidades (Número de Póliza)," in resultado.lineas
        assert resultado.lineas[-1] == "  Su consulta: "
        session = guided_flow.session_store.load(sesion_lista)
        assert session["state"] == "REFORMULAR"


# ── Confirmación y ejecución (Agente 2) ─────────────────────────────────────────

class TestConfirmacion:
    def _hasta_confirmacion(self, key, monkeypatch):
        _mock_agente1(monkeypatch, PREGUNTA_SQL)
        resultado = guided_flow.step(key, "cuantos contratos activos")
        assert guided_flow.session_store.load(key)["state"] == "CONFIRM"

    def test_confirmar_ejecuta_y_muestra_resultado(self, sesion_lista, monkeypatch):
        self._hasta_confirmacion(sesion_lista, monkeypatch)
        _mock_agente2(monkeypatch, confirmado=True)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "Tienes 42 contratos activos.")

        resultado = guided_flow.step(sesion_lista, "sí, correcto")
        assert "  Procesando su consulta, por favor espere..." in resultado.lineas
        assert "RESULTADO:" in resultado.lineas
        assert "Tienes 42 contratos activos." in resultado.lineas
        assert resultado.lineas[-1] == guided_flow._PROMPT_OTRA

    def test_boton_confirmar_ejecuta(self, sesion_lista, monkeypatch):
        self._hasta_confirmacion(sesion_lista, monkeypatch)
        _mock_agente2(monkeypatch, confirmado=True)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "ok")
        # El botón rápido envía el sentinel afirmativo.
        resultado = guided_flow.step(sesion_lista, guided_flow.CONFIRMAR_SENTINEL)
        assert "ok" in resultado.texto
        assert guided_flow.session_store.load(sesion_lista)["state"] == "OTRA"

    def test_prints_internos_se_capturan(self, sesion_lista, monkeypatch):
        self._hasta_confirmacion(sesion_lista, monkeypatch)
        _mock_agente2(monkeypatch, confirmado=True)

        def ejecutar_con_prints(*a, **k):
            print()
            print("🤖 Consultando el sistema..")
            return "Respuesta final."

        monkeypatch.setattr(cli, "ejecutar_consulta", ejecutar_con_prints)
        resultado = guided_flow.step(sesion_lista, "sí")
        assert "🤖 Consultando el sistema.." in resultado.texto
        assert "Respuesta final." in resultado.texto

    def test_no_confirmado_reclasifica_con_query_ajustada(self, sesion_lista, monkeypatch):
        self._hasta_confirmacion(sesion_lista, monkeypatch)
        # El Agente 2 no confirma pero extrae una corrección → Agente 1 reclasifica.
        _mock_agente2(monkeypatch, confirmado=False, query_ajustada="contratos inactivos")
        recibido = {}

        def agente1(query, cats):
            recibido["query"] = query
            return ({"nombre": "Contratos Inactivos", "pregunta": PREGUNTA_SQL, "catalogo": "A"},
                    "Entiendo: Contratos Inactivos. ¿Es correcto?", False)

        monkeypatch.setattr(cli, "seleccionar_caso_de_uso_llm", agente1)
        resultado = guided_flow.step(sesion_lista, "no, los inactivos")

        assert recibido["query"] == "contratos inactivos"
        assert "Contratos Inactivos" in resultado.texto
        # Sigue en CONFIRM (loop de afinamiento, sin límite de reintentos).
        assert guided_flow.session_store.load(sesion_lista)["state"] == "CONFIRM"

    def test_respuesta_vacia_repite_propuesta(self, sesion_lista, monkeypatch):
        self._hasta_confirmacion(sesion_lista, monkeypatch)
        resultado = guided_flow.step(sesion_lista, "")
        assert any("¿Es correcto?" in l or "Entiendo" in l for l in resultado.lineas)
        assert guided_flow.session_store.load(sesion_lista)["state"] == "CONFIRM"

    def test_otra_consulta_texto_libre_encadena_como_nueva(self, sesion_lista, monkeypatch):
        self._hasta_confirmacion(sesion_lista, monkeypatch)
        _mock_agente2(monkeypatch, confirmado=True)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "ok")
        guided_flow.step(sesion_lista, "sí")  # ejecuta → OTRA
        assert guided_flow.session_store.load(sesion_lista)["state"] == "OTRA"

        # Escribir otra consulta desde OTRA encadena directo como nueva consulta.
        guided_flow.step(sesion_lista, "y los inactivos")
        assert guided_flow.session_store.load(sesion_lista)["state"] == "CONFIRM"

    def test_otra_consulta_no_despide(self, sesion_lista, monkeypatch):
        self._hasta_confirmacion(sesion_lista, monkeypatch)
        _mock_agente2(monkeypatch, confirmado=True)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "ok")
        guided_flow.step(sesion_lista, "sí")  # ejecuta → OTRA

        # Una respuesta en _PALABRAS_NO termina la sesión.
        resultado = guided_flow.step(sesion_lista, "no, gracias")
        assert resultado.terminado
        assert "¡Hasta pronto!" in resultado.texto


# ── Keywords globales ──────────────────────────────────────────────────────────

class TestKeywordsGlobales:
    def test_salir_finaliza_sesion(self, sesion_lista):
        resultado = guided_flow.step(sesion_lista, "salir")
        assert resultado.terminado
        assert resultado.lineas == ["", "Sesión finalizada. ¡Hasta pronto!"]
        resultado = guided_flow.step(sesion_lista, "lo que sea")
        assert "🔐 IDENTIFICACIÓN DE USUARIO" in resultado.texto

    def test_nueva_sesion_reinicia_identificacion(self, sesion_lista):
        resultado = guided_flow.step(sesion_lista, "nueva sesión")
        assert "  Volviendo a la identificación de usuario..." in resultado.lineas
        assert "Seleccione su tipo de usuario:" in resultado.texto

    def test_instrucciones_muestra_pantalla_y_repregunta(self, sesion_lista):
        resultado = guided_flow.step(sesion_lista, "instrucciones")
        assert "INSTRUCCIONES DE USO" in resultado.texto
        assert resultado.lineas[-1] == "  Su consulta: "

    def test_saludo_responde_y_repregunta(self, sesion_lista):
        resultado = guided_flow.step(sesion_lista, "hola")
        assert "  ¡Hola! Soy tu asistente de Claro Insurance." in resultado.lineas
        assert resultado.lineas[-1] == "  Su consulta: "

    def test_escalar_en_proceso(self, sesion_lista):
        resultado = guided_flow.step(sesion_lista, "escalar")
        assert "  Funcionalidad todavía en proceso." in resultado.lineas


# ── Historial (sliding window) ─────────────────────────────────────────────────

class TestHistorial:
    def test_historial_guarda_pregunta_y_respuesta(self, sesion_lista, monkeypatch):
        _mock_agente1(monkeypatch, PREGUNTA_SQL)
        _mock_agente2(monkeypatch, confirmado=True)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "respuesta-asistente")

        guided_flow.step(sesion_lista, "contratos activos")
        guided_flow.step(sesion_lista, "sí")

        sesion = session_store_mod.session_store.load(sesion_lista)
        roles = [h["role"] for h in sesion["historial_reciente"]]
        assert roles == ["user", "assistant"]
        assert sesion["historial_reciente"][-1]["content"] == "respuesta-asistente"

    def test_rewriter_recibe_ultimas_dos_entradas(self, sesion_lista, monkeypatch):
        recibido = {}

        def rewriter(historial, query):
            recibido["historial"] = list(historial)
            return query

        _mock_agente1(monkeypatch, PREGUNTA_SQL)
        _mock_agente2(monkeypatch, confirmado=True)
        monkeypatch.setattr(cli, "reescribir_consulta", rewriter)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "ok")

        guided_flow.step(sesion_lista, "contratos activos")
        guided_flow.step(sesion_lista, "sí")
        guided_flow.step(sesion_lista, "y los inactivos")

        # Tras el primer turno hay (user, assistant); el rewriter recibe esas dos.
        assert recibido["historial"] == [
            {"role": "user", "content": "contratos activos"},
            {"role": "assistant", "content": "ok"},
        ]

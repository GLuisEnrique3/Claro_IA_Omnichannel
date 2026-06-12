"""
Tests de paridad de core/guided_flow.py contra el flujo del CLI (main.py).

Las funciones pesadas (LLM, BigQuery, embeddings, Chroma) se mockean; lo que
se verifica es la ORQUESTACIÓN: estados, textos exactos y orden de líneas.

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
def sesion_management(monkeypatch):
    """Sesión ya identificada como Management (tipo 3), lista para consultar."""
    key = "gchat::spaces/TEST::users/1"
    guided_flow.iniciar_sesion(key)
    resultado = guided_flow.step(key, "3")
    assert "✅ Bienvenido, Management" in resultado.texto
    return key


def _mock_pipeline_sql(monkeypatch, pregunta: dict, score: float = 0.91, entidades: dict | None = None):
    monkeypatch.setattr(cli, "reescribir_consulta", lambda hist, q: q)
    monkeypatch.setattr(cli, "transformar_consulta_con_llm", lambda q: "consulta normalizada")
    monkeypatch.setattr(
        cli, "detectar_caso_de_uso",
        lambda norm, cats: ({"nombre": pregunta["texto"], "pregunta": pregunta, "catalogo": "A"}, score),
    )
    monkeypatch.setattr(cli, "extraer_entidades", lambda *a, **k: entidades or {})


PREGUNTA_SQL = {
    "id": "A1",
    "texto": "Contratos Activos",
    "tipo": "sql",
    "parametros": ["o.Carrier"],
    "sql_by_role": {"3": "SELECT 1 {dynamic_filters}"},
}


# ── Inicio e identificación ────────────────────────────────────────────────────

class TestIdentificacion:
    def test_primer_mensaje_muestra_header_y_menu(self):
        resultado = guided_flow.step("gchat::s::u", "hola cualquier cosa")
        assert "SISTEMA DE CONSULTAS IA - BIENVENIDO" in resultado.texto
        assert "🔐 IDENTIFICACIÓN DE USUARIO" in resultado.texto
        assert "Seleccione su tipo de usuario:" in resultado.texto
        assert "  1. Representante de Agencia" in resultado.texto
        assert resultado.botones == [
            ("1. Representante de Agencia", "1"),
            ("2. Agente NPN", "2"),
            ("3. Management", "3"),
        ]

    def test_opcion_invalida_repite_menu(self):
        key = "gchat::s::u"
        guided_flow.iniciar_sesion(key)
        resultado = guided_flow.step(key, "9")
        assert resultado.lineas[0] == "⚠  Opción no válida. Intente nuevamente."
        assert "Seleccione su tipo de usuario:" in resultado.texto

    def test_tipo_3_da_bienvenida_y_menu_de_consulta(self):
        key = "gchat::s::u"
        guided_flow.iniciar_sesion(key)
        resultado = guided_flow.step(key, "3")
        assert resultado.lineas[:2] == ["", "✅ Bienvenido, Management"]
        assert resultado.lineas[2:] == [SEP, "", "¿Qué desea consultar hoy?", "", "  Su consulta: "]

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


# ── Pipeline de consulta ───────────────────────────────────────────────────────

class TestPipelineConsulta:
    def test_flujo_detectado_pide_confirmacion(self, sesion_management, monkeypatch):
        _mock_pipeline_sql(monkeypatch, PREGUNTA_SQL)
        resultado = guided_flow.step(sesion_management, "cuantos contratos activos")

        assert "  Analizando su consulta..." in resultado.lineas
        assert "  #--DEBUG Pregunta Formateada: consulta normalizada" in resultado.lineas
        assert "  Tu pregunta se ha identificado mediante el flujo: Contratos Activos con un nivel de coincidencia del 0.91%." in resultado.lineas
        assert resultado.lineas[-1].startswith("  ¿Desea Confirmar?")
        assert resultado.botones == guided_flow._BOTONES_CONFIRMAR

    def test_entidades_detectadas_van_solo_en_data(self, sesion_management, monkeypatch):
        entidades = {"o.Carrier": ("Humana", 0.93, "Carrier", "AND o.Carrier = 'Humana'")}
        _mock_pipeline_sql(monkeypatch, PREGUNTA_SQL, entidades=entidades)
        resultado = guided_flow.step(sesion_management, "contratos activos humana")

        # El CLI ya no muestra entidades al usuario — viajan solo en data del bloque flujo
        assert "  Con las siguientes entidades:" not in resultado.lineas
        bloque_flujo = next(b for b in resultado.bloques if b.kind == "flujo")
        assert bloque_flujo.data["entidades"] == [
            {"label": "Carrier", "valor": "Humana", "score": 0.93}
        ]

    def _mock_pipeline_multiple(self, monkeypatch, entidades: dict):
        """Caso multiple con filtros_requeridos (réplica del caso B/7)."""
        pregunta = {
            "id": "7",
            "texto": "Porque no me han pagado mis comisiones",
            "tipo": "multiple",
            "invoca": ["1"],
            "filtros_requeridos": ["p.Policy_Number__c"],
        }
        monkeypatch.setitem(cli.USE_CASES["options"], "B", {
            "nombre": "Comisiones",
            "preguntas": [
                {"id": "1", "texto": "Detalle Comisiones", "tipo": "sql",
                 "parametros": ["p.Policy_Number__c"]},
                pregunta,
            ],
        })
        monkeypatch.setattr(cli, "reescribir_consulta", lambda hist, q: q)
        monkeypatch.setattr(cli, "transformar_consulta_con_llm", lambda q: "consulta normalizada")
        monkeypatch.setattr(
            cli, "detectar_caso_de_uso",
            lambda norm, cats: ({"nombre": pregunta["texto"], "pregunta": pregunta, "catalogo": "B"}, 0.95),
        )
        monkeypatch.setattr(cli, "extraer_entidades", lambda *a, **k: entidades)

    def test_multiple_con_filtros_requeridos_y_entidades_confirma(self, sesion_management, monkeypatch):
        # Regresión: la fusión de entidades por sub-caso debe alimentar el
        # pre-chequeo de filtros_requeridos — si se pierde, B/7 nunca ejecuta.
        entidades = {"p.Policy_Number__c": ("12345", 0.9, "Número de Póliza", "AND p.Policy_Number__c = '12345'")}
        self._mock_pipeline_multiple(monkeypatch, entidades)

        resultado = guided_flow.step(sesion_management, "porque no me han pagado la póliza 12345")

        assert resultado.lineas[-1].startswith("  ¿Desea Confirmar?")
        session = guided_flow.session_store.load(sesion_management)
        assert session["state"] == "CONFIRM"
        assert "p.Policy_Number__c" in session["pendiente"]["entidades"]

    def test_multiple_con_filtros_requeridos_sin_entidades_pide_reformular(self, sesion_management, monkeypatch):
        self._mock_pipeline_multiple(monkeypatch, entidades={})

        resultado = guided_flow.step(sesion_management, "porque no me han pagado mis comisiones")

        assert any("requiere identificar los siguientes filtros" in l for l in resultado.lineas)
        session = guided_flow.session_store.load(sesion_management)
        assert session["state"] == "REFORMULAR"

    def test_flujo_no_detectado_ofrece_reintento(self, sesion_management, monkeypatch):
        monkeypatch.setattr(cli, "reescribir_consulta", lambda hist, q: q)
        monkeypatch.setattr(cli, "transformar_consulta_con_llm", lambda q: "nada")
        monkeypatch.setattr(cli, "detectar_caso_de_uso", lambda norm, cats: (None, 0.3))

        resultado = guided_flow.step(sesion_management, "asdf qwerty")
        assert "  No fue posible identificar un flujo asociado a su solicitud" in resultado.texto
        assert resultado.lineas[-1] == "  ¿Desea intentar de nuevo? (S/N): "
        assert resultado.botones == [("S", "S"), ("N", "N")]

        # N → despedida y fin de sesión
        resultado = guided_flow.step(sesion_management, "N")
        assert resultado.terminado
        assert "Gracias por utilizar el Sistema de Consultas IA. ¡Hasta pronto!" in resultado.texto

    def test_filtros_requeridos_faltantes_pide_reformular(self, sesion_management, monkeypatch):
        pregunta = dict(PREGUNTA_SQL, filtros_requeridos=["p.Policy_Number__c"], parametros=["p.Policy_Number__c"])
        _mock_pipeline_sql(monkeypatch, pregunta)
        resultado = guided_flow.step(sesion_management, "detalle de comisiones")

        assert "  Esta consulta requiere identificar los siguientes filtros/entidades (Número de Póliza)," in resultado.lineas
        assert resultado.lineas[-1] == "  Su consulta: "

        # La siguiente consulta reinicia el pipeline completo
        entidades = {"p.Policy_Number__c": ("H123", 1.0, "Número de Póliza", "AND p.Policy_Number__c = 'H123'")}
        _mock_pipeline_sql(monkeypatch, pregunta, entidades=entidades)
        resultado = guided_flow.step(sesion_management, "detalle de comisiones poliza H123")
        assert resultado.lineas[-1].startswith("  ¿Desea Confirmar?")


# ── Confirmación y ejecución ───────────────────────────────────────────────────

class TestConfirmacion:
    def _hasta_confirmacion(self, key, monkeypatch):
        _mock_pipeline_sql(monkeypatch, PREGUNTA_SQL)
        resultado = guided_flow.step(key, "cuantos contratos activos")
        assert resultado.lineas[-1].startswith("  ¿Desea Confirmar?")

    def test_confirmar_ejecuta_y_muestra_resultado(self, sesion_management, monkeypatch):
        self._hasta_confirmacion(sesion_management, monkeypatch)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "Tienes 42 contratos activos.")

        resultado = guided_flow.step(sesion_management, "S")
        assert resultado.lineas[:2] == ["", "  Procesando su consulta, por favor espere..."]
        assert "RESULTADO:" in resultado.lineas
        assert "Tienes 42 contratos activos." in resultado.lineas
        assert resultado.lineas[-1] == "¿Desea realizar otra consulta? (S/N): "

    def test_prints_internos_se_capturan(self, sesion_management, monkeypatch):
        self._hasta_confirmacion(sesion_management, monkeypatch)

        def ejecutar_con_prints(*a, **k):
            print()
            print("🤖 Consultando el sistema..")
            return "Respuesta final."

        monkeypatch.setattr(cli, "ejecutar_consulta", ejecutar_con_prints)
        resultado = guided_flow.step(sesion_management, "S")
        assert "🤖 Consultando el sistema.." in resultado.texto
        assert "Respuesta final." in resultado.texto

    def test_omitir_ejecuta_sin_entidades(self, sesion_management, monkeypatch):
        self._hasta_confirmacion(sesion_management, monkeypatch)
        llamadas = {}

        def ejecutar(pregunta, filtro, entidades, texto_usuario, **kw):
            llamadas["entidades"] = entidades
            llamadas["texto_usuario"] = texto_usuario
            return "ok"

        monkeypatch.setattr(cli, "ejecutar_consulta", ejecutar)
        resultado = guided_flow.step(sesion_management, guided_flow.OMITIR_SENTINEL)
        assert llamadas == {"entidades": {}, "texto_usuario": ""}
        assert not resultado.terminado

    def test_n_vuelve_al_menu(self, sesion_management, monkeypatch):
        self._hasta_confirmacion(sesion_management, monkeypatch)
        resultado = guided_flow.step(sesion_management, "N")
        assert "  Volviendo al menú principal..." in resultado.lineas
        assert "¿Qué desea consultar hoy?" in resultado.lineas

    def test_opcion_invalida_repregunta(self, sesion_management, monkeypatch):
        self._hasta_confirmacion(sesion_management, monkeypatch)
        resultado = guided_flow.step(sesion_management, "tal vez")
        assert resultado.lineas[0] == "  ⚠  Opción no válida. Por favor ingrese: S / N / Enter."
        assert resultado.botones == guided_flow._BOTONES_CONFIRMAR

    def test_otra_consulta_s_vuelve_al_menu_n_despide(self, sesion_management, monkeypatch):
        self._hasta_confirmacion(sesion_management, monkeypatch)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "ok")
        guided_flow.step(sesion_management, "S")

        resultado = guided_flow.step(sesion_management, "S")
        assert "¿Qué desea consultar hoy?" in resultado.lineas

        self._hasta_confirmacion(sesion_management, monkeypatch)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "ok")
        guided_flow.step(sesion_management, "S")
        resultado = guided_flow.step(sesion_management, "N")
        assert resultado.terminado


# ── Keywords globales ──────────────────────────────────────────────────────────

class TestKeywordsGlobales:
    def test_salir_finaliza_sesion(self, sesion_management):
        resultado = guided_flow.step(sesion_management, "salir")
        assert resultado.terminado
        assert resultado.lineas == ["", "Sesión finalizada. ¡Hasta pronto!"]
        # El siguiente mensaje arranca una sesión nueva
        resultado = guided_flow.step(sesion_management, "lo que sea")
        assert "🔐 IDENTIFICACIÓN DE USUARIO" in resultado.texto

    def test_nueva_sesion_reinicia_identificacion(self, sesion_management):
        resultado = guided_flow.step(sesion_management, "nueva sesión")
        assert "  Volviendo a la identificación de usuario..." in resultado.lineas
        assert "Seleccione su tipo de usuario:" in resultado.texto

    def test_instrucciones_muestra_pantalla_y_repregunta(self, sesion_management):
        resultado = guided_flow.step(sesion_management, "instrucciones")
        assert "INSTRUCCIONES DE USO" in resultado.texto
        assert "FLUJO GENERAL DEL SISTEMA" in resultado.texto
        assert resultado.lineas[-1] == "  Su consulta: "

    def test_saludo_responde_y_repregunta(self, sesion_management):
        resultado = guided_flow.step(sesion_management, "hola")
        assert "  ¡Hola! Soy tu asistente de Claro Insurance." in resultado.lineas
        assert resultado.lineas[-1] == "  Su consulta: "

    def test_escalar_en_proceso(self, sesion_management):
        resultado = guided_flow.step(sesion_management, "escalar")
        assert "  Funcionalidad todavía en proceso." in resultado.lineas


# ── Historial (sliding window) ─────────────────────────────────────────────────

class TestHistorial:
    def test_historial_reciente_mantiene_ventana_de_10(self, sesion_management, monkeypatch):
        _mock_pipeline_sql(monkeypatch, PREGUNTA_SQL)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "ok")

        for i in range(12):
            guided_flow.step(sesion_management, f"consulta numero {i}")
            guided_flow.step(sesion_management, "S")   # confirmar
            guided_flow.step(sesion_management, "S")   # otra consulta

        sesion = session_store_mod.session_store.load(sesion_management)
        assert len(sesion["historial_reciente"]) == 10
        assert sesion["historial_reciente"][-1] == "consulta numero 11"

    def test_rewriter_recibe_solo_ultimo_turno(self, sesion_management, monkeypatch):
        recibido = {}

        def rewriter(historial, query):
            recibido["historial"] = historial
            return query

        _mock_pipeline_sql(monkeypatch, PREGUNTA_SQL)
        monkeypatch.setattr(cli, "reescribir_consulta", rewriter)
        monkeypatch.setattr(cli, "ejecutar_consulta", lambda *a, **k: "ok")

        guided_flow.step(sesion_management, "contratos activos")
        guided_flow.step(sesion_management, "S")
        guided_flow.step(sesion_management, "S")
        guided_flow.step(sesion_management, "y los inactivos")

        assert recibido["historial"] == [{"role": "user", "content": "contratos activos"}]

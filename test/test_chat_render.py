"""
Tests de adapters/chat_render.py — formato Google Chat de los bloques del flujo.

Verifica que el render de Chat:
  - oculta debug/SQL salvo FLOW_DEBUG=1
  - elimina decoración de terminal y prompts de CLI
  - formatea flujo/entidades/resultado con markdown de Chat
  - parsea la salida capturada de RAG en respuesta + documentos (card)

Ejecutar:
    pytest test/test_chat_render.py -v
"""
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main as cli
from adapters import chat_render
from core.guided_flow import (
    Bloque,
    FlowResult,
    _parse_rag_salida,
    _parse_resultado_multiple,
)

SEP = "━" * 55


def _render(*bloques: Bloque) -> str:
    texto, _cards = chat_render.render_chat(FlowResult(bloques=list(bloques)))
    return texto


# ── Visibilidad de debug ───────────────────────────────────────────────────────

class TestDebug:
    def test_debug_oculto_por_defecto(self, monkeypatch):
        monkeypatch.delenv("FLOW_DEBUG", raising=False)
        texto = _render(
            Bloque("debug", "  #--DEBUG Pregunta Formateada: x"),
            Bloque("sql_debug", "── Query enviada a BigQuery ──\nSELECT 1"),
        )
        assert texto == ""

    def test_debug_visible_con_flag(self, monkeypatch):
        monkeypatch.setenv("FLOW_DEBUG", "1")
        texto = _render(
            Bloque("debug", "  #--DEBUG Pregunta Formateada: x"),
            Bloque("sql_debug", "SELECT 1"),
        )
        assert "#--DEBUG Pregunta Formateada: x" in texto
        assert "```\nSELECT 1\n```" in texto


# ── Limpieza de decoración y prompts ──────────────────────────────────────────

class TestLimpieza:
    def test_header_sin_cajas(self):
        texto = _render(Bloque("header", "\n╔══╗\n║ X ║\n╚══╝\n..."))
        assert texto.startswith("*SISTEMA DE CONSULTAS IA — BIENVENIDO*")
        assert "╔" not in texto

    def test_prompts_de_terminal_se_omiten(self):
        texto = _render(
            Bloque("prompt_opcion", "Opción: "),
            Bloque("prompt_consulta", "  Su consulta: "),
            Bloque("status", "\n  Analizando su consulta..."),
            Bloque("procesando", "\n  Procesando su consulta, por favor espere..."),
        )
        assert texto == ""

    def test_menu_consulta_sin_separador(self):
        texto = _render(Bloque("menu_consulta", SEP + "\n\n¿Qué desea consultar hoy?\n\n  Su consulta: "))
        assert texto == "*¿Qué desea consultar hoy?*"
        assert "━" not in texto

    def test_ident_menu_usa_data(self):
        bloque = Bloque("ident_menu", "raw cli", data={"tipos": [("1", "Representante de Agencia"), ("2", "Agente NPN")]})
        texto = _render(bloque)
        assert "*🔐 Identificación de usuario*" in texto
        assert "1. Representante de Agencia" in texto
        assert "Opción:" not in texto

    def test_bloques_passthrough_pierden_indentacion(self):
        texto = _render(Bloque("volver", "\n  Volviendo al menú principal...\n"))
        assert texto == "Volviendo al menú principal..."


# ── Confirmación, meta y resultado ──────────────────────────────────────────────

class TestFlujo:
    def test_resultado_formateado(self):
        bloque = Bloque("resultado", "\nRESULTADO:\n" + SEP + "\nHay 42 contratos.", data={"respuesta": "Hay 42 contratos."})
        texto = _render(bloque)
        assert texto == "*Resultado*\nHay 42 contratos."

    def test_confirmacion_muestra_mensaje_del_agente(self):
        # El mensaje de confirmación (Agente 1) se muestra tal cual, sin S/N.
        bloque = Bloque(
            "confirmar_prompt",
            "\n  Entiendo que quieres consultar Contratos Activos. ¿Es correcto?\n\n  Su respuesta: ",
            data={"mensaje": "Entiendo que quieres consultar Contratos Activos. ¿Es correcto?", "tiene_caso": True},
        )
        texto = _render(bloque)
        assert "Entiendo que quieres consultar Contratos Activos. ¿Es correcto?" in texto
        assert "(S/N)" not in texto
        assert "Su respuesta:" not in texto

    def test_confirmacion_convierte_markdown(self):
        bloque = Bloque(
            "confirmar_prompt", "raw",
            data={"mensaje": "Quieres ver los **contratos activos**. ¿Correcto?", "tiene_caso": True},
        )
        texto = _render(bloque)
        assert "*contratos activos*" in texto  # **negrita** → *negrita* (formato Chat)

    def test_meta_responde_directo(self):
        bloque = Bloque("meta", "\n  Soy el asistente de **Claro Insurance**.")
        texto = _render(bloque)
        assert "Soy el asistente de *Claro Insurance*." in texto

    def test_otra_naturalizada(self):
        texto = _render(Bloque("otra_prompt", "¿Hay algo más en lo que pueda ayudarte? "))
        assert "¿Hay algo más en lo que pueda ayudarte?" in texto
        assert "(S/N)" not in texto


# ── Instrucciones: pantalla del CLI → formato Chat ─────────────────────────────

class TestInstrucciones:
    @pytest.fixture
    def render_instrucciones(self):
        from core.guided_flow import _capturado
        salida, _ = _capturado(cli._mostrar_instrucciones)
        return _render(Bloque("instrucciones", salida))

    def test_sin_decoracion_de_terminal(self, render_instrucciones):
        for char in ("═", "│", "┌", "├", "└", "━", "╔", "║", "╚"):
            assert char not in render_instrucciones, f"queda decoración: {char}"

    def test_titulos_en_negrita(self, render_instrucciones):
        # La pantalla se recortó (function-calling, CHANGELOG punto 7): ya no hay
        # tabla de filtros ni lista de consultas de ejemplo; ahora hay "TEMAS QUE
        # PUEDE CONSULTAR" resuelto dinámicamente por el Agente 1.
        assert "*INSTRUCCIONES DE USO*" in render_instrucciones
        assert "*FLUJO GENERAL DEL SISTEMA*" in render_instrucciones
        assert "*TEMAS QUE PUEDE CONSULTAR*" in render_instrucciones
        assert "*CONTRATOS*" in render_instrucciones
        assert "*PAGOS Y COMISIONES*" in render_instrucciones

    def test_temas_como_vinetas(self, render_instrucciones):
        assert "Comisiones Pagadas" in render_instrucciones
        assert "Licencias" in render_instrucciones

    def test_contenido_se_conserva(self, render_instrucciones):
        assert "1. IDENTIFICACIÓN" in render_instrucciones
        assert "· Comisiones Pagadas" in render_instrucciones
        assert "instrucciones / help / ayuda — muestra esta pantalla" in render_instrucciones


# ── Formato Chat → HTML para cards ─────────────────────────────────────────────

class TestTextoAHtmlCard:
    def test_negrita_y_cursiva(self):
        assert chat_render.texto_a_html_card("*Título*\n_nota_") == "<b>Título</b>\n<i>nota</i>"

    def test_identificadores_con_underscore_no_se_tocan(self):
        texto = "La columna Payment_Status__c y p.Pay_on_Date__c"
        assert chat_render.texto_a_html_card(texto) == texto

    def test_asterisco_suelto_no_se_toca(self):
        texto = "(*) Estas consultas requieren al menos un filtro"
        assert chat_render.texto_a_html_card(texto) == texto

    def test_escapa_html(self):
        assert chat_render.texto_a_html_card("a < b") == "a &lt; b"


# ── Markdown del LLM → formato Chat ────────────────────────────────────────────

class TestMarkdownAChat:
    def test_negrita_doble_asterisco(self):
        assert chat_render.markdown_a_chat("**Writing number**: el NPN") == "*Writing number*: el NPN"

    def test_vinetas_asterisco_y_guion(self):
        texto = "* primer item\n  * anidado\n- con guion"
        assert chat_render.markdown_a_chat(texto) == "• primer item\n  • anidado\n• con guion"

    def test_vineta_con_negrita(self):
        # Caso real de Gemini: "    *   **Commission type**: ..."
        texto = "    *   **Commission type**: Debes seleccionar \"Monthly\"."
        assert chat_render.markdown_a_chat(texto) == "    • *Commission type*: Debes seleccionar \"Monthly\"."

    def test_encabezados(self):
        assert chat_render.markdown_a_chat("## Pasos generales") == "*Pasos generales*"

    def test_links(self):
        assert chat_render.markdown_a_chat("Ver [guía](https://x.com/g)") == "Ver <https://x.com/g|guía>"

    def test_numeracion_no_cambia(self):
        texto = "1.  **Identificar las oportunidades**: filtra por carrier."
        assert chat_render.markdown_a_chat(texto) == "1.  *Identificar las oportunidades*: filtra por carrier."

    def test_se_aplica_a_resultado_rag_y_multiple(self):
        respuesta = "Pasos:\n1. **Identificar**: accede.\n* **Writing number**: NPN."
        esperado = "Pasos:\n1. *Identificar*: accede.\n• *Writing number*: NPN."
        for kind, data in [
            ("resultado", {"respuesta": respuesta}),
            ("rag", {"respuesta": respuesta, "documentos": [], "carrier": None}),
            ("multiple_exec", {"respuesta": respuesta, "debug": ""}),
        ]:
            texto = _render(Bloque(kind, "raw", data=data))
            assert esperado in texto, kind


# ── Parsers de salida capturada ────────────────────────────────────────────────

def _salida_rag_sintetica() -> str:
    """Replica el formato exacto de prints de ejecutar_rag()."""
    border = "─" * cli._BOX_W
    lineas = ["", "   Carrier detectado: Cigna (confianza: 0.71)", ""]
    lineas += ["📄 RESULTADOS ENCONTRADOS (Top 2):", ""]
    for i, (fuente, sim, extracto) in enumerate([
        ("CALENDARIO DE COMISIONES 2026.pdf (pág. 3)", 87.3, "Las comisiones se pagan el día 15 de cada mes según el calendario vigente."),
        ("CLARO INSURANCE FAQ.pdf (pág. 7)", 75.0, "El agente debe estar activo para recibir pagos."),
    ], 1):
        lineas.append(f"┌{border}┐")
        lineas.append(cli._box_line(f"📌 DOCUMENTO {i}  (Similitud: {sim:.1f}%)"))
        lineas.append(cli._box_line(f"Fuente: {fuente}"))
        lineas.append(cli._box_line(""))
        for linea in textwrap.wrap(f'"{extracto}"', width=cli._BOX_W - 2):
            lineas.append(cli._box_line(linea))
        lineas.append(f"└{border}┘")
        lineas.append("")
    lineas += ["RESPUESTA:", SEP, "Las comisiones se pagan el 15 de cada mes.", "Recuerda que puedes escalar a un humano."]
    return "\n".join(lineas)


class TestParseRag:
    def test_extrae_respuesta_y_documentos(self):
        data = _parse_rag_salida(_salida_rag_sintetica())
        assert data["respuesta"].startswith("Las comisiones se pagan el 15 de cada mes.")
        assert data["carrier"] == "Cigna (confianza: 0.71)"
        assert len(data["documentos"]) == 2
        doc = data["documentos"][0]
        assert doc["fuente"] == "CALENDARIO DE COMISIONES 2026.pdf (pág. 3)"
        assert doc["similitud"] == 87.3
        assert "día 15 de cada mes" in doc["extracto"]

    def test_formato_inesperado_cae_a_texto_crudo(self):
        data = _parse_rag_salida("   No se encontraron documentos relacionados con tu consulta.")
        assert data["respuesta"] == "No se encontraron documentos relacionados con tu consulta."
        assert data["documentos"] == []

    def test_render_rag_con_card_de_documentos(self):
        bloque = Bloque("rag", _salida_rag_sintetica(), data=_parse_rag_salida(_salida_rag_sintetica()))
        texto, cards = chat_render.render_chat(FlowResult(bloques=[bloque]))
        assert "*Resultado*\nLas comisiones se pagan el 15 de cada mes." in texto
        assert "┌" not in texto
        assert len(cards) == 1
        assert cards[0]["card"]["header"]["title"] == "📄 Documentos consultados"
        widgets = cards[0]["card"]["sections"][0]["widgets"]
        assert len(widgets) == 2
        assert "CALENDARIO DE COMISIONES 2026.pdf" in widgets[0]["textParagraph"]["text"]


class TestParseMultiple:
    def test_separa_debug_de_respuesta(self):
        salida = "\n".join([
            "",
            "  Ejecutando 2 consultas en paralelo...",
            "",
            "── Query [Caso A] " + "─" * 30,
            "SELECT 1",
            "─" * 55,
            "",
            "RESULTADO:",
            SEP,
            "Tienes 10 contratos y 5 comisiones.",
        ])
        data = _parse_resultado_multiple(salida)
        assert data["respuesta"] == "Tienes 10 contratos y 5 comisiones."
        assert "SELECT 1" in data["debug"]

    def test_render_multiple_oculta_debug(self, monkeypatch):
        monkeypatch.delenv("FLOW_DEBUG", raising=False)
        bloque = Bloque("multiple_exec", "raw", data={"respuesta": "Resumen total.", "debug": "SELECT 1"})
        texto = _render(bloque)
        assert texto == "*Resultado*\nResumen total."

    def test_render_multiple_muestra_debug_con_flag(self, monkeypatch):
        monkeypatch.setenv("FLOW_DEBUG", "1")
        bloque = Bloque("multiple_exec", "raw", data={"respuesta": "Resumen total.", "debug": "SELECT 1"})
        texto = _render(bloque)
        assert "```\nSELECT 1\n```" in texto
        assert "*Resultado*\nResumen total." in texto

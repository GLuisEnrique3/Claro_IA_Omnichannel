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
        bloque = Bloque("ident_menu", "raw cli", data={"tipos": [("1", "Representante de Agencia"), ("3", "Management")]})
        texto = _render(bloque)
        assert "*🔐 Identificación de usuario*" in texto
        assert "1. Representante de Agencia" in texto
        assert "Opción:" not in texto

    def test_bloques_passthrough_pierden_indentacion(self):
        texto = _render(Bloque("volver", "\n  Volviendo al menú principal...\n"))
        assert texto == "Volviendo al menú principal..."


# ── Flujo identificado y resultado ─────────────────────────────────────────────

class TestFlujo:
    def test_flujo_con_entidades(self):
        bloque = Bloque("flujo", "raw", data={
            "nombre": "Cantidad de Contratos Activos", "score": 0.95,
            "entidades": [{"label": "Carrier", "valor": "Ambetter", "score": 1.0}],
            "multiple": None,
        })
        texto = _render(bloque)
        assert "*Flujo identificado:* Cantidad de Contratos Activos _(coincidencia: 0.95)_" in texto
        assert "• Carrier: Ambetter _(1.00)_" in texto

    def test_flujo_sin_entidades(self):
        bloque = Bloque("flujo", "raw", data={"nombre": "X", "score": 0.9, "entidades": [], "multiple": None})
        assert "_Sin ninguna entidad detectada._" in _render(bloque)

    def test_resultado_formateado(self):
        bloque = Bloque("resultado", "\nRESULTADO:\n" + SEP + "\nHay 42 contratos.", data={"respuesta": "Hay 42 contratos."})
        texto = _render(bloque)
        assert texto == "*Resultado*\nHay 42 contratos."

    def test_prompts_de_confirmacion_naturalizados(self):
        texto = _render(
            Bloque("confirmar_prompt", "  ¿Desea Confirmar? (S = confirmar / ...): "),
            Bloque("otra_prompt", "¿Desea realizar otra consulta? (S/N): "),
        )
        assert "¿Desea confirmar?" in texto
        assert "¿Desea realizar otra consulta?" in texto
        assert "(S/N)" not in texto


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

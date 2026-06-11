"""
Render de FlowResult para Google Chat.

El flujo guiado (core/guided_flow.py) emite Bloques semánticos cuyo `texto`
es la réplica exacta del CLI (optimizada para terminal). Este módulo los
convierte al formato nativo de Google Chat:
  - títulos en *negrita*, sin cajas ╔═╗ ni separadores ━━━
  - prompts de terminal ("Opción:", "Su consulta:") eliminados — los botones
    y las preguntas ya lo comunican
  - líneas #--DEBUG y SQL ocultos salvo FLOW_DEBUG=1
  - documentos RAG como card aparte con fuente, similitud y extracto

Formato de texto de Chat: *negrita*, _cursiva_, `mono`, ```bloque```.
Las cards (textParagraph) usan HTML simple: <b>, <i>, <br>.
"""
import html
import os
import re

from core.guided_flow import Bloque, FlowResult


def _debug_activo() -> bool:
    return os.getenv("FLOW_DEBUG", "") == "1"


def markdown_a_chat(texto: str) -> str:
    """
    Convierte Markdown estándar (lo que genera el LLM) al formato de Google
    Chat: **negrita** → *negrita*, viñetas * → •, ## títulos → *negrita*,
    [link](url) → <url|link>. Se aplica SOLO a respuestas generadas por el
    LLM — los textos del flujo ya vienen en formato Chat.
    """
    # Viñetas: "* item" / "  * item" → "• item" (antes que bold: ** no matchea)
    texto = re.sub(r"(?m)^(\s*)\*\s+", r"\1• ", texto)
    # Viñetas con guion: "- item" → "• item"
    texto = re.sub(r"(?m)^(\s*)-\s+", r"\1• ", texto)
    # Negrita: **texto** → *texto*
    texto = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto)
    # Encabezados: "## Título" → "*Título*"
    texto = re.sub(r"(?m)^#{1,6}\s+(.+?)\s*$", r"*\1*", texto)
    # Links: [texto](url) → <url|texto>
    texto = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", texto)
    return texto


def _limpiar(texto: str) -> str:
    """Quita indentación de terminal y líneas vacías en los bordes."""
    lineas = [linea.strip() for linea in texto.split("\n")]
    while lineas and not lineas[0]:
        lineas.pop(0)
    while lineas and not lineas[-1]:
        lineas.pop()
    return "\n".join(lineas)


# ── Formatters por tipo de bloque ──────────────────────────────────────────────
# Cada formatter retorna str (texto a incluir) o None (bloque omitido en Chat).

def _fmt_header(b: Bloque) -> str:
    return (
        "*SISTEMA DE CONSULTAS IA — BIENVENIDO*\n\n"
        'Escribe "instrucciones" en cualquier momento para ver el flujo.\n'
        'Escribe "salir" o "exit" para salir del sistema.'
    )


def _fmt_ident_menu(b: Bloque) -> str:
    tipos = (b.data or {}).get("tipos", [])
    opciones = "\n".join(f"{k}. {nombre}" for k, nombre in tipos)
    return f"*🔐 Identificación de usuario*\n\nSeleccione su tipo de usuario:\n{opciones}"


def _fmt_menu_consulta(b: Bloque) -> str:
    return "*¿Qué desea consultar hoy?*"


def _fmt_debug(b: Bloque) -> str | None:
    if not _debug_activo():
        return None
    return f"`{_limpiar(b.texto)}`"


def _fmt_sql_debug(b: Bloque) -> str | None:
    if not _debug_activo():
        return None
    return f"```\n{_limpiar(b.texto)}\n```"


def _fmt_flujo(b: Bloque) -> str:
    data = b.data or {}
    partes = [
        f"*Flujo identificado:* {data.get('nombre')} _(coincidencia: {data.get('score', 0):.2f})_"
    ]
    multiple = data.get("multiple")
    if multiple:
        nombres = multiple.get("invoca_nombres", [])
        partes.append(f"Se ejecutarán {len(nombres)} consultas en paralelo:")
        partes += [f"• {n}" for n in nombres]
        for sub in multiple.get("subcasos", []):
            partes.append(f"\nEntidades detectadas [{sub['nombre']}]:")
            if sub["entidades"]:
                partes += [
                    f"• {e['label']}: {e['valor']} _({e['score']:.2f})_"
                    for e in sub["entidades"]
                ]
            else:
                partes.append("_(ninguna)_")
    elif data.get("entidades"):
        partes.append("Entidades detectadas:")
        partes += [
            f"• {e['label']}: {e['valor']} _({e['score']:.2f})_"
            for e in data["entidades"]
        ]
    else:
        partes.append("_Sin ninguna entidad detectada._")
    return "\n".join(partes)


def _fmt_filtros_requeridos(b: Bloque) -> str:
    labels = (b.data or {}).get("labels", "")
    return (
        f"⚠️ Esta consulta requiere identificar los siguientes filtros/entidades: *{labels}*\n"
        "Por favor reformula tu pregunta considerando dichos filtros."
    )


def _fmt_confirmar(b: Bloque) -> str:
    return "¿Desea confirmar?"


def _fmt_retry(b: Bloque) -> str:
    return "¿Desea intentar de nuevo?"


def _fmt_otra(b: Bloque) -> str:
    return "¿Desea realizar otra consulta?"


def _fmt_resultado(b: Bloque) -> str:
    respuesta = (b.data or {}).get("respuesta", _limpiar(b.texto))
    return f"*Resultado*\n{markdown_a_chat(respuesta)}"


def _fmt_multiple_exec(b: Bloque) -> str:
    data = b.data or {}
    partes = []
    if _debug_activo() and data.get("debug"):
        partes.append(f"```\n{data['debug']}\n```")
    respuesta = data.get("respuesta", _limpiar(b.texto))
    partes.append(f"*Resultado*\n{markdown_a_chat(respuesta)}")
    return "\n\n".join(partes)


def _fmt_rag(b: Bloque) -> str:
    data = b.data or {}
    partes = []
    if data.get("carrier"):
        partes.append(f"_Carrier detectado: {data['carrier']}_")
    respuesta = data.get("respuesta", _limpiar(b.texto))
    partes.append(f"*Resultado*\n{markdown_a_chat(respuesta)}")
    return "\n\n".join(partes)


def _fmt_instrucciones(b: Bloque) -> str:
    # Quitar la caja ╔═╗ del título y dejar el resto del contenido del CLI
    lineas = [
        linea for linea in b.texto.split("\n")
        if not linea.startswith(("╔", "║", "╚"))
    ]
    cuerpo = "\n".join(lineas).strip("\n")
    return f"*INSTRUCCIONES DE USO*\n\n{cuerpo}"


def _fmt_omitir(b: Bloque) -> None:
    return None


def _fmt_default(b: Bloque) -> str | None:
    limpio = _limpiar(b.texto)
    return limpio or None


_FORMATTERS = {
    "header": _fmt_header,
    "ident_menu": _fmt_ident_menu,
    "menu_consulta": _fmt_menu_consulta,
    "prompt_opcion": _fmt_omitir,
    "prompt_consulta": _fmt_omitir,
    "status": _fmt_omitir,
    "procesando": _fmt_omitir,
    "salto": _fmt_omitir,
    "debug": _fmt_debug,
    "sql_debug": _fmt_sql_debug,
    "flujo": _fmt_flujo,
    "filtros_requeridos": _fmt_filtros_requeridos,
    "confirmar_prompt": _fmt_confirmar,
    "retry_prompt": _fmt_retry,
    "otra_prompt": _fmt_otra,
    "resultado": _fmt_resultado,
    "multiple_exec": _fmt_multiple_exec,
    "rag": _fmt_rag,
    "instrucciones": _fmt_instrucciones,
    # prompt_valor, bienvenida, ident_error, no_flujo, invalida, saludo,
    # escalar, volver, nueva_sesion, despedida, salir → _fmt_default
}


def _card_documentos(documentos: list[dict]) -> dict:
    """Card con los documentos consultados por RAG (fuente, similitud, extracto)."""
    widgets = []
    for i, doc in enumerate(documentos, 1):
        fuente = html.escape(doc.get("fuente") or "Documento")
        extracto = html.escape(doc.get("extracto") or "")
        similitud = doc.get("similitud")
        titulo = f"<b>📌 {fuente}</b>"
        if similitud is not None:
            titulo += f" — similitud {similitud:.1f}%"
        widgets.append({
            "textParagraph": {"text": f"{titulo}<br><i>“{extracto}”</i>"}
        })
    return {
        "cardId": "documentos",
        "card": {
            "header": {"title": "📄 Documentos consultados"},
            "sections": [{"widgets": widgets}],
        },
    }


def render_chat(result: FlowResult) -> tuple[str, list[dict]]:
    """
    Convierte un FlowResult al formato de Google Chat.
    Retorna (texto_del_mensaje, cards_adicionales).
    """
    partes: list[str] = []
    cards: list[dict] = []

    for bloque in result.bloques:
        formatter = _FORMATTERS.get(bloque.kind, _fmt_default)
        parte = formatter(bloque)
        if parte:
            partes.append(parte)

        if bloque.kind == "rag" and (bloque.data or {}).get("documentos"):
            cards.append(_card_documentos(bloque.data["documentos"]))

    return "\n\n".join(partes), cards

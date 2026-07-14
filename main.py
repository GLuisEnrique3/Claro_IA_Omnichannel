import sys
import json
import textwrap
import datetime
import os
import re
import time
import torch
from config.chroma_client import ChromaClient
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_LLM_MAX_RETRIES = 3
_LLM_RETRY_BASE_S = 5  # backoff: 5s, 10s, 20s

def _llm_call(model, prompt: str):
    """Llama al modelo LLM con retry/backoff para errores 429 (rate limit)."""
    for attempt in range(1, _LLM_MAX_RETRIES + 1):
        try:
            return model.generate_content(prompt)
        except Exception as exc:
            msg = str(exc)
            is_rate_limit = "429" in msg or "Resource exhausted" in msg or "rate" in msg.lower()
            if is_rate_limit and attempt < _LLM_MAX_RETRIES:
                wait = _LLM_RETRY_BASE_S * (2 ** (attempt - 1))
                print(f"   ⏳ Rate limit Vertex AI, reintentando en {wait}s (intento {attempt}/{_LLM_MAX_RETRIES})...")
                time.sleep(wait)
                continue
            raise

# ── Nombres de meses en español (para parser de fechas) ───────────────────────
_MESES_NUM = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
_MESES_ES = list(_MESES_NUM.keys())

sys.path.insert(0, str(Path(__file__).parent))

from config import llm_model, llm_sql_model, llm_confirm_model, client, FILTROS_VALIDOS, FILTROS_EMBEDDINGS
from config.logger import get_session, nueva_sesion

# ── Carga de casos de uso ──────────────────────────────────────────────────────
USE_CASES_PATH = Path(__file__).parent / "data" / "use_cases.json"
with open(USE_CASES_PATH, encoding="utf-8") as _f:
    USE_CASES = json.load(_f)

# ── Configuración de tipos de usuario ─────────────────────────────────────────
#  umbral: similitud coseno mínima (0-1) para aceptar la identificación.
TIPOS_USUARIO = {
    "1": {
        "nombre": "Representante de Agencia",
        "filtro_key": "agency",
        "sql_filtro": "AND a.Name_Agencies = '{valor}'",
        "catalogos": ["A", "B", "C"],
        "umbral": 0.65,
        "prompt_id": "Ingrese el nombre de su agencia: ",
    },
    "2": {
        "nombre": "Agente NPN",
        "filtro_key": "npn",
        "sql_filtro": "AND c.NPN__c = '{valor}'",
        "catalogos": ["A", "B","C"],
        "umbral": 0.95,
        "prompt_id": "Ingrese su NPN: ",
    },
}

# Permisos de catálogos por tipo de usuario (configurable en data/catalog_permissions.json).
_CATALOG_PERMS_PATH = Path(__file__).parent / "data" / "catalog_permissions.json"
_CATALOG_PERMS: dict = {}
try:
    with open(_CATALOG_PERMS_PATH, encoding="utf-8") as _f:
        _CATALOG_PERMS = json.load(_f)
except Exception:
    _CATALOG_PERMS = {}

# ── Mapping: parámetro SQL → (filtro_key, etiqueta_display, snippet_sql, umbral) ──
#  umbral: similitud coseno mínima para aceptar la entidad (0-1).
PARAM_TO_FILTRO = {
    "c.NPN__c":           ("npn",                "NPN",                "AND c.NPN__c = '{v}'",                              0.99),
    "c.Name":             ("name",              "Agente",             "AND c.Name = '{v}'",                                0.95),
    "a.Name_Agencies":    ("agency",             "Agencia",            "AND a.Name_Agencies = '{v}'",                       0.90),
    "o.Carrier":          ("carrier",            "Carrier",            "AND o.Carrier = '{v}'",                             0.85),
    "o.State":            ("state",              "Estado",             "AND o.State = '{v}'",                               0.80),
    "o.Line_Of_Business": ("line_of_business",   "Línea de Negocio",   "AND o.Line_Of_Business = '{v}'",                    0.80),
    "p.StageName":        ("stage_name",         "Etapa",              "AND s.StageName = '{v}'",                           0.80),
    "p.Sub_Stage__c":        ("sub_stage_name",     "Sub-etapa",          "AND s.Sub_Stage__c = '{v}'",                           0.80),
    "nc.Status__c":       ("contract_status",    "Estado de Contrato", "AND nc.Status__c = '{v}'",                          0.80),
    # Parámetros de tipo fecha — filtro_key "__fecha__" activa el parser de lenguaje natural
    "p.Pay_on_Date__c":      ("__fecha__",            "Fecha de Pago",    "AND DATE_TRUNC(DATE(p.Pay_on_Date__c), MONTH) = DATE '{v}'",  None),
    # Commission Month: se activa solo cuando el usuario menciona "commission month" o "mes de comision/es"
    "p.Commission_Month__c": ("__commission_month__", "Mes de Comisión",  "AND DATE_TRUNC(p.Commission_Month__c, MONTH) = DATE '{v}'",   None),
    "p.Payment_Status__c":   ("payment_status",       "Estado de Pago",   "AND p.Payment_Status__c = '{v}'",                             0.82),
    "p.Payment_Type__c":     ("payment_type",         "Tipo de Pago",     "AND p.Payment_Type__c = '{v}'",                               0.82),
    # Parámetro de póliza — filtro_key "__policy_number__" activa extracción por regex
    "p.Policy_Number__c": ("__policy_number__",  "Número de Póliza",   "AND p.Policy_Number__c = '{v}'",                       None),
    "b.Policy_Number__c": ("__policy_number__",  "Número de Póliza",   "AND r.Policy_ID__c = '{v}'",                           None),
    # Parámetros de comisiones en avance
    "p.Agent_Name__c":           ("agent_name",        "Nombre del Agente",           "AND p.Agent_Name__c = '{v}'",                        0.90),
    "p.CommissionsInAdvance__c": ("__cia_id__",         "ID de Comisión en Avance",    "AND p.CommissionsInAdvance__c = '{v}'",               None),
    "p.Pre_Approved__c":         ("pre_approved",       "Estado de Aprobación",        "AND p.Pre_Approved__c = '{v}'",                      0.85),
    "p.ProcessingStatus__c":     ("processing_status",  "Estado de Procesamiento",     "AND p.ProcessingStatus__c = '{v}'",                  0.85),
    # Parámetros de licencias
    "o.Name":             ("license_state",       "Estado (Geográfico)", "AND o.Name = '{v}'",                                  0.80),
    "l.LicenseType__c":   ("license_type",        "Tipo de Licencia",   "AND l.LicenseType__c = '{v}'",                         0.80),
    "l.LicenseStatus__c": ("license_status",      "Estado Licencia (Activa/Inactiva)", "AND l.LicenseStatus__c = '{v}'",       0.85),
    "l.Disposition__c":   ("license_disposition", "Disposición",        "AND l.Disposition__c = '{v}'",                         0.80),
    "l.SubDisposition__c":("license_sub_disposition","Sub-Disposición", "AND l.SubDisposition__c = '{v}'",                     0.80),
    "l.Source__c":        ("license_source",      "Fuente (Interna/NIPR)", "AND l.Source__c = '{v}'",                           0.85),
    "nl.LicenseClassName__c": ("license_class",            "Clase de Licencia",   "AND nl.LicenseClassName__c = '{v}'",        0.80),
    "nl.LineOfAuthority__c":  ("license_line_of_authority", "Línea de Autoridad", "AND nl.LineOfAuthority__c = '{v}'",         0.80),
    # Fechas de licencia — filtro_key propio activa el parser de lenguaje natural solo si el usuario menciona activación/expiración
    "l.LicenseActivationDate__c": ("__license_activation_date__", "Fecha de Activación de Licencia", "AND DATE_TRUNC(DATE(l.LicenseActivationDate__c), MONTH) = DATE '{v}'", None),
    "l.LicenseExpirationDate__c": ("__license_expiration_date__", "Fecha de Expiración de Licencia", "AND DATE_TRUNC(DATE(l.LicenseExpirationDate__c), MONTH) = DATE '{v}'", None),
    "l.DateUpdated__c":           ("__license_date_updated__",    "Fecha de Actualización de Licencia", "AND DATE_TRUNC(DATE(l.DateUpdated__c), MONTH) = DATE '{v}'",           None),
}
_MAX_REINTENTOS_SQL = 2   # intentos máximos de construcción SQL con LLM

# ── Salida limpia ──────────────────────────────────────────────────────────────
class SalirError(Exception):
    pass

class VoverError(Exception):
    pass

class MenuError(Exception):
    pass

_PALABRAS_SALIR         = {"salir", "exit", "q", "quit"}
_PALABRAS_VOLVER        = {"volver", "back"}
_PALABRAS_MENU          = {"nueva sesión","nueva sesion"}
_PALABRAS_INSTRUCCIONES = {"instrucciones", "help", "ayuda", "guia", "guía"}
_PALABRAS_ESCALAR       = {"escalar", "escalar a un humano"}
_PALABRAS_NO            = {"no", "n", "no gracias", "nada", "nada mas", "nada más", "no, gracias", "eso es todo", "ninguna"}
_PALABRAS_SALUDO        = {"hola", "hola!", "hey", "buenas", "buenas tardes", "buenos días", "buenos dias", "buenas noches"}


def _input(prompt: str) -> str:
    valor = input(prompt).strip()
    if valor.lower() in _PALABRAS_SALIR:
        raise SalirError
    if valor.lower() in _PALABRAS_VOLVER:
        raise VoverError
    if valor.lower() in _PALABRAS_MENU:
        raise MenuError
    if valor.lower() in _PALABRAS_INSTRUCCIONES:
        _mostrar_instrucciones()
        return _input(prompt)
    if valor.lower() in _PALABRAS_ESCALAR:
        print()
        print("  Funcionalidad todavía en proceso.")
        print()
        return _input(prompt)
    if valor.lower() in _PALABRAS_SALUDO:
        print()
        print("  ¡Hola! Soy tu asistente de Claro Insurance.")
        print("  Puedo ayudarte con consultas sobre contratos, comisiones y documentos normativos.")
        print('  Escribe "instrucciones" para ver todo lo que puedes consultar.')
        print("  ¡Quedo atento!")
        print()
        return _input(prompt)
    return valor


# ── Helpers de UI ──────────────────────────────────────────────────────────────
def _sep():
    print("━" * 55)


def _header():
    print()
    print("╔" + "═" * 54 + "╗")
    print("║" + "SISTEMA DE CONSULTAS IA - BIENVENIDO".center(54) + "║")
    print("╚" + "═" * 54 + "╝")
    print()
    print('  Escribe "instrucciones" en cualquier momento para ver el flujo.')
    print('  Escribe "salir" o "exit" para salir del sistema.')
    print()


_TEMAS_POR_CATALOGO = {
    "A": """CONTRATOS
    · Cantidad y Detalle de Contratos (incluye status/estado y fecha de aprobación)
    · Cantidad y Detalle de Contratos Pendientes (incluye motivo del retraso)
    · Instructivo Gestión de Contratos (documentos: Humana, Caresource, Ascension, AmeriHealth, Ambetter, Alliant)
    · Licencias
    · Oportunidades de Contratación por Carrier""",
    "B": """PAGOS Y COMISIONES
    · Comisiones Pagadas
    · Comisiones Bloqueadas
    · Detalle Comisiones  
    · Pre-Liquidación de Comisiones
    · Detalle de Pre-Liquidación de Comisiones  
    · Frecuencia de Liquidación por Carrier (documento: Calendario Pago de Liquidaciones 2026)
    · Calendario de Pago de Comisiones a Agentes (documento: Calendario Pago de Comisiones 2026)
    · Estado del Contrato y Agente por Póliza
    · Porque no me han pagado mis comisiones  
    · Detalle de Reconciliaciones por Póliza  
    · Guía de Reconciliaciones de Comisiones (documento: Guía de Reconciliaciones de Comisiones)
    · Detalle de Comisiones en Avance  
    · Bonus Compensation ACA (documento: 2026 ACA Bonus Compensation)
    · Override Compensation ACA (documento: 2026 ACA Agency Override, general y por agencia)
    · Commission Compensation ACA (documento: 2026 ACA Agents Commission)""",
    "C": """DOCUMENTOS NORMATIVOS
    · Plazos Estándar de Envío de Contratos (documento: Envíos de Plazos Estándar de Contratos)
    · Horarios y Roles del Equipo (documento: Horario y Roles)
    · ARC Off-Exchanges FAQ (documento: ARC Off-Exchanges FAQ)
    · Claro Insurance FAQ (documento: Claro Insurance FAQ)""",
}


def _construir_recurso_presentacion(catalogos_permitidos: list) -> str:
    """
    Texto de presentación (quién es el asistente y qué temas puede consultar el
    usuario según sus catálogos permitidos). Se le pasa al LLM del Agente 1 como
    recurso para responder preguntas conversacionales/meta sobre el propio asistente,
    en vez de que el modelo invente una respuesta genérica.
    """
    temas = "\n\n".join(
        _TEMAS_POR_CATALOGO[c] for c in catalogos_permitidos if c in _TEMAS_POR_CATALOGO
    )
    return (
        "Eres el asistente de Claro Insurance. Ayudas a representantes de agencia, "
        "agentes NPN y management a consultar contratos, comisiones/pagos y documentos "
        "normativos en lenguaje natural: detectas el tema solicitado, extraes filtros "
        "mencionados en la consulta, pides confirmación antes de ejecutar, y devuelves "
        "el resultado con una explicación.\n\n"
        "Temas que el usuario puede consultar:\n\n"
        f"{temas}\n\n"
        " Estas consultas requieren al menos un filtro obligatorio para ejecutarse."
    )


def _mostrar_instrucciones():
    print()
    print("╔" + "═" * 54 + "╗")
    print("║" + "INSTRUCCIONES DE USO".center(54) + "║")
    print("╚" + "═" * 54 + "╝")
    print("""
  ══════════════════════════════════════════════════════
  FLUJO GENERAL DEL SISTEMA
  ══════════════════════════════════════════════════════

  1. IDENTIFICACIÓN
     Al ingresar, seleccione su tipo de usuario:
       1 — Representante de Agencia
               Identifíquese con el nombre de su agencia.
               Sus consultas se filtran automáticamente
               a los datos de esa agencia.
       2 — Agente NPN
               Identifíquese con su número NPN.
               Sus consultas se filtran a sus propios datos.

  2. CONSULTA EN LENGUAJE NATURAL
     Una vez identificado, escriba su consulta como si
     le hablara a una persona. El sistema:
       a) Detecta automáticamente el tema solicitado.
       b) Extrae filtros mencionados en su consulta.
       c) Le pide confirmación antes de ejecutar.
       d) Devuelve el resultado con una explicación.

  ══════════════════════════════════════════════════════
  TEMAS QUE PUEDE CONSULTAR
  ══════════════════════════════════════════════════════

  CONTRATOS
    · Cantidad y Detalle de Contratos (incluye status/estado y fecha de aprobación)
    · Cantidad y Detalle de Contratos Pendientes (incluye motivo del retraso)
    · Instructivo Gestión de Contratos
        (documentos: Humana, Caresource, Ascension, AmeriHealth, Ambetter, Alliant)
    · Licencias
    · Oportunidades de Contratación por Carrier

  PAGOS Y COMISIONES
    · Comisiones Pagadas
    · Comisiones Bloqueadas
    · Detalle Comisiones  
    · Pre-Liquidación de Comisiones
    · Detalle de Pre-Liquidación de Comisiones  
    · Frecuencia de Liquidación por Carrier
        (documento: Calendario Pago de Liquidaciones 2026)
    · Calendario de Pago de Comisiones a Agentes
        (documento: Calendario Pago de Comisiones 2026)
    · Estado del Contrato y Agente por Póliza
    · Porque no me han pagado mis comisiones  
    · Detalle de Reconciliaciones por Póliza  
    · Guía de Reconciliaciones de Comisiones
        (documento: Guía de Reconciliaciones de Comisiones)
    · Detalle de Comisiones en Avance  
    · Bonus Compensation ACA
        (documento: 2026 ACA Bonus Compensation)
    · Override Compensation ACA
        (documento: 2026 ACA Agency Override, general y por agencia)
    · Commission Compensation ACA
        (documento: 2026 ACA Agents Commission)

  DOCUMENTOS NORMATIVOS
    · Plazos Estándar de Envío de Contratos
        (documento: Envíos de Plazos Estándar de Contratos)
    · Horarios y Roles del Equipo
        (documento: Horario y Roles)
    · ARC Off-Exchanges FAQ
        (documento: ARC Off-Exchanges FAQ)
    · Claro Insurance FAQ
        (documento: Claro Insurance FAQ)



  ══════════════════════════════════════════════════════
  COMANDOS GLOBALES (disponibles en todo momento)
  ══════════════════════════════════════════════════════

  instrucciones / help / ayuda — muestra esta pantalla
  salir / exit / q             — cierra el sistema
  volver / back                — regresa al menú anterior
  nueva sesión                 — reinicia la identificación
""")
    _sep()


def _extraer_json(texto: str) -> dict:
    """Limpia bloques markdown que el LLM pueda agregar y parsea el JSON resultante."""
    limpio = re.sub(r"^```(?:json)?\s*", "", texto.strip(), flags=re.MULTILINE)
    limpio = re.sub(r"```\s*$", "", limpio, flags=re.MULTILINE)
    return json.loads(limpio.strip())


_PROMPT_SELECCIONAR_CASO_USO = """Eres el clasificador de intención del sistema de consultas de Claro Insurance.

RECURSO DE PRESENTACIÓN (quién eres y qué temas puede consultar el usuario):
{recurso_presentacion}

Tienes la siguiente lista de casos de uso disponibles. Cada uno tiene un nombre y una
descripción de "usa esto cuando" que indica exactamente en qué situación aplica:

{lista_casos}

Historial reciente de la conversación (puede estar vacío si es el primer turno):
{historial}

Nueva consulta del usuario: "{query}"

Tu tarea:
0. PASO PREVIO — Reescritura de contexto: antes de clasificar, decidí si la "Nueva
   consulta" necesita contexto del historial para entenderse. Reglas (en orden de
   prioridad):
   a. Si la consulta menciona explícitamente "por carrier", "todos los carriers", "por
      estado", "todos los agentes" u otra expresión de alcance general → usala
      EXACTAMENTE igual, sin ningún cambio. Esas expresiones indican que el usuario NO
      quiere heredar filtros del historial.
   b. Si la consulta es COMPLETA y tiene sentido por sí sola, o claramente cambia de
      tema → usala EXACTAMENTE igual, sin ningún cambio.
   c. Si la consulta es AMBIGUA, INCOMPLETA o es una extensión directa del turno
      anterior (añade un filtro, aclara algo, pregunta sobre lo mismo con un nuevo
      parámetro) → reescribila incorporando el contexto mínimo necesario del historial.
   IMPORTANTE: ante la duda, NO reescribas — es mejor usar la consulta original que
   añadir contexto incorrecto (ej. no arrastres un carrier/NPN/póliza de un turno
   anterior si la nueva consulta ya menciona su propio carrier/NPN/póliza explícito,
   aunque el turno anterior haya sido sobre otro tema). El resultado de este paso es
   "query_reescrita", y es la versión que debés usar para TODOS los pasos siguientes
   (clasificación, detección de meta, redacción del mensaje de confirmación) — no la
   consulta cruda.
1. Primero decide si la consulta es una pregunta CONVERSACIONAL/META sobre el propio
   asistente (ej. "quién eres", "qué eres", "qué puedes hacer", "cómo funcionas", "para
   qué sirves", "ayúdame", "no sé cómo preguntar", "dame instrucciones", "qué temas puedo
   consultar" sin más contexto) en vez de una solicitud real de negocio. Si es así, usa el
   RECURSO DE PRESENTACIÓN de arriba para construir tu respuesta — resúmelo o cita los
   temas más relevantes a lo que preguntó, en vez de inventar una respuesta genérica fuera
   de ese recurso. Reglas para redactar "mensaje_confirmacion" en este caso:
   - NO empieces con "Soy el/tu asistente de Claro Insurance" ni ninguna frase fija
     de apertura.
   - Si la consulta pregunta específicamente por tu identidad ("quién eres", "qué eres"),
     ahí sí podés presentarte brevemente, una sola vez.
   - Si la consulta ya pide algo concreto (ej. "qué temas puedo consultar", "dame
     instrucciones", "cómo pregunto", "qué puedes hacer", "en qué me puedes ayudar",
     "cómo funcionas"), ve DIRECTO al contenido pedido (lista de temas o ejemplos del
     recurso) sin reintroducirte ni repetir quién eres ni decir "Soy el/tu asistente de
     Claro Insurance" — estas son preguntas sobre capacidades/funcionalidad, NO sobre
     identidad, así que no requieren presentación.
   - Si el usuario indica que ya está confundido o insiste en no saber cómo formular su
     pregunta, sé más concreto citando 2 o 3 temas puntuales del recurso en vez de repetir
     una explicación general.
   - Si el usuario no proporciona suficiente información, pide más detalles de forma natural.
   - Si el usuario solicita Escalar a un Humano, redacta un mensaje que indique que dicha funcionalidad no se encuentra disponible todavía.
   - "es_meta_conversacional": true
   - "encontrado": false, "catalogo": null, "id": null
   - "mensaje_confirmacion": la respuesta basada en el recurso, NATURAL y DIRECTA (NO una
     pregunta de confirmación).
2. Si NO es una pregunta meta, elige el caso de uso (catalogo + id) cuya descripción
   "usa esto cuando" corresponda mejor a la intención de la consulta. Si ninguno
   corresponde con claridad, indica que no se encontró invitando a escalar a un humano para recibir asistencia adicional.
3. Si no es meta y encontraste un caso de uso, redacta un mensaje breve en español,
   natural y conversacional, que diga qué entendiste que el usuario quiere consultar.
   NO comiences con un saludo genérico (ej. "Hola", "¡Hola!") — ve directo al mensaje.
   Empieza nombrando de forma natural el caso de uso encontrado (parafraseando su
   "nombre", ej. "Detalle de Contratos" → "el detalle de tu contrato/contratos",
   "Cantidad de Contratos Pendientes" → "la cantidad de tus contratos pendientes"),
   y SOLO si el usuario escribió datos concretos en su consulta, continúa con
   "específicamente..." mencionando explícitamente ese carrier, estado, fecha, póliza,
   agencia, NPN u otro dato puntual. Si el usuario NO dio ningún dato adicional, no
   uses "específicamente" — deja el mensaje con solo el nombre del caso de uso, sin
   sonar redundante. IMPORTANTE: NO inventes ni asumas filtros, estados (ej. "activos",
   "inactivos", "pendientes") u otros datos que el usuario NO haya escrito
   explícitamente en su consulta — aunque la descripción "usa esto cuando" del caso de
   uso mencione palabras como "activos" o algún valor de ejemplo, eso es solo para
   ayudarte a clasificar la intención, NO son datos confirmados que debas repetirle al
   usuario. Si el usuario no especificó un filtro, el mensaje debe reflejar la
   solicitud de forma genérica (ej. "la cantidad total de tus contratos", sin agregar
   "activos"). Esa explicación SIEMPRE debe terminar EXACTAMENTE con esta línea, sin
   variarla ni reemplazarla por otra pregunta (ej. NO uses "¿es correcto?", "¿necesitas
   algún filtro específico?" ni variantes propias):
   "Por favor, confírmame si esto es correcto."
   Si no es meta y no encontraste un caso de uso claro, el mensaje debe decir que no
   pudiste identificar la solicitud y pedir más detalle (sin saludo genérico tampoco
   acá; en este caso NO uses la línea de
   confirmación anterior, ya que no hay nada que confirmar).

Responde ÚNICAMENTE con un JSON válido, sin markdown ni explicaciones, con este formato exacto:
{{"query_reescrita": "texto", "es_meta_conversacional": true_o_false, "encontrado": true_o_false, "catalogo": "A_o_B_o_C_o_null", "id": "id_o_null", "mensaje_confirmacion": "mensaje"}}""".strip()

_MENSAJE_SIN_CASO_DEFAULT = (
    "No logro identificar con claridad qué necesitas. "
    "¿Podrías darme un poco más de detalle sobre lo que quieres consultar?"
)


def seleccionar_caso_de_uso_llm(
    query: str,
    catalogos_permitidos: list,
    historial: list[dict] | None = None,
) -> tuple[dict | None, str, bool, str]:
    """
    Agente 1: en una sola llamada, (0) reescribe la consulta usando el historial si
    es necesario, (1) clasifica la consulta resultante contra los casos de uso
    permitidos usando su descripción "usa_esto_cuando", y (2) redacta el mensaje de
    confirmación. El LLM redacta el mensaje leyendo directamente la consulta (no se
    le pasan entidades pre-extraídas, ya que esas dependen del caso de uso que aún no
    se conoce).
    Retorna (use_case_entry, mensaje_confirmacion, es_meta_conversacional, query_reescrita).
    use_case_entry es None si no se identificó un caso de uso claro, si el LLM devolvió
    un id inexistente, o si la consulta es conversacional/meta sobre el propio asistente
    (en ese caso es_meta_conversacional es True y mensaje_confirmacion ya es la respuesta
    final, no una pregunta de confirmación). query_reescrita es la consulta original si
    no hubo historial o no hizo falta reescribirla.
    """
    candidatos_lineas = []
    for catalogo_key in catalogos_permitidos:
        catalogo = USE_CASES["options"].get(catalogo_key, {})
        for pregunta in catalogo.get("preguntas", []):
            usa_esto_cuando = pregunta.get("usa_esto_cuando", "")
            candidatos_lineas.append(
                f'- catalogo="{catalogo_key}" id="{pregunta["id"]}" nombre="{pregunta["texto"]}"\n'
                f'  usa_esto_cuando: {usa_esto_cuando}'
            )
    lista_casos = "\n".join(candidatos_lineas) if candidatos_lineas else "(sin casos de uso disponibles)"
    recurso_presentacion = _construir_recurso_presentacion(catalogos_permitidos)

    if historial:
        lineas_hist = []
        for msg in historial:
            rol = "Usuario" if msg["role"] == "user" else "Asistente"
            contenido = msg["content"][:200].replace("\n", " ")
            lineas_hist.append(f'{rol}: "{contenido}"')
        historial_str = "\n".join(lineas_hist)
    else:
        historial_str = "(sin historial — primer turno)"

    prompt = _PROMPT_SELECCIONAR_CASO_USO.format(
        recurso_presentacion=recurso_presentacion, lista_casos=lista_casos,
        historial=historial_str, query=query,
    )

    try:
        response = _llm_call(llm_model, prompt)
        resultado = _extraer_json(response.text)
    except Exception:
        return None, _MENSAJE_SIN_CASO_DEFAULT, False, query

    query_reescrita = resultado.get("query_reescrita") or query

    es_meta = bool(resultado.get("es_meta_conversacional", False))
    if es_meta:
        mensaje = resultado.get("mensaje_confirmacion") or (
            "¡Hola! Soy el asistente de Claro Insurance. Puedo ayudarte con consultas sobre "
            "contratos, comisiones/pagos y documentos normativos. ¿Qué te gustaría consultar?"
        )
        return None, mensaje, True, query_reescrita

    if not resultado.get("encontrado"):
        return None, resultado.get("mensaje_confirmacion") or _MENSAJE_SIN_CASO_DEFAULT, False, query_reescrita

    catalogo_key = resultado.get("catalogo")
    uc_id = resultado.get("id")
    catalogo = USE_CASES["options"].get(catalogo_key, {})
    preguntas_map = {p["id"]: p for p in catalogo.get("preguntas", [])}
    pregunta = preguntas_map.get(uc_id)

    if pregunta is None:
        # El LLM "alucinó" un catalogo/id inexistente — se trata como no encontrado.
        return None, _MENSAJE_SIN_CASO_DEFAULT, False, query_reescrita

    use_case_entry = {"nombre": pregunta["texto"], "pregunta": pregunta, "catalogo": catalogo_key}
    mensaje = resultado.get("mensaje_confirmacion") or f'Entiendo que quieres consultar: {pregunta["texto"]}. ¿Es correcto?'
    return use_case_entry, mensaje, False, query_reescrita


_PROMPT_INTERPRETAR_CONFIRMACION = """Eres un asistente que interpreta si un usuario confirmó o no una propuesta.

Se le mostró al usuario este mensaje de confirmación:
"{mensaje_confirmacion}"

El usuario respondió:
"{respuesta_usuario}"

Determina si el usuario confirmó que quiere proceder tal cual, o no. Si NO confirmó pero
su respuesta trae suficiente información para ajustar la consulta (una corrección, una
aclaración, un cambio de filtro), genera la consulta ajustada combinando la intención
original con su corrección. Si simplemente rechazó sin aclarar nada, deja query_ajustada en null.

Responde ÚNICAMENTE con un JSON válido, sin markdown ni explicaciones, con este formato exacto:
{{"confirmado": true_o_false, "query_ajustada": "texto_o_null"}}""".strip()


def _interpretar_confirmacion(mensaje_confirmacion: str, respuesta_usuario: str) -> dict:
    """
    Agente 2: decide si la respuesta libre del usuario confirma la propuesta del
    Agente 1 o no. Ante cualquier duda/error se resuelve como no confirmado
    (fail-closed), para evitar ejecutar una consulta que el usuario no aprobó.
    """
    prompt = _PROMPT_INTERPRETAR_CONFIRMACION.format(
        mensaje_confirmacion=mensaje_confirmacion, respuesta_usuario=respuesta_usuario
    )
    try:
        response = _llm_call(llm_confirm_model, prompt)
        resultado = _extraer_json(response.text)
        return {
            "confirmado": bool(resultado.get("confirmado", False)),
            "query_ajustada": resultado.get("query_ajustada") or None,
        }
    except Exception:
        return {"confirmado": False, "query_ajustada": None}


# ── Coincidencia semántica ─────────────────────────────────────────────────────
_embed_model = None

def _get_embed_model():
    global _embed_model
    if _embed_model is None:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _embed_model

def _buscar_semantico(
    query: str,
    candidatos: list,
    umbral: float,
    embs_precalc: list | None = None,
) -> tuple[str | None, float]:
    """Similitud coseno. Usa embeddings pre-computados si están disponibles."""
    if not candidatos:
        return None, 0.0
    from sentence_transformers import util
    model = _get_embed_model()
    q_emb = model.encode(query, convert_to_tensor=True)
    if embs_precalc is not None:
        c_emb = torch.tensor(embs_precalc, device=q_emb.device)
    else:
        c_emb = model.encode([str(c) for c in candidatos], convert_to_tensor=True)
    scores = util.cos_sim(q_emb, c_emb)[0]
    best_idx = int(scores.argmax())
    best_score = round(scores[best_idx].item(), 4)
    if best_score >= umbral:
        return str(candidatos[best_idx]), best_score
    return None, best_score

def _detectar_carrier_rag(
    query: str,
    carriers: list[str],
    umbral: float,
) -> tuple[str | None, float]:
    """
    Detecta el carrier mencionado en la query usando n-gramas (1-4 palabras).
    Retorna (carrier_match, score) o (None, best_score) si no supera el umbral.
    """
    if not carriers:
        return None, 0.0
    from sentence_transformers import util
    model = _get_embed_model()
    palabras = query.lower().split()
    ngrams = [
        " ".join(palabras[i: i + n])
        for n in range(1, 5)
        for i in range(len(palabras) - n + 1)
    ]
    if not ngrams:
        return None, 0.0
    ngram_embs = model.encode(ngrams, convert_to_tensor=True)
    carrier_embs = model.encode(carriers, convert_to_tensor=True)
    all_scores = util.cos_sim(ngram_embs, carrier_embs)
    best_per_carrier = all_scores.max(dim=0).values
    best_idx = int(best_per_carrier.argmax())
    best_score = round(best_per_carrier[best_idx].item(), 4)
    if best_score >= umbral:
        return carriers[best_idx], best_score
    return None, best_score


def _resolver_carrier_rag(
    user_query: str,
    carriers_soportados: list[str],
    umbral: float,
) -> tuple[str | None, str | None]:
    """
    Intenta matchear el carrier de la consulta contra los carriers indexados en la
    colección (carriers_soportados). Si no matchea ahí, pero la consulta sí menciona
    un carrier real del catálogo global (FILTROS_VALIDOS["carrier"]) que no está
    soportado por esta colección, retorna un mensaje de bloqueo — para evitar que el
    RAG caiga a una búsqueda sin filtro y responda con info de OTRO carrier como si
    fuera el solicitado. Esta es una red de seguridad determinística: el Agente 1
    (clasificador de casos de uso) ya intenta rechazar estas consultas vía
    "usa_esto_cuando", pero no es 100% confiable, así que esto actúa como respaldo.
    Retorna (carrier_match, mensaje_bloqueo); como máximo uno de los dos es no-None.
    """
    carrier_match, _ = _detectar_carrier_rag(user_query, carriers_soportados, umbral)
    if carrier_match:
        return carrier_match, None

    carriers_global = FILTROS_VALIDOS.get("carrier", [])
    carrier_no_soportado, _ = _detectar_carrier_rag(user_query, carriers_global, umbral)
    if carrier_no_soportado and carrier_no_soportado not in carriers_soportados:
        mensaje = (
            f"No tengo información sobre el proceso de contratación con {carrier_no_soportado}. "
            f"Actualmente solo cuento con instructivos de: {', '.join(sorted(carriers_soportados))}. "
            "Si deseas consultar este carrier, puedes escalar tu consulta a un humano."
        )
        return None, mensaje

    return None, None

# ── Parser de fechas en lenguaje natural ──────────────────────────────────────
def _parse_fecha_natural(texto: str) -> datetime.date | None:
    """
    Convierte expresiones de fecha en español al primer día del mes
    correspondiente (para usar con DATE_TRUNC ... MONTH en BigQuery).

    Soporta:
      hoy / este mes / mes actual            → mes en curso
      el mes pasado / mes anterior           → mes anterior
      hace N meses                           → N meses atrás
      mayo / el mes de mayo                  → mayo del año en curso
                                               (año anterior si mayo ya pasó)
      mayo 2024 / mayo del 2024              → mayo de ese año
    """
    hoy = datetime.date.today()
    t = texto.lower()

    # Mes en curso
    if any(k in t for k in ("hoy", "este mes", "mes actual", "mes en curso")):
        return hoy.replace(day=1)

    # Mes anterior
    if any(k in t for k in ("mes pasado", "mes anterior", "último mes", "ultimo mes")):
        if hoy.month == 1:
            return datetime.date(hoy.year - 1, 12, 1)
        return datetime.date(hoy.year, hoy.month - 1, 1)

    # "hace N meses"
    m = re.search(r"hace\s+(\d+)\s+mes", t)
    if m:
        n = int(m.group(1))
        mes = hoy.month - n
        year = hoy.year
        while mes <= 0:
            mes += 12
            year -= 1
        return datetime.date(year, mes, 1)

    # "mayo 2024" / "mayo del 2024" / "mayo de 2024"
    for nombre, num in _MESES_NUM.items():
        m = re.search(rf"{nombre}\s+(?:del?\s+)?(\d{{4}})", t)
        if m:
            return datetime.date(int(m.group(1)), num, 1)

    # Solo nombre del mes — año actual; si ya pasó, año anterior
    for nombre, num in _MESES_NUM.items():
        if re.search(rf"\b{nombre}\b", t):
            year = hoy.year
            if num > hoy.month:
                year -= 1
            return datetime.date(year, num, 1)

    return None

# ── Detección de entidades dinámicas ──────────────────────────────────────────
def extraer_entidades(
    texto: str,
    parametros: list[str],
    filtro_fijo_key: str | None,
) -> dict[str, tuple[str, float, str, str]]:
    """
    Descompone texto libre en n-gramas (1-4 palabras) y busca coincidencia
    semántica contra cada categoría de filtro aplicable.
    Retorna {param: (valor_match, score, label, sql_snippet)}.
    """
    from sentence_transformers import util

    palabras = texto.lower().split()
    ngrams = []
    for n in range(1, 5):
        for i in range(len(palabras) - n + 1):
            ngrams.append(" ".join(palabras[i : i + n]))

    if not ngrams:
        return {}

    model = _get_embed_model()
    ngram_embs = model.encode(ngrams, convert_to_tensor=True)

    detectados = {}
    for param in parametros:
        if param not in PARAM_TO_FILTRO:
            continue
        filtro_key, label, sql_tpl, umbral_entidad = PARAM_TO_FILTRO[param]

        if filtro_key == filtro_fijo_key:  # ya cubierto por el filtro de identificación
            continue

        # ── Rama fecha: parser de lenguaje natural, sin coseno ────────────────
        if filtro_key == "__fecha__":
            fecha = _parse_fecha_natural(texto)
            if fecha:
                val_str = fecha.strftime("%Y-%m-%d")
                label_fecha = f"{_MESES_ES[fecha.month - 1].capitalize()} {fecha.year}"
                sql_snippet = sql_tpl.replace("{v}", val_str)
                detectados[param] = (label_fecha, 1.0, label, sql_snippet)
            continue
        # ─────────────────────────────────────────────────────────────────────

        # ── Rama commission month: solo se activa si el usuario menciona el campo ──
        if filtro_key == "__commission_month__":
            _KW_COMMISSION_MONTH = r'commission[\s_-]*month|mes\s+de\s+comisi[oó]n(?:es)?'
            if re.search(_KW_COMMISSION_MONTH, texto, re.IGNORECASE):
                fecha = _parse_fecha_natural(texto)
                if fecha:
                    val_str = fecha.strftime("%Y-%m-%d")
                    label_fecha = f"{_MESES_ES[fecha.month - 1].capitalize()} {fecha.year}"
                    sql_snippet = sql_tpl.replace("{v}", val_str)
                    detectados[param] = (label_fecha, 1.0, label, sql_snippet)
            continue
        # ─────────────────────────────────────────────────────────────────────

        # ── Rama fecha de activación de licencia: solo si el usuario la menciona ──
        if filtro_key == "__license_activation_date__":
            _KW_LICENSE_ACTIVATION = r'activaci[oó]n|activad[ao]|activa\s+desde|fecha\s+de\s+activaci[oó]n'
            if re.search(_KW_LICENSE_ACTIVATION, texto, re.IGNORECASE):
                fecha = _parse_fecha_natural(texto)
                if fecha:
                    val_str = fecha.strftime("%Y-%m-%d")
                    label_fecha = f"{_MESES_ES[fecha.month - 1].capitalize()} {fecha.year}"
                    sql_snippet = sql_tpl.replace("{v}", val_str)
                    detectados[param] = (label_fecha, 1.0, label, sql_snippet)
            continue
        # ─────────────────────────────────────────────────────────────────────

        # ── Rama fecha de expiración de licencia: solo si el usuario la menciona ──
        if filtro_key == "__license_expiration_date__":
            _KW_LICENSE_EXPIRATION = r'expiraci[oó]n|expira|vence|vencimiento|fecha\s+de\s+expiraci[oó]n'
            if re.search(_KW_LICENSE_EXPIRATION, texto, re.IGNORECASE):
                fecha = _parse_fecha_natural(texto)
                if fecha:
                    val_str = fecha.strftime("%Y-%m-%d")
                    label_fecha = f"{_MESES_ES[fecha.month - 1].capitalize()} {fecha.year}"
                    sql_snippet = sql_tpl.replace("{v}", val_str)
                    detectados[param] = (label_fecha, 1.0, label, sql_snippet)
            continue
        # ─────────────────────────────────────────────────────────────────────

        # ── Rama fecha de actualización de licencia: solo si el usuario la menciona ──
        if filtro_key == "__license_date_updated__":
            _KW_LICENSE_DATE_UPDATED = r'actualizaci[oó]n|actualizada?|modificad[ao]|fecha\s+de\s+actualizaci[oó]n'
            if re.search(_KW_LICENSE_DATE_UPDATED, texto, re.IGNORECASE):
                fecha = _parse_fecha_natural(texto)
                if fecha:
                    val_str = fecha.strftime("%Y-%m-%d")
                    label_fecha = f"{_MESES_ES[fecha.month - 1].capitalize()} {fecha.year}"
                    sql_snippet = sql_tpl.replace("{v}", val_str)
                    detectados[param] = (label_fecha, 1.0, label, sql_snippet)
            continue
        # ─────────────────────────────────────────────────────────────────────

        # ── Rama ID de oportunidad: regex después de "id" ────────────────────────
        if filtro_key == "__opp_id__":
            match = re.search(
                r'\bid[:\s]+([A-Za-z0-9]{10,18})',
                texto, re.IGNORECASE
            )
            if match:
                valor = match.group(1)
                sql_snippet = sql_tpl.replace("{v}", valor.replace("'", "''"))
                detectados[param] = (valor, 1.0, label, sql_snippet)
            continue
        # ─────────────────────────────────────────────────────────────────────

        # ── Rama número de póliza: regex después de "poliza" / "póliza" ─────────
        if filtro_key == "__policy_number__":
            match = re.search(
                r'p[oó]li[zs]a\s+(?:n[úu]m(?:ero)?\.?\s*|#\s*)?([A-Za-z0-9][A-Za-z0-9\-/]*)',
                texto, re.IGNORECASE
            )
            # Un número de póliza real siempre tiene al menos un dígito — esto evita
            # falsos positivos cuando el usuario no da un número (ej. "esta póliza
            # este mes" capturaría "este" como si fuera el valor de la póliza).
            if match and any(ch.isdigit() for ch in match.group(1)):
                valor = match.group(1)
                sql_snippet = sql_tpl.replace("{v}", valor.replace("'", "''"))
                detectados[param] = (valor, 1.0, label, sql_snippet)
            continue
        # ─────────────────────────────────────────────────────────────────────

        # NPN: regex explícita si el usuario menciona "NPN XXXXXXX"; si no, embeddings con fallback numérico
        if filtro_key == "npn":
            npn_match = re.search(r'n\.?p\.?n\.?\s*[:#]?\s*(\d{5,10})', texto, re.IGNORECASE)
            if npn_match:
                valor = npn_match.group(1)
                sql_snippet = sql_tpl.replace("{v}", valor.replace("'", "''"))
                detectados[param] = (valor, 1.0, label, sql_snippet)
                continue
            if not re.search(r'\d{4,}', texto):
                continue

        candidatos = FILTROS_VALIDOS.get(filtro_key, [])
        if not candidatos:
            continue

        embs_pre = FILTROS_EMBEDDINGS.get(filtro_key, {}).get("embeddings")
        if embs_pre:
            c_emb = torch.tensor(embs_pre, device=ngram_embs.device)
        else:
            c_emb = model.encode([str(c) for c in candidatos], convert_to_tensor=True)

        # Matriz (n_ngrams × n_candidatos) → mejor score por candidato
        all_scores = util.cos_sim(ngram_embs, c_emb)
        best_per_cand = all_scores.max(dim=0).values
        best_cand_idx = int(best_per_cand.argmax())
        best_score = round(best_per_cand[best_cand_idx].item(), 4)

        if best_score >= umbral_entidad:
            valor = str(candidatos[best_cand_idx])
            sql_snippet = sql_tpl.replace("{v}", valor.replace("'", "''"))
            detectados[param] = (valor, best_score, label, sql_snippet)

    return detectados

# ── Identificación de usuario ──────────────────────────────────────────────────
def identificar_usuario() -> tuple[str, str, str | None, float | None]:
    """Retorna (tipo_key, nombre_tipo, valor_identificado | None, score | None)."""
    while True:
        print("🔐 IDENTIFICACIÓN DE USUARIO")
        _sep()
        print("Seleccione su tipo de usuario:")
        for k, v in TIPOS_USUARIO.items():
            print(f"  {k}. {v['nombre']}")
        print()
        opcion = _input("Opción: ").strip()

        if opcion not in TIPOS_USUARIO:
            print("⚠  Opción no válida. Intente nuevamente.\n")
            continue

        tipo = TIPOS_USUARIO[opcion]

        if tipo["filtro_key"] is None:
            return opcion, tipo["nombre"], None, None

        candidatos = FILTROS_VALIDOS.get(tipo["filtro_key"], [])
        embs_pre = FILTROS_EMBEDDINGS.get(tipo["filtro_key"], {}).get("embeddings")

        if not candidatos:
            print(f"⚠  Sin registros en filtro '{tipo['filtro_key']}'. Contacte al administrador.\n")
            continue

        while True:
            valor_input = _input(tipo["prompt_id"]).strip()
            if not valor_input:
                print("⚠  El campo no puede estar vacío.\n")
                continue

            match, score = _buscar_semantico(
                valor_input, candidatos, tipo["umbral"], embs_pre
            )

            if match:
                return opcion, tipo["nombre"], match, score

            print(
                f"⚠  Sin coincidencia para '{valor_input}' "
                f"(mejor similitud: {score:.2f}, umbral: {tipo['umbral']:.2f}).\n"
                "   Verifique e intente nuevamente.\n"
            )


# ── Construcción de SQL con LLM ───────────────────────────────────────────────
def _construir_sql_con_llm(
    pregunta: dict,
    filtro_fijo: str | None,
    entidades: dict,
    texto_usuario: str,
    error_previo: str | None = None,
    tipo_key: str = "1",
) -> str:
    """
    Llama a gemini-2.5-pro para construir el SQL final a partir de la plantilla
    base del use case, las entidades confirmadas y el texto libre del usuario.
    Si error_previo está definido, el LLM recibe el error para que lo corrija.
    """
    # El filtro fijo se inyecta directamente en el SQL base para garantizar
    # que siempre esté presente, independientemente de lo que genere el LLM.
    sql_base = pregunta["sql_by_role"][tipo_key].replace("{dynamic_filters}", filtro_fijo or "").strip()

    descripciones = pregunta.get("descripciones", {})

    entidades_lines = []
    for param, v in entidades.items():
        desc = descripciones.get(param, param)
        entidades_lines.append(f"  - {param} ({desc}): '{v[0]}'")
    entidades_str = "\n".join(entidades_lines) if entidades_lines else "  (ninguna)"

    # Catálogo completo de columnas para que el LLM use los nombres exactos
    # aunque el usuario mencione conceptos como "NPN", "carrier", "estado", etc.
    schema_lines = [f"  - {param}: {desc}" for param, desc in descripciones.items()]
    schema_str = "\n".join(schema_lines) if schema_lines else "  (ninguna)"

    filtro_fijo_bloque = (
        f"\nFILTRO OBLIGATORIO (ya incluido en la sintaxis base, no remover ni modificar):\n  {filtro_fijo}\n"
        if filtro_fijo else ""
    )

    solicitud_bloque = (
        f'\nSOLICITUD ESPECÍFICA DEL USUARIO:\n"{texto_usuario}"\n'
        if texto_usuario
        else "\nNo hay solicitud adicional. Ejecuta la consulta base con los filtros indicados.\n"
    )

    error_bloque = (
        f"\nINTENTO PREVIO FALLIDO — Error de BigQuery:\n{error_previo}\n"
        "Corrige el SQL para resolver este error exactamente.\n"
        if error_previo else ""
    )

    prompt = (
        "Eres un experto en BigQuery SQL para el sistema Claro Insurance.\n\n"
        f"OBJETIVO DE LA CONSULTA: {pregunta['texto']}\n\n"
        "Tu tarea es construir una consulta SQL válida para BigQuery que responda "
        "la solicitud específica del usuario, partiendo de la sintaxis base.\n\n"
        "1. SINTAXIS BASE (usa estos JOINs y aliases exactamente):\n"
        f"{sql_base}\n\n"
        "2. REFERENCIA DE COLUMNAS DISPONIBLES PARA FILTROS WHERE (usa SIEMPRE estos nombres exactos en WHERE):\n"
        f"{schema_str}\n\n"
        "3. ENTIDADES DETECTADAS (aplica como filtros WHERE adicionales):\n"
        f"{entidades_str}\n"
        f"{filtro_fijo_bloque}"
        f"{solicitud_bloque}"
        f"{error_bloque}"
        "\nREGLAS ESTRICTAS:\n"
        "- Los JOINs de la sintaxis base NO pueden removerse ni modificarse\n"
        "- Las columnas del SELECT de la sintaxis base NO pueden modificarse pero sí pueden ampliarse "
        "basado en la solicitud explícita del usuario, usando únicamente columnas de la sección "
        "REFERENCIA DE COLUMNAS DISPONIBLES. Si amplías el SELECT, agrega esas mismas columnas al GROUP BY\n"
        "- El GROUP BY de la sintaxis base NO puede modificarse ni eliminarse, pero sí puede ampliarse "
        "si se agregan columnas nuevas al SELECT\n"
        "- Si agregas GROUP BY, el campo de agrupación DEBE estar también en el SELECT\n"
        "- El filtro obligatorio del usuario SIEMPRE debe estar en el WHERE\n"
        "- Usa ÚNICAMENTE los nombres de columna de la sección REFERENCIA DE COLUMNAS\n"
        "- SOLO agrega condiciones WHERE adicionales basadas en la sección ENTIDADES DETECTADAS. "
        "La REFERENCIA DE COLUMNAS es solo para que sepas qué columnas existen y puedas ampliar el "
        "SELECT/GROUP BY si el usuario lo pide explícitamente — NO la uses para inferir o inventar "
        "filtros WHERE a partir de palabras sueltas de la SOLICITUD ESPECÍFICA que coincidan con el "
        "nombre o la descripción de una columna. Si un concepto de la solicitud (ej. 'pendientes', "
        "'activos') ya está implícito en el OBJETIVO DE LA CONSULTA o en la sintaxis base, NO agregues "
        "un filtro adicional para ese concepto a menos que esté explícitamente en ENTIDADES DETECTADAS\n"
        "- NUNCA uses LIKE, wildcards (%) ni comparaciones parciales para filtrar — usa siempre "
        "igualdad exacta (=) y únicamente con los valores tal cual aparecen en ENTIDADES DETECTADAS\n"
        "- Responde ÚNICAMENTE con el SQL listo para ejecutar en BigQuery\n"
        "- NO incluyas explicaciones, comentarios, markdown ni backticks\n"
        "- SOLO puedes generar consultas SELECT de solo lectura. "
        "Está TERMINANTEMENTE PROHIBIDO generar DELETE, INSERT, UPDATE, DROP, "
        "TRUNCATE, CREATE, ALTER, MERGE o cualquier operación de escritura o modificación de datos\n"
    )

    response = _llm_call(llm_sql_model, prompt)
    sql = response.text.strip()
    # Eliminar bloques markdown si el LLM los agrega de todas formas
    sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.MULTILINE)
    sql = re.sub(r"```\s*$", "", sql, flags=re.MULTILINE)
    return sql.strip()


# ── Ejecución de consulta SQL ──────────────────────────────────────────────────
def _formatear_filas(rows) -> str:
    data = [dict(row) for row in rows]
    if not data:
        return ""
    keys = list(data[0].keys())
    lines = [" | ".join(keys), "-" * len(" | ".join(keys))]
    for row in data:
        lines.append(" | ".join(str(row.get(k, "")) for k in keys))
    return "\n".join(lines)


def ejecutar_consulta(
    pregunta: dict,
    filtro_fijo: str | None,
    entidades: dict,
    texto_usuario: str,
    query_log=None,
    tipo_key: str = "1",
) -> str:
    if pregunta.get("tipo") != "sql" or "sql_by_role" not in pregunta:
        return (
            "⚙  Esta opción se encuentra en proceso de implementación. "
            "Gracias por su paciencia."
        )

    error_previo: str | None = None

    for intento in range(1, _MAX_REINTENTOS_SQL + 1):
        if query_log:
            query_log.sql_intentos = intento
        print()
        if intento == 1:
            print("🤖 Consultando el sistema..")
        else:
            print(f"🔄 Reintentando construcción SQL (intento {intento}/{_MAX_REINTENTOS_SQL})...")

        _t_sql = time.perf_counter()
        try:
            sql_final = _construir_sql_con_llm(
                pregunta, filtro_fijo, entidades, texto_usuario, error_previo, tipo_key
            )
        except Exception as exc:
            error_previo = str(exc)
            if query_log:
                query_log.sql_exitoso = False
                query_log.sql_error = str(exc)
                query_log.latencia_sql_ms = int((time.perf_counter() - _t_sql) * 1000)
            if intento == _MAX_REINTENTOS_SQL:
                return "❌ No disponible. No fue posible construir la consulta con el modelo de IA."
            continue

        if query_log:
            query_log.latencia_sql_ms = int((time.perf_counter() - _t_sql) * 1000)
            query_log.sql_generada = sql_final

        # ── Capa de seguridad: solo SELECT permitido ──────────────────────────
        _SQL_FORBIDDEN = re.compile(
            r'^\s*(DELETE|INSERT|UPDATE|DROP|TRUNCATE|CREATE|ALTER|MERGE|CALL)\b',
            re.IGNORECASE | re.MULTILINE,
        )
        if _SQL_FORBIDDEN.search(sql_final):
            return "❌ Operación no permitida. Solo se aceptan consultas de lectura (SELECT)."
        # ─────────────────────────────────────────────────────────────────────

        print()
        print("── Query enviada a BigQuery " + "─" * 27)
        print(sql_final)
        print("─" * 55)

        _t_bq = time.perf_counter()
        try:
            rows = list(client.query(sql_final).result())
        except Exception as exc:
            error_previo = str(exc)
            if query_log:
                query_log.bq_exitoso = False
                query_log.bq_error = str(exc)
                query_log.latencia_bq_ms = int((time.perf_counter() - _t_bq) * 1000)
            print(f"   ⚠  Error en BigQuery (intento {intento}/{_MAX_REINTENTOS_SQL}).")
            if intento == _MAX_REINTENTOS_SQL:
                return (
                    "❌ No disponible. La consulta no pudo ejecutarse correctamente.\n"
                    "Por favor intente reformular su solicitud."
                )
            continue

        if query_log:
            query_log.latencia_bq_ms = int((time.perf_counter() - _t_bq) * 1000)
            query_log.bq_exitoso = True
            query_log.bq_filas = len(rows)
            query_log.sql_exitoso = True

        if not rows:
            return "No se encontraron resultados para su consulta. Recuerde que puede escalar a un humano si desea asistencia adicional."

        _MAX_FILAS_PROMPT = 50
        filas_truncadas = len(rows) > _MAX_FILAS_PROMPT
        tabla = _formatear_filas(rows[:_MAX_FILAS_PROMPT])
        aviso_truncado = (
            f"\n[NOTA: Se muestran las primeras {_MAX_FILAS_PROMPT} filas de {len(rows)} totales. "
            "Indica al usuario que puede agregar filtros para acotar los resultados.]\n"
            if filas_truncadas else ""
        )
        solicitud_display = texto_usuario if texto_usuario else pregunta["texto"]
        entity_resolution = pregunta.get("entity_resolution", "")
        entity_resolution_bloque = (
            f"\nFORMATO ESPECÍFICO DE RESPUESTA PARA ESTE CASO DE USO:\n{entity_resolution}\n"
            if entity_resolution else ""
        )
        prompt = (
            f"Eres un asistente especializado de Claro Insurance.\n"
            f"El usuario solicitó: \"{solicitud_display}\".\n\n"
            f"Datos obtenidos de la base de datos:\n{tabla}{aviso_truncado}\n\n"
            f"INSTRUCCIONES DE RESPUESTA:\n"
            f"- NO comiences con un saludo genérico (ej. 'Hola', '¡Hola!'). Ve directo al "
            f"resultado de forma profesional, sin frase de apertura fija.\n"
            f"- Si los datos tienen UNA sola fila con totales o conteos globales: preséntala como un número resumen.\n"
            f"- Si los datos tienen MÚLTIPLES filas agrupadas (por carrier, agencia, estado, etc.): "
            f"muestra CADA fila con su categoría y valor, en formato de lista o tabla simple. "
            f"No colapses el desglose en un solo total.\n"
            f"- Si los datos son registros individuales (IDs, contratos, oportunidades): "
            f"NO los enumeres uno por uno. Solo indica cuántos se encontraron "
            f"y ofrece al usuario explorar detalles específicos si lo desea.\n"
            f"- Tutea siempre al usuario, usa 'tú' en lugar de 'usted'.\n"
            f"- Al final agrega estas dos líneas exactas, en este orden:\n"
            f"  '{pregunta.get('ending_resolution', 'Te invito a realizar otra pregunta para seguir explorando.')}'\n"
            f"  'Recuerda que si tu consulta no fue efectiva, puedes escribir \"escalar a un humano\"'.\n"
            f"- NO uses cierres formales como 'Atentamente' ni firmas de ningún tipo.\n"
            f"- Responde en español de forma clara y concisa.\n"
            f"{entity_resolution_bloque}"
        )

        _t_resp = time.perf_counter()
        try:
            response = _llm_call(llm_model, prompt)
            if query_log:
                query_log.latencia_respuesta_ms = int((time.perf_counter() - _t_resp) * 1000)
                query_log.llm_respuesta = response.text
            return response.text
        except Exception as exc:
            return f"❌ Error al generar la respuesta con el modelo:\n{exc}"

    return "❌ No disponible. No fue posible completar la consulta."


# ── RAG: consulta sobre documentos normativos ──────────────────────────────────
_CHROMA_PATH = os.getenv("CHROMA_PATH", "./chroma_db")
_BOX_W = 57


def _box_line(text: str) -> str:
    return f"│ {text:<{_BOX_W - 2}} │"


def _aplicar_filtro_agencia(
    pregunta_config: dict,
    base_filter: dict | None,
    agency_name: str | None,
) -> dict | None:
    """
    Si el caso de uso requiere filtro de agencia, resuelve el valor 'agency' a usar:
    - Si la agencia autenticada está en 'agency_values' (agencias con
      documento propio para este caso de uso), se usa ese valor exacto.
    - En cualquier otro caso se usa "general".
    """
    if not pregunta_config.get("agency_filter"):
        return base_filter

    agencia_filtro = "general"
    if agency_name in pregunta_config.get("agency_values", []):
        agencia_filtro = agency_name

    clausula = {"agency": agencia_filtro}
    return {"$and": [base_filter, clausula]} if base_filter else clausula


def ejecutar_rag(
    pregunta_config: dict,
    user_query: str,
    query_log=None,
    agency_name: str | None = None,
    tipo_key: str = "1",
) -> str:
    coleccion_nombre = pregunta_config.get("coleccion_chroma", "documentos_normativos")
    carrier_detection = pregunta_config.get("carrier_detection", False)
    carrier_umbral = pregunta_config.get("carrier_detection_umbral", 0.55)

    if query_log:
        query_log.rag_coleccion = coleccion_nombre

    _t_rag = time.perf_counter()
    try:
        chroma_client = ChromaClient(path=_CHROMA_PATH)
        collection = chroma_client.get_collection(coleccion_nombre)
    except Exception as exc:
        print(f"❌ No se pudo conectar a ChromaDB: {exc}")
        return ""

    model = _get_embed_model()
    border = "─" * _BOX_W

    print()
    query_embedding = model.encode(user_query).tolist()

    where_filter = pregunta_config.get("metadata_filter") or None

    if carrier_detection:
        all_meta = collection.get(include=["metadatas"])
        carriers = list({m["carrier"] for m in all_meta["metadatas"] if "carrier" in m})
        if carriers:
            carrier_match, mensaje_bloqueo = _resolver_carrier_rag(user_query, carriers, carrier_umbral)
            if mensaje_bloqueo:
                print(mensaje_bloqueo)
                return mensaje_bloqueo
            if carrier_match:
                where_filter = {"carrier": carrier_match}
                if query_log:
                    query_log.rag_carrier = carrier_match

    where_filter = _aplicar_filtro_agencia(pregunta_config, where_filter, agency_name)

    try:
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=5,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:
        print(f"❌ Error al consultar ChromaDB: {exc}")
        return ""

    if query_log:
        query_log.latencia_rag_ms = int((time.perf_counter() - _t_rag) * 1000)

    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    if query_log:
        query_log.rag_documentos = len(docs)

    print()
    if not docs:
        print("   No se encontraron documentos relacionados con tu consulta. Recuerda que puedes escalar a un humano si deseas asistencia adicional.")
        return ""
    """
    print(f"📄 RESULTADOS ENCONTRADOS (Top {len(docs)}):")
    print()
    for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
        sim_pct = max(0.0, (1.0 - (dist ** 2) / 2.0)) * 100
        filename = meta.get("filename", "Documento")
        page_num = meta.get("page") or meta.get("page_number")
        fuente = f"{filename} (pág. {page_num})" if page_num else filename

        preview = doc.replace("\n", " ").strip()
        if len(preview) > 420:
            preview = preview[:420] + "..."

        print(f"┌{border}┐")
        print(_box_line(f"📌 DOCUMENTO {i}  (Similitud: {sim_pct:.1f}%)"))
        print(_box_line(f"Fuente: {fuente}"))
        print(_box_line(""))
        for line in textwrap.wrap(f'"{preview}"', width=_BOX_W - 2):
            print(_box_line(line))
        print(f"└{border}┘")
        print()
    """

    contexto_completo = "\n\n---\n\n".join(docs)
    entity_resolution = pregunta_config.get("entity_resolution", "")
    entity_resolution_bloque = (
        f"\n\nFORMATO ESPECÍFICO DE RESPUESTA PARA ESTE CASO DE USO:\n{entity_resolution}"
        if entity_resolution else ""
    )
    prompt = (
        f"Eres un asistente especializado de Claro Insurance.\n"
        f"El usuario preguntó: \"{user_query}\"\n\n"
        f"Basándote ÚNICAMENTE en los siguientes fragmentos de documentos:\n\n"
        f"{contexto_completo}\n\n"
        f"Responde directamente la pregunta en español de forma clara y concisa. "
        f"NO comiences con un saludo genérico (ej. 'Hola', '¡Hola!') — ve directo a la respuesta. "
        f"Tutea siempre al usuario, usa 'tú' en lugar de 'usted'. "
        f"Si la información no está en los documentos, indícalo. "
        f"NO uses cierres formales como 'Atentamente' ni firmas. "
        f"Al final agrega estas dos líneas exactas, en este orden: "
        f"'{pregunta_config.get('ending_resolution', 'Te invitamos a realizar otra pregunta para seguir explorando.')}' "
        f"'Recuerda que si tu consulta no fue efectiva, puedes escribir \"escalar a un humano\"'."
        f"{entity_resolution_bloque}"
    )
    #print("RESPUESTA:")
    _sep()
    _t_resp = time.perf_counter()
    try:
        response = _llm_call(llm_model, prompt)
        if query_log:
            query_log.latencia_respuesta_ms = int((time.perf_counter() - _t_resp) * 1000)
            query_log.llm_respuesta = response.text
        texto_respuesta = response.text.strip()
        print(texto_respuesta)
        return texto_respuesta
    except Exception as exc:
        print(f"❌ Error al generar respuesta: {exc}")
        return ""


# ── Ejecución silenciosa para flujos múltiples ────────────────────────────────
def _ejecutar_consulta_silenciosa(
    pregunta: dict,
    filtro_fijo: str | None,
    entidades: dict,
    texto_usuario: str,
    tipo_key: str = "1",
) -> dict:
    """Ejecuta SQL y retorna dict con resultado. No imprime nada en pantalla."""
    result: dict = {"tipo": "sql", "nombre": pregunta["texto"], "sql": None, "tabla": None, "error": None}
    error_previo: str | None = None

    for intento in range(1, _MAX_REINTENTOS_SQL + 1):
        try:
            sql_final = _construir_sql_con_llm(
                pregunta, filtro_fijo, entidades, texto_usuario, error_previo, tipo_key
            )
            result["sql"] = sql_final
        except Exception as exc:
            error_previo = str(exc)
            if intento == _MAX_REINTENTOS_SQL:
                result["error"] = "No fue posible construir la consulta SQL."
            continue

        try:
            rows = list(client.query(sql_final).result())
        except Exception as exc:
            error_previo = str(exc)
            if intento == _MAX_REINTENTOS_SQL:
                result["error"] = "La consulta SQL no pudo ejecutarse correctamente."
            continue

        result["tabla"] = _formatear_filas(rows) if rows else ""
        return result

    return result


def _ejecutar_rag_silenciosa(
    pregunta_config: dict,
    user_query: str,
    agency_name: str | None = None,
    tipo_key: str = "1",
) -> dict:
    """Consulta ChromaDB y retorna dict con documentos. No imprime nada en pantalla."""
    result: dict = {"tipo": "rag", "nombre": pregunta_config["texto"], "documentos": [], "error": None}

    coleccion_nombre = pregunta_config.get("coleccion_chroma", "documentos_normativos")
    carrier_detection = pregunta_config.get("carrier_detection", False)
    carrier_umbral = pregunta_config.get("carrier_detection_umbral", 0.55)

    try:
        chroma_client = ChromaClient(path=_CHROMA_PATH)
        collection = chroma_client.get_collection(coleccion_nombre)
    except Exception as exc:
        result["error"] = str(exc)
        return result

    model = _get_embed_model()
    query_embedding = model.encode(user_query).tolist()

    where_filter = pregunta_config.get("metadata_filter") or None

    if carrier_detection:
        all_meta = collection.get(include=["metadatas"])
        carriers = list({m["carrier"] for m in all_meta["metadatas"] if "carrier" in m})
        if carriers:
            carrier_match, mensaje_bloqueo = _resolver_carrier_rag(user_query, carriers, carrier_umbral)
            if mensaje_bloqueo:
                result["error"] = mensaje_bloqueo
                return result
            if carrier_match:
                where_filter = {"carrier": carrier_match}

    where_filter = _aplicar_filtro_agencia(pregunta_config, where_filter, agency_name)

    try:
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=3,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as exc:
        result["error"] = str(exc)
        return result

    result["documentos"] = results.get("documents", [[]])[0]
    return result


def _sintetizar_respuestas_multiples(
    user_query: str,
    resultados: dict,
    entity_resolution: str = "",
    cierre: str = "",
) -> str:
    """Recibe los resultados de todos los sub-casos y los unifica en una sola respuesta."""
    bloques = []
    for nombre, res in resultados.items():
        if res.get("error"):
            bloques.append(f"--- {nombre} ---\nNo disponible: {res['error']}")
            continue

        descripciones = res.get("descripciones", {})
        schema_str = (
            "\nDescripción de columnas:\n"
            + "\n".join(f"  - {col}: {desc}" for col, desc in descripciones.items())
            if descripciones else ""
        )

        if res["tipo"] == "sql":
            tabla = res.get("tabla")
            if tabla:
                bloques.append(f"--- {nombre} ---{schema_str}\n{tabla}")
            else:
                bloques.append(f"--- {nombre} ---\nNo se encontraron registros.")
        elif res["tipo"] == "rag":
            docs = res.get("documentos", [])
            if docs:
                bloques.append(f"--- {nombre} ---\n" + "\n\n".join(docs))
            else:
                bloques.append(f"--- {nombre} ---\nNo se encontraron documentos relacionados.")

    contexto = "\n\n".join(bloques)
    entity_resolution_bloque = (
        f"REGLA PRINCIPAL PARA ESTA CONSULTA (tiene prioridad sobre las instrucciones generales):\n{entity_resolution}\n\n"
        if entity_resolution else ""
    )
    prompt = (
        f"Eres un asistente especializado de Claro Insurance.\n"
        f"El usuario preguntó: \"{user_query}\"\n\n"
        f"{entity_resolution_bloque}"
        f"Para responder de forma completa se ejecutaron {len(resultados)} consultas. "
        f"Aquí están los resultados:\n\n"
        f"{contexto}\n\n"
        f"INSTRUCCIONES GENERALES:\n"
        f"- NO comiences con un saludo genérico (ej. 'Hola', '¡Hola!') — ve directo a la respuesta.\n"
        f"- Sintetiza TODA la información en UNA SOLA respuesta clara, directa y en español.\n"
        f"- No repitas los encabezados de sección (---).\n"
        f"- Usa la descripción de columnas para interpretar correctamente cada valor de la tabla.\n"
        f"- Presenta los datos de forma cohesiva como si fuera una sola explicación.\n"
        f"- Si alguna consulta no arrojó resultados, menciónalo brevemente.\n"
        f"- Tutea siempre al usuario, usa 'tú' en lugar de 'usted'.\n"
        f"- NO uses cierres formales como 'Atentamente' ni firmas de ningún tipo.\n"
        f"- Al final agrega estas dos líneas exactas, en este orden:\n"
        f"  '{cierre or 'Te invitamos a realizar otra pregunta para seguir explorando.'}'\n"
        f"  'Recuerda que si tu consulta no fue efectiva, puedes escribir \"escalar a un humano\"'.\n"
    )
    try:
        response = _llm_call(llm_model, prompt)
        return response.text.strip()
    except Exception as exc:
        return f"❌ Error al generar la respuesta: {exc}"


def ejecutar_multiple(
    pregunta_multiple: dict,
    catalogo_key: str,
    sql_filtro: str | None,
    filtro_fijo_key: str | None,
    user_query: str,
    entidades_previas: dict | None = None,
    tipo_key: str = "1",
    agency_name: str | None = None,
) -> str:
    """Ejecuta en paralelo todos los sub-casos declarados en 'invoca' y sintetiza la respuesta."""
    invoca_ids = pregunta_multiple.get("invoca", [])
    if not invoca_ids:
        print("⚠  Este flujo no tiene sub-consultas configuradas.")
        return ""

    catalogo = USE_CASES["options"].get(catalogo_key, {})
    preguntas_map = {p["id"]: p for p in catalogo.get("preguntas", [])}
    sub_preguntas = [preguntas_map[i] for i in invoca_ids if i in preguntas_map]

    if not sub_preguntas:
        print("⚠  No se encontraron las sub-consultas referenciadas.")
        return ""

    # Construir entidades por sub-caso: cada uno usa solo sus propios parametros
    entidades_combinadas: dict = entidades_previas or {}
    if not entidades_combinadas:
        params_all = list(dict.fromkeys(
            p for sp in sub_preguntas if sp.get("tipo") == "sql"
            for p in sp.get("parametros", [])
        ))
        if params_all:
            entidades_combinadas = extraer_entidades(user_query, params_all, filtro_fijo_key)

    def _entidades_para(sp: dict) -> dict:
        sp_params = set(sp.get("parametros", []))
        return {p: v for p, v in entidades_combinadas.items() if p in sp_params}

    print()
    #print(f"  Ejecutando {len(sub_preguntas)} consultas en paralelo...")

    resultados: dict = {}
    with ThreadPoolExecutor(max_workers=len(sub_preguntas)) as executor:
        futures: dict = {}
        for sp in sub_preguntas:
            tipo_sp = sp.get("tipo")
            if tipo_sp == "sql":
                fut = executor.submit(
                    _ejecutar_consulta_silenciosa, sp, sql_filtro, _entidades_para(sp), user_query, tipo_key
                )
            elif tipo_sp == "rag":
                fut = executor.submit(_ejecutar_rag_silenciosa, sp, user_query, agency_name, tipo_key)
            else:
                continue
            futures[fut] = sp

        for fut in as_completed(futures):
            sp = futures[fut]
            try:
                res = fut.result()
                res["descripciones"] = sp.get("descripciones", {})
                resultados[sp["texto"]] = res
            except Exception as exc:
                resultados[sp["texto"]] = {
                    "tipo": sp.get("tipo", "unknown"),
                    "nombre": sp["texto"],
                    "error": str(exc),
                    "descripciones": sp.get("descripciones", {}),
                }

    # Mostrar SQLs generados una vez que todos los threads terminaron
    for nombre, res in resultados.items():
        if res["tipo"] == "sql" and res.get("sql"):
            print()
            print(f"── Query [{nombre}] " + "─" * 30)
            print(res["sql"])
            print("─" * 55)

    respuesta = _sintetizar_respuestas_multiples(
        user_query,
        resultados,
        entity_resolution=pregunta_multiple.get("entity_resolution", ""),
        cierre=pregunta_multiple.get("cierre", ""),
    )
    print()
    print("RESULTADO:")
    _sep()
    print(respuesta)
    return respuesta


# ── Ciclo principal de consultas ───────────────────────────────────────────────
def ciclo_consultas(
    catalogos: list[str],
    sql_filtro: str | None,
    filtro_fijo_key: str | None,
    tipo_key: str = "1",
    agency_name: str | None = None,
):
    _next_query: str | None = None
    _historial_reciente: list[dict] = []
    while True:
        if _next_query is not None:
            user_query = _next_query
            _next_query = None
        else:
            _sep()
            print()
            print("¿Qué desea consultar hoy?")
            print()
            user_query = _input("  Su consulta: ").strip()
            if not user_query:
                print("  Por favor ingrese una consulta.")
                continue

        q = get_session().nueva_consulta()
        q.query_original = user_query

        print()
        print("  Analizando su consulta...")

        # ── Agente 1 + Agente 2: identificación del caso de uso y confirmación ────
        # El Agente 1 reescribe la consulta usando el último intercambio (pregunta +
        # respuesta real) en la misma llamada en que clasifica — solo en el primer
        # intento de cada turno; el historial entre turnos ya no aplica en los
        # reintentos de confirmación dentro del mismo turno (ahí la "query_ajustada"
        # ya es autosuficiente). Itera indefinidamente, afinando la clasificación con
        # cada respuesta del usuario, hasta que confirme explícitamente un caso de
        # uso. No hay límite de reintentos: el usuario sale del loop solo
        # confirmando, o con un comando global (salir, volver, nueva sesión),
        # manejado por _input().
        query_clasificar = user_query
        user_query_efectiva = user_query
        intentos_confirmacion = 0
        primera_iteracion = True

        es_turno_meta = False

        while True:
            historial_para_llm = _historial_reciente[-2:] if primera_iteracion else None
            _t = time.perf_counter()
            use_case_entry, mensaje_confirmacion, es_meta, query_reescrita = seleccionar_caso_de_uso_llm(
                query_clasificar, catalogos, historial=historial_para_llm
            )
            q.latencia_deteccion_ms = int((time.perf_counter() - _t) * 1000)
            q.caso_exitoso = use_case_entry is not None
            q.confirmacion_mensaje = mensaje_confirmacion

            # Siempre se actualiza con el resultado más reciente — si el usuario corrige
            # o aclara durante el loop de confirmación, esa corrección debe reflejarse
            # en la extracción de entidades de más abajo, no quedarse con la primera
            # interpretación del turno.
            user_query_efectiva = query_reescrita
            primera_iteracion = False

            if use_case_entry is not None:
                q.caso_nombre = use_case_entry["nombre"]
                q.caso_catalogo = use_case_entry.get("catalogo")

            print()
            print(f"  {mensaje_confirmacion}")

            if es_meta:
                # Pregunta conversacional sobre el propio asistente (quién eres, qué
                # puedes hacer, etc.): el mensaje ya es la respuesta final, no hay nada
                # que confirmar ni ejecutar.
                es_turno_meta = True
                break

            q.reiniciar_timer()  # excluir el tiempo de espera del usuario al responder
            respuesta_usuario = _input("  Su respuesta: ").strip()
            if not respuesta_usuario:
                continue  # vuelve a mostrar la misma propuesta y pide respuesta de nuevo

            interpretacion = _interpretar_confirmacion(mensaje_confirmacion, respuesta_usuario)
            q.confirmado = interpretacion["confirmado"]
            intentos_confirmacion += 1
            q.intentos_confirmacion = intentos_confirmacion

            if interpretacion["confirmado"] and use_case_entry is not None:
                break  # caso de uso confirmado, se sale del loop para ejecutar

            # No confirmó: usa el ajuste sugerido o, si no hay, su respuesta libre
            # como nueva consulta a clasificar, y vuelve a intentar.
            query_clasificar = interpretacion["query_ajustada"] or respuesta_usuario

        if es_turno_meta:
            _historial_reciente.append({"role": "user", "content": user_query_efectiva})
            _historial_reciente.append({"role": "assistant", "content": mensaje_confirmacion[:300]})
            _historial_reciente = _historial_reciente[-4:]
            get_session().cerrar_consulta()
            continue

        pregunta = use_case_entry["pregunta"]

        # Pre-detectar entidades (solo para SQL individual).
        # Usa la query reescrita para capturar entidades de follow-ups ambiguos
        # (ej. "Y los de Florida?" → reescrita añade el carrier del turno anterior).
        tipo_pregunta = pregunta.get("tipo")
        entidades_previas = {}
        if tipo_pregunta == "sql":
            _t = time.perf_counter()
            entidades_previas = extraer_entidades(
                user_query_efectiva, pregunta.get("parametros", []), filtro_fijo_key
            )
            q.latencia_entidades_ms = int((time.perf_counter() - _t) * 1000)
            q.entidades = [
                {"param": p, "label": v[2], "valor": v[0], "score": v[1]}
                for p, v in entidades_previas.items()
            ]

        if tipo_pregunta == "multiple":
            catalogo_actual = USE_CASES["options"].get(use_case_entry["catalogo"], {})
            preguntas_map_actual = {p["id"]: p for p in catalogo_actual.get("preguntas", [])}
            invoca_nombres = [
                preguntas_map_actual[i]["texto"]
                for i in pregunta.get("invoca", [])
                if i in preguntas_map_actual
            ]
            #print(f"  Se ejecutarán {len(invoca_nombres)} consultas en paralelo:")
            #for n in invoca_nombres:
            #    print(f"      • {n}")
            # Extraer entidades por sub-caso y mostrar agrupadas
            sub_pqs = [preguntas_map_actual[i] for i in pregunta.get("invoca", []) if i in preguntas_map_actual]
            entidades_por_subcaso: dict = {}
            for sp in sub_pqs:
                if sp.get("tipo") == "sql" and sp.get("parametros"):
                    entidades_por_subcaso[sp["texto"]] = extraer_entidades(
                        user_query_efectiva, sp["parametros"], filtro_fijo_key
                    )
            #print()
            #for nombre_sp, ents in entidades_por_subcaso.items():
            #    print(f"  Entidades detectadas [{nombre_sp}]:")
            #    if ents:
            #        for _, (val, sc_e, lbl, _) in ents.items():
            #            print(f"      • {lbl:<20} → {val}  (confianza: {sc_e:.2f})")
            #    else:
            #        print(f"      (ninguna)")
            # Fusionar para ejecución (sin duplicados, el primero gana)
            entidades_previas = {}
            for ents in entidades_por_subcaso.values():
                for param, val in ents.items():
                    if param not in entidades_previas:
                        entidades_previas[param] = val
            if entidades_previas:
                q.entidades = [
                    {"param": p, "label": v[2], "valor": v[0], "score": v[1]}
                    for p, v in entidades_previas.items()
                ]
        #elif entidades_previas:
        #    print("  Con las siguientes entidades:")
        #    for _, (val, sc_e, lbl, _) in entidades_previas.items():
        #        print(f"      • {lbl:<20} → {val}  (confianza: {sc_e:.2f})")
        #else:
        #    print("  Sin ninguna entidad detectada.")

        q.tipo_ejecucion = tipo_pregunta

        try:
            # ── Validación de filtros obligatorios ────────────────────────────
            # Si falta un dato obligatorio para este caso de uso, no hay nada que
            # confirmar: se le pide directamente al usuario y se reinicia el
            # pipeline completo (Agente 1 vuelve a clasificar) con la nueva consulta.
            filtros_req_pre = pregunta.get("filtros_requeridos", [])
            if filtros_req_pre and tipo_pregunta != "rag" and not all(p in entidades_previas for p in filtros_req_pre):
                labels_req_pre = [PARAM_TO_FILTRO[p][1] for p in filtros_req_pre if p in PARAM_TO_FILTRO]
                lbl_slash_pre = "/".join(labels_req_pre)
                print()
                print(f"  Esta consulta requiere identificar los siguientes filtros/entidades ({lbl_slash_pre}),")
                print("  por favor reformule su pregunta considerando dichos filtros")
                print()
                nueva_query = _input("  Su consulta: ").strip()
                if not nueva_query:
                    raise VoverError
                _next_query = nueva_query
                get_session().cerrar_consulta()
                continue  # reinicia el pipeline completo con la nueva consulta
            # ─────────────────────────────────────────────────────────────────

            # Reiniciar timer: excluir el tiempo que el usuario tardó en confirmar
            q.reiniciar_timer()

            if tipo_pregunta == "multiple":
                print()
                print("  Procesando su consulta, por favor espere...")
                respuesta_mostrada = ejecutar_multiple(
                    pregunta, use_case_entry["catalogo"], sql_filtro, filtro_fijo_key, user_query_efectiva,
                    entidades_previas, tipo_key=tipo_key, agency_name=agency_name,
                )
            elif tipo_pregunta == "rag":
                respuesta_mostrada = ejecutar_rag(
                    pregunta, user_query_efectiva, query_log=q, agency_name=agency_name, tipo_key=tipo_key
                )
            else:
                print()
                print("  Procesando su consulta, por favor espere...")
                respuesta_mostrada = ejecutar_consulta(
                    pregunta, sql_filtro, entidades_previas, user_query_efectiva, query_log=q, tipo_key=tipo_key
                )
                print()
                print("RESULTADO:")
                _sep()
                print(respuesta_mostrada)

            _historial_reciente.append({"role": "user", "content": user_query_efectiva})
            _historial_reciente.append({"role": "assistant", "content": (respuesta_mostrada or "")[:300]})
            _historial_reciente = _historial_reciente[-4:]
        except VoverError:
            print()
            print("  Volviendo al menú principal...")
            print()
            get_session().cerrar_consulta()
            continue

        get_session().cerrar_consulta()

        print()
        siguiente = _input("¿Hay algo más en lo que pueda ayudarte? ").strip()
        if not siguiente or siguiente.lower() in _PALABRAS_NO:
            print()
            print("Gracias por utilizar el Sistema de Consultas IA. ¡Hasta pronto!")
            break
        _next_query = siguiente


# ── Punto de entrada ───────────────────────────────────────────────────────────
def main():
    _header()
    nueva_sesion()
    try:
        while True:
            try:
                tipo_key, nombre_tipo, valor_id, score = identificar_usuario()

                get_session().registrar_identificacion(nombre_tipo, valor_id, score)

                tipo = TIPOS_USUARIO[tipo_key]
                sql_filtro = None
                if tipo["sql_filtro"] and valor_id:
                    valor_seguro = valor_id.replace("'", "''")
                    sql_filtro = tipo["sql_filtro"].replace("{valor}", valor_seguro)

                # Resolver agencia del usuario (para filtros de RAG por agencia)
                agency_name = None
                if tipo_key == "1":
                    agency_name = valor_id
                elif tipo_key == "2" and valor_id:
                    valor_seguro = valor_id.replace("'", "''")
                    rows = list(client.query(f"""
                        SELECT a.Name_Agencies
                        FROM `claroinsurance-dataplatform.salesforce_claro.contact` c
                        LEFT JOIN `claroinsurance-dataplatform.claro_bi.dim_account_2` a ON a.Id = c.AccountId
                        WHERE c.NPN__c = '{valor_seguro}'
                        LIMIT 1
                    """).result())
                    agency_name = rows[0]["Name_Agencies"] if rows else None

                print()
                if valor_id and score is not None:
                    if tipo_key == "2" and agency_name:
                        print(
                            f'✅ Bienvenido, el sistema ha reconocido tu ingreso "NPN:{valor_id}", '
                            f'"Agencia:{agency_name}", nivel de coincidencia {score:.2f}'
                        )
                    else:
                        print(
                            f'✅ Bienvenido, el sistema ha reconocido tu ingreso "{valor_id}", '
                            f"nivel de coincidencia {score:.2f}"
                        )
                else:
                    print(f"✅ Bienvenido, {nombre_tipo}")

                _perms = _CATALOG_PERMS.get(tipo_key)
                _catalogos = [c for c in tipo["catalogos"] if c in _perms] if _perms else tipo["catalogos"]
                ciclo_consultas(_catalogos, sql_filtro, tipo["filtro_key"], tipo_key, agency_name)
                break

            except MenuError:
                print()
                print("  Volviendo a la identificación de usuario...")
                print()
                continue

    except SalirError:
        print()
        print("Sesión finalizada. ¡Hasta pronto!")
        sys.exit(0)

if __name__ == "__main__":
    main()

# Cómo funciona `main.py` — Guía de estudio

Este documento explica, de punta a punta, cómo está armado `main.py`: qué hace
cada bloque, en qué orden se ejecutan las cosas, y cómo se conectan entre sí.
La idea es que sirva como mapa mental antes de leer el código línea por línea.

---

## 1. Visión general — ¿qué es este programa?

Es un **asistente conversacional por consola (CLI)** para Claro Insurance.
Un usuario se identifica (agencia / agente / management), escribe preguntas en
lenguaje natural sobre contratos, comisiones o documentos normativos, y el
sistema:

1. Entiende qué quiere ("clasifica" la intención contra una lista de casos de
   uso predefinidos en `data/use_cases.json`).
2. Le confirma al usuario qué entendió.
3. Si es una pregunta de datos: construye SQL con un LLM, lo ejecuta contra
   BigQuery, y redacta la respuesta con otro LLM.
4. Si es una pregunta normativa: busca en una base vectorial (ChromaDB / RAG)
   y redacta la respuesta con el LLM basada en esos documentos.
5. Registra cada interacción en logs (`config/logger.py`) para trazabilidad.

Hay **3 motores de IA en juego**, cada uno con su propio rol:

| Modelo / mecanismo                | Para qué se usa |
|---|---|
| `llm_model` (`gemini-2.5-flash-lite`) | Clasificar intención, redactar confirmaciones, interpretar respuestas del usuario, redactar la respuesta final |
| `llm_sql_model` (`gemini-2.5-pro`)    | Construir el SQL final que se ejecuta en BigQuery |
| `SentenceTransformer` (`all-MiniLM-L6-v2`) | Coincidencia semántica: detectar agencia/NPN/carrier/estado/etc. dentro del texto libre del usuario (sin LLM, por similitud coseno) |

---

## 2. Mapa del archivo (de arriba hacia abajo)

```
main.py
├── Configuración / constantes globales
│   ├── _LLM_MAX_RETRIES, _llm_call()        → retry/backoff ante rate limit (429)
│   ├── _MESES_NUM / _MESES_ES               → parser de fechas en español
│   ├── USE_CASES (data/use_cases.json)      → catálogo de "qué se puede preguntar"
│   ├── TIPOS_USUARIO                        → 1=Agencia, 2=NPN
│   ├── PARAM_TO_FILTRO                       → mapa columna SQL → cómo detectarla en texto libre
│   └── Excepciones de control: SalirError / VoverError / MenuError
│
├── Helpers de UI por consola
│   ├── _input(), _input_sn()                 → leer texto, interceptando comandos globales
│   └── _header(), _mostrar_instrucciones()
│
├── Memoria conversacional
│   └── reescribir_consulta()                 → LLM reescribe la pregunta usando el historial
│
├── Identificación de intención (Agente 1 + Agente 2)
│   ├── seleccionar_caso_de_uso_llm()         → Agente 1: ¿qué caso de uso es esto?
│   └── _interpretar_confirmacion()           → Agente 2: ¿el usuario confirmó o corrigió?
│
├── Detección de entidades (sin LLM, por embeddings)
│   ├── _buscar_semantico() / _detectar_carrier_rag()
│   ├── _parse_fecha_natural()
│   └── extraer_entidades()                   → extrae NPN, carrier, estado, fechas, pólizas, etc.
│
├── Identificación de usuario
│   ├── identificar_usuario()
│   └── seleccionar_catalogo() / seleccionar_pregunta()  (modo manual, sin IA)
│
├── Motor SQL
│   ├── _construir_sql_con_llm()              → LLM arma el SQL final
│   └── ejecutar_consulta()                   → ejecuta en BigQuery + redacta respuesta
│
├── Motor RAG (documentos normativos)
│   ├── _aplicar_filtro_agencia()
│   └── ejecutar_rag()                        → busca en ChromaDB + redacta respuesta
│
├── Ejecución de múltiples sub-casos en paralelo
│   ├── _ejecutar_consulta_silenciosa() / _ejecutar_rag_silenciosa()
│   ├── _sintetizar_respuestas_multiples()
│   └── ejecutar_multiple()
│
├── ciclo_consultas()                          → el loop principal de conversación
└── main()                                     → punto de entrada: login + loop de sesión
```

---

## 3. El flujo completo, turno por turno

### Paso 0 — Arranque (`main()`)

```python
_header()
nueva_sesion()                 # crea un SessionLogger nuevo (un UUID de sesión)
tipo_key, nombre_tipo, valor_id, score = identificar_usuario()
```

`identificar_usuario()` pide el tipo de usuario (1/2/3) y, si aplica, valida
el nombre de agencia o el NPN contra `FILTROS_VALIDOS` usando similitud
coseno (`_buscar_semantico`). Esto determina:

- `sql_filtro`: el `WHERE` fijo que se va a inyectar SIEMPRE en cualquier SQL
  generado (ej. `AND a.Name_Agencies = 'Comfort Insurance'`).
- `filtro_fijo_key`: qué dimensión ya está "cubierta" por la identificación,
  para no volver a pedirla como filtro dinámico.

Luego entra a `ciclo_consultas(...)`, que es donde vive toda la conversación.

### Paso 1 — El usuario escribe una consulta

Dentro de `ciclo_consultas()` ([main.py:1559](../main.py#L1559)):

```python
user_query = _input("  Su consulta: ").strip()
```

`_input()` ([main.py:151](../main.py#L151)) no es un simple `input()`: intercepta
comandos globales (`salir`, `volver`, `nueva sesión`, `instrucciones`,
`escalar a un humano`, saludos) ANTES de devolver el texto. Si detecta uno,
lanza una excepción (`SalirError`, `VoverError`, `MenuError`) o imprime algo y
vuelve a pedir input recursivamente.

### Paso 2 — Reescritura con memoria (`reescribir_consulta`)

```python
user_query_efectiva = reescribir_consulta(_historial_reciente[-2:], user_query)
```

Le pasa al LLM los últimos 2 elementos del historial (la pregunta anterior +
la respuesta real que se le mostró al usuario) más la consulta nueva. El
prompt (`_PROMPT_REESCRIBIR`, [main.py:367](../main.py#L367)) le pide al LLM:

- Si la consulta ya es autosuficiente → devolverla intacta.
- Si es ambigua o depende del turno anterior (ej. "y los de Florida?") →
  reescribirla incorporando el contexto que falta.

Esto es lo que permite preguntas de seguimiento como "¿y por carrier?" sin
que el usuario tenga que repetir todo.

### Paso 3 — Clasificación de intención (Agente 1) + Confirmación (Agente 2)

Este es el corazón del sistema, un loop `while True` ([main.py:1596](../main.py#L1596)):

```
┌─────────────────────────────────────────────────────────┐
│  query_clasificar = user_query_efectiva                  │
│                                                            │
│  while True:                                              │
│      1. seleccionar_caso_de_uso_llm(query_clasificar)     │
│         → ¿cuál de los casos de USE_CASES calza mejor?    │
│         → genera un mensaje de confirmación en lenguaje   │
│           natural ("Entiendo que quieres... ¿correcto?")  │
│                                                            │
│      2. se le muestra el mensaje al usuario                │
│         respuesta_usuario = _input("Su respuesta: ")       │
│                                                            │
│      3. _interpretar_confirmacion(mensaje, respuesta)       │
│         → ¿confirmó? ¿corrigió algo? ¿rechazó sin más?     │
│                                                            │
│      4. si confirmó            → break (sale a ejecutar)   │
│         si no confirmó         → usa la corrección o la    │
│                                   respuesta libre como      │
│                                   nueva query_clasificar    │
│                                   y vuelve a clasificar     │
└─────────────────────────────────────────────────────────┘
```

**Importante**: este loop no tiene límite de reintentos — sigue afinando la
clasificación con cada respuesta del usuario hasta que confirme
explícitamente, o hasta que use un comando global (`salir`, `volver`, etc.)
manejado por `_input()`.

`seleccionar_caso_de_uso_llm()` ([main.py:440](../main.py#L440)) le da al LLM
**toda la lista de casos de uso permitidos** (según el tipo de usuario, vía
`catalogos_permitidos`) junto con su campo `usa_esto_cuando` (la descripción
que dice "usa este caso cuando..."), y le pide que elija el mejor calce y
redacte el mensaje de confirmación citando explícitamente cualquier
carrier/estado/fecha/póliza que el usuario haya mencionado.

`_interpretar_confirmacion()` ([main.py:489](../main.py#L489)) es **fail-closed**:
ante cualquier duda o error, asume que NO confirmó, para evitar ejecutar algo
que el usuario no aprobó.

### Paso 4 — Extracción de entidades (sin LLM)

Una vez identificado el caso de uso (`pregunta = use_case_entry["pregunta"]`),
se extraen los filtros mencionados en el texto con `extraer_entidades()`
([main.py:650](../main.py#L650)). Esto **no usa un LLM** — usa:

- **N-gramas + similitud coseno** contra listas de valores válidos
  (`FILTROS_VALIDOS`, precalculados en `config/__init__.py` desde
  `data/filtros_validos.pkl` / `filtros_embeddings.pkl`) para cosas como
  carrier, estado, agencia, etapa, etc. Cada filtro tiene su propio umbral de
  confianza en `PARAM_TO_FILTRO` ([main.py:97](../main.py#L97)) — más estricto
  para NPN (0.99) que para carrier (0.85), por ejemplo.
- **Regex** para casos estructurados: número de póliza, ID de oportunidad,
  NPN explícito ("NPN 1234567").
- **Parser de fechas en español** (`_parse_fecha_natural`,
  [main.py:595](../main.py#L595)) para "el mes pasado", "mayo 2024", "hace 2
  meses", etc.

El resultado es un diccionario `{columna_sql: (valor, score, label, snippet_sql)}`
que después se usa tanto para mostrarle al usuario qué se detectó como para
construir el SQL.

Si el caso de uso tiene `filtros_requeridos` (ej. detalle de comisiones
necesita sí o sí una póliza) y no se detectó ninguno, no se ejecuta nada — se
le pide directamente al usuario que reformule, y se reinicia el pipeline
completo desde el Paso 1 con la nueva consulta.

### Paso 5 — Ejecución (3 caminos posibles)

Según `pregunta["tipo"]` en `data/use_cases.json`:

**a) `"sql"` → `ejecutar_consulta()`** ([main.py:1024](../main.py#L1024))
1. `_construir_sql_con_llm()` ([main.py:927](../main.py#L927)): le pasa al
   `llm_sql_model` la plantilla SQL base del caso de uso
   (`sql_by_role[tipo_key]`), las entidades detectadas, el filtro fijo del
   usuario, y el texto libre — el LLM amplía el `SELECT`/`WHERE`/`GROUP BY`
   respetando reglas estrictas (no tocar los JOINs, solo `SELECT`, etc.).
2. Capa de seguridad: regex que bloquea cualquier cosa que no sea `SELECT`
   (`_SQL_FORBIDDEN`).
3. Se ejecuta en BigQuery (`client.query(sql_final)`). Si falla, reintenta
   hasta `_MAX_REINTENTOS_SQL` veces, pasándole el error al LLM para que lo
   corrija.
4. Con los resultados (tabla truncada a 80 filas), `llm_model` redacta la
   respuesta final en lenguaje natural, siguiendo instrucciones de formato
   (totales vs. desglose vs. listados).

**b) `"rag"` → `ejecutar_rag()`** ([main.py:1189](../main.py#L1189))
1. Si el caso tiene `carrier_detection`, detecta el carrier mencionado por
   n-gramas + embeddings (`_detectar_carrier_rag`).
2. Aplica filtro de agencia si corresponde (`_aplicar_filtro_agencia`).
3. Hace `collection.query()` contra ChromaDB (top 5 documentos por similitud
   vectorial).
4. `llm_model` redacta la respuesta basándose ÚNICAMENTE en esos fragmentos.

**c) `"multiple"` → `ejecutar_multiple()`** ([main.py:1465](../main.py#L1465))
Para preguntas que necesitan combinar varios sub-casos (ej. "status completo
de mi contrato" = varias sub-consultas SQL + RAG a la vez):
1. Ejecuta todos los sub-casos **en paralelo** con `ThreadPoolExecutor`,
   usando versiones "silenciosas" que no imprimen nada
   (`_ejecutar_consulta_silenciosa`, `_ejecutar_rag_silenciosa`).
2. `_sintetizar_respuestas_multiples()` junta todos los resultados en un solo
   prompt y le pide al LLM una respuesta unificada y coherente.

### Paso 6 — Memoria hacia el siguiente turno

Después de mostrar la respuesta, se guarda en `_historial_reciente`:

```python
_historial_reciente.append({"role": "user", "content": user_query_efectiva})
_historial_reciente.append({"role": "assistant", "content": (respuesta_mostrada or "")[:300]})
_historial_reciente = _historial_reciente[-4:]   # ventana de 4 elementos (2 turnos)
```

Esto es lo que el Paso 2 del siguiente turno usa para entender contexto.

### Paso 7 — Loop

```python
siguiente = _input("¿Hay algo más en lo que pueda ayudarte? ").strip()
if not siguiente or siguiente in _PALABRAS_NO:
    break        # termina la sesión
_next_query = siguiente   # vuelve al Paso 1 sin re-mostrar "¿Qué desea consultar hoy?"
```

---

## 4. Las piezas de datos externas que main.py consume

| Archivo / fuente | Para qué |
|---|---|
| `data/use_cases.json` | Catálogo de catálogos (A/B/C) → preguntas → plantillas SQL por rol, parámetros, descripciones, mensajes de cierre |
| `data/catalog_permissions.json` | Qué catálogos puede ver cada tipo de usuario (1/2/3) |
| `data/filtros_validos.pkl` | Listas de valores reales (carriers, estados, agencias, etc.) contra las que se hace matching semántico |
| `data/filtros_embeddings.pkl` | Embeddings precalculados de esas listas (evita recalcularlos en cada consulta) |
| `chroma_db/` | Base vectorial de documentos normativos (PDFs indexados) para RAG |
| `config/__init__.py` | Inicializa Vertex AI, los modelos LLM, el cliente de BigQuery, y carga los pickles de filtros |
| `config/logger.py` | `SessionLogger` / `QueryLog`: registra cada consulta en `logs/AAAA-MM-DD.jsonl` y de forma asíncrona en BigQuery (`claro_IA.model_tracking`) |

---

## 5. Las 4 "capas de IA" que actúan en cada consulta

1. **Reescritura** (`reescribir_consulta`) — resuelve ambigüedad de contexto.
2. **Clasificación** (`seleccionar_caso_de_uso_llm`) — decide QUÉ caso de uso es.
3. **Confirmación** (`_interpretar_confirmacion`) — valida que el usuario esté de acuerdo antes de tocar datos.
4. **Construcción de SQL** (`_construir_sql_con_llm`) y **Redacción de respuesta** (dentro de `ejecutar_consulta`/`ejecutar_rag`/`_sintetizar_respuestas_multiples`) — ya con la intención confirmada, genera la consulta y la respuesta final.

La extracción de entidades (carrier, estado, NPN, fechas, pólizas) es la
única pieza que **no** usa LLM — es puramente embeddings + regex, lo cual la
hace rápida y barata, pero también la razón por la que sus umbrales de
confianza (`PARAM_TO_FILTRO`) son tan importantes: si están mal calibrados,
un filtro se aplica de forma equivocada (falso positivo) o no se detecta
(falso negativo).

---

## 6. Manejo de errores y comandos globales

- `SalirError` / `VoverError` / `MenuError`: excepciones de control lanzadas
  desde `_input()` cuando el usuario escribe `salir`, `volver` o `nueva
  sesión`. `main()` atrapa `SalirError` (termina el programa) y `MenuError`
  (vuelve a pedir identificación). `VoverError` se atrapa dentro de
  `ciclo_consultas()` para volver al prompt de consulta sin cerrar sesión.
- `_llm_call()` ([main.py:16](../main.py#L16)): wrapper con retry/backoff
  exponencial (5s, 10s, 20s) para cualquier llamada a un modelo Gemini,
  específicamente para errores 429 (rate limit).
- Capa de seguridad SQL: cualquier SQL generado que empiece con
  `DELETE/INSERT/UPDATE/DROP/TRUNCATE/CREATE/ALTER/MERGE/CALL` se bloquea
  antes de llegar a BigQuery.

---

## 7. Glosario rápido de variables clave dentro de `ciclo_consultas`

| Variable | Qué contiene |
|---|---|
| `user_query` | Lo que el usuario escribió tal cual |
| `user_query_efectiva` | La consulta después de pasar por `reescribir_consulta` (puede ser igual a `user_query`) |
| `query_clasificar` | La consulta que se le pasa a `seleccionar_caso_de_uso_llm` en cada vuelta del loop de confirmación (se va actualizando si el usuario corrige) |
| `use_case_entry` | `{"nombre", "pregunta", "catalogo"}` — el caso de uso identificado, una vez confirmado |
| `entidades_previas` | Filtros detectados en el texto (carrier, estado, NPN, fechas...) |
| `_historial_reciente` | Lista de hasta 4 mensajes (2 turnos: user+assistant) usada como memoria conversacional |
| `q` (`QueryLog`) | Objeto donde se acumulan todas las métricas de la consulta actual para el log/BigQuery |

---

## 8. Sugerencia de orden de lectura

Si vas a leer el código fuente directamente, este orden tiene más sentido
pedagógico que el orden físico del archivo:

1. `main()` y `identificar_usuario()` — cómo arranca todo.
2. `ciclo_consultas()` — el loop principal (es el "director de orquesta").
3. `reescribir_consulta()` y `seleccionar_caso_de_uso_llm()` /
   `_interpretar_confirmacion()` — cómo se entiende la intención.
4. `extraer_entidades()` y sus ramas (regex, fechas, NPN) — cómo se detectan filtros.
5. `ejecutar_consulta()` / `_construir_sql_con_llm()` — el camino SQL.
6. `ejecutar_rag()` — el camino de documentos.
7. `ejecutar_multiple()` y las versiones "silenciosas" — el camino combinado.
8. `config/logger.py` — cómo se registra todo (al final, es lo menos crítico para entender el comportamiento del asistente).

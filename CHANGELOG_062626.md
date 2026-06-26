# Changelog — 2026-06-26

Sesión de fixes y ajustes sobre `main.py`, `data/use_cases.json` y `config/__init__.py`,
posteriores al rediseño documentado en `CHANGELOG.md`. Cubre bugs de contexto entre
turnos, alucinaciones de SQL/RAG, eliminación del rol Management, y ajustes de
latencia/formato de respuesta.

---

## 1. Modelos LLM (`config/__init__.py`)

| Variable | Antes | Ahora | Por qué |
|---|---|---|---|
| `llm_model` | `gemini-2.5-flash-lite` | `gemini-2.5-flash` | Flash-Lite era poco confiable clasificando casos de uso (fallos de reconocimiento documentados en la sesión). Flash mejoró notablemente sin el costo de latencia de Pro. |
| `llm_sql_model` | `gemini-2.5-pro` | probamos `gemini-2.5-flash` → **revertido a `gemini-2.5-pro`** | Con Flash, la construcción de SQL violó reglas explícitas (modificó el SELECT base, inventó columnas no autorizadas) y no fue más rápida que Pro en la práctica. Se mantiene Pro solo para esta tarea, la única con restricciones estructurales estrictas (JOINs fijos, SELECT parcialmente fijo, anti-alucinación). |
| `llm_confirm_model` | (no existía, usaba `llm_model`) | `gemini-2.5-flash-lite` (nuevo) | El Agente 2 (interpretar confirmación) es una tarea simple de clasificación sí/no/corrección — no necesita el modelo completo. |

---

## 2. Fusión de reescritura de contexto + clasificación en una sola llamada

**Antes:** `reescribir_consulta()` (una llamada LLM) seguida de `seleccionar_caso_de_uso_llm()`
(otra llamada LLM) — dos llamadas secuenciales en cada turno con historial.

**Ahora:** `seleccionar_caso_de_uso_llm()` recibe un parámetro `historial` opcional y hace
ambas tareas en una sola llamada: reescribe la consulta (Paso 0, con las mismas 3 reglas
que tenía `reescribir_consulta`) y clasifica, devolviendo `query_reescrita` como cuarto
elemento de la tupla de retorno (antes devolvía 3 elementos, ahora 4).

- `_PROMPT_SELECCIONAR_CASO_USO`: agregado bloque `{historial}` y "PASO PREVIO" con las
  reglas de reescritura; el JSON de salida ahora incluye `"query_reescrita"`.
- Eliminados `reescribir_consulta()` y `_PROMPT_REESCRIBIR` (ya sin uso).
- En `ciclo_consultas()`, el historial solo se pasa en la **primera** iteración del loop
  de confirmación de cada turno (los reintentos de corrección dentro del mismo turno no
  necesitan reescritura basada en historial, la corrección del usuario ya es autosuficiente).

**Ahorro:** una llamada LLM completa menos por cada turno de seguimiento (todos excepto el
primero de la sesión).

---

## 3. Dos bugs de "contexto pegado" entre turnos (`user_query_efectiva`)

Estos fueron los bugs más críticos detectados en la sesión — ambos causaban que filtros
o carriers de turnos **anteriores** se filtraran a consultas nuevas que no los mencionaban.

### 3.1 `user_query_efectiva` no se actualizaba tras una corrección del usuario

En `ciclo_consultas()`, la asignación `user_query_efectiva = query_reescrita` estaba
adentro de un `if primera_iteracion:` — solo se ejecutaba en la primera vuelta del loop
de confirmación. Si el usuario corregía algo después ("no, sin esos filtros"), el loop
reclasificaba correctamente pero la corrección se perdía: `user_query_efectiva` seguía
siendo la del primer intento.

**Fix:** se quitó el `if`, la asignación ahora es incondicional en cada vuelta del loop.

### 3.2 Las funciones de ejecución usaban `user_query` (crudo) en vez de `user_query_efectiva`

Aun con el fix anterior, `ejecutar_multiple()`, `ejecutar_rag()` y `ejecutar_consulta()`
seguían recibiendo `user_query` — el texto literal escrito al **inicio** del turno — en
vez de `user_query_efectiva` (la versión corregida/reescrita más reciente). Esto causaba,
por ejemplo, que tras preguntar por un carrier no soportado y luego corregir a uno
soportado, el guard de carrier en RAG (`_resolver_carrier_rag`) siguiera bloqueando con el
carrier viejo, porque analizaba el texto original sin la corrección.

**Fix:** en `ciclo_consultas()`, los 3 call sites de ejecución (y la extracción de
entidades de sub-casos `multiple`) pasan ahora `user_query_efectiva` en vez de `user_query`.

---

## 4. Reglas anti-alucinación en construcción de SQL (`_construir_sql_con_llm`)

Agregadas dos reglas estrictas al prompt:
- Solo se permiten condiciones WHERE basadas en `ENTIDADES DETECTADAS` — la
  `REFERENCIA DE COLUMNAS` es solo informativa, no autoriza inferir filtros de palabras
  sueltas de la solicitud (ej. "pendientes" ya implícito en la sintaxis base no debe
  generar un filtro adicional como `StageName LIKE '%Pending%'`).
- Prohibido el uso de `LIKE`/wildcards — siempre igualdad exacta con valores de
  `ENTIDADES DETECTADAS`.

---

## 5. Guard de carrier no soportado en RAG (`_resolver_carrier_rag`)

Nuevo, en `ejecutar_rag()` y `_ejecutar_rag_silenciosa()`. Si la consulta menciona un
carrier real (`FILTROS_VALIDOS["carrier"]`) que no está indexado en la colección
(ej. "Oscar" preguntando por el instructivo de gestión de contratos, que solo cubre 6
carriers), corta determinísticamente **antes** de buscar en ChromaDB con un mensaje
explícito — evita que una búsqueda sin filtro devuelva contenido de OTRO carrier y el
LLM lo presente como si fuera el solicitado. Se intentó primero resolver esto solo con
una regla de prompt en `usa_esto_cuando` (que el Agente 1 rechazara el caso si el carrier
no es soportado), pero resultó no ser 100% confiable — el guard determinístico quedó como
respaldo.

---

## 6. Eliminación del rol "Management" (3 roles → 2)

- `TIPOS_USUARIO`: eliminado el bloque `"3"` (Management). Quedan solo `"1"` (Agencia) y
  `"2"` (NPN).
- Todos los defaults `tipo_key: str = "3"` en funciones (`_construir_sql_con_llm`,
  `ejecutar_consulta`, `ejecutar_rag`, etc.) cambiados a `"1"`.
- `data/catalog_permissions.json`: eliminada la entrada `"3"`.
- `_aplicar_filtro_agencia()`: eliminado el parámetro `tipo_key` (quedó sin uso al
  quitar el chequeo `tipo_key in ("1","2")`, redundante con solo 2 roles).
- Texto de `_mostrar_instrucciones()` y `_TEMAS_POR_CATALOGO` actualizados para no
  mencionar más la opción 3 / Management.

---

## 7. `PARAM_TO_FILTRO` — fixes de columnas mal mapeadas

| Antes | Ahora | Motivo |
|---|---|---|
| `p.Sub_stage__c` | `p.Sub_Stage__c` | Mismatch de mayúsculas que lo hacía inalcanzable (nunca matcheaba con `use_cases.json`). |
| (no existía) | `nc.Status__c` → `contract_status` | Faltaba mapeo para el estado de contrato en catálogo A. |
| `l.Status__c` | `l.LicenseStatus__c` | La columna real en la query de Licencias es `LicenseStatus__c`, no `Status__c` (causaba error de BigQuery "Unrecognized name"). |
| `l.LicenseState__c` | `o.Name` | `LicenseState__c` en la tabla cruda es un ID de Salesforce; el nombre de estado legible solo existe vía el JOIN a `States__c` (`o.Name`). Filtrar contra el ID nunca habría matcheado un valor de texto. |
| `p.Id`, `ae.Name` | _(eliminados)_ | Dead code — ningún caso de uso los declaraba en `parametros`. |

Se agregó también la generación de candidatos para `contract_status` en
`scripts/precalcular_filtros.py` (requiere re-ejecutar el script para poblar
`filtros_validos.pkl`).

---

## 8. Regex de número de póliza — exige al menos un dígito

En `extraer_entidades()`, la rama `__policy_number__` aceptaba cualquier palabra después
de "póliza" como si fuera el número (ej. "esta póliza **este** mes" capturaba "este").
Eso satisfacía `filtros_requeridos` con un valor inválido y dejaba pasar la ejecución sin
pedir el dato real. Ahora se exige que el valor capturado contenga al menos un dígito.

---

## 9. `data/use_cases.json` — Catálogo A consolidado (15 → 7 casos)

| Antes | Ahora |
|---|---|
| Cantidad de Contratos Activos + Cantidad de Contratos Inactivos | **Cantidad de Contratos** (estado es filtro opcional, ya no asume "activos" por defecto) |
| Detalle de Contratos Activos + Inactivos + Detalle General | **Detalle de Contratos** |
| Identificar motivo del retraso (caso separado) | Absorbido en **Detalle de Contratos Pendientes** |
| Instructivo ACA / Medicare / Life / Supplementario (4 casos) | **Instructivo Gestión de Contratos** (1 caso, colección `instructivos_contratos_arc`, 6 carriers: Humana, Caresource, Ascension, AmeriHealth, Ambetter, Alliant) |
| Oferta de Productos de Claro | _(eliminado)_ |

Catálogos B y C quedaron con el mismo inventario.

---

## 10. Reglas de formato de respuesta — "Cantidad"/"Opp_Open" + anti-tabla-cruda

Aplicado a **Detalle de Contratos** (id A-2), **Detalle de Contratos Pendientes** (id A-4)
y **Licencias** (id A-6):

- Se agregó `COUNT(*) as Cantidad` (o `COUNT(DISTINCT opportunity_id) as Opp_Open` en
  Pendientes) al SELECT — antes el `entity_resolution` pedía un desglose por grupo que no
  existía como columna real en la query.
- `entity_resolution` reescrito con una regla de máxima prioridad: el LLM **no puede**
  copiar/pegar la tabla cruda de datos en su respuesta, y **no puede** sumar/combinar
  `Cantidad`/`Opp_Open` entre filas distintas (eso causaba que la redacción tardara hasta
  53s tratando de hacer aritmética manual sobre 80 filas). Ahora describe cada fila con su
  propio valor, sin agregaciones extra — bajó la latencia de redacción de ~53s a ~10s en
  pruebas con volumen similar.
- "Detalle de Contratos Pendientes": se restauraron columnas que habían quedado fuera del
  SELECT (`State`, `StageName`, `Sub_Stage__c`, `Internal_Notes__c`, `NPN__c`) aunque
  `entity_resolution`/`parametros`/`ending_resolution` ya las daban por hecho.
- "Instructivo Gestión de Contratos": agregado `entity_resolution` (no existía) para que,
  si el usuario no menciona ningún carrier, el sistema pregunte cuál en vez de responder
  con contenido de un carrier al azar de los fragmentos recuperados.

---

## 11. Instrucciones y recurso de presentación — documentos reales cargados

`_TEMAS_POR_CATALOGO` y `_mostrar_instrucciones()` (la pantalla de "instrucciones") se
sincronizaron con el inventario real de `data/use_cases.json` y
`scripts/precalcular_embeddings.py`:
- Quitadas referencias a casos que ya no existen ("Oferta de Productos", "Validación
  para generación de contrato").
- Agregados los 4 casos de catálogo B que faltaban (Detalle de Comisiones en Avance,
  Bonus/Override/Commission Compensation ACA).
- Agregado el nombre de los documentos PDF reales que respaldan cada tema RAG (ej.
  "Instructivo Gestión de Contratos (documentos: Humana, Caresource, Ascension,
  AmeriHealth, Ambetter, Alliant)").

---

## 12. Saludos repetidos en cada respuesta

`ejecutar_consulta()` tenía una instrucción explícita ("Saluda brevemente...") que hacía
que **toda** respuesta SQL empezara con un saludo tipo "Hola,". `ejecutar_rag()` y
`_sintetizar_respuestas_multiples()` no lo prohibían tampoco. Se agregó la regla "NO
comiences con un saludo genérico" a los 3 prompts de ejecución, y a las 3 ramas de
`_PROMPT_SELECCIONAR_CASO_USO` (meta, caso encontrado, no encontrado). También se
reforzó la regla para que "qué puedes hacer"/"en qué me puedes ayudar" no dispare una
reintroducción de identidad ("Soy el asistente de Claro Insurance...").

---

## 13. Latencia de respuesta — `_MAX_FILAS_PROMPT`

Bajado de 80 a **50** filas mostradas al LLM al redactar la respuesta de resultados SQL
con muchas filas agrupadas — reduce el trabajo de lectura/síntesis sin perder
representatividad del resumen.

(Se probó también saltar la llamada a `llm_sql_model` por completo cuando no hay
entidades detectadas — ahorraba ~12s — pero se revirtió a pedido explícito.)

---

## Resumen ejecutivo

La sesión se enfocó en cerrar dos categorías de problemas: **fugas de contexto entre
turnos** (filtros/carriers de preguntas anteriores apareciendo en consultas nuevas que no
los mencionaban — 2 bugs distintos en `user_query_efectiva`) y **alucinaciones de
formato** (LLM inventando columnas SQL, copiando tablas crudas, sumando datos a mano de
forma lenta e incorrecta). Además, se simplificó el modelo de roles (Management
eliminado), se consolidó el catálogo de Contratos, y se ajustaron los modelos LLM por
etapa según qué tan estricta es cada tarea (Pro solo para SQL, Flash/Flash-Lite para el
resto).

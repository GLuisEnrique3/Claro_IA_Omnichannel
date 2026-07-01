# Changelog — 2026-07-01

Sesión de diagnóstico y corrección sobre `data/use_cases.json`, `main.py` y
`scripts/precalcular_filtros.py`. Los cambios se concentran en los casos de uso
A6 (Licencias) y A7 (Carrier Oportunidades de Contratación): se corrigieron
filtros dinámicos inoperativos, referencias a campos inexistentes en el SELECT,
instrucciones con errores gramaticales y ausencias de descripciones de columnas.

---

## 1. A6 Licencias — Filtros dinámicos inoperativos (`main.py`)

### Problema

Cuatro parámetros declarados en `parametros` del JSON **nunca generaban un
filtro WHERE** porque no estaban registrados en `PARAM_TO_FILTRO`. La función
`extraer_entidades()` silenciosamente los saltaba (`if param not in
PARAM_TO_FILTRO: continue`, línea 718), y el prompt del constructor SQL
(`_construir_sql_con_llm`) prohíbe explícitamente al LLM añadir filtros WHERE
que no vengan de "ENTIDADES DETECTADAS". Resultado: si un usuario pedía
"licencias tipo Life" o "licencias que vencen en junio", el sistema ignoraba
el filtro sin avisar.

Los parámetros afectados eran:

| Parámetro | Tipo |
|---|---|
| `nl.LicenseClassName__c` | Categórico (embedding) |
| `nl.LineOfAuthority__c` | Categórico (embedding) |
| `l.LicenseActivationDate__c` | Fecha (lenguaje natural) |
| `l.LicenseExpirationDate__c` | Fecha (lenguaje natural) |
| `l.DateUpdated__c` | Fecha (lenguaje natural) |

### Fix — `PARAM_TO_FILTRO`

Se agregaron las cinco entradas al diccionario `PARAM_TO_FILTRO`:

```python
"nl.LicenseClassName__c":     ("license_class",               "Clase de Licencia",              "AND nl.LicenseClassName__c = '{v}'",                                   0.80),
"nl.LineOfAuthority__c":      ("license_line_of_authority",   "Línea de Autoridad",             "AND nl.LineOfAuthority__c = '{v}'",                                    0.80),
"l.LicenseActivationDate__c": ("__license_activation_date__", "Fecha de Activación de Licencia","AND DATE_TRUNC(DATE(l.LicenseActivationDate__c), MONTH) = DATE '{v}'", None),
"l.LicenseExpirationDate__c": ("__license_expiration_date__", "Fecha de Expiración de Licencia","AND DATE_TRUNC(DATE(l.LicenseExpirationDate__c), MONTH) = DATE '{v}'", None),
"l.DateUpdated__c":           ("__license_date_updated__",    "Fecha de Actualización",         "AND DATE_TRUNC(DATE(l.DateUpdated__c), MONTH) = DATE '{v}'",            None),
```

### Fix — `extraer_entidades()` (tres ramas nuevas)

Los filtros de fecha siguen el mismo patrón que `__commission_month__`:
se activan **solo** si el usuario menciona la keyword correspondiente,
para evitar que una fecha mencionada de pasada se aplique al campo incorrecto.

| filtro_key | Keywords que lo activan |
|---|---|
| `__license_activation_date__` | `activación`, `activada`, `activo desde`, `fecha de activación` |
| `__license_expiration_date__` | `expiración`, `expira`, `vence`, `vencimiento`, `fecha de expiración` |
| `__license_date_updated__` | `actualización`, `actualizada`, `modificado`, `fecha de actualización` |

---

## 2. A6 Licencias — Catálogo de valores válidos (`scripts/precalcular_filtros.py`)

Los filtros categóricos `license_class` y `license_line_of_authority` dependen
de `FILTROS_VALIDOS` (y sus embeddings precomputados) para el matching semántico.
Sin entradas en el pkl, `FILTROS_VALIDOS.get(filtro_key, [])` devuelve `[]` y
el filtro queda inactivo en runtime aunque el código esté correcto.

Se agregaron dos queries a `cargar_filtros_desde_bq()`:

```python
# License Class Name
SELECT DISTINCT LicenseClassName__c
FROM `claroinsurance-dataplatform.salesforce_raw.NIPRLicenses__c`
WHERE LicenseClassName__c IS NOT NULL

# License Line of Authority
SELECT DISTINCT LineOfAuthority__c
FROM `claroinsurance-dataplatform.salesforce_raw.NIPRLicenses__c`
WHERE LineOfAuthority__c IS NOT NULL
```

**Acción requerida:** ejecutar `python scripts/precalcular_filtros.py` para
regenerar `data/filtros_validos.pkl` y `data/filtros_embeddings.pkl`.

---

## 3. A6 Licencias — `entity_resolution` y `descripciones` (`use_cases.json`)

### 3.1 Referencia a campo inexistente (`l.Name`)

`entity_resolution` instruía: *"Menciona el Id de licencia (l.Name)…"*, pero
`l.Name` **no existe en el SELECT** (el campo real es `l.LicenceNumber__c`).
El LLM recibía una instrucción que no podía cumplir porque el dato no llegaba
en las filas. Corregido a `l.LicenceNumber__c`.

### 3.2 Columnas sin documentar

El SQL original incluía `l.DateUpdated__c` y `l.CreatedDate` en SELECT y
GROUP BY pero no estaban en `descripciones` ni en `entity_resolution`,
dejando al LLM de redacción sin contexto sobre esas columnas. La SQL fue
limpiada eliminando ambas columnas del SELECT/GROUP BY; `l.DateUpdated__c`
pasó a ser exclusivamente un parámetro de filtro (no se muestra en el output).

### 3.3 Mejoras de contenido en `entity_resolution`

- Reemplazada la referencia `l.Name` por `l.LicenceNumber__c`.
- Añadida guía explícita para `nl.LicenseClassName__c` y `nl.LineOfAuthority__c`
  (clases y líneas de autoridad) en el resumen ejecutivo.
- Añadida instrucción para `l.LicenseActivationDate__c` /
  `l.LicenseExpirationDate__c`: mencionarlas solo si el usuario pregunta por
  vigencia o vencimiento, no incluirlas por defecto.

---

## 4. A7 Carrier Oportunidades de Contratación — `entity_resolution` y `descripciones` (`use_cases.json`)

### 4.1 Errores de gramática y redacción

| Ubicación | Antes | Ahora |
|---|---|---|
| Apertura | "Esta tabla **ofrece** identifica oportunidades…" | "Esta tabla identifica oportunidades…" |
| Condicional | "si te **pregunten** sobre la oferta" | "si te **preguntan** sobre la oferta" |

### 4.2 Instrucción confusa de DISTINCT

**Antes:** *"puedes usar DISTINCT o.Carrier y o.Line_Of_Business"* — le hablaba
al LLM de redacción como si pudiera ejecutar SQL.

**Ahora:** *"lista los valores únicos de o.Carrier y o.Line_Of_Business
presentes en los resultados"* — indica qué hacer con los datos ya recibidos.

### 4.3 Prohibición de tabla cruda ausente

A7 era el único use case SQL sin la instrucción
*"PROHIBIDO copiar, pegar o reproducir la tabla de datos cruda (nunca uses '|'…)"*.
Añadida para consistencia y para evitar respuestas en formato tabla.

### 4.4 Columna `Disposition` sin contexto

El SELECT incluye `'Opportunity' as Disposition` (constante hardcodeada).
`entity_resolution` no la mencionaba, dejando al LLM sin saber qué hacer con
ella en cada fila. Añadida aclaración: *"la columna 'Disposition' siempre
tendrá el valor 'Opportunity' — es una etiqueta fija, no la interpretes como
un campo variable"*.

### 4.5 Instrucción de "80 filas" imprecisa

**Antes:** *"debes mencionar que estás analizando un extracto de la data"* —
incorrecto, el LLM recibe todos los datos, no un extracto.

**Ahora:** *"indica el volumen al usuario y sugiérele acotar la consulta con
un filtro (carrier, estado o línea de negocio específicos)"*.

### 4.6 `Cantidad` y `Total_General` sin descripción en `descripciones`

Añadidas ambas entradas para consistencia con A6 y para que el LLM constructor
de SQL tenga referencia completa del schema:

| Columna | Descripción agregada |
|---|---|
| `Cantidad` | Número de agentes que carecen del contrato para esa combinación específica. Sumar para subtotales por grupo — no usar para el total general. |
| `Total_General` | Total real de oportunidades detectadas (valor de ventana, igual en todas las filas). Usar como gran total; nunca sumar Cantidad para obtenerlo. |

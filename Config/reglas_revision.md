# Reglas de revisión posteriores a la codificación

Estas reglas se aplicaron **después** de la codificación automática (etapas 0–2,
guiadas por `sistema.md`). No modifican los resultados originales: los JSON de
`Salida/etapa0`, `Salida/etapa1` y `Salida/etapa2` quedan intactos. Las correcciones
viven en `Config/correcciones.csv` y las notas de revisión en
`Config/revision_manual.csv`; `python Claude.py exportar` las aplica y registra cada
cambio (valor anterior → valor nuevo, motivo, fuente) en la hoja
`Bitacora_correcciones` de `Salida/reformas_caracterizadas.xlsx`.

Revisión realizada por Claude (Anthropic) con búsqueda web el 24-oct-2026, a pedido
del autor. Está pendiente la auditoría humana y la de otros modelos.

## R1. Fecha de corte del estado de vigencia
`estado_vigencia` se evalúa **al 31-dic-2025**, último año del panel de resultados.
Los hechos de 2026 (p. ej. Ley 27.802 de Argentina, Ley 189 de Cuba, Decreto 45-2026
de Honduras) se registran en `eventos_posteriores_2025` y no cambian el estado.

## R2. "Vigente" vs. "modificada_o_derogada_despues"
Es `modificada_o_derogada_despues` si, antes del 31-dic-2025, otra norma modificó una
disposición **sustantiva** de la reforma o la derogó en todo o en parte. Los
reglamentos, los ajustes procesales menores y los cambios institucionales no cuentan.

## R3. Anulación judicial parcial
Es `anulada_o_suspendida_parcial` si un tribunal anuló o suspendió un componente
sustantivo después de la promulgación (como define `sistema.md`). El control
preventivo **anterior** a la promulgación (p. ej. el Tribunal Constitucional de Chile
en 2016) no cambia el estado, porque la ley publicada nunca contuvo esas normas; se
anota en `nota_revision`. Los fallos con efecto solo entre las partes
(inconstitucionalidad en caso concreto, inaplicabilidad) tampoco lo cambian.

## R4. Tipo de fecha
`tipo_fecha` declara qué representa `fecha_entrada_vigencia`:
`vigencia` · `vigencia_calculada` (a partir de una vacatio legis verificada) ·
`vigencia_inferida` (de una regla general) · `publicacion` · `publicacion_asumida` ·
`promulgacion` · `sancion` · `fecha_de_la_norma` (la fecha coincide con la del título;
posible promulgación) · `vigencia_segun_etapa2_no_auditada` (valor por defecto: lo que
codificó la etapa 2, sin auditar).

## R5. Fecha de tratamiento
`fecha_tratamiento` es la fecha desde la cual la reforma operó efectivamente. Por
defecto es igual a `fecha_entrada_vigencia`, salvo una suspensión judicial verificada
(GTM_2019: suspendida de oct-2019 a jul-2021). Si la norma se publicó pero nunca operó,
vale `no_aplicada` y `anio_tratamiento` queda vacío (HTI_2006). `anio_tratamiento` se
deriva de ella y es la que conviene usar como año del evento.

(La regla R6 de una versión anterior, sobre uso como tratamiento, se eliminó a pedido del autor.)

## R7. Exclusión del análisis de confiabilidad
`excluir_confiabilidad = 1` para COL_2002, que es el ejemplo de calibración de
`sistema.md`: la codificación del modelo no es independiente del ejemplo.

## R8. Recodificación de dimensiones con el texto de la norma
Una dimensión solo se recodifica cuando se leyó el articulado (nivel 1). En
`correcciones.csv` se usa `campo = dim_<dimension>` y `valor_nuevo` = `aumenta`,
`reduce`, `reorganiza` o vacío (la reforma no toca esa dimensión). `exportar` actualiza
`dim_`, `cod_` y la hoja `Dimensiones_largo`, donde la columna `origen` distingue las
filas de la etapa 2 (`etapa2`) de las recodificadas (`correccion_revision`, con los
artículos citados en `comentario`). El sentido se juzga contra la norma anterior; si no
se tiene esa norma, se declara de dónde sale el juicio. Se aplicó a HTI_2006 (texto
completo del decreto; la etapa 2 no lo había hallado) y a PRY_1995 (cotejo de la Ley 496
con el texto de 1993).

## Orientación general
No se cambió ninguna `orientacion_general`. Cuando la etiqueta y el saldo de las
dimensiones divergen, se mantuvo la etiqueta según el **componente central** de la
reforma y se dejó la justificación en `nota_revision` (ARG_1991, ARG_1998, HND_2014,
MEX_2012, URY_2021, VEN_2002, PRY_1995, HTI_2006).

## Tipo de instrumento
`codigo` solo cuando la norma promulga un código completo nuevo o una codificación
(DOM_1992, PRY_1993, NIC_1996, NIC_2012, CUB_2013, ECU_2005). Las reformas parciales
de un código, o las leyes procesales que no se denominan código (CRI_2017, PER_2010),
son `ley`. Con esta regla la codificación existente es consistente y no se cambió.

## Colores (`revision_manual`): estándar estricto según la jerarquía de fuentes de `sistema.md`
Niveles de fuente: **1** = texto oficial de la norma (gaceta, diario oficial, congreso,
ministerio) o reproducción íntegra identificable de ese texto; **2** = OIT (NATLEX, EPLex,
informes); **3** = CEPAL, BID, Banco Mundial, artículos académicos; **4** = prensa de
referencia, admitida solo para contexto político o estado judicial. Los blogs y los
despachos de abogados **no** cuentan como fuente de verificación.

- `verde`: la norma y sus datos clave (identificación, fecha, estado) se verificaron en
  nivel 1 o 2; el estado judicial puede apoyarse en nivel 4; los detalles que no se
  pudieron auditar se declaran en `nota_revision`.
- `amarillo`: la verificación solo alcanzó nivel 3–4, o queda una decisión metodológica
  pendiente para el autor (p. ej. si la norma cuenta como "reforma laboral").
- `rojo`: la evidencia contradice la codificación y no se pudo corregir. En la revisión
  del 24-oct-2026 ninguna reforma quedó en rojo.
- vacío en el CSV (solo nota): se anota sin cambiar el color automático.
- `sin_alerta` / `sin_revisar`: no revisadas manualmente (según las alertas automáticas).

## Criterio de inclusión (decisión del autor, 24-oct-2026)
Se adopta un criterio **amplio**: cuenta como reforma toda norma relacionada con el
trabajo, incluidas las de pensiones y seguridad social, el empleo público, los ajustes de
salario mínimo, las codificaciones y las medidas laborales temporales. Por esta decisión
pasaron a `verde` BOL_1999, BOL_2010, SLV_2022, VEN_2024, PAN_2020 y ECU_2005, cuyo único
pendiente era ese criterio. Para distinguir el tipo de norma en los análisis, la columna
`ambito_reforma` (archivo `Config/ambito_reforma.csv`) clasifica las 55 según el objeto de
la norma: `reforma_laboral_sustantiva`, `codigo_laboral_nuevo`,
`justicia_procedimiento_laboral`, `seguridad_social_pensiones`, `sector_publico`,
`constitucion`, `salario_minimo`, `codificacion`, `temporal_emergencia`.

## Documentos aportados por el autor (24-oct-2026)
Para las tres reformas que quedaban en amarillo, el autor aportó las fuentes. Se leyeron
completas y se citan en `fuente` / `fuentes_revision` como "documento aportado por el
autor":
- HTI_2006: copia escaneada de Le Moniteur n.º 53 (7-jun-2006) con el decreto completo.
  Nivel 1. El estado ("nunca se aplicó", sin acto formal de derogación ni de suspensión)
  se confirmó después con documentos oficiales del gobierno haitiano (OMRH 2018 y
  Programme de modernisation de l'État 2018-2023) y con la OCDE (2021) → verde.
- HTI_2022: copia escaneada de Le Moniteur, Spécial n.º 5 (21-feb-2022) con el arrêté
  completo. Nivel 1 → verde.
- PRY_1995: texto íntegro de la Ley 496. Nivel 1 (coincide con la ficha de la BACN). Para
  el cotejo con 1993 se usó una copia académica no oficial del Código (Universidad
  Nacional de Itapúa), declarada como tal → verde.

## Validación
La columna `NORMA` de `Datos/normas_validadas.xlsx` está vacía: **no hubo validación
humana registrada** de la norma; las etapas 1–2 usaron la norma propuesta por la
etapa 0. La columna `validacion` del Excel final distingue la revisión con IA y la
validación humana.

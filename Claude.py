"""
Caracterización de reformas laborales (hoja OIT de BD_Art_Sept26.xlsx) con la API de Claude.

Etapas
  0  identificar : qué norma corresponde a cada país-año (Opus + búsqueda web)
  1  investigar  : contenido e intenciones de la norma validada (Opus + búsqueda web)
  2  estructurar : texto de la etapa 1 -> JSON según Config/esquema.json (Haiku, structured outputs)

Comandos (desde la carpeta Cambios_legales)
  python Claude.py prueba                              # prueba corta: 2 reformas, etapas 0-1-2 seguidas + Excel
  python Claude.py prueba-batch                        # la misma prueba corta, pero por Batch API (espera cada lote)
  python Claude.py prueba --etapa 0                    # solo una etapa para las reformas de prueba
  python Claude.py prueba --etapa 1 --ids PER_1991 HTI_2006
  python Claude.py prueba --etapa 0 --todas            # las 55 sin batch
  python Claude.py batch-enviar  --etapa 0             # todas las pendientes en un lote (50% menos en tokens)
  python Claude.py batch-recoger --etapa 0             # descarga resultados cuando el lote termine
  python Claude.py validar                             # etapa 0 -> Datos/normas_validadas.xlsx (revisar a mano)
  python Claude.py exportar                            # etapa 2 -> Salida/reformas_caracterizadas.xlsx
  Agrega --forzar para rehacer reformas que ya tienen resultado.

Flujo recomendado
  etapa 0 -> validar -> (revisar/llenar columna NORMA en Excel) -> etapa 1 -> etapa 2 -> exportar
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic
import pandas as pd

# ----------------------------------------------------------------------------- configuración
BASE = Path(__file__).resolve().parent
CONFIG, DATOS, SALIDA = BASE / "Config", BASE / "Datos", BASE / "Salida"
EXCEL_BASE = DATOS / "BD_Art_Sept26.xlsx"
EXCEL_VALIDADAS = DATOS / "normas_validadas.xlsx"
EXCEL_FINAL = SALIDA / "reformas_caracterizadas.xlsx"
DIR_ETAPA = {e: SALIDA / f"etapa{e}" for e in (0, 1, 2)}
DIR_LOTES = SALIDA / "lotes"
COSTOS_CSV = SALIDA / "costos.csv"

MODELO_INVESTIGACION = "claude-opus-5-5"   # etapas 0 y 1
MODELO_ESTRUCTURA = "claude-opus-5-5"      # etapa 2 (Opus también codifica: decide las variables de tratamiento)
MAX_TOKENS = {0: 4000, 1: 12000, 2: 8000}   # la prueba mostró cortes con 4000/3000
MAX_BUSQUEDAS = {0: 4, 1: 6}

HERRAMIENTA_WEB = {
    "type": "web_search_20250305",
    "name": "web_search",
    # Sitios de baja calidad o sin autoría que aparecieron en la prueba.
    # (No se puede combinar con allowed_domains.)
    "blocked_domains": ["scribd.com", "slideshare.net", "studocu.com", "x.com", "twitter.com",
                        "facebook.com", "youtube.com", "tiktok.com", "instagram.com"],
}

# Prueba corta: una reciente y conocida (Colombia 2025) y una antigua y ambigua (Perú 1991).
# COL_2002 no se usa porque es el ejemplo de calibración del system prompt.
REFORMAS_PRUEBA = ["COL_2025", "PER_1991"]

# Alertas manuales tras revisar la etapa 0: casos donde la norma elegida puede no ser
# una reforma del mercado laboral privado. Decisión metodológica pendiente.
ALERTAS_MANUALES = {
    "BOL_1999": "Norma solo del sector público (Estatuto del Funcionario); alternativa DS 25421",
    "HTI_2006": "Confianza baja; decreto de función pública territorial, sin reforma del Código",
    "HTI_2022": "Ajuste periódico de salario mínimo, no reforma estructural",
    "ECU_2005": "Codificación (consolidación) del Código, sin cambios de fondo; alternativa Ley 2005-3",
    "BOL_2010": "Ley de pensiones; alternativa DS 0495 (reincorporación por despido)",
    "SLV_2022": "Ley de pensiones; alternativas: decretos que reforman el Código de Trabajo",
    "VEN_2024": "Ley de pensiones, no de relación laboral",
    "BOL_2009": "Constitución completa; alternativa DS 0110 (indemnización)",
}
# Revisión manual con búsqueda web (24-oct-2026). Los datos viven en archivos legibles para auditoría:
#   Config/revision_manual.csv : ID, color, nota, fuentes   (color vacío = solo nota, no cambia el color)
#   Config/correcciones.csv    : ID, campo, valor_nuevo, motivo, fuente
#   Config/reglas_revision.md  : reglas R1–R7 que justifican cada corrección
# Color (recomendación de uso):
#   verde    = identificación y datos clave verificados con fuentes; usable como tratamiento.
#   amarillo = verificada, pero queda una decisión metodológica.
#   rojo     = no usar como tratamiento (nunca se aplicó, no surtió efectos o no cambia nada).
FECHA_REVISION = "2026-10-24"
CAMPOS_BOOL = {"vigencia_escalonada", "revierte_reforma_previa"}
CAMPOS_INT = {"excluir_confiabilidad"}


def cargar_revision_manual() -> dict:
    ruta = CONFIG / "revision_manual.csv"
    if not ruta.exists():
        return {}
    with ruta.open(encoding="utf-8") as f:
        return {r["ID"]: r for r in csv.DictReader(f)}


def cargar_correcciones() -> dict:
    ruta = CONFIG / "correcciones.csv"
    if not ruta.exists():
        return {}
    out = {}
    with ruta.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out.setdefault(r["ID"], []).append(r)
    return out


def cargar_ambito() -> dict:
    ruta = CONFIG / "ambito_reforma.csv"
    if not ruta.exists():
        return {}
    with ruta.open(encoding="utf-8") as f:
        return {r["ID"]: r["ambito_reforma"] for r in csv.DictReader(f)}


def convertir(campo: str, valor: str):
    if campo in CAMPOS_BOOL:
        return str(valor).strip().lower() in ("1", "true", "verdadero", "si", "sí")
    if campo in CAMPOS_INT or campo.startswith("pob_"):
        return int(valor)
    return valor


def anio_de(fecha) -> "int | None":
    m = re.match(r"(\d{4})", str(fecha or ""))
    return int(m.group(1)) if m else None


# Pocas fuentes oficiales/académicas entre las citadas en la etapa 1 (revisión del 23-oct).
UMBRAL_FUENTES_OFICIALES = 2
PATRON_OFICIAL = re.compile(
    r"(\.gob\.|\.gov\.|\.gub\.|\.gouv\.|ilo\.org|natlex|cepal|iadb|worldbank|oas\.org|congreso|asamblea|"
    r"senado|camara|diputados|leg\.br|planalto|bcn\.cl|parlamento|gaceta|moniteur|impo\.com\.uy|lexivox|"
    r"corte|scielo|redalyc|dialnet|repositorio|\.edu)", re.I)

# USD por millón de tokens: (entrada, salida, factor lectura de caché)
PRECIOS = {
    "claude-opus-5-5": (4.0, 20.0, 0.05),
    "claude-haiku-5-5": (0.10, 0.50, 0.10),
}
PRECIO_BUSQUEDA = 0.01

INSTRUCCIONES = {
    0: (
        "ETAPA 0 — IDENTIFICACIÓN.\n"
        "Reforma de la base: {pais} ({iso}), año {anio}.\n"
        "Identifica cuál es la reforma laboral principal de ese país en ese año, usando la "
        "búsqueda web y la jerarquía de fuentes. Resume en 3-5 oraciones qué es y por qué "
        "crees que es la que corresponde. Si hay varias candidatas, explica cuál eliges y por qué.\n\n"
        "Termina tu respuesta EXACTAMENTE con este bloque, sin nada después:\n"
        "NORMA: <identificador oficial de la norma principal, o 'no identificada'>\n"
        "OTRAS: <otras normas laborales relevantes del mismo año separadas por ';', o 'ninguna'>\n"
        "CONFIANZA: <alta|media|baja>"
    ),
    1: (
        "ETAPA 1 — INVESTIGACIÓN.\n"
        "Reforma de la base: {pais} ({iso}), año {anio}. Norma a caracterizar: {norma}.\n\n"
        "Informe de identificación previo (ya verificado; no repitas esa búsqueda, úsalo como punto "
        "de partida y concentra tus búsquedas en el contenido de la norma):\n{previo}\n\n"
        "Investiga con la búsqueda web el contenido de esta norma y redacta un informe en "
        "prosa (sin cifras, según las reglas) que cubra, en este orden: objetivo declarado; qué "
        "cambia efectivamente, dimensión por dimensión del codebook (solo las que apliquen, con su "
        "efecto); población a la que apunta; contexto político y económico; si revierte una "
        "reforma previa; fecha de entrada en vigor y si partes clave tienen aplicación escalonada "
        "o diferida; y su estado de vigencia posterior (modificaciones, derogación, fallos "
        "judiciales, distinguiendo si lo anulado es sustantivo o menor). Señala lo que no pudiste "
        "verificar.\n"
        "Sé conciso: máximo unas 1.500 palabras. Es preferible cubrir TODAS las secciones con "
        "brevedad que desarrollar mucho las primeras y dejar las últimas sin escribir."
    ),
    2: (
        "ETAPA 2 — CODIFICACIÓN.\n"
        "Reforma de la base: {pais} ({iso}), año {anio}. Norma: {norma}.\n"
        "Codifica el siguiente informe de investigación según el codebook del sistema. "
        "Usa SOLO la información del informe; si algo no aparece, usa la categoría más "
        "conservadora ('desconocido', confianza 'baja') y explícalo en 'notas'. "
        "Respeta la regla de no incluir magnitudes cuantitativas.\n"
        "Escribe en español con tildes y eñes. 'normas_relacionadas' son OTRAS normas laborales "
        "expedidas el mismo año o que complementan directamente esta reforma (reglamentos, "
        "decretos paralelos); las leyes anteriores que la reforma modifica NO van ahí, menciónalas "
        "en 'contexto' y úsalas para decidir 'revierte_reforma_previa'. Sé breve en los campos de "
        "texto: 'descripcion_intenciones' de 3 a 5 oraciones y cada 'comentario' de 1 o 2.\n\n"
        "Otras normas laborales del mismo año identificadas en la etapa de identificación "
        "(inclúyelas en 'normas_relacionadas' salvo que el informe las descarte): {otras}\n\n"
        "Fuentes consultadas en la investigación:\n{fuentes}\n\n"
        "INFORME:\n{informe}"
    ),
}


# ----------------------------------------------------------------------------- utilidades
def cliente() -> anthropic.Anthropic:
    archivo = CONFIG / "anthropic_api_key.txt"
    origen, clave = None, None
    if archivo.exists():
        # utf-8-sig quita el BOM que agrega el Bloc de notas; la regex ignora comillas,
        # espacios, saltos de línea o un prefijo tipo "ANTHROPIC_API_KEY="
        crudo = archivo.read_text(encoding="utf-8-sig")
        m = re.search(r"sk-ant-[A-Za-z0-9_\-]+", crudo)
        if m:
            origen, clave = "archivo Config/anthropic_api_key.txt", m.group(0)
    if not clave and os.environ.get("ANTHROPIC_API_KEY"):
        origen, clave = "variable de entorno ANTHROPIC_API_KEY", os.environ["ANTHROPIC_API_KEY"].strip()
    if not clave:
        sys.exit("No encontré una clave que empiece con 'sk-ant-' en Config/anthropic_api_key.txt "
                 "ni en la variable ANTHROPIC_API_KEY.")
    print(f"Clave tomada de: {origen} ({len(clave)} caracteres, termina en ...{clave[-4:]})")
    if not clave.startswith("sk-ant-"):
        print("Aviso: la clave no empieza con 'sk-ant-'; revisa que esté completa.")
    return anthropic.Anthropic(api_key=clave, max_retries=5, timeout=600)


def sistema() -> list:
    texto = (CONFIG / "sistema.md").read_text(encoding="utf-8")
    # TTL de 5 min: la escritura cuesta 1,25x (la de 1 h cuesta 2x) y la prueba mostró que el
    # volumen de escritura en caché (resultados de búsqueda) es grande.
    return [{"type": "text", "text": texto, "cache_control": {"type": "ephemeral"}}]


def esquema() -> dict:
    return json.loads((CONFIG / "esquema.json").read_text(encoding="utf-8"))


def cargar_reformas() -> pd.DataFrame:
    df = pd.read_excel(EXCEL_BASE, sheet_name="OIT")
    df["ID"] = df["ISO"].str.strip() + "_" + df["YEAR"].astype(int).astype(str)
    return df


def leer_json(ruta: Path):
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else None


def escribir_json(ruta: Path, datos) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")


def norma_de(id_: str) -> str:
    """NORMA validada a mano; si está vacía, la propuesta de la etapa 0."""
    if EXCEL_VALIDADAS.exists():
        v = pd.read_excel(EXCEL_VALIDADAS).set_index("ID")
        if id_ in v.index:
            fila = v.loc[id_]
            if pd.notna(fila.get("NORMA")) and str(fila["NORMA"]).strip():
                return str(fila["NORMA"]).strip()
            if pd.notna(fila.get("NORMA_PROPUESTA")):
                print(f"  Aviso {id_}: NORMA sin validar, uso la propuesta de la etapa 0.")
                return str(fila["NORMA_PROPUESTA"]).strip()
    r0 = leer_json(DIR_ETAPA[0] / f"{id_}.json")
    if r0 and r0.get("norma_propuesta"):
        print(f"  Aviso {id_}: sin normas_validadas.xlsx, uso la propuesta de la etapa 0.")
        return r0["norma_propuesta"]
    return "por identificar"


def registrar_costo(id_: str, etapa: int, modelo: str, uso: dict, batch: bool) -> float:
    pin, pout, fcache = PRECIOS.get(modelo, (0, 0, 0))
    desc = 0.5 if batch else 1.0
    entrada = uso.get("input_tokens", 0) or 0
    escritura = uso.get("cache_creation_input_tokens", 0) or 0
    lectura = uso.get("cache_read_input_tokens", 0) or 0
    salida = uso.get("output_tokens", 0) or 0
    busquedas = ((uso.get("server_tool_use") or {}).get("web_search_requests", 0)) or 0
    costo = desc * (entrada * pin + escritura * pin * 1.25 + lectura * pin * fcache + salida * pout) / 1e6
    costo += busquedas * PRECIO_BUSQUEDA
    nuevo = not COSTOS_CSV.exists()
    COSTOS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with COSTOS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["fecha", "id", "etapa", "modelo", "batch", "entrada", "cache_escritura",
                        "cache_lectura", "salida", "busquedas", "costo_usd"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), id_, etapa, modelo, batch,
                    entrada, escritura, lectura, salida, busquedas, round(costo, 4)])
    return costo


def sumar_uso(a: dict, b: dict) -> dict:
    out = dict(a)
    for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        out[k] = (a.get(k) or 0) + (b.get(k) or 0)
    ws = ((a.get("server_tool_use") or {}).get("web_search_requests", 0) or 0) + \
         ((b.get("server_tool_use") or {}).get("web_search_requests", 0) or 0)
    out["server_tool_use"] = {"web_search_requests": ws}
    return out


# ----------------------------------------------------------------------------- construcción de solicitudes
def params(etapa: int, fila) -> dict:
    id_ = fila.ID
    if etapa == 0:
        contenido = INSTRUCCIONES[0].format(pais=fila.COUNTRY, iso=fila.ISO, anio=fila.YEAR)
    elif etapa == 1:
        r0 = leer_json(DIR_ETAPA[0] / f"{id_}.json") or {}
        previo = re.split(r"\n\s*\**NORMA\**\s*:", r0.get("texto", ""))[0].strip() or "(no disponible)"
        norma = norma_de(id_)
        if not norma or norma.lower().startswith(("no identificada", "por identificar")):
            # No se paga una investigación sin norma: escribe la norma en la columna NORMA del Excel.
            raise FileNotFoundError(f"{id_}: sin norma identificada; llena NORMA en normas_validadas.xlsx")
        contenido = INSTRUCCIONES[1].format(pais=fila.COUNTRY, iso=fila.ISO, anio=fila.YEAR,
                                            norma=norma, previo=previo)
    else:
        r1 = leer_json(DIR_ETAPA[1] / f"{id_}.json")
        if not r1:
            raise FileNotFoundError(f"{id_}: falta el resultado de la etapa 1")
        if r1.get("incompleto"):
            raise FileNotFoundError(f"{id_}: el informe de la etapa 1 está incompleto; rehaz la etapa 1")
        citadas = [f for f in r1["fuentes"] if f.get("citada")] or r1["fuentes"]
        fuentes = "\n".join(f"- {f['titulo']} ({f['url']})" for f in citadas) or "- (ninguna)"
        r0 = leer_json(DIR_ETAPA[0] / f"{id_}.json") or {}
        otras = r0.get("otras") or "ninguna"
        if EXCEL_VALIDADAS.exists():  # si corregiste OTRAS_VALIDADAS a mano en el Excel, manda esa
            v = pd.read_excel(EXCEL_VALIDADAS).set_index("ID")
            if id_ in v.index and "OTRAS_VALIDADAS" in v.columns:
                val = v.loc[id_]["OTRAS_VALIDADAS"]
                if pd.notna(val) and str(val).strip():
                    otras = str(val).strip()
        contenido = INSTRUCCIONES[2].format(pais=fila.COUNTRY, iso=fila.ISO, anio=fila.YEAR,
                                            norma=r1["norma"], otras=otras, fuentes=fuentes,
                                            informe=r1["texto"])

    p = {
        "model": MODELO_ESTRUCTURA if etapa == 2 else MODELO_INVESTIGACION,
        "max_tokens": MAX_TOKENS[etapa],
        "system": sistema(),
        "messages": [{"role": "user", "content": contenido}],
    }
    if etapa in (0, 1):
        p["tools"] = [dict(HERRAMIENTA_WEB, max_uses=MAX_BUSQUEDAS[etapa])]
    else:
        p["output_config"] = {"format": {"type": "json_schema", "schema": esquema()}}
    return p


# ----------------------------------------------------------------------------- lectura de respuestas
def extraer(contenido: list) -> tuple[str, list]:
    """Texto final y fuentes (URLs citadas + resultados de búsqueda) de una lista de bloques."""
    textos, fuentes = [], {}
    for b in contenido:
        tipo = b.get("type")
        if tipo == "text":
            textos.append(b.get("text", ""))
            for c in b.get("citations") or []:
                if c.get("url"):
                    fuentes[c["url"]] = {"url": c["url"], "titulo": c.get("title") or "", "citada": True}
        elif tipo == "web_search_tool_result":
            res = b.get("content")
            if isinstance(res, list):
                for r in res:
                    if r.get("url") and r["url"] not in fuentes:
                        fuentes[r["url"]] = {"url": r["url"], "titulo": r.get("title") or "", "citada": False}
    citadas_primero = sorted(fuentes.values(), key=lambda f: not f["citada"])
    return "".join(textos).strip(), citadas_primero


def normalizar_confianza(valor: str) -> str:
    m = re.search(r"\b(alta|media|baja)\b", str(valor or "").lower())
    return m.group(1) if m else ""


def guardar(etapa: int, fila, contenido: list, uso: dict, modelo: str, stop: str, batch: bool) -> None:
    id_ = fila.ID
    texto, fuentes = extraer(contenido)
    costo = registrar_costo(id_, etapa, modelo, uso, batch)
    base = {"id": id_, "iso": fila.ISO, "pais": fila.COUNTRY, "anio": int(fila.YEAR),
            "modelo": modelo, "stop_reason": stop, "costo_usd": round(costo, 4),
            "incompleto": stop in ("pause_turn", "max_tokens", "refusal") or not texto.strip()}

    if etapa == 0:
        def campo(nombre):
            # La ÚLTIMA aparición: el bloque final. (En PER_2014 el texto mencionaba
            # "Confianza: media..." antes del bloque y se capturó esa línea.)
            ms = re.findall(rf"^\s*\**{nombre}\**\s*:\s*(.+)$", texto, re.M | re.I)
            return ms[-1].strip().strip("*").strip() if ms else ""
        base.update(norma_propuesta=campo("NORMA"), otras=campo("OTRAS"),
                    confianza=normalizar_confianza(campo("CONFIANZA")), texto=texto, fuentes=fuentes)
    elif etapa == 1:
        base.update(norma=norma_de(id_), texto=texto, fuentes=fuentes)
    else:
        try:
            base["caracterizacion"] = json.loads(texto)
        except json.JSONDecodeError:
            base["caracterizacion"] = None
            base["incompleto"] = True
            base["texto_crudo"] = texto
        r1 = leer_json(DIR_ETAPA[1] / f"{id_}.json") or {}
        base["fuentes"] = [f for f in r1.get("fuentes", []) if f.get("citada")] or r1.get("fuentes", [])

    escribir_json(DIR_ETAPA[etapa] / f"{id_}.json", base)
    marca = "  [INCOMPLETO]" if base["incompleto"] else ""
    print(f"  {id_} etapa {etapa}: ${costo:.3f}{marca}")


# ----------------------------------------------------------------------------- modos de ejecución
def seleccionar(df, etapa, ids, todas, forzar):
    if ids:
        sel = df[df.ID.isin(ids)]
        faltan = set(ids) - set(sel.ID)
        if faltan:
            print("IDs no encontrados:", ", ".join(sorted(faltan)))
    elif todas:
        sel = df
    else:
        sel = df[df.ID.isin(REFORMAS_PRUEBA)]
    if not forzar:
        hechos = []
        for i in sel.ID:
            r = leer_json(DIR_ETAPA[etapa] / f"{i}.json")
            if r and not r.get("incompleto"):
                hechos.append(i)
        if hechos:
            print(f"Omito {len(hechos)} ya procesadas (usa --forzar para rehacerlas).")
        sel = sel[~sel.ID.isin(hechos)]
    return sel


def correr_prueba(etapa, ids, todas, forzar):
    df = seleccionar(cargar_reformas(), etapa, ids, todas, forzar)
    if df.empty:
        return print("Nada pendiente.")
    c, total = cliente(), 0.0
    for fila in df.itertuples():
        try:
            p = params(etapa, fila)
            contenido, uso = [], {}
            for _ in range(5):  # continúa si la búsqueda web devuelve pause_turn
                r = c.messages.create(**p) if etapa != 2 else \
                    c.messages.create(**{k: v for k, v in p.items() if k != "output_config"},
                                      extra_body={"output_config": p["output_config"]})
                d = r.model_dump()
                contenido += d["content"]
                uso = sumar_uso(uso, d["usage"])
                if d["stop_reason"] != "pause_turn":
                    break
                p["messages"] = p["messages"] + [{"role": "assistant", "content": r.content}]
            guardar(etapa, fila, contenido, uso, p["model"], d["stop_reason"], batch=False)
            total += leer_json(DIR_ETAPA[etapa] / f"{fila.ID}.json")["costo_usd"]
        except anthropic.AuthenticationError:
            sys.exit("Clave del API inválida (401). Corrige Config/anthropic_api_key.txt y vuelve a correr.")
        except Exception as e:  # una reforma fallida no detiene el resto
            print(f"  {fila.ID} etapa {etapa}: ERROR {type(e).__name__}: {e}")
    print(f"Costo de esta corrida: ${total:.2f}")


def batch_enviar(etapa, ids, forzar, todas=None):
    """Envía un lote. Devuelve True si se envió algo."""
    todas = (not ids) if todas is None else todas
    # Protección contra pagar dos veces: si hay un lote anterior de esta etapa sin recoger, no envío.
    previo = leer_json(DIR_LOTES / f"etapa{etapa}.json")
    if previo and not previo.get("recogido"):
        sys.exit(f"Ya hay un lote de la etapa {etapa} ({previo['id']}) enviado el {previo['creado'][:16]} "
                 f"y sin recoger. Recógelo primero con: python Claude.py batch-recoger --etapa {etapa} --esperar")
    df = seleccionar(cargar_reformas(), etapa, ids, todas=todas, forzar=forzar)
    if df.empty:
        print("Nada pendiente.")
        return False
    reqs = []
    for fila in df.itertuples():
        try:
            reqs.append({"custom_id": fila.ID, "params": params(etapa, fila)})
        except FileNotFoundError as e:
            print(f"  Omito: {e}")
    if not reqs:
        print("Nada que enviar.")
        return False
    lote = cliente().messages.batches.create(requests=reqs)
    escribir_json(DIR_LOTES / f"etapa{etapa}.json",
                  {"id": lote.id, "etapa": etapa, "n": len(reqs), "creado": datetime.now().isoformat()})
    print(f"Lote {lote.id} enviado con {len(reqs)} reformas. Recoge con: python Claude.py batch-recoger --etapa {etapa}")
    return True


def prueba_batch(ids, forzar):
    """Reformas de prueba por Batch API: etapas 0 -> 1 -> 2 en lotes sucesivos, esperando cada uno."""
    ids = ids or REFORMAS_PRUEBA
    for e in (0, 1, 2):
        print(f"\n=== Etapa {e} (batch) ===")
        if batch_enviar(e, ids, forzar, todas=False):
            batch_recoger(e, esperar=True)
    validar()
    exportar()
    print("\nRevisa: Datos/normas_validadas.xlsx, Salida/reformas_caracterizadas.xlsx "
          "y los textos completos en Salida/etapa1/*.json")


def batch_recoger(etapa, esperar):
    info = leer_json(DIR_LOTES / f"etapa{etapa}.json")
    if not info:
        return print(f"No hay lote enviado para la etapa {etapa}.")
    if info.get("recogido"):
        return print(f"El lote {info['id']} de la etapa {etapa} ya fue recogido el {info['recogido'][:16]}.")
    c = cliente()
    while True:
        lote = c.messages.batches.retrieve(info["id"])
        cuentas = lote.request_counts
        print(f"Lote {lote.id}: {lote.processing_status} | ok {cuentas.succeeded}, "
              f"error {cuentas.errored}, en proceso {cuentas.processing}")
        if lote.processing_status == "ended":
            break
        if not esperar:
            return print("Aún no termina. Vuelve a ejecutar más tarde (o usa --esperar).")
        time.sleep(60)

    filas = cargar_reformas().set_index("ID")
    total, fallidas = 0.0, []
    for r in c.messages.batches.results(info["id"]):
        if r.result.type != "succeeded":
            fallidas.append(r.custom_id)
            continue
        d = r.result.message.model_dump()
        fila = filas.loc[r.custom_id]
        fila = type("Fila", (), {"ID": r.custom_id, "ISO": fila.ISO, "COUNTRY": fila.COUNTRY, "YEAR": fila.YEAR})
        guardar(etapa, fila, d["content"], d["usage"], d["model"], d["stop_reason"], batch=True)
        total += leer_json(DIR_ETAPA[etapa] / f"{r.custom_id}.json")["costo_usd"]
    info["recogido"] = datetime.now().isoformat()
    escribir_json(DIR_LOTES / f"etapa{etapa}.json", info)
    print(f"Costo del lote: ${total:.2f}")
    if fallidas:
        print("Fallaron (reintenta con prueba --ids):", " ".join(fallidas))
    incompletas = [p.stem for p in DIR_ETAPA[etapa].glob("*.json") if leer_json(p).get("incompleto")]
    if incompletas:
        print("Quedaron incompletas (pause_turn/max_tokens), rehazlas con prueba --ids:", " ".join(incompletas))


def validar():
    df = cargar_reformas()
    filas = []
    for fila in df.itertuples():
        r = leer_json(DIR_ETAPA[0] / f"{fila.ID}.json") or {}
        filas.append({"ID": fila.ID, "ISO": fila.ISO, "COUNTRY": fila.COUNTRY, "YEAR": fila.YEAR,
                      "NORMA_PROPUESTA": r.get("norma_propuesta", ""), "OTRAS": r.get("otras", ""),
                      "CONFIANZA": normalizar_confianza(r.get("confianza", "")), "NORMA": "", "OTRAS_VALIDADAS": "",
                      "RESUMEN": re.split(r"\n\s*\**NORMA\**\s*:", r.get("texto", ""))[0].strip(),
                      "FUENTES": "\n".join(f["url"] for f in r.get("fuentes", [])[:5])})
    nuevo = pd.DataFrame(filas)
    if EXCEL_VALIDADAS.exists():  # conserva lo que ya validaste a mano (NORMA y OTRAS_VALIDADAS)
        viejo = pd.read_excel(EXCEL_VALIDADAS).set_index("ID")
        for col in ("NORMA", "OTRAS_VALIDADAS"):
            if col in viejo.columns:
                nuevo[col] = nuevo.ID.map(viejo[col]).fillna("")
    try:
        nuevo.to_excel(EXCEL_VALIDADAS, index=False)
    except PermissionError:
        sys.exit(f"No pude escribir {EXCEL_VALIDADAS.name}: ciérralo en Excel y vuelve a correr.")
    print(f"Escrito {EXCEL_VALIDADAS.name}. Revisa NORMA_PROPUESTA y escribe la norma correcta en la "
          f"columna NORMA (vacía = se usa la propuesta); si corriges las otras normas del año, "
          f"escríbelas en OTRAS_VALIDADAS. Pendientes de etapa 0: {(nuevo.NORMA_PROPUESTA == '').sum()}")


def exportar():
    props = esquema()["properties"]
    todas_dims = props["dimensiones"]["items"]["properties"]["dimension"]["enum"]
    todas_pobs = props["poblacion_objetivo"]["items"]["enum"]
    codigo = {"aumenta": 1, "reduce": -1, "reorganiza": 0}
    filas, largo, bitacora = [], [], []
    revision, correcciones, ambito = cargar_revision_manual(), cargar_correcciones(), cargar_ambito()
    validadas_humano = set()
    if EXCEL_VALIDADAS.exists():
        v = pd.read_excel(EXCEL_VALIDADAS)
        validadas_humano = set(v.loc[v["NORMA"].notna() & (v["NORMA"].astype(str).str.strip() != ""), "ID"])
    for ruta in sorted(DIR_ETAPA[2].glob("*.json")):
        r = leer_json(ruta)
        c = r.get("caracterizacion")
        if not c:
            print(f"  {r['id']}: sin caracterización válida, omitida.")
            continue
        fila = {"ID": r["id"], "ISO": r["iso"], "COUNTRY": r["pais"], "YEAR": r["anio"]}
        for k, v in c.items():
            if k in ("dimensiones", "poblacion_objetivo", "normas_relacionadas"):
                continue
            fila[k] = v
        fila["normas_relacionadas"] = "; ".join(c["normas_relacionadas"])
        # La confianza de IDENTIFICACIÓN la decide la etapa 0 (la que buscó la norma). En la corrida
        # del 24-oct la etapa 2 la subió de media a alta en 9 casos sin evidencia nueva.
        r0_conf = normalizar_confianza((leer_json(DIR_ETAPA[0] / f"{r['id']}.json") or {}).get("confianza", ""))
        fila["confianza_identificacion_e2"] = c.get("confianza_identificacion")
        if r0_conf:
            fila["confianza_identificacion"] = r0_conf
        # Año efectivo de tratamiento (de la fecha de entrada en vigor) y si difiere del año de la base.
        m = re.match(r"(\d{4})", str(c.get("fecha_entrada_vigencia", "")))
        fila["anio_vigencia"] = int(m.group(1)) if m else None
        fila["vigencia_difiere_anio_base"] = int(bool(m) and int(m.group(1)) != int(r["anio"]))
        # Todas las dimensiones siempre presentes. dim_* = texto; cod_* = +1 aumenta, -1 reduce,
        # 0 reorganiza, vacío = la reforma no toca esa dimensión. Si hay varias entradas de la
        # misma dimensión (p. ej. varias 'otro'), se unen los textos y cod_ queda vacío si discrepan.
        efectos = {}
        for d in c["dimensiones"]:
            efectos.setdefault(d["dimension"], []).append(d["efecto_en_proteccion"])
            largo.append({"ID": r["id"], **d, "origen": "etapa2"})
        for dim in todas_dims:
            e = efectos.get(dim, [])
            fila[f"dim_{dim}"] = "/".join(dict.fromkeys(e))
            fila[f"cod_{dim}"] = codigo[e[0]] if e and len(set(e)) == 1 else None
        for pob in todas_pobs:
            fila[f"pob_{pob}"] = int(pob in c["poblacion_objetivo"])
        fila["fuentes"] = "\n".join(f["url"] for f in r.get("fuentes", []))
        fila["ambito_reforma"] = ambito.get(r["id"], "")   # Config/ambito_reforma.csv
        # Alerta para revisión manual: motivos manuales + automáticos.
        motivos = []
        if r["id"] in ALERTAS_MANUALES:
            motivos.append(ALERTAS_MANUALES[r["id"]])
        r0 = leer_json(DIR_ETAPA[0] / f"{r['id']}.json") or {}
        conf0 = normalizar_confianza(r0.get("confianza", ""))
        if conf0 in ("media", "baja"):
            motivos.append(f"Identificación con confianza {conf0}")
        oficiales = sum(bool(PATRON_OFICIAL.search(f["url"])) for f in r.get("fuentes", []))
        if oficiales <= UMBRAL_FUENTES_OFICIALES:
            motivos.append(f"Pocas fuentes oficiales/académicas ({oficiales})")
        r1 = leer_json(DIR_ETAPA[1] / f"{r['id']}.json") or {}
        sin_fuente = len(re.findall(r"sin fuente", r1.get("texto", ""), re.I))
        if sin_fuente >= 4:
            motivos.append(f"Varias afirmaciones sin fuente ({sin_fuente})")
        # Campos derivados por defecto (antes de correcciones).
        fila["tipo_fecha"] = "vigencia_segun_etapa2_no_auditada"
        fila["fecha_tratamiento"] = None
        fila["excluir_confiabilidad"] = 0
        fila["eventos_posteriores_2025"] = ""
        # Correcciones verificadas (Config/correcciones.csv), con bitácora del valor anterior.
        for cor in correcciones.get(r["id"], []):
            campo = cor["campo"]
            anterior = fila.get(campo)
            if campo.startswith("dim_"):
                # Recodificación de una dimensión (regla R8): valor = aumenta | reduce | reorganiza,
                # o vacío = la reforma no toca esa dimensión. Actualiza dim_, cod_ y la hoja larga.
                dim, efecto = campo[4:], cor["valor_nuevo"].strip()
                if dim not in todas_dims or (efecto and efecto not in codigo):
                    sys.exit(f"Corrección inválida en {r['id']}: {campo}={efecto!r}")
                fila[campo] = efecto
                fila[f"cod_{dim}"] = codigo.get(efecto)
                largo = [x for x in largo if not (x["ID"] == r["id"] and x["dimension"] == dim)]
                if efecto:
                    largo.append({"ID": r["id"], "dimension": dim, "efecto_en_proteccion": efecto,
                                  "comentario": cor["motivo"], "origen": "correccion_revision"})
            else:
                fila[campo] = convertir(campo, cor["valor_nuevo"])
            bitacora.append({"ID": r["id"], "campo": campo, "valor_anterior": anterior,
                             "valor_nuevo": fila[campo], "motivo": cor["motivo"],
                             "fuente": cor["fuente"], "fecha_revision": FECHA_REVISION})
        # Derivados finales (después de correcciones).
        if not fila["fecha_tratamiento"]:
            fila["fecha_tratamiento"] = fila.get("fecha_entrada_vigencia")
        fila["anio_vigencia"] = anio_de(fila.get("fecha_entrada_vigencia"))
        fila["anio_tratamiento"] = anio_de(fila["fecha_tratamiento"])
        fila["vigencia_difiere_anio_base"] = int(fila["anio_vigencia"] is not None
                                                 and fila["anio_vigencia"] != int(r["anio"]))
        # Revisión manual (Config/revision_manual.csv).
        rev = revision.get(r["id"])
        fila["motivos_alerta_inicial"] = "; ".join(motivos)   # alertas automáticas originales (histórico)
        color = (rev or {}).get("color", "").strip()
        if color:
            fila["revision_manual"] = color
            fila["alerta_manual"] = int(color != "verde")
            fila["motivo_alerta"] = rev["nota"] if color != "verde" else ""
            fila["validacion"] = f"revisión IA con fuentes ({FECHA_REVISION})"
        else:
            fila["revision_manual"] = "sin_revisar" if motivos else "sin_alerta"
            fila["alerta_manual"] = int(bool(motivos))
            fila["motivo_alerta"] = "; ".join(motivos)
            fila["validacion"] = (f"nota de revisión IA ({FECHA_REVISION})" if rev
                                  else "sin validación adicional")
        fila["nota_revision"] = (rev or {}).get("nota", "")
        fila["fuentes_revision"] = (rev or {}).get("fuentes", "")
        if r["id"] in validadas_humano:
            fila["validacion"] += "; NORMA validada por humano"
        filas.append(fila)
    if not filas:
        return print("No hay resultados de la etapa 2.")
    ancho = pd.DataFrame(filas)
    cod_cols = [c for c in ancho.columns if c.startswith("cod_")]
    ancho[cod_cols] = ancho[cod_cols].astype("Int64")
    for col in ("anio_vigencia", "anio_tratamiento"):   # enteros aunque haya vacíos (p. ej. no_aplicada)
        ancho[col] = ancho[col].astype("Int64")
    with pd.ExcelWriter(EXCEL_FINAL, engine="openpyxl") as xw:
        ancho.to_excel(xw, sheet_name="Reformas", index=False)
        pd.DataFrame(largo).sort_values("ID", kind="stable").to_excel(
            xw, sheet_name="Dimensiones_largo", index=False)
        pd.DataFrame(bitacora, columns=["ID", "campo", "valor_anterior", "valor_nuevo", "motivo",
                                        "fuente", "fecha_revision"]).to_excel(
            xw, sheet_name="Bitacora_correcciones", index=False)
    print(f"Escrito {EXCEL_FINAL} con {len(ancho)} reformas y {len(bitacora)} correcciones registradas.")


# ----------------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Caracterización de reformas laborales con Claude")
    ap.add_argument("comando", choices=["prueba", "prueba-batch", "batch-enviar", "batch-recoger",
                                        "validar", "exportar"])
    ap.add_argument("--etapa", type=int, choices=[0, 1, 2])
    ap.add_argument("--ids", nargs="+", help="IDs tipo COL_2002")
    ap.add_argument("--todas", action="store_true", help="en modo prueba, procesa las 55")
    ap.add_argument("--forzar", action="store_true", help="rehace reformas ya procesadas")
    ap.add_argument("--esperar", action="store_true", help="en batch-recoger, espera a que termine")
    a = ap.parse_args()

    if a.comando in ("batch-enviar", "batch-recoger") and a.etapa is None:
        ap.error("este comando requiere --etapa 0, 1 o 2")
    if a.comando == "prueba" and a.etapa is None:
        # sin --etapa: corre 0 -> 1 -> 2 seguidas (usa la norma propuesta) y exporta, para revisar a mano
        for e in (0, 1, 2):
            print(f"\n=== Etapa {e} ===")
            correr_prueba(e, a.ids, a.todas, a.forzar)
        validar()
        exportar()
        print("\nRevisa: Datos/normas_validadas.xlsx, Salida/reformas_caracterizadas.xlsx "
              "y los textos completos en Salida/etapa1/*.json")
    elif a.comando == "prueba":
        correr_prueba(a.etapa, a.ids, a.todas, a.forzar)
    elif a.comando == "prueba-batch":
        prueba_batch(a.ids, a.forzar)
    elif a.comando == "batch-enviar":
        batch_enviar(a.etapa, a.ids, a.forzar)
    elif a.comando == "batch-recoger":
        batch_recoger(a.etapa, a.esperar)
    elif a.comando == "validar":
        validar()
    else:
        exportar()


if __name__ == "__main__":
    main()

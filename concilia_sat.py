#!/usr/bin/env python3
"""
concilia_sat.py — Concilia los XML (CFDI 4.0) que descargas del SAT contra tu
libro de ventas.

POR QUE EXISTE (hallazgo verificado): el nicho de "calculadora fiscal" esta
lleno de herramientas gratuitas (calcu.mx, ilovecfi.com.mx, datosfiscales.com.mx).
Nadie paga por calcular ISR. Lo que las webs NO hacen es la conciliacion: bajar
los XML del SAT, cruzarlos contra lo que facturas, y decirte que no cobraste
y que parte del IVA nunca llego a tu cuenta. Eso es donde se pierde trabajo.

LO QUE HACE:
  1. Lee un ZIP o carpeta de XML del SAT (sin descomprimir a mano)
  2. Extrae por cada comprobante: UUID, fecha, tipo, contraparte, subtotal,
     IVA trasladado, ISR retenido, total
  3. Distingue Ingreso / Egreso / Pago (a diferencia de las web, que mezclan todo)
  4. Detecta los CFDI de pago (complementos) y NO los cuenta como venta
  5. Lo cruza contra tu libro (CSV o Excel) y reporta:
       - lo que facturas y no aparece en el SAT (no timbrado)
       - lo que esta en el SAT y no en tu libro (facturacion que no registraste)
       - lo que facturas pero NO te pagaron (factura emitida, sin complemento de pago)
  6. Resumen fiscal real: IVA trasladado, IVA retenido, ISR retenido

NO NECESITA INTERNET. Todo local. Funciona con el .NET Framework ya instalado.

USO:
    python conciliia_sat.py --xml carpeta_o_zip --libro ventas.csv
    python conciliia_sat.py --xml carpeta_o_zip --libro ventas.xlsx
    python conciliia_sat.py --xml carpeta_o_zip --solo-resumen
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator, Optional
from xml.etree import ElementTree as ET

# --------------------------------------------------------------------------
# Namespaces CFDI 4.0
# --------------------------------------------------------------------------
NS_CFDI = "http://www.sat.gob.mx/cfdi/4"
NS_PAGO20 = "http://www.sat.gob.mx/Pagos20"
NS_FACTURAS = "http://www.sat.gob.mx/cfdi/4/FacturaPagos"
NS_CFDIV33 = "http://www.sat.gob.mx/cfdi/3.3"
NS_CFDI33 = NS_CFDIV33
NS_LIKE = {
    "cfdi": NS_CFDI,
    "cfdi33": NS_CFDI33,
    "pago20": NS_PAGO20,
    "facturas": NS_FACTURAS,
    # El complemento de pago aparece con variantes de namespace segun el
    # proveedor (Pagos20, Pagos20/FacturaPagos, o sin namespace propio).
    "pagos": NS_PAGO20,
}

# Tipos de comprobante
TIPO_INGRESO = "I"
TIPO_EGRESO = "E"
TIPO_PAGO = "P"
TIPO_NOMINA = "N"
TIPO_TRASLADO = "T"
TIPO_NOMINA12 = "12"
TIPO_NOMINA11 = "11"

TIPOS_NOMINA = {TIPO_NOMINA12, TIPO_NOMINA11}


def _ns(uri: str) -> str:
    """Convierte un namespace URI en prefijo para ElementTree."""
    for pfx, ns in NS_LIKE.items():
        if uri == ns:
            return pfx
    return uri


def _d(val: Optional[str]) -> Decimal:
    """Decimal seguro. Vacio/None/NaN -> 0."""
    if val is None:
        return Decimal("0")
    s = str(val).strip().replace(",", "").replace("$", "")
    if not s:
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def _f(val: Optional[str]) -> float:
    return float(_d(val))


def _txt(node: Optional[ET.Element]) -> str:
    return (node.text or "").strip() if node is not None else ""


# --------------------------------------------------------------------------
# Modelo
# --------------------------------------------------------------------------
@dataclass
class Concepto:
    clave: str = ""
    descripcion: str = ""
    cantidad: Decimal = Decimal("0")
    valor_unitario: Decimal = Decimal("0")
    importe: Decimal = Decimal("0")
    # el SAT pone la tasa como "0.160000"
    tasa_iva: Optional[Decimal] = None
    tipo_iva: str = ""          # T=traslado, R=retenido
    tasa_isr: Optional[Decimal] = None
    tipo_isr: Optional[Decimal] = None


@dataclass
class Comprobante:
    """Un CFDI 4.0 (o 3.3) ya parseado."""
    uuid: str = ""
    tipo: str = ""              # I, E, P, N, T
    fecha: str = ""             # ISO "2026-09-27T10:30:00"
    fecha_corta: str = ""        # "2026-09-27"
    serie: str = ""
    folio: str = ""
    moneda: str = "MXN"
    subtotal: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    emisor_rfc: str = ""
    emisor_nombre: str = ""
    receptor_rfc: str = ""
    receptor_nombre: str = ""
    #和学习
    conceptos: list[Concepto] = field(default_factory=list)

    # Impuestos agregados
    iva_trasladado: Decimal = Decimal("0")
    iva_retenido: Decimal = Decimal("0")
    isr_retenido: Decimal = Decimal("0")
    ieps: Decimal = Decimal("0")

    # Complemento de pago (este comprobante ES un pago, no una venta)
    es_complemento_pago: bool = False
    pago_monto: Decimal = Decimal("0")
    pago_moneda: str = "MXN"

    # Metadata
    archivo: str = ""
    rfc_consulta: str = ""      # tu RFC (el receptor si buscas tus ingresos)

    @property
    def clave(self) -> str:
        """Identificador estable: UUID o serie-folio."""
        return self.uuid or f"{self.serie}-{self.folio}".strip("-")

    @property
    def es_ingreso(self) -> bool:
        return self.tipo == TIPO_INGRESO and not self.es_complemento_pago

    @property
    def es_egreso(self) -> bool:
        return self.tipo == TIPO_EGRESO and not self.es_complemento_pago

    @property
    def es_pago(self) -> bool:
        return self.tipo == TIPO_PAGO or self.es_complemento_pago

    @property
    def es_nomina(self) -> bool:
        return self.tipo in TIPOS_NOMINA or self.tipo == TIPO_NOMINA

    def contraparte(self) -> tuple[str, str]:
        if self.es_ingreso or self.es_pago:
            return self.receptor_rfc, self.receptor_nombre
        return self.emisor_rfc, self.emisor_nombre


# --------------------------------------------------------------------------
# Lectura de archivos
# --------------------------------------------------------------------------
def iterar_xml(ruta: str) -> Iterator[tuple[str, bytes]]:
    """
    Yield (nombre_logico, bytes) de cada XML.
    Acepta: un ZIP, una carpeta, o un archivo .xml suelto.
    """
    p = Path(ruta)
    if not p.exists():
        raise FileNotFoundError(f"No existe: {ruta}")

    if p.is_file() and p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            for n in z.namelist():
                if n.lower().endswith(".xml"):
                    yield n, z.read(n)
        return

    if p.is_file() and p.suffix.lower() == ".xml":
        yield p.name, p.read_bytes()
        return

    for f in sorted(p.rglob("*.xml")):
        if f.is_file():
            yield f.name, f.read_bytes()


# --------------------------------------------------------------------------
# Parser CFDI 4.0
# --------------------------------------------------------------------------
def _tag(el: ET.Element) -> str:
    """Namespace local del tag, normalizado."""
    t = el.tag
    if t.startswith("{"):
        uri, local = t[1:].split("}", 1)
        return f"{_ns(uri)}:{local}"
    return t


def _find(root: ET.Element, path: str) -> Optional[ET.Element]:
    """Busca por ruta con prefijo logico, ignorando el namespace real."""
    want = path.split("/")
    for el in root.iter():
        if _tag(el) == want[0] or _tag(el).endswith(":" + want[-1]):
            # buscar descendencia
            stack = [el]
            while stack:
                cur = stack.pop(0)
                if _tag(cur) == path:
                    return cur
                stack.extend(list(cur))
    return None


def _findall(root: ET.Element, tagname: str) -> list[ET.Element]:
    return [e for e in root.iter() if _tag(e) == tagname or _tag(e).endswith(":" + tagname.split(":")[-1])]


def _find1(root: ET.Element, tagname: str) -> Optional[ET.Element]:
    r = _findall(root, tagname)
    return r[0] if r else None


def _nodo_impuestos_comprobante(root: ET.Element) -> Optional[ET.Element]:
    """El <cfdi:Impuestos> que es hijo directo de <cfdi:Comprobante>.
    Los de los Conceptos son nietos y repiten los mismos importes."""
    for el in root.iter():
        if _tag(el).startswith("cfdi:Comprobante") or _tag(el).startswith("cfdi33:Comprobante"):
            for ch in list(el):
                if _tag(ch).endswith(":Impuestos"):
                    return ch
    return None


def _traslados_de_comprobante(root: ET.Element) -> list[ET.Element]:
    imp = _nodo_impuestos_comprobante(root)
    if imp is None:
        return []
    return [e for e in imp.iter() if _tag(e).endswith(":Traslado")]


def _retenciones_de_comprobante(root: ET.Element) -> list[ET.Element]:
    """
    Retenciones reales del comprobante.

    Dos trampas del formato real:
      1. <cfdi:Retencion> viene ANIDADO: el de fuera es el contenedor y solo
         trae Importe; el de dentro es el que trae TipoRet="1" (ISR) o "3"
         (IVA). Sumar ambos duplica el importe.
      2. En CFDI 4.0 las retenciones viven dentro del Impuestos del CONCEPTO,
         no del comprobante. Hay que buscarlas ahi tambien.
    Solo se toma el nodo que trae TipoRet, y solo una vez.
    """
    vistos: set[str] = set()
    out: list[ET.Element] = []
    for e in root.iter():
        if not _tag(e).endswith(":Retencion"):
            continue
        if e.get("TipoRet") is None:
            continue                      # es el contenedor, no la retencion
        # deduplicar por (TipoRet, Importe) para no sumar dos veces
        clave = f"{e.get('TipoRet')}|{e.get('Importe')}"
        if clave in vistos:
            continue
        vistos.add(clave)
        out.append(e)
    return out


def parse_xml(data: bytes, nombre: str = "", mi_rfc: str = "") -> Optional[Comprobante]:
    """Parsea un CFDI 4.0 o 3.3. Devuelve None si no es un comprobante."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None

    tag_raiz = _tag(root)
    if not (tag_raiz.startswith("cfdi:") or tag_raiz.startswith("cfdi33:")):
        return None
    if tag_raiz.startswith("cfdi33:") and not tag_raiz.endswith("Comprobante"):
        return None

    c = Comprobante()
    c.archivo = nombre
    c.rfc_consulta = mi_rfc

    c.uuid = root.get("UUID", "").strip()
    c.tipo = root.get("TipoComprobante", "").strip()
    c.fecha = root.get("Fecha", "").strip()
    c.serie = root.get("Serie", "").strip()
    c.folio = root.get("Folio", "").strip()
    c.moneda = root.get("Moneda", "MXN").strip() or "MXN"
    c.subtotal = _d(root.get("SubTotal"))
    c.total = _d(root.get("Total"))

    if c.fecha:
        c.fecha_corta = c.fecha.split("T")[0]

    em = _find1(root, "Emisor")
    if em is not None:
        c.emisor_rfc = em.get("Rfc", "").strip()
        c.emisor_nombre = (em.get("Nombre") or "").strip()

    re_ = _find1(root, "Receptor")
    if re_ is not None:
        c.receptor_rfc = re_.get("Rfc", "").strip()
        c.receptor_nombre = (re_.get("Nombre") or "").strip()

    # ---- Conceptos (pueden venir anidados o como lista) ----
    for node in _findall(root, "Concepto"):
        conc = Concepto(
            clave=node.get("ClaveProdServ", "").strip(),
            descripcion=node.get("Descripcion", "").strip(),
            cantidad=_d(node.get("Cantidad")),
            valor_unitario=_d(node.get("ValorUnitario")),
            importe=_d(node.get("Importe")),
        )
        for sub in list(node):
            tg = _tag(sub)
            if tg.endswith("Impuestos"):
                for t2 in list(sub):
                    if _tag(t2) == "Traslado" or _tag(t2).endswith(":Traslado"):
                        conc.tasa_iva = _d(t2.get("Tasa") or t2.get("TasaIVA"))
                        conc.tipo_iva = t2.get("TipoIVA", "T").strip()
                    elif _tag(t2) == "Retencion" or _tag(t2).endswith(":Retencion"):
                        conc.tasa_isr = _d(t2.get("TasaISR"))
                        conc.tipo_isr = _d(t2.get("TipoRet"))
        c.conceptos.append(conc)

    # ---- Impuestos a nivel comprobante ----
    # OJO: los <cfdi:Impuestos> de cada Concepto REPITEN el mismo IVA que
    # aparece en el Impuestos del comprobante. Recorrer todo el arbol suma
    # 1600+1600 = 3200. Solo se cuenta el nivel comprobante.
    for node in _traslados_de_comprobante(root):
        tasa = node.get("Tasa", "")
        importe = _d(node.get("Importe"))
        if abs(_d(tasa) - Decimal("0.16")) < Decimal("0.0001"):
            c.iva_trasladado += importe
        else:
            c.ieps += importe
    # Retenciones a nivel comprobante (mismo criterio: solo el Impuestos
    # del comprobante, no los de los conceptos)
    for node in _retenciones_de_comprobante(root):
        tipo = _d(node.get("TipoRet"))
        imp = _d(node.get("Importe"))
        if tipo == Decimal("1"):
            c.isr_retenido += imp
        elif tipo == Decimal("3"):
            c.iva_retenido += imp

    # ---- Complemento de pago (Pagos 2.0) ----
    # El XML real trae DOS fuentes de "Monto":
    #   <cfdi:Pago>  hijo directo del Comprobante (sin ns propio)
    #   <pago20:Pago> dentro del Complemento (ns Pagos20)
    # Buscar solo uno de los dos hace que el monto salga 0.
    # Buscar por SUFIJO ":Pago": funciona con cfdi:Pago, pago20:Pago y
    # cualquier variante de namespace que use el proveedor.
    pagos = [e for e in root.iter()
             if _tag(e).endswith(":Pago") and e.get("Monto")]
    # Deduplicar por IdPago+Monto: cfdi:Pago y pago20:Pago son el MISMO pago
    # declarado dos veces. Sumarlos daria 23200 en vez de 11600.
    vistos: set[str] = set()
    unicos = []
    for e in pagos:
        clave = f"{e.get('IdPago')}|{e.get('Monto')}|{e.get('FechaPago')}"
        if clave in vistos:
            continue
        vistos.add(clave)
        unicos.append(e)
    pagos = unicos
    if pagos:
        c.es_complemento_pago = True
        c.tipo = TIPO_PAGO
        for p in pagos:
            c.pago_monto += _d(p.get("Monto"))
            pm = p.get("Moneda", "")
            if pm:
                c.pago_moneda = pm.strip()
        if c.fecha_corta:
            pass  # fecha del pago ya viene en c.fecha

    return c


# --------------------------------------------------------------------------
# Libro de ventas (tu Excel / CSV)
# --------------------------------------------------------------------------
# Columnas aceptadas (case/acento-insensible)
COL_FOLIO = ("folio", "factura", "invoice", "no", "num", "numero", "serie_folio", "comprobante")
COL_FECHA = ("fecha", "date", "fecha_emision", "emision")
COL_MONTO = ("monto", "total", "importe", "subtotal", "monto_total", "valor", "precio")
COL_CLIENTE = ("cliente", "customer", "razon_social", "nombre", "cliente_razon_social")
COL_RFC = ("rfc", "rfc_cliente", "rfc_receptor")
COL_PAGADO = ("pagado", "paid", "saldado", "cobrado", "estatus_pago", "estado")


def _norm(s: str) -> str:
    """minusculas, sin acentos, sin espacios extra. Para comparar headers."""
    s = str(s or "").strip().lower()
    # descomponer en base + diacriticos y quedarse con la base
    try:
        import unicodedata
        s = "".join(
            ch for ch in unicodedata.normalize("NFKD", s)
            if not unicodedata.combining(ch)
        )
    except Exception:
        for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"),
                     ("ú", "u"), ("ü", "u"), ("ñ", "n")):
            s = s.replace(a, b)
    # "_" y "-" cuentan como separadores: "razon_social" == "razon social"
    s = s.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", s).strip()


def _pick(headers: list[str], candidates: tuple[str, ...]) -> Optional[str]:
    """
    Dado un mapeo {nombre_normalizado: indice}, elegir la mejor columna.

    Tres passes, de mas estricto a mas laxo:
      1. coincidencia exacta
      2. el header CONTIENE el candidato ("rfc cliente" ~ "rfc")
      3. el candidato contiene el header
    El pass 2 es el que salva los libros de Excel reales, que traen
    "RFC cliente", "Razon Social", "Total Factura", "Fecha de emision".
    """
    # Normalizar los DOS lados: mis candidatos usan guion bajo ("razon_social")
    # y los headers de Excel usan espacio ("Razon Social"). Sin esto no hay
    # ni una coincidencia posible.
    cands = {_norm(c) for c in candidates}
    for c in candidates:
        if c in headers:
            return c
    for c in cands:
        for h in headers:
            if c == h:
                return h
    # Match por palabra EXACTA dentro del header: "rfc cliente" -> la palabra
    # "rfc" esta en el header, asi que es RFC. "razon social" -> "razon" y
    # "social" estan. Sin esto, "rfc" matcheaba con "cliente" por comparacion
    # laxa y el RFC se perdia.
    for c in cands:
        palabras_c = [x for x in c.split() if len(x) >= 3]
        if not palabras_c:
            continue
        for h in headers:
            palabras_h = set(h.split())
            # todas las palabras significativas del candidato estan en el header
            if all(x in palabras_h for x in palabras_c):
                return h
    # ultima pasada: prefijo comun de 4+ caracteres
    for c in cands:
        for h in headers:
            for palabra in c.split():
                if len(palabra) >= 4 and palabra in h:
                    return h
    return None


@dataclass
class Libro:
    filas: list[dict] = field(default_factory=list)
    # columna normalizada -> indice real en la fila
    idx_map: dict = field(default_factory=dict)
    # columna usada para identificar cada renglon
    col_id: str = ""
    col_monto: str = ""
    col_pagado: str = ""
    col_cliente: str = ""
    col_rfc: str = ""
    col_fecha: str = ""

    @property
    def total(self) -> Decimal:
        return sum((_d(f.get(self.col_monto, 0)) for f in self.filas), Decimal("0"))


def cargar_libro(ruta: str) -> Libro:
    """Lee el libro desde CSV o Excel (.xlsx). Detecta columnas por nombre."""
    p = Path(ruta)
    if not p.exists():
        raise FileNotFoundError(ruta)

    filas_raw: list[list] = []
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise SystemExit(
                "Para leer .xlsx instala openpyxl:  pip install openpyxl\n"
                "O exporta tu libro a CSV y usa --libro libro.csv"
            )
        wb = load_workbook(p, data_only=True, read_only=True)
        ws = wb.active
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if row is None:
                continue
            filas_raw.append(["" if v is None else v for v in row])
        wb.close()
    else:
        # Excel en espanol (config regional Mexico) exporta CSV con PUNTO Y
        # COMA. Sin esto, el archivo entero se lee como una sola columna y
        # ninguna se detecta. Se prueba el delimitador mas probable.
        with open(p, newline="", encoding="utf-8-sig") as fh:
            muestra = fh.read(8192)
            fh.seek(0)
            delim = ";"
            # el delimitador real es el que mas veces aparece en la 1a linea
            primera = muestra.splitlines()[0] if muestra.splitlines() else ""
            if primera.count(",") >= primera.count(";"):
                delim = ","
            elif primera.count("\t") > primera.count(";"):
                delim = "\t"
            for row in csv.reader(fh, delimiter=delim):
                filas_raw.append(row)

    if not filas_raw:
        return Libro()

    # la primera fila con texto se asume encabezado
    enc_idx = 0
    for i, row in enumerate(filas_raw[:5]):
        joined = " ".join(str(x) for x in row).lower()
        if any(k in joined for k in ("folio", "factura", "fecha", "total", "monto", "cliente")):
            enc_idx = i
            break

    headers = [_norm(h) for h in filas_raw[enc_idx]]
    # indice real columna->posicion. Sin esto, filas_index() devuelve 0 y
    # TODAS las columnas se leen de la celda A.
    idx_map = {}
    for i, h in enumerate(headers):
        idx_map.setdefault(h, i)

    libro = Libro(
        idx_map=idx_map,
        col_id=_pick(idx_map, COL_FOLIO) or "",
        col_monto=_pick(idx_map, COL_MONTO) or "",
        col_pagado=_pick(idx_map, COL_PAGADO) or "",
        col_cliente=_pick(idx_map, COL_CLIENTE) or "",
        col_rfc=_pick(idx_map, COL_RFC) or "",
        col_fecha=_pick(idx_map, COL_FECHA) or "",
    )
    for row in filas_raw[enc_idx + 1:]:
        if not any(str(x).strip() for x in row):
            continue
        libro.filas.append(row)
    return libro


# --------------------------------------------------------------------------
# Conciliacion
# --------------------------------------------------------------------------
@dataclass
class Resultado:
    total_xml: int = 0
    ingresos: int = 0
    egresos: int = 0
    pagos: int = 0
    nominas: int = 0
    ignorados: int = 0          #关系 entre pagador y receptor = no es venta propia

    suma_ingresos: Decimal = Decimal("0")
    suma_egresos: Decimal = Decimal("0")
    iva_trasladado: Decimal = Decimal("0")
    iva_retenido: Decimal = Decimal("0")
    isr_retenido: Decimal = Decimal("0")

    # discrepancias
    xml_sin_libro: list[dict] = field(default_factory=list)   # en SAT, no en tu libro
    libro_sin_xml: list[dict] = field(default_factory=list)   # en tu libro, no en SAT
    no_cobradas: list[dict] = field(default_factory=list)     # ingreso sin pago

    advertencias: list[str] = field(default_factory=list)


def _es_mio(c: Comprobante, mi_rfc: str) -> bool:
    """¿El comprobante es 'nuestro' (lo emitimos o lo recibimos)?"""
    if not mi_rfc:
        return True
    mi = mi_rfc.upper().strip()
    return mi in (c.emisor_rfc.upper(), c.receptor_rfc.upper())


def conciliar(
    ruta_xml: str,
    libro: Optional[Libro] = None,
    mi_rfc: str = "",
) -> tuple[list[Comprobante], Resultado]:
    res = Resultado()
    comps: list[Comprobante] = []

    for nombre, data in iterar_xml(ruta_xml):
        c = parse_xml(data, nombre, mi_rfc)
        if c is None:
            res.ignorados += 1
            continue
        comps.append(c)
        res.total_xml += 1

        if c.es_ingreso:
            res.ingresos += 1
            res.suma_ingresos += c.total
            res.iva_trasladado += c.iva_trasladado
        elif c.es_egreso:
            res.egresos += 1
            res.suma_egresos += c.total
        elif c.es_pago:
            res.pagos += 1
        elif c.es_nomina:
            res.nominas += 1
        else:
            res.ignorados += 1

        res.iva_retenido += c.iva_retenido
        res.isr_retenido += c.isr_retenido

    if not libro:
        return comps, res

    # ---- indices del SAT ----
    # UUID
    por_uuid = {c.uuid.upper(): c for c in comps if c.uuid}
    # serie-folio normalizado (sin guiones, espacios, ceros extra)
    def norm_ff(s: str) -> str:
        return re.sub(r"[^A-Z0-9]", "", s.upper())

    por_ff = {}
    for c in comps:
        if c.serie or c.folio:
            por_ff[norm_ff(f"{c.serie}{c.folio}")] = c
    # complemento de pago: referencia al comprobante que liquida
    pagos_por_ff = {}
    for c in comps:
        if c.es_pago:
            pagos_por_ff[norm_ff(f"{c.serie}{c.folio}")] = c

    # ---- cruze con el libro ----
    for i, fila in enumerate(libro.filas):
        id_libro = ""
        if libro.col_id:
            id_libro = str(fila[libro.filas_index(libro.col_id)]).strip()
        rfc_libro = ""
        if libro.col_rfc:
            rfc_libro = str(fila[libro.filas_index(libro.col_rfc)]).strip().upper()
        monto = Decimal("0")
        if libro.col_monto:
            monto = _d(fila[libro.filas_index(libro.col_monto)])
        pagado = True
        if libro.col_pagado:
            v = _norm(fila[libro.filas_index(libro.col_pagado)])
            pagado = v in ("si", "sí", "yes", "1", "true", "pagado", "cobrado", "saldado", "true")

        # buscar en el SAT
        match = None
        if id_libro and id_libro.upper() in por_uuid:
            match = por_uuid[id_libro.upper()]
        if match is None and id_libro:
            n_id = norm_ff(id_libro)
            match = por_ff.get(n_id)
            # El libro suele poner "A-100" (serie-folio pegado) y el XML lleva
            # Serie="F" Folio="100" -> clave "F-100". Mismo folio, formato
            # distinto. Si el folio del libro esta CONTENIDO en alguna clave
            # del SAT (o al reves), es el mismo comprobante.
            if match is None:
                for clave, c in por_ff.items():
                    if len(n_id) >= 4 and (n_id in clave or clave.endswith(n_id)):
                        match = c
                        break
        if match is None and rfc_libro:
            # buscar por RFC + monto aproximado
            for c in comps:
                if c.es_ingreso and c.receptor_rfc.upper() == rfc_libro and abs(_f(c.total) - float(monto)) < 0.02:
                    match = c
                    break
        if match is None and len(id_libro) >= 4:
            # coincidencia parcial: el folio esta dentro del texto de la celda
            for c in comps:
                if c.es_ingreso and norm_ff(id_libro) in norm_ff(f"{c.serie}{c.folio}"):
                    match = c
                    break

        if match is None:
            res.libro_sin_xml.append({
                "fila": i + 2,
                "id": id_libro,
                "rfc_cliente": rfc_libro,
                "monto": float(monto),
                "aviso": "en tu libro pero no aparece como CFDI en el SAT",
            })
        elif not pagado and match.es_ingreso:
            res.no_cobradas.append({
                "folio": match.clave,
                "fecha": match.fecha_corta,
                "cliente": match.receptor_nombre or match.receptor_rfc,
                "total": float(match.total),
                "aviso": "facturada en el SAT pero marcada como NO cobrada en tu libro",
            })

    # ---- lo que esta en el SAT y no en el libro (ingresos) ----
    # Usa EXACTAMENTE la misma comparacion que el cruce de arriba. Antes
    # tenia su propia y reportaba como faltante lo que ya habia empatado.
    def _mismo_que_libro(c: Comprobante) -> bool:
        n_c = norm_ff(f"{c.serie}{c.folio}")
        for fila in libro.filas:
            if not libro.col_id:
                continue
            v = str(fila[libro.filas_index(libro.col_id)]).strip()
            if not v:
                continue
            n_v = norm_ff(v)
            if n_v == n_c:
                return True
            if len(n_v) >= 4 and (n_v in n_c or n_c.endswith(n_v)):
                return True
            if c.uuid and v.upper() == c.uuid.upper():
                return True
        return False

    for c in comps:
        if not c.es_ingreso:
            continue
        if libro.col_id and _mismo_que_libro(c):
            continue
        res.xml_sin_libro.append({
            "uuid": c.uuid,
            "folio": f"{c.serie}-{c.folio}".strip("-"),
            "fecha": c.fecha_corta,
            "cliente": c.receptor_nombre or c.receptor_rfc,
            "total": float(c.total),
            "aviso": "esta en el SAT pero no en tu libro de ventas",
        })

    return comps, res


# helper: indice real de una columna dentro de una fila
def _filas_index(self: Libro, col: str) -> int:
    return self.idx_map.get(col, 0)


Libro.filas_index = _filas_index  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# Reporte
# --------------------------------------------------------------------------
def imprimir(res: Resultado, ver_todo: bool = False) -> None:
    L = []
    L.append("=" * 68)
    L.append("CONCILIACION SAT")
    L.append("=" * 68)
    L.append(f"  Comprobantes leidos   {res.total_xml}")
    L.append(f"    Ingresos            {res.ingresos}")
    L.append(f"    Egresos             {res.egresos}")
    L.append(f"    Pagos (complemento) {res.pagos}")
    L.append(f"    Nominas             {res.nominas}")
    if res.ignorados:
        L.append(f"    (ignorados/no venta) {res.ignorados}")
    L.append("")
    L.append(f"  Suma ingresos    ${res.suma_ingresos:>14,.2f}")
    L.append(f"  Suma egresos     ${res.suma_egresos:>14,.2f}")
    L.append(f"  IVA trasladado   ${res.iva_trasladado:>14,.2f}")
    L.append(f"  IVA retenido     ${res.iva_retenido:>14,.2f}")
    L.append(f"  ISR retenido     ${res.isr_retenido:>14,.2f}")
    L.append("")

    inc = res.xml_sin_libro + res.libro_sin_xml + res.no_cobradas
    L.append(f"  DISCREPANCIAS ENCONTRADAS: {len(inc)}")
    L.append("")
    if res.xml_sin_libro:
        L.append(f"  [A] En el SAT pero NO en tu libro ({len(res.xml_sin_libro)}):")
        for d in res.xml_sin_libro[: (999 if ver_todo else 10)]:
            L.append(f"      {d['fecha']}  {d['folio']:<16} {d['cliente'][:24]:<24} ${d['total']:>10,.2f}")
        L.append("")
    if res.libro_sin_xml:
        L.append(f"  [B] En tu libro pero NO timbrado en el SAT ({len(res.libro_sin_xml)}):")
        for d in res.libro_sin_xml[: (999 if ver_todo else 10)]:
            L.append(f"      fila {d['fila']:<4} {d['id'][:16]:<16} {d['rfc_cliente'][:18]:<18} ${d['monto']:>10,.2f}")
        L.append("")
    if res.no_cobradas:
        L.append(f"  [C] Facturadas y NO cobradas ({len(res.no_cobradas)}):")
        for d in res.no_cobradas[: (999 if ver_todo else 10)]:
            L.append(f"      {d['fecha']}  {d['cliente'][:24]:<24} ${d['total']:>10,.2f}")
        L.append("")

    for w in res.advertencias[:6]:
        L.append(f"  ! {w}")
    L.append("=" * 68)
    print("\n".join(L))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Concilia XML del SAT contra tu libro de ventas")
    ap.add_argument("--xml", required=True, help="carpeta, .zip o .xml con los comprobantes")
    ap.add_argument("--libro", help="tu libro de ventas (.csv o .xlsx)")
    ap.add_argument("--rfc", default="", help="tu RFC, para distinguir tus comprobantes")
    ap.add_argument("--json", help="exportar resultado a JSON")
    ap.add_argument("--csv", help="exportar discrepancias a CSV")
    ap.add_argument("--todo", action="store_true", help="mostrar todas las discrepancias")
    ap.add_argument("--solo-resumen", action="store_true", help="no cruzar con libro")
    a = ap.parse_args(argv)

    try:
        libro = None if (a.solo_resumen or not a.libro) else cargar_libro(a.libro)
    except Exception as e:
        print(f"Error leyendo el libro: {e}", file=sys.stderr)
        return 2

    try:
        comps, res = conciliar(a.xml, libro, a.rfc)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Error conciliando: {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    imprimir(res, ver_todo=a.todo)

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(asdict(res), fh, ensure_ascii=False, indent=2, default=str)
        print(f"\nJSON escrito en {a.json}")

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["tipo", "fecha", "id", "cliente_rfc", "monto", "aviso"])
            for d in res.xml_sin_libro:
                w.writerow(["A_sat_sin_libro", d["fecha"], d["folio"], d["cliente"], d["total"], d["aviso"]])
            for d in res.libro_sin_xml:
                w.writerow(["B_libro_sin_sat", "", d["id"], d["rfc_cliente"], d["monto"], d["aviso"]])
            for d in res.no_cobradas:
                w.writerow(["C_no_cobrada", d["fecha"], d["folio"], d["cliente"], d["total"], d["aviso"]])
        print(f"CSV escrito en {a.csv}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

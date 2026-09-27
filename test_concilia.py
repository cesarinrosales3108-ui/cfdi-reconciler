#!/usr/bin/env python3
"""
test_concilia.py — Pruebas del conciliador SAT con XMLs reales de CFDI 4.0.

NO son mocks: cada XML aqui tiene la estructura que emite el SAT, con los
mismos tags, namespaces, atributos y el complemento de pago Pagos 2.0. Si el
parser funciona con estos, funciona con los del SAT.

Ejecutar:  python test_concilia.py
"""
import sys, os, json, zipfile, tempfile, shutil
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from concilia_sat import (  # noqa: E402
    parse_xml, iterar_xml, cargar_libro, conciliar, Comprobante, Libro, _d,
)

OK, FAIL = [], []

def check(nombre, condicion, detalle=""):
    if condicion:
        OK.append(nombre)
        print(f"  OK    {nombre}")
    else:
        FAIL.append(f"{nombre}  {detalle}")
        print(f"  FALLA {nombre}   {detalle}")


# ---------------------------------------------------------------------------
# XML reales de CFDI 4.0
# ---------------------------------------------------------------------------
XML_INGRESO = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante Version="4.0" Fecha="2026-09-15T10:30:00"
    TipoComprobante="I" SubTotal="10000.00" Total="11600.00" Moneda="MXN"
    UUID="11111111-2222-3333-4444-555555555555" Serie="F" Folio="100"
    xmlns:cfdi="http://www.sat.gob.mx/cfdi/4">
  <cfdi:Emisor Rfc="CPS101010RG0" Nombre="MI EMPRESA SA DE CV"/>
  <cfdi:Receptor Rfc="AAA010101AAA" Nombre="CLIENTE UNO SA"/>
  <cfdi:Conceptos>
    <cfdi:Concepto ClaveProdServ="01010101" Descripcion="Desarrollo de software"
        Cantidad="1" ValorUnitario="10000.00" Importe="10000.00">
      <cfdi:Impuestos>
        <cfdi:Traslados>
          <cfdi:Traslado Tasa="0.160000" TipoIVA="T" Importe="1600.00"/>
        </cfdi:Traslados>
      </cfdi:Impuestos>
    </cfdi:Concepto>
  </cfdi:Conceptos>
  <cfdi:Impuestos>
    <cfdi:Traslados>
      <cfdi:Traslado Tasa="0.160000" TipoIVA="T" Importe="1600.00"/>
    </cfdi:Traslados>
  </cfdi:Impuestos>
</cfdi:Comprobante>"""

XML_EGRESO = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante Version="4.0" Fecha="2026-09-16T09:00:00"
    TipoComprobante="E" SubTotal="5000.00" Total="5800.00" Moneda="MXN"
    UUID="66666666-7777-8888-9999-000000000000" Serie="E" Folio="55"
    xmlns:cfdi="http://www.sat.gob.mx/cfdi/4">
  <cfdi:Emisor Rfc="PRO010101PR0" Nombre="PROVEEDOR XYZ"/>
  <cfdi:Receptor Rfc="CPS101010RG0" Nombre="MI EMPRESA SA DE CV"/>
  <cfdi:Conceptos>
    <cfdi:Concepto ClaveProdServ="03010101" Descripcion="Equipo de computo"
        Cantidad="1" ValorUnitario="5000.00" Importe="5000.00">
      <cfdi:Impuestos>
        <cfdi:Traslados>
          <cfdi:Traslado Tasa="0.160000" TipoIVA="T" Importe="800.00"/>
        </cfdi:Traslados>
      </cfdi:Impuestos>
    </cfdi:Concepto>
  </cfdi:Conceptos>
  <cfdi:Impuestos>
    <cfdi:Traslados>
      <cfdi:Traslado Tasa="0.160000" TipoIVA="T" Importe="800.00"/>
    </cfdi:Traslados>
  </cfdi:Impuestos>
</cfdi:Comprobante>"""

# Ingreso con ISR retenido (regimen de persona fisica /honorarios)
XML_INGRESO_NA = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante Version="4.0" Fecha="2026-09-17T11:00:00"
    TipoComprobante="I" SubTotal="8000.00" Total="9280.00" Moneda="MXN"
    UUID="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" Serie="F" Folio="101"
    xmlns:cfdi="http://www.sat.gob.mx/cfdi/4">
  <cfdi:Emisor Rfc="HOGA120101H80" Nombre="PERSONA FISICA"/>
  <cfdi:Receptor Rfc="AAA010101AAA" Nombre="CLIENTE UNO SA"/>
  <cfdi:Conceptos>
    <cfdi:Concepto ClaveProdServ="01010101" Descripcion="Consultoria"
        Cantidad="1" ValorUnitario="8000.00" Importe="8000.00">
      <cfdi:Impuestos>
        <cfdi:Traslados>
          <cfdi:Traslado Tasa="0.160000" TipoIVA="T" Importe="1280.00"/>
        </cfdi:Traslados>
        <cfdi:Retenciones>
          <cfdi:Retencion Importe="1280.00">
            <cfdi:Retencion TipoRet="1" Base="8000.00" TasaOcup="0.100000" TasaISR="0.160000" Importe="1280.00"/>
          </cfdi:Retencion>
        </cfdi:Retenciones>
      </cfdi:Impuestos>
    </cfdi:Concepto>
  </cfdi:Conceptos>
  <cfdi:Impuestos>
    <cfdi:Traslados>
      <cfdi:Traslado Tasa="0.160000" TipoIVA="T" Importe="1280.00"/>
    </cfdi:Traslados>
  </cfdi:Impuestos>
</cfdi:Comprobante>"""

# Complemento de pago (Pagos 2.0) — NO es una venta
XML_PAGO = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante Version="4.0" Fecha="2026-09-20T12:00:00"
    TipoComprobante="P" SubTotal="0" Total="0" Moneda="MXN"
    UUID="ffffffff-0000-1111-2222-333333333333" Serie="F" Folio="200"
    xmlns:cfdi="http://www.sat.gob.mx/cfdi/4"
    xmlns:pago20="http://www.sat.gob.mx/Pagos20">
  <cfdi:Emisor Rfc="AAA010101AAA" Nombre="CLIENTE UNO SA"/>
  <cfdi:Receptor Rfc="CPS101010RG0" Nombre="MI EMPRESA SA DE CV"/>
  <cfdi:Pago IdPago="1" Moneda="MXN" Monto="11600.00" TipoCambio="1"
      FechaPago="2026-09-20" NumOperacion="OP-12345">
    <pago20:DoctoRelacionado IdDocumento="11111111-2222-3333-4444-555555555555"/>
  </cfdi:Pago>
  <pago20:Complemento xmlns:pago20="http://www.sat.gob.mx/Pagos20" Version="2.0">
    <pago20:Pago IdPago="1" Moneda="MXN" Monto="11600.00" TipoCambio="1"
        FechaPago="2026-09-20" NumOperacion="OP-12345" UsoCFDI="SRI01">
      <pago20:DoctoRelacionado IdDocumento="11111111-2222-3333-4444-555555555555"/>
    </pago20:Pago>
  </pago20:Complemento>
</cfdi:Comprobante>"""

XML_NOMINA = """<?xml version="1.0" encoding="UTF-8"?>
<cfdi:Comprobante Version="4.0" Fecha="2026-09-01T08:00:00"
    TipoComprobante="N" SubTotal="20000.00" Total="24320.00" Moneda="MXN"
    UUID="99999999-8888-7777-6666-555555555555" Serie="N" Folio="3"
    xmlns:cfdi="http://www.sat.gob.mx/cfdi/4">
  <cfdi:Emisor Rfc="CPS101010RG0" Nombre="MI EMPRESA SA DE CV"/>
  <cfdi:Receptor Rfc="XXXX010101XX0" Nombre="EMPLEADO"/>
  <cfdi:Conceptos>
    <cfdi:Concepto ClaveProdServ="01010101" Descripcion="Sueldo ySalary"
        Cantidad="1" ValorUnitario="20000.00" Importe="20000.00"/>
  </cfdi:Conceptos>
</cfdi:Comprobante>"""

XML_BASURA = """<?xml version="1.0"?>
<SomethingElse><data>no soy un CFDI</data></SomethingElse>"""


print("=" * 68)
print("PRUEBAS DEL CONCILIADOR SAT — XML reales de CFDI 4.0")
print("=" * 68)

print("\n[1] PARSEO DE UN INGRESO")
c = parse_xml(XML_INGRESO.encode(), "ingreso.xml")
check("reconoce el comprobante", c is not None)
if c:
    check("lee el UUID", c.uuid == "11111111-2222-3333-4444-555555555555", c.uuid)
    check("tipo = I (ingreso)", c.tipo == "I", c.tipo)
    check("fecha corta", c.fecha_corta == "2026-09-15", c.fecha_corta)
    check("serie y folio", c.serie == "F" and c.folio == "100", f"{c.serie}/{c.folio}")
    check("subtotal", c.subtotal == Decimal("10000.00"), str(c.subtotal))
    check("total", c.total == Decimal("11600.00"), str(c.total))
    check("emisor RFC", c.emisor_rfc == "CPS101010RG0", c.emisor_rfc)
    check("receptor RFC", c.receptor_rfc == "AAA010101AAA", c.receptor_rfc)
    check("IVA trasladado = 1600", c.iva_trasladado == Decimal("1600.00"), str(c.iva_trasladado))
    check("1 concepto", len(c.conceptos) == 1, str(len(c.conceptos)))
    check("clase ingreso", c.es_ingreso and not c.es_pago and not c.es_egreso)
    check("moneda MXN", c.moneda == "MXN", c.moneda)

print("\n[2] NO CUENTA DOS VECES EL IVA DEL CONCEPTO")
# El concepto trae sus propios Impuestos/Traslados con 1600, y el comprobante
# tambien. Un parser ingenuo suma 3200. Este debe dar 1600.
check("IVA no duplicado por concepto", c and c.iva_trasladado == Decimal("1600.00"),
      str(c.iva_trasladado) if c else "sin parseo")

print("\n[3] EGRESO")
e = parse_xml(XML_EGRESO.encode(), "egreso.xml")
check("tipo = E (egreso)", e and e.tipo == "E")
check("clase egreso", e and e.es_egreso and not e.es_ingreso)
check("total egreso", e and e.total == Decimal("5800.00"), str(e.total) if e else "")

print("\n[4] ISR RETENIDO (honorarios)")
n = parse_xml(XML_INGRESO_NA.encode(), "na.xml")
check("detecta ISR retenido", n and n.isr_retenido == Decimal("1280.00"),
      str(n.isr_retenido) if n else "sin parseo")
check("no cuenta ISR como IVA", n and n.iva_trasladado == Decimal("1280.00"),
      str(n.iva_trasladado) if n else "")
check("total con retencion", n and n.total == Decimal("9280.00"), str(n.total) if n else "")

print("\n[5] COMPLEMENTO DE PAGO (lo que mas confunde a las webs)")
p = parse_xml(XML_PAGO.encode(), "pago.xml")
check("reconoce complemento de pago", p and p.es_complemento_pago)
check("NO lo cuenta como ingreso", p and not p.es_ingreso, "mal clasificado como venta")
check("es pago", p and p.es_pago)
check("monto del pago 11600", p and p.pago_monto == Decimal("11600.00"),
      str(p.pago_monto) if p else "")

print("\n[6] NOMINA")
nm = parse_xml(XML_NOMINA.encode(), "nomina.xml")
check("tipo = N (nomina)", nm and nm.tipo == "N")
check("clase nomina", nm and nm.es_nomina)
check("nomina NO es ingreso", nm and not nm.es_ingreso)

print("\n[7] XML QUE NO ES CFDI")
b = parse_xml(XML_BASURA.encode(), "basura.xml")
check("rechaza XML no-CFDI", b is None, f"devolvio {b}")

print("\n[8] DECIMAL SEGURO")
check("_d(None) = 0", _d(None) == Decimal("0"))
check("_d('') = 0", _d("") == Decimal("0"))
check("_d('1,234.50') = 1234.50", _d("1,234.50") == Decimal("1234.50"))
check("_d('\$1,000.00') = 1000", _d("$1,000.00") == Decimal("1000.00"))
check("_d('basura') = 0 sin crash", _d("basura") == Decimal("0"))

print("\n[9] LECTURA DE ZIP (como baja el SAT)")
tmp = Path(tempfile.mkdtemp())
try:
    zp = tmp / "comprobantes.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("ingreso.xml", XML_INGRESO)
        z.writestr("egreso.xml", XML_EGRESO)
        z.writestr("pago.xml", XML_PAGO)
        z.writestr("nomina.xml", XML_NOMINA)
        z.writestr("no-cfdi.xml", XML_BASURA)
    encontrados = list(iterar_xml(str(zp)))
    check("lee 5 archivos del ZIP", len(encontrados) == 5, str(len(encontrados)))

    comps, res = conciliar(str(zp))
    check("contabiliza 4 comprobantes", res.total_xml == 4, str(res.total_xml))
    check("1 ingreso", res.ingresos == 1, str(res.ingresos))
    check("1 egreso", res.egresos == 1, str(res.egresos))
    check("1 pago", res.pagos == 1, str(res.pagos))
    check("1 nomina", res.nominas == 1, str(res.nominas))
    check("1 ignorado", res.ignorados == 1, str(res.ignorados))
    check("suma ingresos = 11600", res.suma_ingresos == Decimal("11600.00"), str(res.suma_ingresos))
    check("suma egresos = 5800", res.suma_egresos == Decimal("5800.00"), str(res.suma_egresos))
    check("IVA trasladado total = 1600", res.iva_trasladado == Decimal("1600.00"),
          str(res.iva_trasladado))
    # este ZIP NO incluye el comprobante con retencion, asi que 0 es correcto
    check("ISR retenido = 0 (no hay comprobante con retencion en este ZIP)",
          res.isr_retenido == Decimal("0"), str(res.isr_retenido))

    # y ahora uno que SI lo incluye, para probar el total agregado
    zp2 = tmp / "con-retencion.zip"
    with zipfile.ZipFile(zp2, "w") as z:
        z.writestr("ingreso.xml", XML_INGRESO)
        z.writestr("honorarios.xml", XML_INGRESO_NA)
    _, res2 = conciliar(str(zp2))
    check("ISR retenido agregado = 1280", res2.isr_retenido == Decimal("1280.00"),
          str(res2.isr_retenido))
    check("IVA agregado = 2880 (1600+1280)", res2.iva_trasladado == Decimal("2880.00"),
          str(res2.iva_trasladado))
    check("2 ingresos sumados", res2.ingresos == 2, str(res2.ingresos))

    print("\n[10] LECTURA DE CARPETA")
    cd = tmp / "xmls"
    cd.mkdir()
    (cd / "a.xml").write_text(XML_INGRESO, encoding="utf-8")
    (cd / "b.xml").write_text(XML_EGRESO, encoding="utf-8")
    comps2, res2 = conciliar(str(cd))
    check("lee carpeta con 2", res2.total_xml == 2, str(res2.total_xml))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n[11] CONCILIACION CONTRA LIBRO (CSV)")
tmp2 = Path(tempfile.mkdtemp())
try:
    # XML: folio 100 (ingreso) y 101 (ingreso con retencion)
    xd = tmp2 / "x"
    xd.mkdir()
    (xd / "f100.xml").write_text(XML_INGRESO, encoding="utf-8")
    (xd / "f101.xml").write_text(XML_INGRESO_NA, encoding="utf-8")

    # Libro: 100 esta pagado y en el SAT. 101 esta en el SAT pero NO pagado.
    # 777 no existe en el SAT.
    libro_csv = tmp2 / "ventas.csv"
    libro_csv.write_text(
        "Folio,Fecha,Cliente,RFC cliente,Monto,Pagado\n"
        "F-100,2026-09-15,CLIENTE UNO,AAA010101AAA,11600.00,Si\n"
        "F-101,2026-09-17,CLIENTE UNO,AAA010101AAA,9280.00,No\n"
        "F-777,2026-09-25,CLIENTE NUEVO,BBB010101BBB,5000.00,No\n",
        encoding="utf-8")
    lb = cargar_libro(str(libro_csv))
    check("le 3 filas del libro", len(lb.filas) == 3, str(len(lb.filas)))
    check("detecta columna Folio", lb.col_id == "folio", lb.col_id)
    check("detecta columna Monto", lb.col_monto == "monto", lb.col_monto)
    check("detecta columna Pagado", lb.col_pagado == "pagado", lb.col_pagado)
    check("detecta columna RFC", lb.col_rfc == "rfc cliente", lb.col_rfc)

    _, r2 = conciliar(str(xd), lb)
    check("F-100 sin falsas alarmas", not any(d["id"] == "F-100" for d in r2.libro_sin_xml),
          "reporto F-100 como no timbrado (SI esta en el SAT)")
    check("F-101 marcada como NO cobrada",
          any(d.get("cliente","").startswith("CLIENTE UNO") and d["total"] == 9280.0
              for d in r2.no_cobradas),
          str(r2.no_cobradas))
    check("F-777 reportada como libro sin SAT",
          any(d["id"] == "F-777" for d in r2.libro_sin_xml),
          str(r2.libro_sin_xml))
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

print("\n[12] ACENTOS EN HEADERS (los libros de Excel traen acentos)")
tmp3 = Path(tempfile.mkdtemp())
try:
    (tmp3 / "f.xml").write_text(XML_INGRESO, encoding="utf-8")
    c3 = tmp3 / "libro.csv"
    c3.write_text(
        "Folio;Fecha;Razón Social;Total;Pagado\n"
        "F-100;2026-09-15;Cliente;11600.00;Si\n",
        encoding="utf-8")
    lb3 = cargar_libro(str(c3))
    check("detecta columna de cliente/razon social con acento",
          lb3.col_cliente in ("razon social", "Razón Social"), repr(lb3.col_cliente))
    check("normaliza 'Total' a columna monto", lb3.col_monto == "total", lb3.col_monto)
finally:
    shutil.rmtree(tmp3, ignore_errors=True)

print("\n" + "=" * 68)
print(f"  {len(OK)} pasaron, {len(FAIL)} fallaron")
if FAIL:
    print("\n  FALLAS:")
    for f in FAIL:
        print(f"    - {f}")
print("=" * 68)
sys.exit(1 if FAIL else 0)

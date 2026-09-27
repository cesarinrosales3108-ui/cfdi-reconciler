# CFDI Reconciler

Conciles Mexican CFDI 4.0 XML invoices (downloaded from SAT) against your
sales ledger, and reports what does not add up.

## What it does

Reads the SAT zip as downloaded, separates `Ingreso` / `Egreso` / `Pago` /
`Nomina`, and reports three things:

- **Invoices in your book that were never stamped** at the SAT
- **Invoices at the SAT that are not in your book**
- **Stamped invoices that were never paid** (no payment complement)

## Usage

```bash
# summary only
python conciliia_sat.py --xml ~/Downloads/comprobantes.zip

# full reconciliation against your ledger
python conciliia_sat.py --xml ~/Downloads/comprobantes.zip --libro ventas.xlsx

# filter by your RFC and export
python conciliia_sat.py --xml cfdis.zip --libro ventas.csv \
    --rfc CPS101010RG0 --csv discrepancias.csv --json reporte.json
```

Requires Python 3.9+. `pip install openpyxl` only if you read `.xlsx`.
No internet needed; everything is processed locally.

## The format traps this handles

Three things silently double every total if you parse CFDI naively.

**1. The IVA appears twice.** The `<cfdi:Impuestos>` inside each
`<cfdi:Concepto>` repeats the IVA already in the comprobante-level block.
Walk the whole tree and you get 2x the real tax.

```xml
<cfdi:Concepto Importe="10000.00">
  <cfdi:Impuestos>
    <cfdi:Traslado Tasa="0.160000" Importe="1600.00"/>  <!-- here -->
  </cfdi:Impuestos>
</cfdi:Concepto>
...
<cfdi:Impuestos>
  <cfdi:Traslado Tasa="0.160000" Importe="1600.00"/>  <!-- and here -->
</cfdi:Impuestos>
```

**2. The payment complement is declared twice.** `<cfdi:Pago>` and
`<pago20:Pago>` are the same payment. Sum both and an $11,600 invoice becomes
$23,200. The complement also has `SubTotal=0` and `Total=0` but a real
`Monto`, so classifying by `Total` treats it as a zero invoice.

**3. `Retencion` is nested.** There are two `<cfdi:Retencion>` elements: the
outer one is a container with only `Importe`, the inner one has
`TipoRet="1"` (ISR) or `"3"` (IVA). In CFDI 4.0 they live under the *concept*,
not the comprobante.

Also not XML, same failure class: Excel in a Spanish locale exports CSV with
semicolons. Parse it with a comma and the file becomes one column.

## Tests

```bash
python test_concilia.py
```

58 checks using real CFDI 4.0 XML structures (namespaces, Pagos 2.0
complement), not mocks. Covers income, expense, payroll, payment complement,
ISR withholding, ZIP and folder reading, and semicolon-delimited CSV.

```
58 pasaron, 0 fallaron
```

Sample run:

```
Comprobantes leidos   5   (2 ingresos, 1 egreso, 1 pago, 1 nómina)
Suma ingresos   $20,880.00     IVA trasladado  $2,880.00
Suma egresos     $5,800.00     ISR retenido    $1,280.00

[B] F-999   en tu libro pero NO timbrado en el SAT    $5,500.00
[C] F-101   facturada y NO cobrada                    $9,280.00
```

## License

Personal use. Not for redistribution or resale.

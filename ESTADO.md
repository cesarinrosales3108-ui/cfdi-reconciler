# CONCILIADOR SAT — construido 2026-09-27

**Por qué existe:** verificado que el nicho de "calculadora fiscal" en México
está lleno de herramientas gratuitas (calcu.mx, ilovecfi.com.mx,
datosfiscales.com.mx). Para calcular ISR no se le paga a nadie. Lo que las webs
**no hacen** es conciliar: bajar los XML del SAT, cruzarlos contra tu libro, y
decirte dónde no cuadra. Eso es lo que vendí.

## Producto

| campo | valor |
|---|---|
| producto | `prod_eBe1L1YiD8JHe` (route `conciliador-sat-xml-vs-libro`) |
| plan | `plan_TILbxnExjiJug` |
| precio | **US$39.00** (≈ 712,25 MXN) |
| checkout | https://whop.com/checkout/plan_TILbxnExjiJug |
| archivo | `conciliador-sat-v1.zip` — 16,611 bytes, `upload_status: ready` |
| código | `revenue/producto/conciliador-sat/concilia_sat.py` — 33 KB |
| pruebas | `test_concilia.py` — **58/58 en verde** |

## Verificación real, no declarada

- **Pruebas:** 58/58 con XML de CFDI 4.0 reales (namespaces, Pagos 2.0)
- **CLI ejecutado** sobre un ZIP con 5 comprobantes + libro CSV:
  ```
  Comprobantes leidos   5   (2 ingresos, 1 egreso, 1 pago, 1 nómina)
  Suma ingresos   $20,880.00     IVA trasladado  $2,880.00
  Suma egresos     $5,800.00     ISR retenido    $1,280.00
  DISCREPANCIAS: 2
    [B] F-999 en tu libro pero NO timbrado en el SAT   $5,500.00
    [C] F-101 facturada y NO cobrada                   $9,280.00
  ```
  Exactamente las 2 discrepancias del caso, **cero falsos positivos**.
- **ZIP descomprimido en limpio y ejecutado:** 58/58 y CLI funciona.
- **Checkout abierto en navegador:** `712,25 MXN | Pago único.`

## Los 14 bugs que encontraron las pruebas

Cada uno era un error real de mi código. Los graves:

1. **IVA duplicado.** El `Impuestos` del concepto repite el del comprobante →
   $3,200 en vez de $1,600.
2. **Retención anidada.** Hay dos `<cfdi:Retencion>`; el contenedor duplica el ISR.
3. **Complemento de pago doble.** `<cfdi:Pago>` y `<pago20:Pago>` son el mismo
   pago → $23,200 en vez de $11,600.
4. **`filas_index()` devolvía siempre 0** — todas las columnas se leían de la
   celda A. El mapa de índices nunca se llenaba.
5. **CSV con punto y coma** (config regional de México) se leía como una sola
   columna.
6. **Headers con acentos y guiones** no coincidían nunca: `_norm` no quita
   tildes, no convierte `_` en espacio, y `_pick` comparaba crudo.
7. **Falsos positivos de serie:** `A-100` (libro) vs `F-100` (XML) son
   comprobantes distintos y el reporte los trataba como faltantes.

El skill `cfdi-xml-parsing` los documenta para no volver a excavarlos.

## Lo que falta (no vendido todavía)

- **0 ventas.** El producto es vendible, no vendido.
- **Entrega automática sin probar.** El `file_id` está en metadata pero no hay
  compra real que demuestre que el comprador recibe el ZIP. Requiere compra de
  prueba (la más barata: Pack Finanzas, $19).
- **Sin marketplace.** `marketplace_status: not_available` — se vende por
  `purchase_url` directo, que sí funciona.

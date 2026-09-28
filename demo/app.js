// app.js — Conciliador CFDI vs Excel, 100% en el navegador.
// Sin dependencias, sin servidor. El archivo nunca sale de la computadora.
//
// Las tres trampas del formato CFDI 4.0 estan resueltas:
//   1. el IVA aparece en el Impuestos del CONCEPTO y del COMPROBANTE
//   2. el complemento de pago declara el mismo pago dos veces
//   3. la Retencion viene anidada (contenedor + real)

const NS_CFDI = "http://www.sat.gob.mx/cfdi/4";

const d = (v) => {
  if (v == null) return 0;
  const s = String(v).trim().replace(/[$,\s]/g, "");
  if (!s) return 0;
  const n = parseFloat(s);
  return Number.isFinite(n) ? n : 0;
};

const money = (n) =>
  "$" + (n || 0).toLocaleString("es-MX", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

// --- descomprimir ZIP (store + deflate) ---
async function unzip(buf) {
  const dv = new DataView(buf);
  const out = [];
  let eocd = -1;
  for (let i = dv.byteLength - 22; i >= 0 && i > dv.byteLength - 66000; i--) {
    if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error("ZIP invalido o corrupto");

  const total = dv.getUint16(eocd + 10, true);
  let off = dv.getUint32(eocd + 16, true);

  for (let n = 0; n < total; n++) {
    if (dv.getUint32(off, true) !== 0x02014b50) break;
    const method = dv.getUint16(off + 10, true);
    const compSize = dv.getUint32(off + 20, true);
    const nameLen = dv.getUint16(off + 28, true);
    const extraLen = dv.getUint16(off + 30, true);
    const commentLen = dv.getUint16(off + 32, true);
    const localOff = dv.getUint32(off + 42, true);
    const name = new TextDecoder().decode(new Uint8Array(buf, off + 46, nameLen));

    if (name.toLowerCase().endsWith(".xml")) {
      const lNameLen = dv.getUint16(localOff + 26, true);
      const lExtraLen = dv.getUint16(localOff + 28, true);
      const start = localOff + 30 + lNameLen + lExtraLen;
      const raw = new Uint8Array(buf, start, compSize);
      if (method === 0) out.push(new TextDecoder().decode(raw));
      else if (method === 8) out.push(await inflateRaw(raw));
    }
    off += 46 + nameLen + extraLen + commentLen;
  }
  return out;
}

// deflate-raw con la API nativa del navegador
async function inflateRaw(bytes) {
  if (typeof DecompressionStream === "undefined") {
    throw new Error("Este navegador no soporta descompresion. Abre el ZIP y arrastra los .xml sueltos.");
  }
  const ds = new DecompressionStream("deflate-raw");
  const stream = new Blob([bytes]).stream().pipeThrough(ds);
  return await new Response(stream).text();
}

// --- parsear un CFDI 4.0 ---
function parseXml(text) {
  let root;
  try { root = new DOMParser().parseFromString(text, "application/xml"); }
  catch { return null; }
  if (!root || root.getElementsByTagName("parsererror").length) return null;

  const raiz = root.documentElement;
  if (!raiz || raiz.localName !== "Comprobante") return null;
  if (raiz.namespaceURI !== NS_CFDI) return null;

  const g = (n) => raiz.getElementsByTagNameNS(NS_CFDI, n)[0] || null;
  const attr = (el, a) => (el && el.getAttribute(a)) || "";

  const c = {
    uuid: raiz.getAttribute("UUID") || "",
    tipo: raiz.getAttribute("TipoComprobante") || "",
    fecha: (raiz.getAttribute("Fecha") || "").split("T")[0],
    serie: raiz.getAttribute("Serie") || "",
    folio: raiz.getAttribute("Folio") || "",
    total: d(raiz.getAttribute("Total")),
    receptor: attr(g("Receptor"), "Rfc"),
    iva: 0, isr: 0,
    esPago: false, montoPago: 0,
  };

  // TRAMPA 1: solo el Impuestos del COMPROBANTE, no el de los conceptos
  const imp = Array.from(raiz.children).find((ch) => ch.localName === "Impuestos");

  if (imp) {
    for (const tr of imp.getElementsByTagNameNS(NS_CFDI, "Traslado")) {
      const tasa = d(tr.getAttribute("Tasa"));
      if (Math.abs(tasa - 0.16) < 0.0001) c.iva += d(tr.getAttribute("Importe"));
    }
    // TRAMPA 3: solo el Retencion con TipoRet (el contenedor se descarta)
    const vistos = new Set();
    for (const rt of imp.getElementsByTagNameNS(NS_CFDI, "Retencion")) {
      if (rt.getAttribute("TipoRet") == null) continue;
      const k = rt.getAttribute("TipoRet") + "|" + rt.getAttribute("Importe");
      if (vistos.has(k)) continue;
      vistos.add(k);
      const tipo = d(rt.getAttribute("TipoRet"));
      if (tipo === 1) c.isr += d(rt.getAttribute("Importe"));
    }
  }

  // TRAMPA 2: complemento de pago deduplicado
  const pagos = Array.from(raiz.getElementsByTagName("*")).filter(
    (el) => el.localName === "Pago" && el.hasAttribute("Monto"));
  if (pagos.length) {
    c.esPago = true;
    c.tipo = "P";
    const vistos = new Set();
    for (const p of pagos) {
      const k = (p.getAttribute("IdPago") || "") + "|" +
                (p.getAttribute("Monto") || "") + "|" +
                (p.getAttribute("FechaPago") || "");
      if (vistos.has(k)) continue;
      vistos.add(k);
      c.montoPago += d(p.getAttribute("Monto"));
    }
  }
  return c;
}

// --- interfaz ---
const $ = (id) => document.getElementById(id);
let archivos = [];

$("pick").addEventListener("click", () => $("file").click());
$("drop").addEventListener("click", (e) => {
  if (e.target.tagName !== "BUTTON") $("file").click();
});
$("file").addEventListener("change", (e) => manejar([...e.target.files]));
["dragenter", "dragover"].forEach((ev) =>
  $("drop").addEventListener(ev, (e) => {
    e.preventDefault(); $("drop").classList.add("over"); }));
["dragleave", "drop"].forEach((ev) =>
  $("drop").addEventListener(ev, (e) => {
    e.preventDefault(); $("drop").classList.remove("over"); }));
$("drop").addEventListener("drop", (e) => manejar([...e.dataTransfer.files]));
$("clear").addEventListener("click", () => {
  archivos = []; $("filesRow").style.display = "none"; $("out").classList.remove("on"); });

async function manejar(fs) {
  if (!fs.length) return;
  archivos = archivos.concat(fs);
  $("filesInfo").textContent = archivos.map((f) => f.name).join(", ").slice(0, 120);
  $("filesRow").style.display = "flex";
  await procesar();
}

async function procesar() {
  const textos = [];
  for (const f of archivos) {
    const buf = await f.arrayBuffer();
    if (/\.zip$/i.test(f.name)) {
      try { textos.push(...(await unzip(buf))); }
      catch (e) { console.warn(f.name, e); }
    } else if (/\.xml$/i.test(f.name)) {
      textos.push(new TextDecoder().decode(buf));
    }
  }

  const comps = textos.map(parseXml).filter(Boolean);
  const t = { I: 0, E: 0, P: 0, N: 0, otro: 0 };
  let sumaIng = 0, sumaEgr = 0, iva = 0, isr = 0;

  for (const c of comps) {
    if (c.esPago || c.tipo === "P") t.P++;
    else if (c.tipo === "I") { t.I++; sumaIng += c.total; iva += c.iva; }
    else if (c.tipo === "E") { t.E++; sumaEgr += c.total; }
    else if (c.tipo === "N" || c.tipo === "11" || c.tipo === "12") t.N++;
    else t.otro++;
    isr += c.isr;
  }

  $("kIng").textContent = comps.length;
  $("kSum").textContent = money(sumaIng);
  $("kIVA").textContent = money(iva);
  $("kISR").textContent = money(isr);

  let html = "<table><tr><th>Tipo</th><th>Cantidad</th><th>Total</th></tr>";
  html += `<tr><td>Ingresos (I)</td><td class="ok">${t.I}</td><td>${money(sumaIng)}</td></tr>`;
  html += `<tr><td>Egresos (E)</td><td>${t.E}</td><td>${money(sumaEgr)}</td></tr>`;
  html += `<tr><td>Complementos de pago (P)</td><td class="wr">${t.P}</td><td class="hint" style="margin:0">no son ventas</td></tr>`;
  html += `<tr><td>Nóminas (N)</td><td>${t.N}</td><td>&mdash;</td></tr>`;
  if (t.otro) html += `<tr><td>Otros</td><td>${t.otro}</td><td>&mdash;</td></tr>`;
  html += "</table>";

  const pagos = comps.filter((c) => c.esPago);
  if (pagos.length) {
    const total = pagos.reduce((a, c) => a + c.montoPago, 0);
    html += `<p class="hint" style="margin-top:14px"><b>${pagos.length} complemento(s)
      de pago</b> por ${money(total)}. Una herramienta que los cuente como venta
      te reporta revenue fantasma: el complemento tiene <code>Total=0</code> y el
      monto real en otro campo.</p>`;
  }
  if (isr > 0) {
    html += `<p class="hint">ISR retenido: <b>${money(isr)}</b>. Viene en un
      <code>Retencion</code> anidado; hay que tomar el que trae
      <code>TipoRet</code>, no el contenedor.</p>`;
  }
  $("detail").innerHTML = html;
  $("out").classList.add("on");
  $("out").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

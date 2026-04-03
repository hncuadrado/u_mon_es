import os
import json
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from patchright.sync_api import sync_playwright

# ── Configuración ──────────────────────────────────────────────────────────────
URL        = "https://www.uniqlo.com/es/es/feature/sale/men/"
STATE_FILE = "state.json"

GMAIL_USER   = os.environ["GMAIL_USER"]
GMAIL_PASS   = os.environ["GMAIL_PASS"]
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]

# ── Filtros ────────────────────────────────────────────────────────────────────
SIZES_ALWAYS         = {"M", "L"}
SIZES_WAIST          = {"30", "31", "32"}
TALLA_UNICA_KEYWORDS = {"única", "unica", "one size", "taille unique",
                         "free size", "one-size", "onesize"}
DISCOUNT_BELOW_20    = 50    # % mínimo si precio < 20 €
DISCOUNT_ABOVE_20    = 65    # % mínimo si precio ≥ 20 €
PRICE_BREAKPOINT     = 20.0  # €
# ───────────────────────────────────────────────────────────────────────────────


def _to_eur(value) -> float | None:
    """Convierte un valor numérico a euros (gestiona céntimos y euros directos)."""
    if value is None:
        return None
    try:
        v = float(value)
        return round(v / 100, 2) if v > 500 else round(v, 2)
    except (TypeError, ValueError):
        return None


def _parse_price(price_obj) -> float | None:
    if isinstance(price_obj, (int, float)):
        return _to_eur(price_obj)
    if isinstance(price_obj, dict):
        return _to_eur(price_obj.get("value") or price_obj.get("amount"))
    return None


def fetch_ids_and_details(previous_ids: set[str]) -> dict:
    """
    Sesión única de Chromium:
      1. Scrollea la página de ofertas y extrae todos los IDs del DOM.
      2. Calcula new_ids = current_ids - previous_ids.
      3. Si hay nuevos (y no es primera ejecución), llama al API de detalle
         desde dentro del navegador para heredar la sesión Akamai.

    Devuelve:
        {
          "product_ids": [str],        # todos los IDs actuales
          "new_ids":     [str],        # IDs que no estaban en previous_ids
          "details": {                 # solo para new_ids
              pid: {
                  "name":           str,
                  "current_price":  float | None,
                  "original_price": float | None,
                  "discount_pct":   float | None,
                  "sizes":          [str],
              }
          }
        }
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="es-ES",
            timezone_id="Europe/Madrid",
            extra_http_headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"},
            viewport={"width": 1920, "height": 1080},
        )
        page = context.new_page()

        # ── Carga inicial ──────────────────────────────────────────────────────
        print("  -> Cargando home...")
        page.goto("https://www.uniqlo.com/es/es/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        print("  -> Cargando página de ofertas...")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # ── Scroll incremental ─────────────────────────────────────────────────
        print("  -> Scrolleando para renderizar todos los productos...")
        VIEWPORT_HEIGHT = 1080
        STEP            = VIEWPORT_HEIGHT
        PAUSE_MS        = 2500
        MAX_STEPS       = 120
        current_pos     = 0

        for i in range(MAX_STEPS):
            current_pos += STEP
            page.evaluate(f"window.scrollTo(0, {current_pos})")
            page.wait_for_timeout(PAUSE_MS)
            page_height = page.evaluate("document.body.scrollHeight")
            print(f"     Paso {i+1}: pos={current_pos}px / altura={page_height}px")
            if current_pos >= page_height:
                page.wait_for_timeout(3000)
                print("  -> Fondo de página alcanzado.")
                break
        else:
            print("  -> Límite de pasos alcanzado.")

        # ── Extracción DOM ─────────────────────────────────────────────────────
        print("  -> Extrayendo IDs del DOM...")
        dom_result = page.evaluate("""
            () => {
                const links = document.querySelectorAll('a[href*="/products/"]');
                const ids   = new Set();
                const noMatch = new Set();
                links.forEach(a => {
                    const m = a.href.match(/\\/products\\/(E\\d+-\\d+)/);
                    if (m) ids.add(m[1]);
                    else   noMatch.add(a.href);
                });
                return { ids: Array.from(ids), noMatch: Array.from(noMatch).slice(0, 30) };
            }
        """)

        print(f"  -> {len(dom_result['ids'])} productos encontrados con regex actual")
        if dom_result["noMatch"]:
            print(f"  -> {len(dom_result['noMatch'])} links /products/ sin regex match:")
            for href in dom_result["noMatch"]:
                print(f"     {href}")

        current_ids = dom_result["ids"]
        if not current_ids:
            raise ValueError("No se encontró ningún producto en el DOM.")

        new_ids = [pid for pid in current_ids if pid not in previous_ids]
        details: dict = {}

        # ── API de detalle (solo si hay nuevos y no es primera ejecución) ──────
        if new_ids and previous_ids:
            print(f"  -> Consultando API de detalle para {len(new_ids)} producto(s) nuevo(s)...")
            BATCH_SIZE = 20
            for batch_start in range(0, len(new_ids), BATCH_SIZE):
                batch     = new_ids[batch_start : batch_start + BATCH_SIZE]
                ids_param = ",".join(batch)

                api_response = page.evaluate(f"""
                    async () => {{
                        const url = 'https://www.uniqlo.com/es/api/commerce/v5/es/products'
                                  + '?productIds={ids_param}&priceGroups=SPR';
                        try {{
                            const r = await fetch(url, {{ credentials: 'include' }});
                            return {{ status: r.status, body: await r.json() }};
                        }} catch (e) {{
                            return {{ status: 0, error: e.toString() }};
                        }}
                    }}
                """)

                status = api_response.get("status", 0)
                print(f"     API HTTP {status}")

                if status != 200:
                    print(f"     Respuesta inesperada: {json.dumps(api_response)[:400]}")
                    continue

                body  = api_response.get("body", {})
                # Intentamos las rutas de respuesta más habituales de Uniqlo
                items = (
                    (body.get("result") or {}).get("items")
                    or body.get("items")
                    or body.get("data")
                    or []
                )
                print(f"     {len(items)} item(s) recibidos del API")

                for item in items:
                    pid = item.get("productId") or item.get("id")
                    if not pid:
                        continue

                    # Precios
                    prices     = item.get("prices") or {}
                    orig_price = _parse_price(
                        prices.get("base") or prices.get("original") or prices.get("was")
                    )
                    curr_price = _parse_price(
                        prices.get("promo") or prices.get("sale")
                        or prices.get("now")  or prices.get("current")
                    )
                    disc = (
                        round((1 - curr_price / orig_price) * 100, 1)
                        if orig_price and curr_price and orig_price > 0
                        else None
                    )

                    # Nombre
                    name = (
                        item.get("name") or item.get("title")
                        or item.get("displayCode") or pid
                    )

                    # Tallas con stock disponible
                    sizes_raw   = item.get("sizes") or item.get("availableSizes") or []
                    avail_sizes = []
                    for s in sizes_raw:
                        if isinstance(s, dict):
                            stock = str(s.get("stock") or s.get("stockStatus") or "").upper()
                            if stock and "OUT" not in stock and stock not in {"0", "SOLDOUT"}:
                                sz_name = s.get("name") or s.get("code") or ""
                                if sz_name:
                                    avail_sizes.append(sz_name)
                        elif isinstance(s, str) and s:
                            avail_sizes.append(s)

                    details[pid] = {
                        "name":           name,
                        "current_price":  curr_price,
                        "original_price": orig_price,
                        "discount_pct":   disc,
                        "sizes":          avail_sizes,
                    }
                    print(
                        f"     {pid}: {name!r} | "
                        f"{curr_price}€ (-{disc}%) | "
                        f"tallas: {avail_sizes}"
                    )

        browser.close()

    print(f"  -> Total: {len(current_ids)} productos únicos en oferta")
    return {
        "product_ids": current_ids,
        "new_ids":     new_ids,
        "details":     details,
    }


def passes_filters(pid: str, details: dict) -> bool:
    """True si el producto supera todos los filtros configurados."""
    product = details.get(pid)

    if not product:
        # Sin datos de detalle → incluir por seguridad (fail-open)
        print(f"  [FILTRO] {pid}: sin datos de detalle → incluido por defecto")
        return True

    name          = product.get("name", "")
    current_price = product.get("current_price")
    discount_pct  = product.get("discount_pct")
    sizes         = product.get("sizes", [])

    # ── Filtro precio / descuento ──────────────────────────────────────────────
    if current_price is not None and discount_pct is not None:
        threshold = (
            DISCOUNT_BELOW_20 if current_price < PRICE_BREAKPOINT
            else DISCOUNT_ABOVE_20
        )
        if discount_pct < threshold:
            print(
                f"  [FILTRO] {pid} '{name}': "
                f"{discount_pct:.1f}% dto < {threshold}% → EXCLUIDO"
            )
            return False
        print(f"  [FILTRO] {pid} '{name}': {discount_pct:.1f}% dto ≥ {threshold}% → OK precio")
    else:
        print(f"  [FILTRO] {pid} '{name}': precio/dto no disponible → sin filtro de precio")

    # ── Filtro de talla ────────────────────────────────────────────────────────
    if not sizes:
        print(f"  [FILTRO] {pid}: tallas no disponibles → incluido por defecto")
        return True

    sizes_set   = set(sizes)
    sizes_lower = {s.lower() for s in sizes}

    # Talla única (en cualquier idioma)
    if sizes_lower & TALLA_UNICA_KEYWORDS:
        print(f"  [FILTRO] {pid}: talla única → incluido")
        return True

    # M o L
    if sizes_set & SIZES_ALWAYS:
        print(f"  [FILTRO] {pid}: M/L disponible → incluido")
        return True

    # Cintura 30 / 31 / 32
    if sizes_set & SIZES_WAIST:
        print(f"  [FILTRO] {pid}: cintura 30/31/32 disponible → incluido")
        return True

    # Oversize → S también es válida
    if "oversize" in name.lower() and "S" in sizes_set:
        print(f"  [FILTRO] {pid}: '{name}' es Oversize + talla S → incluido")
        return True

    print(f"  [FILTRO] {pid} '{name}': tallas {sizes} no coinciden → EXCLUIDO")
    return False


def load_state() -> set[str]:
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE) as f:
        data = json.load(f)
    return set(data.get("product_ids", []))


def save_state(ids: list[str]):
    now = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump({"product_ids": ids, "last_updated": now}, f, indent=2)


def send_email(filtered_ids: list[str], details: dict):
    now              = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    product_url_base = "https://www.uniqlo.com/es/es/products"

    items_html = ""
    for pid in filtered_ids:
        short_id     = "-".join(pid.split("-")[:2])
        product_link = f"{product_url_base}/{short_id}/00"
        info         = details.get(pid, {})

        name  = info.get("name", pid)
        curr  = info.get("current_price")
        orig  = info.get("original_price")
        disc  = info.get("discount_pct")
        sizes = info.get("sizes", [])

        if curr is not None and orig is not None and disc is not None:
            price_html = (
                f'<span style="color:#999;text-decoration:line-through;">{orig:.2f}€</span>'
                f'&nbsp;→&nbsp;'
                f'<strong style="color:#e60012;font-size:16px;">{curr:.2f}€</strong>'
                f'&nbsp;<span style="background:#e60012;color:#fff;padding:2px 7px;'
                f'border-radius:3px;font-size:12px;font-weight:bold;">−{disc:.0f}%</span>'
            )
        elif curr is not None:
            price_html = f'<strong>{curr:.2f}€</strong>'
        else:
            price_html = '<span style="color:#aaa;">precio no disponible</span>'

        sizes_html = ""
        if sizes:
            sizes_html = (
                f'<div style="margin-top:4px;color:#555;font-size:13px;">'
                f'Tallas disponibles: {", ".join(sizes)}</div>'
            )

        items_html += f"""
        <li style="margin-bottom:18px;list-style:none;
                   border-left:3px solid #e60012;padding-left:14px;">
          <a href="{product_link}"
             style="font-size:15px;color:#111;text-decoration:none;
                    font-weight:bold;display:block;margin-bottom:5px;">{name}</a>
          <div style="margin-bottom:2px;">{price_html}</div>
          {sizes_html}
          <span style="font-size:10px;color:#ccc;">{pid}</span>
        </li>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#333;">
      <h2 style="color:#e60012;border-bottom:2px solid #e60012;padding-bottom:10px;">
        Uniqlo &mdash; {len(filtered_ids)} artículo(s) nuevo(s) en ofertas
      </h2>
      <p style="color:#666;margin-top:0;">Detectado el <strong>{now}</strong></p>
      <ul style="padding:0;margin:0;">{items_html}</ul>
      <p style="margin-top:28px;">
        <a href="{URL}"
           style="background:#e60012;color:#fff;padding:12px 24px;
                  text-decoration:none;border-radius:4px;font-weight:bold;
                  display:inline-block;">
          Ver sección de ofertas &rarr;
        </a>
      </p>
      <hr style="margin-top:36px;border:none;border-top:1px solid #eee;"/>
      <p style="font-size:11px;color:#bbb;margin:8px 0;">
        Uniqlo Monitor &middot; GitHub Actions
      </p>
    </body></html>
    """

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = f"Uniqlo ofertas: {len(filtered_ids)} artículo(s) nuevo(s)"
    msg["From"]    = GMAIL_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())

    print(f"  -> Email enviado con {len(filtered_ids)} artículo(s)")


def main():
    now = datetime.now(timezone.utc).isoformat()
    print(f"[{now}] Comprobando ofertas...")

    previous_ids = load_state()

    result      = fetch_ids_and_details(previous_ids)
    current_ids = result["product_ids"]
    new_ids     = result["new_ids"]
    details     = result["details"]

    print(f"  -> {len(current_ids)} artículos detectados en total")

    if not previous_ids:
        save_state(current_ids)
        print("  -> Primera ejecución. Estado guardado. No se envía email.")
        return

    if not new_ids:
        print("  -> Sin cambios.")
        save_state(current_ids)
        return

    print(f"  -> {len(new_ids)} artículo(s) NUEVO(S): {new_ids}")
    print("  -> Aplicando filtros...")

    filtered_ids = [pid for pid in new_ids if passes_filters(pid, details)]

    if filtered_ids:
        print(f"  -> {len(filtered_ids)} artículo(s) superan los filtros → enviando email")
        send_email(filtered_ids, details)
    else:
        print(
            f"  -> {len(new_ids)} nuevo(s) detectado(s), "
            f"pero ninguno supera los filtros. No se envía email."
        )

    save_state(current_ids)


if __name__ == "__main__":
    main()

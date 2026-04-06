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


def product_url(pid: str) -> str:
    short_id = "-".join(pid.split("-")[:2])
    return f"https://www.uniqlo.com/es/es/products/{short_id}/00"


def _to_eur(value) -> float | None:
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
        for key in ("value", "amount", "price"):
            if price_obj.get(key) is not None:
                return _to_eur(price_obj[key])
    return None


def _extract_items_from_body(body) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        candidates = [
            (body.get("result") or {}).get("items"),
            body.get("items"),
            body.get("products"),
            body.get("data") if isinstance(body.get("data"), list) else None,
            (body.get("data") or {}).get("items")
                if isinstance(body.get("data"), dict) else None,
        ]
        for c in candidates:
            if c and isinstance(c, list):
                return c
    return []


def _parse_sizes(item: dict) -> list[str]:
    avail = []
    for key in ("sizes", "availableSizes", "skus", "variants"):
        entries = item.get(key)
        if not entries or not isinstance(entries, list):
            continue
        for s in entries:
            if isinstance(s, str):
                avail.append(s)
            elif isinstance(s, dict):
                stock_raw = s.get("stock") or s.get("stockStatus") or s.get("qty")
                stock_str = str(stock_raw).upper() if stock_raw is not None else ""
                if stock_str and (
                    any(bad in stock_str for bad in ("OUT", "SOLD", "NO"))
                    or stock_str == "0"
                ):
                    continue
                sz_name = (
                    s.get("name") or s.get("sizeName") or s.get("code")
                    or s.get("displayCode") or s.get("label")
                )
                if sz_name:
                    avail.append(str(sz_name))
        if avail:
            break
    return avail


def _parse_item(item: dict) -> tuple[str, dict]:
    pid = (
        item.get("productId") or item.get("id")
        or item.get("code") or item.get("displayCode") or ""
    )
    name = item.get("name") or item.get("title") or pid

    prices     = item.get("prices") or {}
    orig_price = _parse_price(
        prices.get("base") or prices.get("original") or prices.get("was")
        or prices.get("regularPrice") or prices.get("listPrice")
        or item.get("regularPrice") or item.get("originalPrice")
    )
    curr_price = _parse_price(
        prices.get("promo") or prices.get("sale") or prices.get("now")
        or prices.get("current") or prices.get("salePrice") or prices.get("finalPrice")
        or item.get("price") or item.get("salePrice")
    )
    disc = (
        round((1 - curr_price / orig_price) * 100, 1)
        if orig_price and curr_price and orig_price > 0
        else None
    )

    return pid, {
        "name":           name,
        "current_price":  curr_price,
        "original_price": orig_price,
        "discount_pct":   disc,
        "sizes":          _parse_sizes(item),
        "image_url":      None,
    }


def fetch_ids_and_details(previous_ids: set[str]) -> dict:
    intercepted: dict  = {}
    captured_req: dict = {}

    def on_request(request):
        if "/api/commerce/v5/es/products" in request.url and not captured_req:
            captured_req["url"]     = request.url
            captured_req["headers"] = dict(request.headers)

    def on_response(response):
        try:
            if (
                "/api/commerce/v5/es/products" in response.url
                and response.status == 200
            ):
                body  = response.json()
                items = _extract_items_from_body(body)
                for item in items:
                    pid_i, parsed = _parse_item(item)
                    if pid_i and pid_i not in intercepted:
                        intercepted[pid_i] = parsed
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage"],
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
        page.on("request",  on_request)
        page.on("response", on_response)

        print("  -> Cargando home...")
        page.goto(
            "https://www.uniqlo.com/es/es/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        page.wait_for_timeout(2000)

        print("  -> Cargando página de ofertas...")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        print("  -> Scrolleando para renderizar todos los productos...")
        current_pos = 0
        for i in range(120):
            current_pos += 1080
            page.evaluate(f"window.scrollTo(0, {current_pos})")
            page.wait_for_timeout(2500)
            page_height = page.evaluate("document.body.scrollHeight")
            print(
                f"     Paso {i+1}: pos={current_pos}px / "
                f"altura={page_height}px | interceptados={len(intercepted)}"
            )
            if current_pos >= page_height:
                page.wait_for_timeout(3000)
                print("  -> Fondo de página alcanzado.")
                break
        else:
            print("  -> Límite de pasos alcanzado.")

        print(f"  -> {len(intercepted)} productos con datos interceptados por red")
        if captured_req:
            print(f"  -> Cabeceras capturadas de: {captured_req['url'][:100]}")
        else:
            print("  -> AVISO: no se capturó ninguna llamada al API durante el scroll")

        # ── Extracción DOM: IDs + URLs de imagen ──────────────────────────────
        print("  -> Extrayendo IDs e imágenes del DOM...")
        dom_result = page.evaluate("""
            () => {
                const ids    = new Set();
                const images = {};
                document.querySelectorAll('a[href*="/products/"]').forEach(a => {
                    const m = a.href.match(/\\/products\\/(E\\d+-\\d+)/);
                    if (!m) return;
                    const pid = m[1];
                    ids.add(pid);
                    if (!images[pid]) {
                        const img = a.querySelector('img');
                        if (img && img.src && img.src.startsWith('http')) {
                            images[pid] = img.src;
                        }
                    }
                });
                return { ids: Array.from(ids), images: images };
            }
        """)
        current_ids = dom_result["ids"]
        dom_images  = dom_result["images"]

        if not current_ids:
            raise ValueError("No se encontró ningún producto en el DOM.")

        for pid_d, img_src in dom_images.items():
            if pid_d in intercepted:
                intercepted[pid_d]["image_url"] = img_src

        missing = [pid for pid in current_ids if pid not in intercepted]
        print(
            f"  -> {len(current_ids)} productos únicos | "
            f"con datos: {len(intercepted)} | "
            f"sin datos: {len(missing)}"
        )

        # ── Fetch complementario via context.request ───────────────────────────
        if missing and captured_req:
            safe_headers = {
                k: v for k, v in captured_req["headers"].items()
                if not k.startswith(":")
                and k.lower() not in ("content-length", "host")
            }
            raw_url  = captured_req["url"]
            base_url = raw_url.split("?")[0]

            print(
                f"  -> Consultando API via context.request "
                f"para {len(missing)} productos sin datos..."
            )
            BATCH = 20
            for start in range(0, len(missing), BATCH):
                batch     = missing[start : start + BATCH]
                ids_param = ",".join(batch)
                api_url   = f"{base_url}?productIds={ids_param}"

                try:
                    resp = context.request.get(
                        api_url,
                        headers=safe_headers,
                        timeout=30000,
                    )
                    print(f"     HTTP {resp.status}")
                    if resp.status == 200:
                        body  = resp.json()
                        items = _extract_items_from_body(body)
                        print(f"     Items recibidos: {len(items)}")
                        if not items:
                            raw = json.dumps(body, ensure_ascii=False)[:800]
                            print(f"     RAW (sin items): {raw}")
                        for item in items:
                            pid_i, parsed = _parse_item(item)
                            if pid_i:
                                parsed["image_url"] = dom_images.get(pid_i)
                                intercepted[pid_i]  = parsed
                                print(
                                    f"     {pid_i}: '{parsed['name']}' | "
                                    f"{parsed['current_price']}€ "
                                    f"(-{parsed['discount_pct']}%) | "
                                    f"tallas: {parsed['sizes']}"
                                )
                    else:
                        raw = resp.text()[:400]
                        print(f"     Respuesta inesperada: {raw}")
                except Exception as e:
                    print(f"     Error en context.request: {e}")

        elif missing and not captured_req:
            print(
                f"  -> AVISO: sin cabeceras capturadas. "
                f"Los {len(missing)} productos sin datos pasarán filtros por defecto."
            )

        final_covered = len([p for p in current_ids if p in intercepted])
        print(f"  -> Cobertura final: {final_covered}/{len(current_ids)}")

        browser.close()

    new_ids = [pid for pid in current_ids if pid not in previous_ids]
    return {
        "product_ids": current_ids,
        "new_ids":     new_ids,
        "details":     intercepted,
    }


def _discount_threshold(price: float) -> float:
    return DISCOUNT_BELOW_20 if price < PRICE_BREAKPOINT else DISCOUNT_ABOVE_20


def passes_filters(pid: str, details: dict) -> bool:
    product = details.get(pid)
    if not product:
        print(f"  [FILTRO] {pid}: sin datos → incluido por defecto (fail-open)")
        return True

    name          = product.get("name", "")
    current_price = product.get("current_price")
    discount_pct  = product.get("discount_pct")
    sizes         = product.get("sizes", [])

    # Precio / descuento
    if current_price is not None and discount_pct is not None:
        threshold = _discount_threshold(current_price)
        if discount_pct < threshold:
            print(
                f"  [FILTRO] {pid} '{name}': "
                f"{discount_pct:.1f}% < {threshold}% → EXCLUIDO"
            )
            return False
        print(f"  [FILTRO] {pid}: {discount_pct:.1f}% ≥ {threshold}% → OK precio")
    else:
        print(f"  [FILTRO] {pid}: precio/dto no disponible → sin filtro de precio")

    # Talla
    if not sizes:
        print(f"  [FILTRO] {pid}: tallas no disponibles → incluido por defecto")
        return True

    sizes_set   = set(sizes)
    sizes_lower = {s.lower() for s in sizes}

    if sizes_lower & TALLA_UNICA_KEYWORDS:
        print(f"  [FILTRO] {pid}: talla única → incluido")
        return True
    if sizes_set & SIZES_ALWAYS:
        print(f"  [FILTRO] {pid}: M/L → incluido")
        return True
    if sizes_set & SIZES_WAIST:
        print(f"  [FILTRO] {pid}: cintura 30/31/32 → incluido")
        return True
    if "oversize" in name.lower() and "S" in sizes_set:
        print(f"  [FILTRO] {pid}: oversize + S → incluido")
        return True

    print(f"  [FILTRO] {pid}: tallas {sizes} no coinciden → EXCLUIDO")
    return False


def find_price_drops(
    current_ids: list[str],
    previous_ids: set[str],
    previous_prices: dict,
    details: dict,
) -> list[str]:
    """
    Devuelve los IDs de productos ya conocidos cuyo descuento ha cruzado
    el umbral hacia arriba respecto al estado guardado.
    Solo se incluyen si además pasan el filtro de tallas.
    """
    drops = []
    # Solo productos que estaban Y siguen estando (no nuevos, no desaparecidos)
    existing = [pid for pid in current_ids if pid in previous_ids]

    for pid in existing:
        product = details.get(pid)
        if not product:
            continue

        curr_price   = product.get("current_price")
        curr_disc    = product.get("discount_pct")

        # Sin datos de precio actuales → no podemos comparar
        if curr_price is None or curr_disc is None:
            continue

        threshold = _discount_threshold(curr_price)

        # El descuento actual no supera el umbral → no notificar
        if curr_disc < threshold:
            continue

        # Recuperar descuento anterior guardado en state.json
        prev_data  = previous_prices.get(pid, {})
        prev_disc  = prev_data.get("discount")   # puede ser None si no había dato

        # Si el descuento anterior ya superaba el umbral → ya lo notificamos antes
        if prev_disc is not None and prev_disc >= threshold:
            continue

        # El descuento cruzó el umbral (o antes era None y ahora sí pasa)
        # Comprobamos también el filtro de tallas
        if not passes_filters(pid, details):
            continue

        prev_str = f"{prev_disc:.1f}%" if prev_disc is not None else "sin dato"
        print(
            f"  [BAJADA] {pid} '{product.get('name', '')}': "
            f"{prev_str} → {curr_disc:.1f}% (umbral {threshold}%) → NOTIFICAR"
        )
        drops.append(pid)

    return drops


def load_state() -> tuple[set[str], dict]:
    """
    Devuelve (previous_ids, previous_prices).
    previous_prices: {pid: {"price": float|None, "discount": float|None}}
    """
    if not os.path.exists(STATE_FILE):
        return set(), {}
    with open(STATE_FILE) as f:
        data = json.load(f)
    ids    = set(data.get("product_ids", []))
    prices = data.get("product_prices", {})
    return ids, prices


def save_state(ids: list[str], details: dict):
    """Guarda IDs y precios/descuentos actuales en state.json."""
    now = datetime.now(timezone.utc).isoformat()
    product_prices = {}
    for pid in ids:
        product = details.get(pid, {})
        product_prices[pid] = {
            "price":    product.get("current_price"),
            "discount": product.get("discount_pct"),
        }
    with open(STATE_FILE, "w") as f:
        json.dump(
            {
                "product_ids":    ids,
                "product_prices": product_prices,
                "last_updated":   now,
            },
            f,
            indent=2,
        )


def _build_card(pid: str, details: dict, label: str = "") -> str:
    """Genera el HTML de una tarjeta de producto para el email."""
    info  = details.get(pid, {})
    name  = info.get("name") or pid
    curr  = info.get("current_price")
    orig  = info.get("original_price")
    disc  = info.get("discount_pct")
    sizes = info.get("sizes", [])
    img   = info.get("image_url") or ""
    link  = product_url(pid)

    # Badge de descuento (más etiqueta opcional "BAJADA DE PRECIO")
    badge_parts = []
    if disc is not None:
        badge_parts.append(
            f'<span style="background:#e60012;color:#fff;padding:3px 8px;'
            f'border-radius:3px;font-size:12px;font-weight:bold;">'
            f'−{disc:.0f}%</span>'
        )
    if label:
        badge_parts.append(
            f'<span style="background:#ff8c00;color:#fff;padding:3px 8px;'
            f'border-radius:3px;font-size:11px;font-weight:bold;'
            f'margin-left:4px;">{label}</span>'
        )
    badge = (
        f'<div style="margin-bottom:6px;">'
        + " ".join(badge_parts)
        + "</div>"
        if badge_parts else ""
    )

    if curr is not None and orig is not None:
        price_html = (
            f'<span style="color:#999;text-decoration:line-through;'
            f'font-size:13px;">{orig:.2f}€</span>&nbsp;→&nbsp;'
            f'<strong style="color:#e60012;font-size:18px;">{curr:.2f}€</strong>'
        )
    elif curr is not None:
        price_html = f'<strong style="font-size:18px;">{curr:.2f}€</strong>'
    else:
        price_html = (
            '<span style="color:#aaa;font-size:13px;">Consultar precio en web</span>'
        )

    if sizes:
        chips = "".join(
            f'<span style="display:inline-block;border:1px solid #ccc;'
            f'border-radius:3px;padding:1px 7px;margin:2px 2px 0 0;'
            f'font-size:12px;">{s}</span>'
            for s in sizes
        )
        sizes_html = (
            f'<div style="margin-top:8px;color:#555;font-size:12px;">'
            f'Tallas disponibles:</div>'
            f'<div style="margin-top:4px;">{chips}</div>'
        )
    else:
        sizes_html = ""

    img_block = (
        f'<a href="{link}">'
        f'<img src="{img}" width="100" height="130" alt="{name}"'
        f' style="display:block;border-radius:4px;object-fit:cover;'
        f'background:#f5f5f5;" /></a>'
        if img else
        f'<a href="{link}" style="display:block;width:100px;height:130px;'
        f'background:#f0f0f0;border-radius:4px;text-align:center;'
        f'line-height:130px;font-size:11px;color:#aaa;">sin imagen</a>'
    )

    return f"""
        <tr>
          <td style="padding:16px 0;border-bottom:1px solid #eee;">
            <table width="100%" cellpadding="0" cellspacing="0"><tr>
              <td width="116" valign="top" style="padding-right:16px;">
                {img_block}
              </td>
              <td valign="top">
                {badge}
                <div style="margin-bottom:6px;">
                  <a href="{link}" style="font-size:15px;color:#111;
                     text-decoration:none;font-weight:bold;
                     line-height:1.3;">{name}</a>
                </div>
                <div style="margin-bottom:4px;">{price_html}</div>
                {sizes_html}
                <div style="margin-top:10px;">
                  <a href="{link}" style="font-size:12px;color:#e60012;
                     text-decoration:none;">Ver producto →</a>
                </div>
              </td>
            </tr></table>
          </td>
        </tr>"""


def _section_header(title: str) -> str:
    return (
        f'<tr><td style="padding:20px 0 4px;">'
        f'<div style="font-size:13px;font-weight:bold;color:#888;'
        f'text-transform:uppercase;letter-spacing:1px;'
        f'border-bottom:2px solid #eee;padding-bottom:6px;">'
        f'{title}</div></td></tr>'
    )


def send_email(new_ids: list[str], price_drop_ids: list[str], details: dict):
    now        = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    total      = len(new_ids) + len(price_drop_ids)
    cards_html = ""

    if new_ids:
        cards_html += _section_header(
            f"{'Artículos nuevos' if len(new_ids) > 1 else 'Artículo nuevo'} "
            f"({len(new_ids)})"
        )
        for pid in new_ids:
            cards_html += _build_card(pid, details)

    if price_drop_ids:
        cards_html += _section_header(
            f"{'Bajadas de precio' if len(price_drop_ids) > 1 else 'Bajada de precio'} "
            f"({len(price_drop_ids)})"
        )
        for pid in price_drop_ids:
            cards_html += _build_card(pid, details, label="↓ PRECIO")

    # Asunto del email
    parts = []
    if new_ids:
        parts.append(
            f"{len(new_ids)} nuevo{'s' if len(new_ids) > 1 else ''}"
        )
    if price_drop_ids:
        parts.append(
            f"{len(price_drop_ids)} bajada{'s' if len(price_drop_ids) > 1 else ''} "
            f"de precio"
        )
    subject = "Uniqlo ofertas: " + " · ".join(parts)

    html_body = f"""
    <html>
    <body style="margin:0;padding:0;background:#f4f4f4;
                 font-family:Arial,Helvetica,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0"
             style="background:#f4f4f4;padding:24px 0;">
        <tr><td>
        <table width="620" cellpadding="0" cellspacing="0" align="center"
               style="background:#fff;border-radius:6px;
                      box-shadow:0 1px 4px rgba(0,0,0,.1);">
          <tr>
            <td style="background:#e60012;padding:22px 28px;
                       border-radius:6px 6px 0 0;">
              <div style="color:#fff;font-size:20px;font-weight:bold;">
                Uniqlo &mdash; {total} artículo(s) de interés
              </div>
              <div style="color:#ffbbbb;font-size:12px;margin-top:4px;">
                Detectado el {now}
              </div>
            </td>
          </tr>
          <tr>
            <td style="padding:8px 28px 16px;">
              <table width="100%" cellpadding="0" cellspacing="0">
                {cards_html}
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding:0 28px 28px;text-align:center;">
              <a href="{URL}"
                 style="display:inline-block;background:#e60012;color:#fff;
                        padding:13px 30px;text-decoration:none;border-radius:4px;
                        font-weight:bold;font-size:15px;">
                Ver todas las ofertas →
              </a>
            </td>
          </tr>
          <tr>
            <td style="border-top:1px solid #eee;padding:12px 28px;
                       text-align:center;font-size:11px;color:#bbb;">
              Uniqlo Monitor &middot; GitHub Actions
            </td>
          </tr>
        </table>
        </td></tr>
      </table>
    </body>
    </html>
    """

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())

    print(f"  -> Email enviado: {len(new_ids)} nuevo(s), "
          f"{len(price_drop_ids)} bajada(s) de precio")


def main():
    now = datetime.now(timezone.utc).isoformat()
    print(f"[{now}] Comprobando ofertas...")

    previous_ids, previous_prices = load_state()
    result      = fetch_ids_and_details(previous_ids)
    current_ids = result["product_ids"]
    new_ids     = result["new_ids"]
    details     = result["details"]

    print(f"  -> {len(current_ids)} artículos detectados en total")

    if not previous_ids:
        save_state(current_ids, details)
        print("  -> Primera ejecución. Estado guardado. No se envía email.")
        return

    # ── Artículos nuevos ──────────────────────────────────────────────────────
    print(f"  -> {len(new_ids)} artículo(s) nuevo(s) detectado(s)")
    filtered_new = [pid for pid in new_ids if passes_filters(pid, details)]

    # ── Bajadas de precio en artículos ya conocidos ───────────────────────────
    print("  -> Comprobando bajadas de precio en artículos existentes...")
    price_drops = find_price_drops(current_ids, previous_ids, previous_prices, details)

    # ── Envío ─────────────────────────────────────────────────────────────────
    if filtered_new or price_drops:
        print(
            f"  -> {len(filtered_new)} nuevo(s) + "
            f"{len(price_drops)} bajada(s) de precio → enviando email"
        )
        send_email(filtered_new, price_drops, details)
    else:
        print("  -> Sin novedades que notificar.")

    save_state(current_ids, details)


if __name__ == "__main__":
    main()

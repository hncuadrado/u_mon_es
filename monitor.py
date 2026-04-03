import os
import json
import smtplib
from urllib.parse import urlparse, parse_qs
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
# ───────────────────────────────────────────────────────────────────────────────


def fetch_product_ids() -> list[str]:
    captured_ids: list[str] = []

    def handle_response(response):
        """Intercepta las páginas del catálogo paginado (offset=36, 72, ...)."""
        url = response.url
        if "/api/commerce/v5/es/products" not in url:
            return
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if "path" not in params:
            return
        try:
            data = response.json()
            items = data.get("result", {}).get("items", [])
            if not items:
                return
            ids = [item["productId"] for item in items if "productId" in item]
            offset = params.get("offset", ["0"])[0]
            print(f"  -> [catálogo offset={offset}] {len(ids)} productos (total acumulado: {len(captured_ids) + len(ids)})")
            captured_ids.extend(ids)
        except Exception as e:
            print(f"  -> Error parseando respuesta paginada: {e}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ]
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
        page.on("response", handle_response)

        print("  -> Cargando home...")
        page.goto("https://www.uniqlo.com/es/es/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        print("  -> Cargando página de ofertas...")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # ── Extraer offset=0 del DOM ───────────────────────────────────────
        # Los primeros productos están renderizados en el HTML inicial,
        # nunca generan petición de red. Los extraemos de los enlaces del DOM.
        print("  -> Extrayendo productos iniciales del DOM...")
        dom_ids = page.evaluate("""
            () => {
                const links = document.querySelectorAll('a[href*="/products/"]');
                const ids = new Set();
                links.forEach(a => {
                    const m = a.href.match(/\\/products\\/(E\\d+-\\d+)/);
                    if (m) ids.add(m[1]);
                });
                return Array.from(ids);
            }
        """)
        if dom_ids:
            print(f"  -> [DOM inicial] {len(dom_ids)} productos encontrados")
            captured_ids.extend(dom_ids)
        else:
            print("  -> [DOM inicial] Sin productos encontrados en el DOM")
        # ──────────────────────────────────────────────────────────────────

        # ── Scroll para disparar offset=36, 72, ... ────────────────────────
        print("  -> Scrolleando para cargar páginas siguientes...")
        VIEWPORT_HEIGHT = 1080
        STEP = VIEWPORT_HEIGHT
        PAUSE_MS = 2500
        MAX_STEPS = 120

        current_pos = 0
        for i in range(MAX_STEPS):
            current_pos += STEP
            page.evaluate(f"window.scrollTo(0, {current_pos})")
            page.wait_for_timeout(PAUSE_MS)

            page_height = page.evaluate("document.body.scrollHeight")
            print(f"     Paso {i+1}: pos={current_pos}px / altura={page_height}px, productos={len(captured_ids)}")

            if current_pos >= page_height:
                page.wait_for_timeout(3000)
                print("  -> Fondo de página alcanzado, scroll completo.")
                break
        else:
            print("  -> Límite de pasos alcanzado, terminando scroll.")

        page.wait_for_timeout(3000)
        browser.close()

    if not captured_ids:
        raise ValueError(
            "No se capturó ningún producto. "
            "La página puede haber cambiado de estructura."
        )

    seen = set()
    result = []
    for pid in captured_ids:
        if pid not in seen:
            seen.add(pid)
            result.append(pid)

    print(f"  -> Total tras deduplicar: {len(result)} productos únicos")
    return result


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


def send_email(new_ids: list[str]):
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    product_url_base = "https://www.uniqlo.com/es/es/products"

    items_html = ""
    for pid in new_ids:
        parts = pid.split("-")
        short_id = "-".join(parts[:2]) if len(parts) >= 2 else pid
        product_link = f"{product_url_base}/{short_id}/00"
        items_html += f'<li><a href="{product_link}" style="font-size:16px;">{pid}</a></li>\n'

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;">
      <h2 style="color:#e60012;">Uniqlo: {len(new_ids)} articulo(s) nuevo(s) en ofertas</h2>
      <p>Detectado el <strong>{now}</strong></p>
      <ul style="line-height:2;">{items_html}</ul>
      <p style="margin-top:20px;">
        <a href="{URL}" style="background:#e60012;color:#fff;padding:10px 20px;
           text-decoration:none;border-radius:4px;font-weight:bold;">
          Ver seccion de ofertas
        </a>
      </p>
      <hr/><p style="font-size:11px;color:#999;">Uniqlo Monitor · GitHub Actions</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Uniqlo ofertas: {len(new_ids)} articulo(s) nuevo(s)"
    msg["From"]    = GMAIL_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())

    print(f"Email enviado con {len(new_ids)} articulo(s) nuevo(s)")


def main():
    now = datetime.now(timezone.utc).isoformat()
    print(f"[{now}] Comprobando ofertas...")

    current_ids = fetch_product_ids()
    print(f"  -> {len(current_ids)} articulos detectados en total")

    previous_ids = load_state()

    if not previous_ids:
        save_state(current_ids)
        print("  -> Primera ejecucion. Estado guardado. No se envia email.")
        return

    new_ids = [pid for pid in current_ids if pid not in previous_ids]

    if new_ids:
        print(f"  -> {len(new_ids)} articulo(s) NUEVO(S): {new_ids}")
        send_email(new_ids)
        save_state(current_ids)
    else:
        print("  -> Sin cambios.")
        save_state(current_ids)


if __name__ == "__main__":
    main()

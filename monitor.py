import os
import json
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

# ── Configuración ──────────────────────────────────────────────────────────────
URL = "https://www.uniqlo.com/es/es/feature/sale/men/"
STATE_FILE = "state.json"

GMAIL_USER   = os.environ["GMAIL_USER"]
GMAIL_PASS   = os.environ["GMAIL_PASS"]
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]
# ───────────────────────────────────────────────────────────────────────────────


def fetch_product_ids() -> list[str]:
    """Carga la página con un navegador real para superar Akamai y extrae los productIds."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
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
        )

        # Ocultar navigator.webdriver para evitar detección básica
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page = context.new_page()

        # Primero visitar la home para obtener cookies
        page.goto("https://www.uniqlo.com/es/es/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)

        # Ahora la página de ofertas
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector('script#__NEXT_DATA__', timeout=15000)

        html = page.content()
        browser.close()

    # Extraer __NEXT_DATA__
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        snippet = html[:800].replace("\n", " ")
        raise ValueError(f"No se encontró __NEXT_DATA__. HTML recibido: {snippet}")

    data = json.loads(match.group(1))

    ids: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("_type") == "CmsProductCollection":
                product_ids = node.get("productIds", {})
                ids.extend(product_ids.get("prioritized", []))
                ids.extend(product_ids.get("default", []))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)

    seen = set()
    result = []
    for pid in ids:
        if pid not in seen:
            seen.add(pid)
            result.append(pid)
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
    print(f"  -> {len(current_ids)} articulos en la seccion ahora")

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

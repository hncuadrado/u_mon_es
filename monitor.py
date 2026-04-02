import os
import json
import re
import smtplib
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

# ── Configuración ──────────────────────────────────────────────────────────────
URL = "https://www.uniqlo.com/es/es/feature/sale/men/"
STATE_FILE = "state.json"

GMAIL_USER   = os.environ["GMAIL_USER"]    # tu correo Gmail
GMAIL_PASS   = os.environ["GMAIL_PASS"]    # app password de Gmail
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]  # destinatario (puede ser el mismo)
# ───────────────────────────────────────────────────────────────────────────────


def fetch_product_ids() -> list[str]:
    """Descarga la página y extrae los productIds de CmsProductCollection."""
    req = urllib.request.Request(URL, headers={
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "es-ES,es;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    # Extraer el bloque __NEXT_DATA__
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise ValueError("No se encontró __NEXT_DATA__ en la página")

    data = json.loads(match.group(1))

    # Recorrer toda la estructura buscando CmsProductCollection
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

    # Normalizar: quitar el sufijo de talla "-00" final si lo tienen
    # y deduplicar manteniendo orden
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
    with open(STATE_FILE, "w") as f:
        json.dump({
            "product_ids": ids,
            "last_updated": datetime.utcnow().isoformat() + "Z"
        }, f, indent=2)


def send_email(new_ids: list[str]):
    now = datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")
    product_url_base = "https://www.uniqlo.com/es/es/products"

    # Construir lista HTML de productos
    items_html = ""
    for pid in new_ids:
        # El ID tiene formato E482873-000-00; el slug de URL usa solo los dos primeros segmentos
        parts = pid.split("-")
        short_id = "-".join(parts[:2]) if len(parts) >= 2 else pid
        product_link = f"{product_url_base}/{short_id}/00"
        items_html += f'<li><a href="{product_link}" style="font-size:16px;">{pid}</a></li>\n'

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;">
      <h2 style="color:#e60012;">🆕 Uniqlo: {len(new_ids)} artículo(s) nuevo(s) en ofertas</h2>
      <p>Detectado el <strong>{now}</strong></p>
      <ul style="line-height:2;">
        {items_html}
      </ul>
      <p style="margin-top:20px;">
        <a href="{URL}" style="background:#e60012;color:#fff;padding:10px 20px;
           text-decoration:none;border-radius:4px;font-weight:bold;">
          Ver sección de ofertas →
        </a>
      </p>
      <hr style="margin-top:30px;"/>
      <p style="font-size:11px;color:#999;">Uniqlo Monitor · GitHub Actions</p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🆕 Uniqlo ofertas: {len(new_ids)} artículo(s) nuevo(s)"
    msg["From"]    = GMAIL_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())

    print(f"✉️  Email enviado con {len(new_ids)} artículo(s) nuevo(s)")


def main():
    print(f"[{datetime.utcnow().isoformat()}] Comprobando ofertas...")

    current_ids = fetch_product_ids()
    print(f"  → {len(current_ids)} artículos en la sección ahora")

    previous_ids = load_state()

    if not previous_ids:
        # Primera ejecución: guardar estado sin avisar
        save_state(current_ids)
        print("  → Primera ejecución. Estado guardado. No se envía email.")
        return

    new_ids = [pid for pid in current_ids if pid not in previous_ids]

    if new_ids:
        print(f"  → {len(new_ids)} artículo(s) NUEVO(S): {new_ids}")
        send_email(new_ids)
        save_state(current_ids)
    else:
        print("  → Sin cambios.")
        # Actualizar igualmente por si han desaparecido artículos
        save_state(current_ids)


if __name__ == "__main__":
    main()

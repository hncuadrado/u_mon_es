import os
import json
import re
import smtplib
import time
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

# ── Configuración ──────────────────────────────────────────────────────────────
URL = "https://www.uniqlo.com/es/es/feature/sale/men/"
STATE_FILE = "state.json"

GMAIL_USER   = os.environ["GMAIL_USER"]
GMAIL_PASS   = os.environ["GMAIL_PASS"]
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]
# ───────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def fetch_product_ids() -> list[str]:
    session = requests.Session()

    # Visita primero la home para obtener cookies de Akamai/sesión
    try:
        session.get("https://www.uniqlo.com/es/es/", headers=HEADERS, timeout=20)
        time.sleep(2)
    except Exception as e:
        print(f"  (aviso: fallo en precarga de home: {e})")

    resp = session.get(URL, headers=HEADERS, timeout=30)

    if resp.status_code == 403:
        raise RuntimeError(
            f"Bloqueado por bot-detection (403). "
            f"Cookies recibidas: {dict(session.cookies)}"
        )
    resp.raise_for_status()
    html = resp.text

    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        snippet = html[:500].replace("\n", " ")
        raise ValueError(f"No se encontró __NEXT_DATA__. Inicio del HTML: {snippet}")

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

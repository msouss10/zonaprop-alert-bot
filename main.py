#!/usr/bin/env python3
import asyncio, os, json, re, sys, time, math
from pathlib import Path
from datetime import datetime, timezone
import requests
from playwright.async_api import async_playwright

# ============ Credenciales ============
BOT_TOKEN = os.getenv("BOT_TOKEN") or "TU_TOKEN_SI_CORRES_LOCAL"
CHAT_ID   = os.getenv("CHAT_ID")   or "TU_CHAT_ID_SI_CORRES_LOCAL"

# ============ Config ============
# Solo enviar si el aviso fue publicado dentro de las últimas N horas
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "24"))

# Máximo a enviar por búsqueda en cada corrida (para no saturar)
TOP_N_PER_SEARCH   = int(os.getenv("TOP_N_PER_SEARCH", "10"))
PER_LINK_DELAY_SEC = float(os.getenv("PER_LINK_DELAY_SEC", "1.0"))

# Listado de búsquedas (solo barrio + recientes)
SEARCHES = [
    # LOCALES
    ("Locales Palermo",  "https://www.zonaprop.com.ar/locales-comerciales-venta-palermo-orden-publicado-descendente.html"),
    ("Locales Belgrano", "https://www.zonaprop.com.ar/locales-comerciales-venta-belgrano-orden-publicado-descendente.html"),
    ("Locales Núñez",    "https://www.zonaprop.com.ar/locales-comerciales-venta-nunez-orden-publicado-descendente.html"),
    ("Locales Recoleta", "https://www.zonaprop.com.ar/locales-comerciales-venta-recoleta-orden-publicado-descendente.html"),
    ("Locales Chacarita","https://www.zonaprop.com.ar/locales-comerciales-venta-chacarita-orden-publicado-descendente.html"),
    # TERRENOS
    ("Terrenos Palermo",  "https://www.zonaprop.com.ar/terrenos-venta-palermo-orden-publicado-descendente.html"),
    ("Terrenos Belgrano", "https://www.zonaprop.com.ar/terrenos-venta-belgrano-orden-publicado-descendente.html"),
    ("Terrenos Núñez",    "https://www.zonaprop.com.ar/terrenos-venta-nunez-orden-publicado-descendente.html"),
    ("Terrenos Recoleta", "https://www.zonaprop.com.ar/terrenos-venta-recoleta-orden-publicado-descendente.html"),
    ("Terrenos Chacarita","https://www.zonaprop.com.ar/terrenos-venta-chacarita-orden-publicado-descendente.html"),
]

# Estado
DATA_DIR  = Path(".data"); DATA_DIR.mkdir(exist_ok=True, parents=True)
SEEN_FILE = DATA_DIR / "seen.json"

# Patrones
AD_PATTERN = re.compile(
    r"^(?:https?://www\.zonaprop\.com\.ar)?/propiedades/(?:clasificado/)?[A-Za-z0-9\-]+-\d+\.html$",
    re.IGNORECASE,
)
RE_MIN  = re.compile(r"hace\s+(\d+)\s*minutos?", re.I)
RE_HRS  = re.compile(r"hace\s+(\d+)\s*horas?", re.I)
RE_DIAS = re.compile(r"hace\s+(\d+)\s*d[ií]as?", re.I)
RE_HOY  = re.compile(r"\bhoy\b", re.I)
RE_AYER = re.compile(r"\bayer\b", re.I)

# ------------ Util Telegram ------------
def send_text(text: str, preview: bool = False):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": (not preview)
    }
    try:
        r = requests.post(api, data=payload, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[Telegram] {e}")

def send_link(url: str):
    # Solo el link: Telegram arma título+descripción+foto
    send_text(url, preview=True)
# ---------------------------------------

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except:
            return set()
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))

def parse_iso_to_utc_hours_ago(iso_str: str) -> float | None:
    try:
        s = iso_str.strip()
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt.astimezone(timezone.utc)
        return delta.total_seconds() / 3600.0
    except Exception:
        return None

def text_hours_ago(txt: str) -> float | None:
    t = (txt or "").lower()

    if RE_HOY.search(t):
        return 0.0
    m = RE_MIN.search(t)
    if m:
        return int(m.group(1)) / 60.0
    m = RE_HRS.search(t)
    if m:
        return float(m.group(1))
    m = RE_DIAS.search(t)
    if m:
        return int(m.group(1)) * 24.0
    if RE_AYER.search(t):
        return 24.0  # aprox
    return None

async def scrape_list(page, url: str) -> list[str]:
    await page.goto(url, timeout=120_000, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=60_000)
    except:
        pass
    # Scroll para cargar más
    for _ in range(12):
        await page.mouse.wheel(0, 6000)
        await page.wait_for_timeout(800)

    hrefs = await page.eval_on_selector_all(
        "a[href*='/propiedades/']",
        "els => els.map(e => e.getAttribute('href') || '')",
    )

    out, dedup = [], set()
    for raw in hrefs:
        if not raw: 
            continue
        if raw.startswith("https://www.zonaprop.com.ar"):
            path = raw.replace("https://www.zonaprop.com.ar", "")
        elif raw.startswith("http://www.zonaprop.com.ar"):
            path = raw.replace("http://www.zonaprop.com.ar", "")
        else:
            path = raw
        if AD_PATTERN.match(path):
            link = raw if raw.startswith("http") else "https://www.zonaprop.com.ar" + path
            pid  = link.split("#")[0]
            if pid in dedup: 
                continue
            dedup.add(pid)
            out.append(link)
    return out

async def get_age_hours(detail_page, url: str) -> float | None:
    """
    Abre el aviso y trata de inferir la edad:
      1) JSON-LD: datePublished / uploadDate / dateModified
      2) <meta property="article:published_time">
      3) Texto visible: 'Publicado hoy / hace X horas/minutos/días / ayer'
    Devuelve horas (float) o None si no pudo inferir.
    """
    try:
        await detail_page.goto(url, timeout=120_000, wait_until="domcontentloaded")
        try:
            await detail_page.wait_for_load_state("networkidle", timeout=60_000)
        except:
            pass

        # 1) JSON-LD
        ld_jsons = await detail_page.eval_on_selector_all(
            "script[type='application/ld+json']",
            "els => els.map(e => e.textContent || '')"
        )
        for raw in ld_jsons:
            try:
                import json as _json
                data = _json.loads(raw)
                # puede ser objeto o lista
                candidates = data if isinstance(data, list) else [data]
                for d in candidates:
                    for key in ("datePublished", "uploadDate", "dateModified"):
                        v = d.get(key)
                        if isinstance(v, str):
                            hrs = parse_iso_to_utc_hours_ago(v)
                            if hrs is not None:
                                return hrs
            except Exception:
                pass

        # 2) OpenGraph/Meta
        meta_time = await detail_page.eval_on_selector(
            "meta[property='article:published_time'], meta[itemprop='datePublished']",
            "el => el ? el.getAttribute('content') : null"
        )
        if meta_time:
            hrs = parse_iso_to_utc_hours_ago(meta_time)
            if hrs is not None:
                return hrs

        # 3) Texto visible heurístico
        whole = await detail_page.eval_on_selector(
            "body", "el => (el.textContent || '').replace(/\\s+/g,' ').trim()"
        )
        hrs = text_hours_ago(whole or "")
        return hrs
    except Exception as e:
        print(f"[age] {e}")
        return None

async def run(force: bool = False, warmup: bool = False):
    seen = load_seen()
    total_new_detected = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ))
        list_page   = await ctx.new_page()
        detail_page = await ctx.new_page()

        for name, url in SEARCHES:
            print(f"Buscando {name} ...")
            try:
                links = await scrape_list(list_page, url)
            except Exception as e:
                print(f"[WARN] {name}: {e}")
                continue

            # Si solo queremos llenar historial inicial
            if warmup:
                nuevos = [u for u in links if u not in seen]
                seen.update(links)
                total_new_detected += len(nuevos)
                print(f"  (warmup) vistos ahora: {len(links)} | nuevos marcados: {len(nuevos)}")
                continue

            # Encabezado (solo si hay algo para enviar tras filtrar)
            enviados = 0

            # Si force: ignoramos historial pero respetamos filtro de antigüedad
            candidates = links if force else [u for u in links if u not in seen]

            # Revisamos detalles hasta completar TOP_N_PER_SEARCH
            to_check = candidates[: max(TOP_N_PER_SEARCH * 3, TOP_N_PER_SEARCH + 5)]
            header_sent = False

            for u in to_check:
                # Chequear antigüedad real
                age = await get_age_hours(detail_page, u)
                if age is None:
                    # si no pudimos inferir, por defecto NO enviar
                    continue
                if age <= MAX_AGE_HOURS:
                    if not header_sent:
                        send_text(f"{name}:")
                        header_sent = True
                    send_link(u)
                    enviados += 1
                    seen.add(u)
                    time.sleep(PER_LINK_DELAY_SEC)
                    if enviados >= TOP_N_PER_SEARCH:
                        break

            if not force:
                total_new_detected += len([u for u in candidates if u not in seen])

        await browser.close()

    save_seen(seen)
    if warmup:
        print("Warm-up listo. Historial guardado en .data/seen.json")
    else:
        print(f"Listo. Nuevos detectados (antes del recorte por tope y filtro): {total_new_detected}")

if __name__ == "__main__":
    FORCE  = "--force"  in sys.argv
    WARMUP = "--warmup" in sys.argv
    asyncio.run(run(force=FORCE, warmup=WARMUP))

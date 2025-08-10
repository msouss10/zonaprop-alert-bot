#!/usr/bin/env python3
import asyncio, os, json, re, sys, time
from pathlib import Path
import requests
from playwright.async_api import async_playwright

# === TUS CREDENCIALES ===
BOT_TOKEN = "8343755970:AAGLqz79F1cRyXS2anb76B0BGCq5wbOPBFw"
CHAT_ID   = "550638260"

# === BÚSQUEDAS (solo barrio + recientes) ===
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

# límites/tiempos
TOP_N_PER_SEARCH   = 10      # normal: cuántos envía por búsqueda
PER_LINK_DELAY_SEC = 1.0     # pausa entre mensajes
SCROLLS_PER_PAGE   = 12      # “scroll infinito”
LOAD_TIMEOUT_MS    = 120_000

# archivo de estado
DATA_DIR  = Path(".data"); DATA_DIR.mkdir(exist_ok=True, parents=True)
SEEN_FILE = DATA_DIR / "seen.json"

# patrón de aviso
AD_PATTERN = re.compile(
    r"^(?:https?://www\.zonaprop\.com\.ar)?/propiedades/(?:clasificado/)?[A-Za-z0-9\-]+-\d+\.html$",
    re.IGNORECASE,
)

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:    return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except: return set()
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))

def send_link(url: str):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": url, "disable_web_page_preview": False}
    r = requests.post(api, data=payload, timeout=20)
    r.raise_for_status()

async def scrape_list(url: str):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()
        await page.goto(url, timeout=LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=60_000)
        except:
            pass
        for _ in range(SCROLLS_PER_PAGE):
            await page.mouse.wheel(0, 6000)
            await page.wait_for_timeout(900)

        hrefs = await page.eval_on_selector_all(
            "a[href*='/propiedades/']",
            "els => els.map(e => e.getAttribute('href') || '')",
        )
        await browser.close()

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

async def run(force: bool = False, warmup: bool = False):
    """
    force  = ignora historial y envía hasta TOP_N_PER_SEARCH
    warmup = NO envía nada; solo guarda todo en seen.json
    """
    already = load_seen()
    total_new = 0

    for name, url in SEARCHES:
        print(f"Buscando {name} ...")
        try:
            links = await scrape_list(url)
        except Exception as e:
            print(f"[WARN] {name}: {e}")
            continue

        if warmup:
            # no enviamos: solo guardamos todo lo que vemos
            nuevos = [u for u in links if u not in already]
            already.update(links)
            total_new += len(nuevos)
            print(f"  (warmup) vistos ahora: {len(links)} | nuevos marcados: {len(nuevos)}")
            continue

        if force:
            to_send = links[:TOP_N_PER_SEARCH]
        else:
            nuevos = [u for u in links if u not in already]
            to_send = nuevos[:TOP_N_PER_SEARCH]
            total_new += len(nuevos)

        for u in to_send:
            try:
                send_link(u)
                time.sleep(PER_LINK_DELAY_SEC)
            except Exception as e:
                print(f"[Telegram] {e}")
            already.add(u)

    save_seen(already)
    if warmup:
        print("Warm-up listo. Historial guardado en .data/seen.json")
    else:
        print(f"Listo. Nuevos detectados (antes de recorte por TOP_N): {total_new}")

if __name__ == "__main__":
    WARMUP = "--warmup" in sys.argv
    FORCE  = "--force"  in sys.argv
    asyncio.run(run(force=FORCE, warmup=WARMUP))

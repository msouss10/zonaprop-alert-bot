#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio, os, json, time
from pathlib import Path
from typing import List, Dict
import requests
from playwright.async_api import async_playwright

# ================== CONFIG ==================
DEF_CFG = {
    # sin filtros de fecha ni "modo_blando"
    "top_n_per_search": 20,          # cuántos links por búsqueda (máximo)
    "per_link_delay_sec": 1.0,       # pausa entre envíos a Telegram
    "searches": [],                  # lista de URLs de búsqueda de Zonaprop
}
def load_cfg() -> Dict:
    cfg = DEF_CFG.copy()
    # config.yaml (opcional): 
    # telegram: { bot_token: "...", chat_id: "..." }
    # top_n_per_search: 20
    # per_link_delay_sec: 1.0
    # searches: ["https://www.zonaprop.com.ar/....html", ...]
    if Path("config.yaml").exists():
        # sin dependencias externas tipo yaml para simplificar: soporte JSON mínimo
        # si preferís YAML, reinstalamos pyyaml y lo volvemos a activar
        try:
            import yaml  # type: ignore
            with open("config.yaml","r",encoding="utf-8") as f:
                file_cfg = yaml.safe_load(f) or {}
        except Exception:
            try:
                # fallback: si el archivo es JSON válido
                import json as _json
                file_cfg = _json.loads(Path("config.yaml").read_text(encoding="utf-8"))
            except Exception:
                file_cfg = {}
        if isinstance(file_cfg, dict):
            for k, v in file_cfg.items():
                if k in DEF_CFG or k == "telegram":
                    cfg[k] = v
    return cfg

CFG = load_cfg()

# BOT_TOKEN/CHAT_ID: primero busca en env; si no, en config.yaml
BOT_TOKEN = os.getenv("BOT_TOKEN") or (CFG.get("telegram",{}) or {}).get("bot_token") or ""
CHAT_ID   = os.getenv("CHAT_ID")   or (CFG.get("telegram",{}) or {}).get("chat_id")   or ""

CACHE_PATH = Path("cache.json")

# ================== UTILIDADES ==================
def load_cache() -> Dict[str, float]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_cache(cache: Dict[str, float]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

# ================== SCRAPER ==================
SEARCH_A_SELECTOR = 'a[href*="/propiedades/"]'

async def extract_search_links(page, url: str, limit: int) -> List[str]:
    print(f"[search] Búsqueda: {url}")
    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"font","media"} else route.continue_())
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    # pequeño scroll para forzar carga de tarjetas
    for _ in range(5):
        await page.mouse.wheel(0, 10000)
        await page.wait_for_timeout(400)

    anchors = await page.query_selector_all(SEARCH_A_SELECTOR)
    print(f"[search] anchors /propiedades/ encontrados: {len(anchors)}")

    hrefs = []
    seen = set()
    for a in anchors:
        href = await a.get_attribute("href")
        if not href:
            continue
        # normalizar enlaces absolutos
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://www.zonaprop.com.ar" + href
        if "/propiedades/" in href and href not in seen:
            seen.add(href)
            hrefs.append(href)
        if len(hrefs) >= limit:
            break

    print(f"[search] tarjetas detectadas: {len(hrefs)}")
    return hrefs

async def extract_og_meta(page, url: str) -> Dict[str, str]:
    # Abrimos la ficha y tomamos og:title / og:description / og:image (más robusto)
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(800)
    get = lambda prop: page.locator(f'meta[property="{prop}"]').get_attribute("content")
    title = await get("og:title")
    desc  = await get("og:description")
    img   = await get("og:image")
    # fallbacks rápidos
    if not title:
        h1 = await page.locator("h1").first.text_content()
        title = (h1 or "").strip()
    if not img:
        img = await page.locator("img").first.get_attribute("src")
    return {
        "title": (title or "").strip(),
        "desc":  (desc or "").strip(),
        "img":   (img or "").strip(),
        "url":   url,
    }

# ================== TELEGRAM ==================
def tg_send_photo(token: str, chat_id: str, photo_url: str, caption: str) -> bool:
    if not token or not chat_id:
        print("[tg] Faltan credenciales BOT_TOKEN/CHAT_ID")
        return False
    endpoint = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption[:1024],   # límite Telegram
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(endpoint, data=data, timeout=30)
        ok = r.ok and (r.json().get("ok") is True)
        if not ok:
            print(f"[tg] sendPhoto fallo: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"[tg] Error sendPhoto: {e}")
        return False

def tg_send_message(token: str, chat_id: str, text: str) -> bool:
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(endpoint, data=data, timeout=30)
        ok = r.ok and (r.json().get("ok") is True)
        if not ok:
            print(f"[tg] sendMessage fallo: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"[tg] Error sendMessage: {e}")
        return False

def build_caption(meta: Dict[str, str]) -> str:
    parts = []
    if meta.get("title"):
        parts.append(f"<b>{meta['title']}</b>")
    if meta.get("desc"):
        parts.append(meta["desc"])
    parts.append(meta["url"])
    return "\n".join(parts).strip()

# ================== MAIN ==================
async def run():
    cfg = CFG
    cache = load_cache()
    print(f"[cache] URLs ya enviadas: {len(cache)}")

    searches: List[str] = cfg.get("searches", []) or []
    if not searches:
        print("[cfg] No hay URLs en 'searches'. Agregá búsquedas en config.yaml")
        return

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
        )
        page = await context.new_page()

        nuevos_enviados = 0

        for url in searches:
            links = await extract_search_links(page, url, cfg.get("top_n_per_search", 20))
            candidatos = [u for u in links if u not in cache]
            print(f"[search] candidatos no vistos: {len(candidatos)}")

            for link in candidatos:
                try:
                    meta = await extract_og_meta(page, link)
                    caption = build_caption(meta)

                    ok = False
                    if meta.get("img"):
                        ok = tg_send_photo(BOT_TOKEN, CHAT_ID, meta["img"], caption)
                    if not ok:
                        ok = tg_send_message(BOT_TOKEN, CHAT_ID, caption)

                    if ok:
                        cache[link] = time.time()
                        nuevos_enviados += 1
                        print(f"[send] enviado ✅  {link}")
                    else:
                        print(f"[send] fallo    ❌  {link}")

                    await page.wait_for_timeout(int(cfg.get("per_link_delay_sec", 1.0) * 1000))
                except Exception as e:
                    print(f"[err] {link}: {e}")

        await browser.close()

    save_cache(cache)
    print(f"[fin] Nuevos enviados: {nuevos_enviados}")

if __name__ == "__main__":
    asyncio.run(run())

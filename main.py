#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio, os, json, time
from pathlib import Path
from typing import List, Dict, Any
import requests
from playwright.async_api import async_playwright

# ================== CONFIG ==================
DEF_CFG = {
    "top_n_per_search": 20,
    "per_link_delay_sec": 1.0,
    "searches": [],
}
def load_cfg() -> Dict[str, Any]:
    cfg = DEF_CFG.copy()
    if Path("config.yaml").exists():
        try:
            import yaml  # type: ignore
            with open("config.yaml","r",encoding="utf-8") as f:
                file_cfg = yaml.safe_load(f) or {}
        except Exception:
            try:
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

def normalize_search(entry: Any) -> Dict[str, str]:
    """Acepta string o dict {name, url} y devuelve {'name': str, 'url': str}."""
    if isinstance(entry, dict):
        return {"name": str(entry.get("name", "")).strip(), "url": str(entry.get("url", "")).strip()}
    return {"name": "", "url": str(entry).strip()}

# ================== SCRAPER ==================
SEARCH_A_SELECTOR = 'a[href*="/propiedades/"]'

async def extract_search_links(page, url: str, limit: int) -> List[str]:
    print(f"[search] Búsqueda: {url}")
    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"font","media"} else route.continue_())
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)

    # scroll para cargar tarjetas
    for _ in range(5):
        await page.mouse.wheel(0, 10000)
        await page.wait_for_timeout(400)

    anchors = await page.query_selector_all(SEARCH_A_SELECTOR)
    print(f"[search] anchors /propiedades/ encontrados: {len(anchors)}")

    hrefs, seen = [], set()
    for a in anchors:
        href = await a.get_attribute("href")
        if not href:
            continue
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
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(800)
    get = lambda prop: page.locator(f'meta[property="{prop}"]').get_attribute("content")
    title = await get("og:title")
    desc  = await get("og:description")
    img   = await get("og:image")
    if not title:
        h1 = await page.locator("h1").first.text_content()
        title = (h1 or "").strip()
    if not img:
        img = await page.locator("img").first.get_attribute("src")
    return {"title": (title or "").strip(), "desc": (desc or "").strip(), "img": (img or "").strip(), "url": url}

# ================== TELEGRAM ==================
def tg_send_photo(token: str, chat_id: str, photo_url: str, caption: str) -> bool:
    if not token or not chat_id:
        print("[tg] Faltan credenciales BOT_TOKEN/CHAT_ID")
        return False
    endpoint = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": chat_id, "photo": photo_url, "caption": caption[:1024], "parse_mode": "HTML", "disable_web_page_preview": True}
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
    data = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": False}
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
    if meta.get("title"): parts.append(f"<b>{meta['title']}</b>")
    if meta.get("desc"):  parts.append(meta["desc"])
    parts.append(meta["url"])
    return "\n".join(parts).strip()

# ================== MAIN ==================
async def run():
    cfg = CFG
    cache = load_cache()
    print(f"[cache] URLs ya enviadas: {len(cache)}")

    raw_searches = cfg.get("searches", []) or []
    if not raw_searches:
        print("[cfg] No hay URLs en 'searches'. Agregá búsquedas en config.yaml")
        return

    searches = [normalize_search(e) for e in raw_searches]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
        context = await browser.new_context(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36")
        page = await context.new_page()

        nuevos_enviados = 0

        for s in searches:
            name, url = s.get("name",""), s.get("url","")
            if not url:
                print("[cfg] entrada de búsqueda sin 'url', se salta.")
                continue
            if name:
                print(f"[search] Fuente: {name}")
            links = await extract_search_links(page, url, int(cfg.get("top_n_per_search", 20)))
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
                    await page.wait_for_timeout(int(float(cfg.get("per_link_delay_sec", 1.0)) * 1000))
                except Exception as e:
                    print(f"[err] {link}: {e}")

        await browser.close()

    save_cache(cache)
    print(f"[fin] Nuevos enviados: {nuevos_enviados}")

if __name__ == "__main__":
    asyncio.run(run())


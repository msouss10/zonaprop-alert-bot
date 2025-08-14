#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio, os, json, re, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ================== CONFIG ==================
DEF_CFG = {
    "max_age_hours": 24,        # ventana "hoy"
    "top_n_per_search": 30,     # por si salen muchas en el día
    "per_link_delay_sec": 1.0,  # pausa entre envíos
    "searches": [],             # strings o {name,url}
}
def load_cfg() -> Dict[str, Any]:
    cfg = DEF_CFG.copy()
    if Path("config.yaml").exists():
        try:
            import yaml  # type: ignore
            with open("config.yaml","r",encoding="utf-8") as f:
                file_cfg = yaml.safe_load(f) or {}
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

CACHE_PATH = Path("cache.json")  # {url: timestamp_envio}

# ================== CACHE ==================
def load_cache() -> Dict[str, float]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}
def save_cache(cache: Dict[str, float]) -> None:
    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

# ================== HELPERS ==================
def normalize_search(entry: Any) -> Dict[str, str]:
    if isinstance(entry, dict):
        return {"name": str(entry.get("name", "")).strip(), "url": str(entry.get("url", "")).strip()}
    return {"name": "", "url": str(entry).strip()}

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def parse_any_date(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s: return None
    try:
        dt = datetime.fromisoformat(s.replace("Z","+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    m = re.search(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}:\d{2}))?", s)
    if m:
        d = m.group(1); t = m.group(2) or "00:00:00"
        try: return datetime.fromisoformat(f"{d}T{t}+00:00")
        except Exception: pass
    return None

def within_hours(dt: Optional[datetime], hours: int) -> bool:
    return bool(dt) and (now_utc() - dt) <= timedelta(hours=hours)

# ================== TELEGRAM ==================
def tg_send_photo(token: str, chat_id: str, photo_url: str, caption: str) -> bool:
    if not token or not chat_id:
        print("[tg] Faltan credenciales BOT_TOKEN/CHAT_ID")
        return False
    endpoint = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": chat_id, "photo": photo_url, "caption": caption[:1024], "parse_mode": "HTML"}
    try:
        r = requests.post(endpoint, data=data, timeout=30)
        ok = r.ok and (r.json().get("ok") is True)
        if not ok: print(f"[tg] sendPhoto fallo: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"[tg] Error sendPhoto: {e}"); return False

def tg_send_message(token: str, chat_id: str, text: str) -> bool:
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML", "disable_web_page_preview": False}
    try:
        r = requests.post(endpoint, data=data, timeout=30)
        ok = r.ok and (r.json().get("ok") is True)
        if not ok: print(f"[tg] sendMessage fallo: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"[tg] Error sendMessage: {e}"); return False

def build_caption(meta: Dict[str, str]) -> str:
    parts = []
    if meta.get("title"): parts.append(f"<b>{meta['title']}</b>")
    if meta.get("desc"):  parts.append(meta["desc"])
    parts.append(meta["url"])
    return "\n".join(parts).strip()

# ================== PLAYWRIGHT UTILS ==================
async def dismiss_popups(page):
    sels = [
        'button:has-text("Aceptar")','button:has-text("Acepto")','button:has-text("Entendido")',
        'button:has-text("Aceptar todas")','div[class*="cookie"] button','div[id*="cookie"] button',
    ]
    for sel in sels:
        try:
            loc = page.locator(sel)
            if await loc.count():
                await loc.first.click(timeout=600)
                await page.wait_for_timeout(200)
        except Exception:
            pass

async def deep_scroll(page, max_loops=24, wait_ms=700):
    last_h = 0; same = 0
    for _ in range(max_loops):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(wait_ms)
        h = await page.evaluate("document.body.scrollHeight")
        same = same + 1 if h == last_h else 0
        last_h = h
        if same >= 2: break

async def collect_links(page) -> List[str]:
    # Incluye iframes por las dudas
    hrefs: List[str] = []; seen = set()
    frames = page.frames
    for fr in frames:
        try:
            a_hrefs: List[str] = await fr.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]')).map(a => a.href || a.getAttribute('href') || '')
            """)
        except Exception:
            a_hrefs = []
        for href in a_hrefs:
            if not href: continue
            if href.startswith("//"): href = "https:" + href
            if href.startswith("/"):  href = "https://www.zonaprop.com.ar" + href
            if "/propiedades/" in href and href not in seen:
                seen.add(href); hrefs.append(href)
    return hrefs

# ================== SCRAPE ==================
async def extract_search_links(page, url: str, limit: int) -> List[str]:
    print(f"[search] Búsqueda: {url}")
    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in {"font","media"} else route.continue_())
    await page.goto(url, wait_until="networkidle", timeout=90000)
    await dismiss_popups(page)
    await deep_scroll(page)

    anchors = await page.query_selector_all('a[href*="/propiedades/"]')
    print(f"[search] anchors /propiedades/ encontrados: {len(anchors)}")

    hrefs, seen = [], set()
    for a in anchors:
        href = await a.get_attribute("href")
        if not href: continue
        if href.startswith("//"): href = "https:" + href
        elif href.startswith("/"): href = "https://www.zonaprop.com.ar" + href
        if "/propiedades/" in href and href not in seen:
            seen.add(href); hrefs.append(href)

    if not hrefs:
        hrefs = await collect_links(page)

    print(f"[search] tarjetas detectadas: {len(hrefs)}")
    return hrefs[:limit]

async def extract_text_safe(page, js: str) -> str:
    try:
        return await page.evaluate(js) or ""
    except Exception:
        return ""

async def extract_meta_and_date(page, url: str, max_age_h: int) -> Optional[Dict[str, str]]:
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)
    await page.wait_for_timeout(400)

    # Sin esperar locators: leemos directo del DOM (evita timeouts)
    title = await extract_text_safe(page, '() => (document.querySelector(\'meta[property="og:title"]\')?.content) || document.title || ""')
    desc  = await extract_text_safe(page, '() => (document.querySelector(\'meta[property="og:description"]\')?.content) || ""')
    img   = await extract_text_safe(page, '() => (document.querySelector(\'meta[property="og:image"]\')?.content) || document.querySelector("img")?.src || ""')

    # Varias fuentes de fecha
    candidates = []
    candidates.append(await extract_text_safe(page, '() => document.querySelector(\'meta[property="article:published_time"]\')?.content || ""'))
    candidates.append(await extract_text_safe(page, '() => document.querySelector(\'[itemprop="datePublished"]\')?.content || ""'))

    # JSON-LD
    ld = await extract_text_safe(page, '() => Array.from(document.querySelectorAll(\'script[type="application/ld+json"]\')).map(s=>s.textContent||"").join("\\n")')
    for k in ("datePublished","dateCreated","dateModified"):
        m = re.search(rf'"{k}"\s*:\s*"([^"]+)"', ld or "")
        if m: candidates.append(m.group(1))

    # Texto natural "Publicado hace X ..."
    body_txt = await extract_text_safe(page, '() => document.body.innerText || ""')
    if re.search(r"Publicado hace\s+\d+\s+(minutos|hora|horas)", body_txt, re.I):
        candidates.append(datetime.now(timezone.utc).isoformat())

    pub_dt = None
    for c in candidates:
        pub_dt = parse_any_date(c)
        if pub_dt: break

    if pub_dt and not within_hours(pub_dt, max_age_h):
        print(f"[fecha] salta por antigüedad: {url} -> {pub_dt.isoformat()}"); return None

    return {"title": (title or "").strip(), "desc": (desc or "").strip(), "img": (img or "").strip(), "url": url}

# ================== MAIN ==================
async def run():
    cfg = CFG
    cache = load_cache()
    print(f"[cache] URLs ya enviadas: {len(cache)}")

    raw_searches = cfg.get("searches", []) or []
    if not raw_searches:
        print("[cfg] No hay URLs en 'searches'."); return
    searches = [normalize_search(e) for e in raw_searches]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage","--no-sandbox"])
        context = await browser.new_context(
            locale="es-AR",
            timezone_id="America/Argentina/Buenos_Aires",
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
            viewport={"width":1280,"height":1800}
        )
        page = await context.new_page()

        nuevos_enviados = 0
        for s in searches:
            name, url = s.get("name",""), s.get("url","")
            if not url: continue
            if name: print(f"[search] Fuente: {name}")
            links = await extract_search_links(page, url, int(cfg.get("top_n_per_search", 30)))
            links = [u for u in links if u not in cache]  # sin repetir
            print(f"[search] candidatos no vistos: {len(links)}")

            for link in links:
                try:
                    meta = await extract_meta_and_date(page, link, int(cfg.get("max_age_hours", 24)))
                    if not meta:
                        continue
                    caption = build_caption(meta)
                    ok = False
                    if meta.get("img"):
                        ok = tg_send_photo(BOT_TOKEN, CHAT_ID, meta["img"], caption)
                    if not ok:
                        ok = tg_send_message(BOT_TOKEN, CHAT_ID, caption)
                    if ok:
                        cache[link] = time.time()
                        nuevos_enviados += 1
                        print(f"[send] enviado ✅ {link}")
                    else:
                        print(f"[send] fallo   ❌ {link}")
                    await page.wait_for_timeout(int(float(cfg.get("per_link_delay_sec", 1.0)) * 1000))
                except Exception as e:
                    print(f"[err] {link}: {e}")

        await browser.close()

    save_cache(cache)
    print(f"[fin] Nuevos enviados: {nuevos_enviados}")

if __name__ == "__main__":
    asyncio.run(run())

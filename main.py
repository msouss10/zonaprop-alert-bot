#!/usr/bin/env python3
import asyncio, os, json, re, sys, time
from pathlib import Path
from datetime import datetime, timezone
import requests, yaml
from playwright.async_api import async_playwright

# ================== CARGA DE CONFIG ==================
DEF_CFG = {
    "max_age_hours": 24,           # Aún se usa para fechas ISO reales (JSON-LD/meta); visible-text fuerza "solo hoy"
    "top_n_per_search": 10,
    "per_link_delay_sec": 1.0,
    "searches": [],
}
def load_cfg():
    cfg = DEF_CFG.copy()
    if Path("config.yaml").exists():
        with open("config.yaml","r",encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
        cfg.update({k:v for k,v in file_cfg.items() if k in DEF_CFG or k=="telegram"})
    return cfg

CFG = load_cfg()

# BOT_TOKEN/CHAT_ID: en GitHub se leen de Secrets; localmente podés ponerlos en config.yaml (telegram.*)
BOT_TOKEN = os.getenv("BOT_TOKEN") or (CFG.get("telegram",{}) or {}).get("bot_token") or ""
CHAT_ID   = os.getenv("CHAT_ID")   or str((CFG.get("telegram",{}) or {}).get("chat_id") or "")

MAX_AGE_HOURS       = int(os.getenv("MAX_AGE_HOURS", str(CFG["max_age_hours"])))
TOP_N_PER_SEARCH    = int(os.getenv("TOP_N_PER_SEARCH", str(CFG["top_n_per_search"])))
PER_LINK_DELAY_SEC  = float(os.getenv("PER_LINK_DELAY_SEC", str(CFG["per_link_delay_sec"])))
SEARCHES            = CFG["searches"]

# ================== ESTADO ==================
DATA_DIR  = Path(".data"); DATA_DIR.mkdir(exist_ok=True, parents=True)
SEEN_FILE = DATA_DIR / "seen.json"

def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except:
            return set()
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))

# ================== TELEGRAM ==================
def send_text(text: str, preview: bool = False):
    if not BOT_TOKEN or not CHAT_ID:
        print("[Telegram] Falta BOT_TOKEN o CHAT_ID")
        return
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": (not preview)}
    try:
        r = requests.post(api, data=payload, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[Telegram] {e}")

def send_link(url: str):
    # Enviar solo el link para que Telegram genere título+descripción+foto
    send_text(url, preview=True)

# ================== EXTRACCIÓN ==================
# URL de aviso válida
AD_PATTERN = re.compile(
    r"^(?:https?://www\.zonaprop\.com\.ar)?/propiedades/(?:clasificado/)?[A-Za-z0-9\-]+-\d+\.html$",
    re.IGNORECASE,
)

# Heurísticas SOLO HOY en texto visible
RE_MIN  = re.compile(r"hace\s+(\d+)\s*min(uto|utos)?", re.I)
RE_HRS  = re.compile(r"hace\s+(\d+)\s*hora(s)?", re.I)
RE_HOY  = re.compile(r"\bpublicado\s+hoy\b|\bhoy\b", re.I)  # cubre "Publicado hoy"

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
        return (now - dt.astimezone(timezone.utc)).total_seconds() / 3600.0
    except Exception:
        return None

def text_hours_today_only(txt: str) -> float | None:
    """
    Devuelve horas transcurridas SOLO si es de hoy:
      - "Publicado hoy" -> 0.0
      - "hace X minutos" -> X/60
      - "hace X horas"   -> X
    Descarta todo lo demás: "ayer", "hace X días", fechas completas, etc.
    """
    t = (txt or "").lower()
    if RE_HOY.search(t):
        return 0.0
    m = RE_MIN.search(t)
    if m: 
        return int(m.group(1)) / 60.0
    m = RE_HRS.search(t)
    if m: 
        return float(m.group(1))
    return None  # nada de ayer/días

async def scrape_list(list_page, url: str) -> list[str]:
    await list_page.goto(url, timeout=120_000, wait_until="domcontentloaded")
    try:
        await list_page.wait_for_load_state("networkidle", timeout=60_000)
    except:
        pass
    # scroll para cargar resultados
    for _ in range(12):
        await list_page.mouse.wheel(0, 6000)
        await list_page.wait_for_timeout(800)

    hrefs = await list_page.eval_on_selector_all(
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
    Devuelve cuántas horas pasaron desde la publicación real del aviso.
    Orden:
      1) JSON-LD: datePublished / uploadDate / dateModified
      2) Meta: article:published_time / itemprop=datePublished
      3) Texto visible SOLO HOY: 'Publicado hoy' / 'hace X horas/minutos'
         (descarta 'ayer' o 'hace X días' devolviendo None)
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

        # 2) Meta
        meta_time = await detail_page.eval_on_selector(
            "meta[property='article:published_time'], meta[itemprop='datePublished']",
            "el => el ? el.getAttribute('content') : null"
        )
        if meta_time:
            hrs = parse_iso_to_utc_hours_ago(meta_time)
            if hrs is not None:
                return hrs

        # 3) Texto visible SOLO HOY
        whole = await detail_page.eval_on_selector(
            "body", "el => (el.textContent || '').replace(/\\s+/g,' ').trim()"
        )
        hrs = text_hours_today_only(whole or "")
        return hrs  # None si no es de hoy
    except Exception as e:
        print(f"[age] {e}")
        return None

# ================== RUN ==================
async def run(force: bool = False, warmup: bool = False):
    if not SEARCHES:
        print("No hay 'searches' en config.yaml")
        return

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

        for s in SEARCHES:
            name, url = s["name"], s["url"]
            print(f"Buscando {name} ...")
            try:
                links = await scrape_list(list_page, url)
            except Exception as e:
                print(f"[WARN] {name}: {e}")
                continue

            if warmup:
                nuevos = [u for u in links if u not in seen]
                seen.update(links)
                total_new_detected += len(nuevos)
                print(f"  (warmup) vistos: {len(links)} | nuevos marcados: {len(nuevos)}")
                continue

            # candidatos (si no es force, excluimos ya enviados)
            candidates = links if force else [u for u in links if u not in seen]

            # chequeamos hasta cubrir el tope (miramos algunos más por si varios quedan afuera)
            to_check = candidates[: max(TOP_N_PER_SEARCH * 3, TOP_N_PER_SEARCH + 5)]
            enviados = 0
            header_sent = False

            for u in to_check:
                age = await get_age_hours(detail_page, u)
                if age is None:
                    # No es de hoy o no pudimos inferir -> descartar
                    continue
                if age <= MAX_AGE_HOURS:
                    if not header_sent:
                        send_text(f"{name}:")  # encabezado
                        header_sent = True
                    send_link(u)
                    enviados += 1
                    seen.add(u)
                    time.sleep(PER_LINK_DELAY_SEC)
                    if enviados >= TOP_N_PER_SEARCH:
                        break

            if not force:
                # métrica: cuántos candidatos NUEVOS vimos esta vez (antes de filtros)
                total_new_detected += len([u for u in candidates if u not in seen])

        await browser.close()

    save_seen(seen)
    if warmup:
        print("Warm-up listo. Historial guardado en .data/seen.json")
    else:
        print(f"Listo. Nuevos detectados (previo a filtro/tope): {total_new_detected}")

if __name__ == "__main__":
    FORCE  = "--force"  in sys.argv
    WARMUP = "--warmup" in sys.argv
    asyncio.run(run(force=FORCE, warmup=WARMUP))

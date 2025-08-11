#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import os
import json
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import yaml
import requests
from playwright.async_api import async_playwright

# ================== CONFIG ==================
DEF_CFG = {
    "max_age_hours": 24,
    "top_n_per_search": 12,
    "per_link_delay_sec": 0.8,
    "sleep_between_msgs_sec": 1.2,   # pausa entre mensajes a Telegram
    "searches": [],
}
CFG_PATH = "config.yaml"
CACHE_PATH = "seen_urls.json"

def load_cfg():
    cfg = DEF_CFG.copy()
    if Path(CFG_PATH).exists():
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
        for k, v in file_cfg.items():
            if k in DEF_CFG:
                cfg[k] = v
    return cfg

CFG = load_cfg()

# ================== TELEGRAM ==================
def send_telegram(text, bot_token=None, chat_id=None, disable_preview=False):
    bot_token = bot_token or os.getenv("BOT_TOKEN")
    chat_id   = chat_id   or os.getenv("CHAT_ID")
    if not bot_token or not chat_id:
        print("[telegram] FALTA BOT_TOKEN o CHAT_ID")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "true" if disable_preview else "false",
            },
            timeout=25,
        )
        ok = r.status_code == 200 and '"ok":true' in r.text
        print(f"[telegram] status={r.status_code} ok={ok} resp={r.text[:160]}")
        return ok
    except Exception as e:
        print(f"[telegram] EXCEPTION: {e}")
        return False

# ================== CACHE ==================
def load_seen():
    if not Path(CACHE_PATH).exists():
        return set()
    try:
        data = json.loads(Path(CACHE_PATH).read_text(encoding="utf-8"))
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()

def save_seen(seen: set):
    try:
        Path(CACHE_PATH).write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[cache] No se pudo guardar cache: {e}")

# ================== FILTRO (modo blando) ==================
def _is_hoy_o_horas(texto: str) -> bool:
    t = (texto or "").lower()
    return any(x in t for x in ["hoy", "min", "minutos", "hora", "horas"])

def _parse_hours_from_label(label: str):
    if not label:
        return None
    t = label.lower()
    m = re.search(r"hace\s+(\d+)\s*min", t)
    if m:
        return max(0.0, float(m.group(1)) / 60.0)
    m = re.search(r"hace\s+(\d+)\s*hora", t)
    if m:
        return float(m.group(1))
    return None

def pasa_filtro_fecha(item, max_age_hours=24):
    age = item.get("age_hours")
    label = " ".join([
        str(item.get("age_label") or ""),
        str(item.get("card_text") or ""),
    ]).strip()

    if isinstance(age, (int, float)):
        return age <= max_age_hours

    approx = _parse_hours_from_label(label)
    if isinstance(approx, (int, float)):
        return approx <= max_age_hours

    if _is_hoy_o_horas(label):
        return True

    # sin señales: no descartamos
    return True

# ================== PARSERS ==================
async def extract_cards_from_search(page, top_n=12):
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(800)

    anchors = await page.query_selector_all("a[href*='/propiedades/']")
    if not anchors:
        anchors = await page.query_selector_all("a[href]")

    seen_urls = set()
    cards = []

    for a in anchors:
        href = (await a.get_attribute("href")) or ""
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            base = page.url.split("/", 3)
            href = base[0] + "//" + base[2] + href
        if "/propiedades/" not in href:
            continue

        title = (await a.text_content()) or ""
        title = re.sub(r"\s+", " ", title).strip()

        if href in seen_urls:
            continue
        seen_urls.add(href)

        cards.append({"url": href, "titulo": title or "Aviso", "card_text": title})
        if len(cards) >= top_n:
            break

    print(f"[search] anchors /propiedades/ encontrados: {len(cards)}")
    return cards

async def enrich_detail_try(page, url, per_link_delay_sec=0.8):
    out = {"url": url, "age_hours": None, "age_label": None, "titulo": None}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(int(per_link_delay_sec * 1000))

        h1 = await page.query_selector("h1")
        if h1:
            t = (await h1.text_content()) or ""
            out["titulo"] = re.sub(r"\s+", " ", t).strip()

        sel_candidates = [
            "meta[itemprop='datePublished']",
            "time[itemprop='datePublished']",
            "meta[property='og:updated_time']",
            "[data-testid*='published']",
            "span:has-text('Publicado')",
            "div:has-text('Publicado hoy')",
            "div:has-text('hace')",
        ]

        label_texts = []
        for sel in sel_candidates:
            els = await page.query_selector_all(sel)
            for el in els:
                tag = (await el.evaluate("(e)=>e.tagName")).lower()
                content = (await el.get_attribute("content")) or (await el.text_content()) or ""
                content = re.sub(r"\s+", " ", content).strip()
                if content:
                    label_texts.append(content)

        label_text = " | ".join(dict.fromkeys(label_texts)) if label_texts else None
        out["age_label"] = label_text

        iso = None
        if label_text:
            m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}[:\d{0,2}]*)", label_text)
            if m:
                iso = m.group(1)
        if iso:
            try:
                dt = datetime.fromisoformat(iso.replace("Z","+00:00"))
                now = datetime.now(timezone.utc)
                out["age_hours"] = max(0.0, (now - dt).total_seconds() / 3600.0)
            except:
                pass
        else:
            approx = _parse_hours_from_label(label_text or "")
            if isinstance(approx, (int, float)):
                out["age_hours"] = approx

    except Exception as e:
        print(f"[detail] ERROR {url}: {e}")
    return out

# ================== PIPELINE ==================
async def run_once():
    searches = CFG.get("searches") or []
    if not searches:
        print("[cfg] No hay búsquedas definidas.")
        return

    seen = load_seen()
    print(f"[cache] URLs ya enviadas: {len(seen)}")

    sleep_between = float(CFG.get("sleep_between_msgs_sec", 1.2))
    nuevos_totales = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        for s in searches:
            label = (s.get("label") or "Búsqueda").strip()
            url   = s.get("url") or ""
            if not url:
                continue

            print(f"\n[search] {label}: {url}")
            try:
                await page.goto(url, wait_until="networkidle", timeout=45000)

                # aceptar cookies
                for sel in [
                    "button:has-text('Aceptar')",
                    "button:has-text('Aceptar y cerrar')",
                    "button:has-text('Acepto')",
                    "[aria-label*='Aceptar']",
                ]:
                    try:
                        btn = await page.query_selector(sel)
                        if btn:
                            await btn.click()
                            await page.wait_for_timeout(500)
                            break
                    except:
                        pass

            except Exception as e:
                print(f"[search] No se pudo abrir listado: {e}")
                continue

            cards = await extract_cards_from_search(page, top_n=int(CFG.get("top_n_per_search", 12)))
            print(f"[search] tarjetas detectadas: {len(cards)}")

            candidatos = [c for c in cards if c["url"] not in seen]
            print(f"[search] candidatos no vistos: {len(candidatos)}")

            enriquecidos = []
            for c in candidatos:
                d = await enrich_detail_try(page, c["url"], per_link_delay_sec=CFG.get("per_link_delay_sec", 0.8))
                d["card_text"] = c.get("card_text")
                if not d.get("titulo"):
                    d["titulo"] = c.get("titulo")
                enriquecidos.append(d)

            filtrados = [it for it in enriquecidos if pasa_filtro_fecha(it, CFG.get("max_age_hours", 24))]
            print(f"[filtro] candidatos={len(enriquecidos)} -> tras modo_blando={len(filtrados)}")
            print(f"[fecha] sin fecha detectable: {len([it for it in enriquecidos if it.get('age_hours') is None])}")

            # Enviar UNO POR UNO para que Telegram muestre la foto de preview
            for it in filtrados:
                titulo = (it.get("titulo") or it.get("card_text") or "").strip()
                msg = f"🆕 {label}\n{titulo}\n{it['url']}".strip()
                print("[send] Enviando item…")
                ok = send_telegram(msg, disable_preview=False)
                if ok:
                    seen.add(it["url"])
                    nuevos_totales.append(it)
                time.sleep(sleep_between)

        await context.close()
        await browser.close()

    save_seen(seen)
    print(f"\n[resumen] Nuevos totales enviados: {len(nuevos_totales)}")

def main():
    try:
        asyncio.run(run_once())
    except KeyboardInterrupt:
        print("Cancelado por usuario.")
    except Exception as e:
        print(f"[fatal] {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()

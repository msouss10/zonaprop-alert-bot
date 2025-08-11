#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Bot Zonaprop - Envío de nuevos avisos a Telegram
- Carga búsquedas desde config.yaml
- Extrae tarjetas, intenta leer fecha (modo blando)
- Evita duplicados con cache local
- Envía resumen por Telegram

Requisitos:
- pip install pyyaml requests playwright
- playwright install chromium
- En GitHub Actions: export BOT_TOKEN y CHAT_ID como secrets

Autor: Bot Zonaprop
"""

import asyncio
import os
import json
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import yaml
import requests
from playwright.async_api import async_playwright

# ================== CONFIG ==================
DEF_CFG = {
    "max_age_hours": 24,            # ventana de antigüedad objetivo
    "top_n_per_search": 12,         # máximo de resultados a considerar por búsqueda
    "per_link_delay_sec": 0.8,      # espera entre visitas a fichas
    "searches": [],                 # [{label: "...", url: "..."}]
}
CFG_PATH = "config.yaml"
CACHE_PATH = "seen_urls.json"

def load_cfg():
    cfg = DEF_CFG.copy()
    if Path(CFG_PATH).exists():
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
        # merge superficial
        for k, v in file_cfg.items():
            if k in DEF_CFG:
                cfg[k] = v
    return cfg

CFG = load_cfg()

# ================== TELEGRAM ==================
def send_telegram(text, bot_token=None, chat_id=None):
    bot_token = bot_token or os.getenv("BOT_TOKEN")
    chat_id   = chat_id   or os.getenv("CHAT_ID")
    if not bot_token or not chat_id:
        print("[telegram] FALTA BOT_TOKEN o CHAT_ID")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=25,
        )
        ok = r.status_code == 200 and '"ok":true' in r.text
        print(f"[telegram] status={r.status_code} ok={ok} resp={r.text[:180]}")
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

# ================== FILTRO “MODO BLANDO” ==================
def _is_hoy_o_horas(texto: str) -> bool:
    t = (texto or "").lower()
    # coberturas rápidas típicas
    # ej: "Publicado hoy", "hace 2 horas", "hace 45 min"
    return any(x in t for x in ["hoy", "min", "minutos", "hora", "horas"])

def _parse_hours_from_label(label: str):
    """
    Intenta extraer horas aproximadas desde textos tipo 'hace 3 horas', 'hace 25 min'
    Devuelve float horas o None
    """
    if not label:
        return None
    t = label.lower()
    m = re.search(r"hace\s+(\d+)\s*min", t)
    if m:
        try:
            return max(0.0, float(m.group(1)) / 60.0)
        except:
            pass
    m = re.search(r"hace\s+(\d+)\s*hora", t)
    if m:
        try:
            return float(m.group(1))
        except:
            pass
    return None

def pasa_filtro_fecha(item, max_age_hours=24):
    """
    MODO BLANDO:
    - Si age_hours está calculado -> exige <= max_age_hours
    - Si NO hay age_hours -> deja pasar si el label sugiere 'hoy/horas/min'
    - Si tampoco hay pistas -> deja pasar (no descarta por falta de fecha)
    """
    age = item.get("age_hours")
    label = " ".join([
        str(item.get("age_label") or ""),
        str(item.get("card_text") or ""),
        str(item.get("snippet") or "")
    ]).strip()

    if isinstance(age, (int, float)):
        return age <= max_age_hours

    # Intento heurístico directo desde el label
    approx = _parse_hours_from_label(label)
    if isinstance(approx, (int, float)):
        return approx <= max_age_hours

    if _is_hoy_o_horas(label):
        return True

    # Sin señales: no descartamos
    return True

# ================== PARSERS ==================
async def extract_cards_from_search(page, top_n=12):
    """
    Extrae tarjetas de un listado. Es genérico; busca anchors con href válidos.
    Adaptado a sitios tipo clasificados (ej. Zonaprop).
    Devuelve lista de {url, titulo, card_text}
    """
    cards = []
    # Espera a que cargue algo (ajustable)
    await page.wait_for_timeout(1200)
    # Tomamos enlaces que parezcan de ficha
    anchors = await page.query_selector_all("a[href]")
    seen_urls = set()
    for a in anchors:
        href = (await a.get_attribute("href")) or ""
        if not href:
            continue
        # Normalizar absolutos
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            base = page.url.split("/", 3)
            href = base[0] + "//" + base[2] + href

        # Heurística simple: evitar paginación/anchors y duplicados
        if any(x in href for x in ["#","javascript:", "whatsapp.com", "facebook.com", "twitter.com"]):
            continue
        # Pista de "ficha"; en Zonaprop suelen tener "/propiedades/" o similar
        if not re.search(r"/propiedad|/propiedades|inmueble|/p/\d", href, re.IGNORECASE):
            continue

        title = (await a.text_content()) or ""
        title = re.sub(r"\s+", " ", title).strip()
        if href not in seen_urls:
            cards.append({"url": href, "titulo": title, "card_text": title})
            seen_urls.add(href)
        if len(cards) >= top_n:
            break
    return cards

async def enrich_detail_try(page, url, per_link_delay_sec=0.8):
    """
    Visita la ficha y trata de extraer fecha/antigüedad de publicación con varios selectores.
    Si no encuentra, deja fields en None (modo blando lo maneja).
    """
    out = {"url": url, "age_hours": None, "age_label": None, "titulo": None}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(int(per_link_delay_sec * 1000))

        # título
        h1 = await page.query_selector("h1") or await page.query_selector("h1,h2,[data-testid='header-title']")
        if h1:
            t = (await h1.text_content()) or ""
            out["titulo"] = re.sub(r"\s+", " ", t).strip()

        # intentos de fecha (varios selectores comunes)
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
                # meta content?
                tag = (await el.evaluate("(e)=>e.tagName")) or ""
                tag = tag.lower()
                content = ""
                if tag == "meta":
                    content = (await el.get_attribute("content")) or ""
                else:
                    content = (await el.text_content()) or ""
                content = re.sub(r"\s+", " ", content).strip()
                if content:
                    label_texts.append(content)

        label_text = " | ".join(dict.fromkeys(label_texts)) if label_texts else None
        out["age_label"] = label_text

        # cálculo age_hours si hay un datetime ISO
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
            except Exception:
                pass
        else:
            # Fallback heurístico desde texto “hace X hora/min”
            approx = _parse_hours_from_label(label_text or "")
            if isinstance(approx, (int, float)):
                out["age_hours"] = approx

    except Exception as e:
        print(f"[detail] ERROR al enriquecer {url}: {e}")
    return out

# ================== PIPELINE PRINCIPAL ==================
async def run_once():
    searches = CFG.get("searches") or []
    if not searches:
        print("[cfg] No hay búsquedas definidas en config.yaml -> nada para hacer.")
        return

    seen = load_seen()
    print(f"[cache] URLs ya enviadas: {len(seen)}")

    nuevos_totales = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ))
        page = await context.new_page()

        for s in searches:
            label = (s.get("label") or "Búsqueda").strip()
            url   = s.get("url") or ""
            if not url:
                print(f"[search] {label}: sin URL, salto.")
                continue

            print(f"\n[search] {label}: {url}")
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                print(f"[search] No se pudo abrir listado: {e}")
                continue

            cards = await extract_cards_from_search(page, top_n=int(CFG.get("top_n_per_search", 12)))
            print(f"[search] tarjetas detectadas: {len(cards)}")

            # nuevos vs cache
            candidatos = [c for c in cards if c["url"] not in seen]
            print(f"[search] candidatos no vistos: {len(candidatos)}")

            # enriquecer cada uno (detalle)
            enriquecidos = []
            for c in candidatos:
                d = await enrich_detail_try(page, c["url"], per_link_delay_sec=CFG.get("per_link_delay_sec", 0.8))
                # merge campos
                d["card_text"] = c.get("card_text")
                if not d.get("titulo"):
                    d["titulo"] = c.get("titulo")
                enriquecidos.append(d)

            # filtro modo blando
            filtrados = [it for it in enriquecidos if pasa_filtro_fecha(it, CFG.get("max_age_hours", 24))]
            print(f"[filtro] candidatos={len(enriquecidos)} -> tras modo_blando={len(filtrados)}")
            sin_fecha = [it for it in enriquecidos if it.get("age_hours") is None]
            print(f"[fecha] sin fecha detectable (info heurística): {len(sin_fecha)}")

            # armar mensaje por búsqueda
            if filtrados:
                titulo = f"🆕 {label} — publicados recientes"
                bloques = [titulo]
                for it in filtrados:
                    t = (it.get("titulo") or it.get("card_text") or "").strip()
                    linea = f"- {t} {it['url']}".strip()
                    bloques.append(linea)
                    # marcar como visto
                    seen.add(it["url"])
                msg = "\n".join(bloques)[:3800]
                print("[send] Enviando a Telegram…")
                send_telegram(msg)
                nuevos_totales.extend(filtrados)
            else:
                print("[send] Nada para enviar en esta búsqueda.")

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

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scraper.log", encoding="utf-8")
    ]
)

PRODUCTS_FILE = os.path.join("data", "products.json")
SHOPS_FILE = "shops.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
REQUEST_DELAY = 3.5  # Kohtelias viive pyyntöjen välillä (sekuntia)

robots_cache = {}


def can_fetch_url(url: str) -> bool:
    """Tarkistaa robots.txt-säännöt kohteliaasti."""
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    if base_url not in robots_cache:
        rp = RobotFileParser()
        rp.set_url(f"{base_url}/robots.txt")
        try:
            resp = requests.get(
                f"{base_url}/robots.txt",
                headers={"User-Agent": USER_AGENT},
                timeout=10
            )
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                rp.allow_all = True
        except Exception as e:
            logging.warning(f"robots.txt luku epäonnistui ({base_url}): {e}")
            rp.allow_all = True
        robots_cache[base_url] = rp
    return robots_cache[base_url].can_fetch(USER_AGENT, url)


def parse_price(raw_value) -> float | None:
    """Muuntaa merkkijonon tai numeron liukuluvuksi (esim. '44,01 €' -> 44.01)."""
    if raw_value is None:
        return None
    if isinstance(raw_value, (int, float)):
        val = float(raw_value)
        return round(val, 2) if val > 0 else None

    text = str(raw_value).strip()
    cleaned = re.sub(r"[^\d,\.]", "", text)
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")

    try:
        val = float(cleaned)
        return round(val, 2) if 0.5 <= val <= 100000 else None
    except ValueError:
        return None


def extract_json_ld(soup: BeautifulSoup) -> tuple[float | None, float | None]:
    """Etsii hinnan ja mahdollisen vertailuhinnan sivun JSON-LD (schema.org) -datasta."""
    current_price = None
    original_price = None

    scripts = soup.find_all("script", type="application/ld+json")
    for script in scripts:
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue

        items = data if isinstance(data, list) else [data]
        expanded = []
        for item in items:
            if isinstance(item, dict) and "@graph" in item:
                expanded.extend(item["@graph"])
            else:
                expanded.append(item)

        for item in expanded:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                is_product = "Product" in item_type
            else:
                is_product = item_type == "Product"

            if not is_product:
                continue

            offers = item.get("offers")
            if not offers:
                continue
            offer_list = offers if isinstance(offers, list) else [offers]

            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue
                p = offer.get("price") or offer.get("lowPrice")
                parsed_p = parse_price(p)
                if parsed_p and current_price is None:
                    current_price = parsed_p

                high_p = offer.get("highPrice")
                parsed_high = parse_price(high_p)
                if parsed_high and current_price and parsed_high > current_price:
                    original_price = parsed_high

                price_spec = offer.get("priceSpecification")
                if isinstance(price_spec, list):
                    for spec in price_spec:
                        if isinstance(spec, dict):
                            pt = str(spec.get("priceType", ""))
                            sp = parse_price(spec.get("price"))
                            if sp and ("ListPrice" in pt or "SRP" in pt or (current_price and sp > current_price)):
                                original_price = sp
                elif isinstance(price_spec, dict):
                    sp = parse_price(price_spec.get("price"))
                    if sp and current_price and sp > current_price:
                        original_price = sp

    return current_price, original_price


def extract_css(soup: BeautifulSoup, shop_config: dict) -> tuple[float | None, float | None]:
    """Etsii hinnan ja yliviivatun normaalihinnan CSS-selektoreilla."""
    current_price = None
    original_price = None

    for selector in shop_config.get("price_selectors", []):
        el = soup.select_one(selector)
        if el:
            val = el.get("content") or el.get_text(" ", strip=True)
            parsed = parse_price(val)
            if parsed:
                current_price = parsed
                break

    for selector in shop_config.get("original_price_selectors", []):
        el = soup.select_one(selector)
        if el:
            val = el.get("content") or el.get_text(" ", strip=True)
            parsed = parse_price(val)
            if parsed and (current_price is None or parsed > current_price):
                original_price = parsed
                break

    return current_price, original_price


def get_domain_key(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def fetch_store_price(url: str, shops_config: dict) -> tuple[float | None, float | None, str | None]:
    """Hakee yksittäisen tuotesivun hinnan. Palauttaa (hinta, normaalihinta, virheviesti)."""
    if not can_fetch_url(url):
        msg = "Estetty sivuston robots.txt-tiedostossa"
        logging.warning(f"{url}: {msg}")
        return None, None, msg

    parsed_target = urlparse(url)
    origin_url = f"{parsed_target.scheme}://{parsed_target.netloc}/"

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "fi-FI,fi;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": origin_url,
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }

    response = None
    last_err = None
    session = requests.Session()

    for attempt in range(1, 4):
        try:
            response = session.get(url, headers=headers, timeout=25)
            response.raise_for_status()
            last_err = None
            break
        except requests.RequestException as e:
            last_err = f"HTTP-virhe: {str(e)}"
            logging.warning(f"{url} (yritys {attempt}/3): {last_err}")
            if attempt < 3:
                time.sleep(2.5 * attempt)

    if last_err is not None or response is None:
        logging.error(f"{url}: {last_err}")
        return None, None, last_err

    soup = BeautifulSoup(response.text, "lxml")
    domain = get_domain_key(url)
    shop_cfg = shops_config.get(domain, {})

    price, orig_price = extract_json_ld(soup)

    css_price, css_orig = extract_css(soup, shop_cfg)
    if price is None:
        price = css_price
    if orig_price is None and css_orig is not None:
        orig_price = css_orig

    if price is None:
        fb_price, fb_orig = extract_fallback_price(soup)
        price = fb_price
        if orig_price is None and fb_orig is not None:
            orig_price = fb_orig
            
    if price is not None and orig_price is not None and orig_price <= price:
        orig_price = None

    if price is None:
        msg = "Hintaa ei löytynyt sivulta (JSON-LD eikä CSS-selektorit täsmänneet)"
        logging.warning(f"{url}: {msg}")
        return None, None, msg

    return price, orig_price, None


def extract_fallback_price(soup: BeautifulSoup) -> tuple[float | None, float | None]:
    """Yleinen varahaku mille tahansa kaupalle, kun JSON-LD ja shops.json eivät tuota hintaa."""
    candidates = []
    currency_pattern = re.compile(r"(€|EUR)", re.I)
    price_attr_pattern = re.compile(r"(price|hinta|amount|cost|tarjous|sale|offer)", re.I)
    ignore_attr_pattern = re.compile(r"(title|name|heading|sku|code|model|shipping|toimitus|vero|tax)", re.I)

    for el in soup.find_all(["meta", "data"]):
        itemprop = str(el.get("itemprop") or "").lower()
        if itemprop == "price" or el.get("data-price-amount"):
            raw_val = el.get("content") or el.get("value") or el.get("data-price-amount")
            parsed = parse_price(raw_val)
            if parsed is not None:
                candidates.append((parsed, False))

    for el in soup.find_all(["span", "div", "p", "strong", "b"]):
        attrs_blob = " ".join([
            str(el.get("id") or ""),
            " ".join(el.get("class") or []),
            str(el.get("itemprop") or "")
        ])
        if not price_attr_pattern.search(attrs_blob):
            continue
        if ignore_attr_pattern.search(attrs_blob):
            continue

        text_val = el.get_text(" ", strip=True)
        if not text_val or len(text_val) > 40 or not currency_pattern.search(text_val):
            continue

        parsed = parse_price(text_val)
        if parsed is not None:
            style_blob = (str(el.get("style") or "") + " " + " ".join(el.get("class") or [])).lower()
            is_struck = (
                el.name in ("del", "s", "strike")
                or el.find_parent(["del", "s", "strike"]) is not None
                or "line-through" in style_blob
                or "old" in style_blob
                or "compare" in style_blob
            )
            candidates.append((parsed, is_struck))

    if not candidates:
        page_text = soup.get_text(" ", strip=True)
        euro_matches = re.findall(r"(\d{1,4}(?:[ \u00a0.]\d{3})*(?:,\d{2})?|\d{1,4}\.\d{2})\s*€", page_text)
        for m in euro_matches:
            parsed = parse_price(m)
            if parsed is not None and parsed >= 5.0:
                candidates.append((parsed, False))

    if not candidates:
        return None, None

    non_struck = [p for p, struck in candidates if not struck]
    struck = [p for p, struck in candidates if struck]

    current_price = min(non_struck) if non_struck else min(p for p, _ in candidates)
    original_price = None
    higher_struck = [p for p in struck if p > current_price]
    if higher_struck:
        original_price = min(higher_struck)

    return current_price, original_price


def main():
    if not os.path.exists(PRODUCTS_FILE):
        logging.error(f"Tiedostoa {PRODUCTS_FILE} ei löydy.")
        return

    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    shops_config = {}
    if os.path.exists(SHOPS_FILE):
        with open(SHOPS_FILE, "r", encoding="utf-8") as f:
            shops_config = json.load(f)

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for product in data.get("products", []):
        logging.info(f"Tarkistetaan tuote: {product.get('name')}")
        stores = product.get("stores", {})

        for store_key, store_info in stores.items():
            url = store_info.get("url")
            if not url:
                continue

            logging.info(f"  Haetaan: {store_info.get('name', store_key)} -> {url}")
            price, orig_price, error = fetch_store_price(url, shops_config)

            if error is None and price is not None:
                previous_orig = store_info.get("original_price")
                effective_orig = orig_price if orig_price is not None else previous_orig
                if effective_orig is not None and effective_orig <= price:
                    effective_orig = None

                store_info["current_price"] = price
                store_info["original_price"] = effective_orig
                store_info["last_checked"] = now_iso
                store_info["error"] = None

                history = store_info.setdefault("history", [])
                if history and history[-1].get("date") == today_str:
                    history[-1]["price"] = price
                    history[-1]["original_price"] = effective_orig
                else:
                    history.append({
                        "date": today_str,
                        "price": price,
                        "original_price": effective_orig
                    })
                logging.info(f"    OK: {price:.2f} € (norm. {effective_orig if effective_orig else '-'})")
            else:
                store_info["error"] = error or "Tuntematon virhe"
                logging.warning(f"    Virhe (säilytetään vanha hinta): {store_info['error']}")

            time.sleep(REQUEST_DELAY)

    data["last_updated"] = now_iso
    with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logging.info("Hintapäivitys valmis.")


if __name__ == "__main__":
    main()

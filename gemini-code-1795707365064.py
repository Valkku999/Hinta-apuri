import json
import os
import re
from urllib.parse import urlparse

PRODUCTS_FILE = os.path.join("data", "products.json")
SHOPS_FILE = "shops.json"


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("ä", "a").replace("ö", "o").replace("å", "a")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "tuote"


def get_domain_key(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def main():
    issue_body = os.environ.get("ISSUE_BODY", "")
    match = re.search(r"```json\s*(\{.*?\})\s*```", issue_body, re.DOTALL)
    if not match:
        raise ValueError("Issuen sisällöstä ei löytynyt kelvollista ```json ... ``` -lohkoa.")

    payload = json.loads(match.group(1))
    action = payload.get("action")

    with open(PRODUCTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    shops_config = {}
    if os.path.exists(SHOPS_FILE):
        with open(SHOPS_FILE, "r", encoding="utf-8") as f:
            shops_config = json.load(f)

    products = data.setdefault("products", [])

    if action == "delete":
        prod_id = payload.get("id")
        data["products"] = [p for p in products if p.get("id") != prod_id]

    elif action in ("add", "edit"):
        name = payload.get("name", "").strip()
        brand = payload.get("brand", "").strip()
        target_price = payload.get("target_price")
        urls = [u.strip() for u in payload.get("urls", []) if u.strip()]

        if not name or not urls:
            raise ValueError("Tuotteen nimi ja vähintään yksi URL vaaditaan.")

        if action == "add":
            base_id = slugify(name)
            prod_id = base_id
            existing_ids = {p["id"] for p in products}
            counter = 2
            while prod_id in existing_ids:
                prod_id = f"{base_id}-{counter}"
                counter += 1

            stores = {}
            for url in urls:
                d_key = get_domain_key(url)
                store_name = shops_config.get(d_key, {}).get("name", d_key)
                stores[d_key] = {
                    "name": store_name,
                    "url": url,
                    "current_price": None,
                    "original_price": None,
                    "last_checked": None,
                    "error": "Odottaa seuraavaa hakua",
                    "history": []
                }

            products.append({
                "id": prod_id,
                "name": name,
                "brand": brand,
                "target_price": float(target_price) if target_price not in (None, "") else None,
                "stores": stores
            })

        elif action == "edit":
            prod_id = payload.get("id")
            for p in products:
                if p.get("id") == prod_id:
                    p["name"] = name
                    p["brand"] = brand
                    p["target_price"] = float(target_price) if target_price not in (None, "") else None

                    old_stores = p.get("stores", {})
                    new_stores = {}
                    for url in urls:
                        d_key = get_domain_key(url)
                        if d_key in old_stores and old_stores[d_key].get("url") == url:
                            new_stores[d_key] = old_stores[d_key]
                        else:
                            store_name = shops_config.get(d_key, {}).get("name", d_key)
                            new_stores[d_key] = {
                                "name": store_name,
                                "url": url,
                                "current_price": None,
                                "original_price": None,
                                "last_checked": None,
                                "error": "Odottaa seuraavaa hakua",
                                "history": []
                            }
                    p["stores"] = new_stores
                    break

    with open(PRODUCTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
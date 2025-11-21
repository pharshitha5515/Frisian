import os
import json
import pandas as pd


# ============================================================
# 1. CONFIGURATION
# ============================================================
JSON_FOLDER = "json_results"
OUTPUT_EXCEL = "final_output.xlsx"


# ============================================================
# 2. ROW NORMALIZATION HELPERS
# ============================================================

def safe_get(obj, *keys):
    """
    Utility: return the first non-empty key found in a dict.
    """
    for key in keys:
        if isinstance(obj, dict) and key in obj and obj[key] not in [None, ""]:
            return obj[key]
    return ""


def build_row(folder, page, **kwargs):
    """
    Create a consistent row with all final Excel columns.
    """
    base = {
        "folder": folder,
        "page_name": page,
        "image_id": "",
        "period": "",
        "brand": "",
        "sku_name": "",
        "promo_type": "",
        "mechanic": "",
        "regular_price": "",
        "promo_price": "",
        "confidence": "",
        "unit": ""
    }
    base.update(kwargs)
    return base


# ============================================================
# 3. EXTRACTORS FOR EACH TYPE OF JSON STRUCTURE
# ============================================================

def extract_standard_flyer(folder, page, extracted):
    """Handles: { image_id, period, items: [...] }"""
    rows = []
    img_id = extracted.get("image_id", "")
    period = extracted.get("period", "")

    for item in extracted.get("items", []):
        rows.append(
            build_row(
                folder, page,
                image_id=img_id,
                period=period,
                brand=item.get("brand", ""),
                sku_name=item.get("sku_name", ""),
                promo_type=item.get("promo_type", ""),
                mechanic=item.get("mechanic", ""),
                regular_price=item.get("regular_price", ""),
                promo_price=item.get("promo_price", ""),
                confidence=item.get("confidence", ""),
                unit=item.get("unit", "")
            )
        )
    return rows


def extract_label_items(folder, page, items):
    """
    Handles entries like:
    { "box_2d": [...], "label": "FRISIAN FLAG 1+ Madu/Vanila 750g box" }

    We treat `label` as brand+sku.
    Brand = first word(s) before numbers.
    SKU = rest of text.
    """
    rows = []

    for item in items:
        if not isinstance(item, dict):
            continue
        if "label" not in item:
            continue

        label = item["label"]
        clean = label.strip()

        # Try split into brand + rest
        parts = clean.split(" ")
        split_index = 1
        for idx, token in enumerate(parts):
            if any(ch.isdigit() for ch in token):  # first digit-containing token = SKU starts
                split_index = idx
                break

        brand = " ".join(parts[:split_index]).strip()
        sku = " ".join(parts[split_index:]).strip()

        rows.append(
            build_row(
                folder, page,
                brand=brand,
                sku_name=sku,
            )
        )

    return rows


def extract_list_items(folder, page, items):
    """Handles: extracted_data = [ {...}, {...} ] (general case)"""
    rows = []

    # If these are label-only bounding boxes → send to label extractor
    if all(isinstance(i, dict) and "label" in i and "box_2d" in i for i in items):
        return extract_label_items(folder, page, items)

    for item in items:
        if not isinstance(item, dict):
            continue

        rows.append(
            build_row(
                folder, page,
                image_id=item.get("image_id", ""),
                period=item.get("period", ""),
                brand=safe_get(item, "brand", "product_brand", "brand_name"),
                sku_name=safe_get(item, "sku_name", "name", "product_name", "item_name"),
                promo_type=safe_get(item, "promo_type", "deal", "deal_description"),
                mechanic=safe_get(item, "mechanic", "additional_info", "other_info"),
                regular_price=safe_get(item, "regular_price", "original_price", "normal_price"),
                promo_price=safe_get(item, "promo_price", "product_price", "final_price", "price"),
                confidence=item.get("confidence", ""),
                unit=safe_get(item, "unit", "quantity", "size")
            )
        )
    return rows


def extract_promo_items(folder, page, extracted):
    """Handles: { promo_items: [...], start_date, end_date }"""
    rows = []
    period = f"{extracted.get('start_date','')} to {extracted.get('end_date','')}"

    for item in extracted["promo_items"]:
        rows.append(
            build_row(
                folder, page,
                period=period,
                brand=item.get("brand", ""),
                sku_name=item.get("name", ""),
                promo_type=item.get("discount", ""),
                mechanic=item.get("quantity_note", ""),
                regular_price=item.get("original_price", ""),
                promo_price=item.get("promo_price", ""),
                unit=item.get("unit", "")
            )
        )
    return rows


def extract_offers(folder, page, extracted):
    """Handles: { offers: [ { deal_description, items: [] } ] }"""
    rows = []
    period = extracted.get("start_date", "")

    for offer in extracted["offers"]:
        for item in offer.get("items", []):
            rows.append(
                build_row(
                    folder, page,
                    period=period,
                    brand=item.get("category", extracted.get("brand", "")),
                    sku_name=item.get("name", ""),
                    promo_type=offer.get("deal_description", ""),
                    mechanic=offer.get("notes", ""),
                    regular_price=offer.get("original_price", ""),
                    promo_price=offer.get("price", "")
                )
            )
    return rows


# ============================================================
# 4. MASTER EXTRACTOR — AUTO-DETECT JSON TYPE
# ============================================================

def extract_rows(folder, page_name, extracted):
    """Detects JSON format and calls correct parser."""
    if isinstance(extracted, dict) and "items" in extracted:
        return extract_standard_flyer(folder, page_name, extracted)

    if isinstance(extracted, list):
        return extract_list_items(folder, page_name, extracted)

    if isinstance(extracted, dict) and "promo_items" in extracted:
        return extract_promo_items(folder, page_name, extracted)

    if isinstance(extracted, dict) and "offers" in extracted:
        return extract_offers(folder, page_name, extracted)

    return []


# ============================================================
# 5. MAIN PROCESSOR — LOAD ALL JSON & SAVE EXCEL
# ============================================================

def process_json_folder():
    all_rows = []

    for file in os.listdir(JSON_FOLDER):
        if not file.endswith(".json"):
            continue

        with open(os.path.join(JSON_FOLDER, file), "r", encoding="utf-8") as f:
            data = json.load(f)

        folder = data.get("folder", "")
        pages = data.get("pages", [])

        print(f" Processing → {file}")

        for page in pages:
            page_name = page.get("page_name", "")
            extracted = page.get("extracted_data", {})

            rows = extract_rows(folder, page_name, extracted)
            all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    df.to_excel(OUTPUT_EXCEL, index=False, engine="openpyxl")
    print("\n FINAL EXCEL CREATED →", OUTPUT_EXCEL)


# ============================================================
# 6. RUN
# ============================================================
if __name__ == "__main__":
    process_json_folder()

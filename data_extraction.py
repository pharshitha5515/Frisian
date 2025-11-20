import requests
import json
import base64
import os
import time

# --------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------
API_KEY = "sk-FXSqn80ty6ZwsoAJO2natQ"     # <-- PUT YOUR TIGER ANALYTICS KEY
BASE_URL = "https://api.ai-gateway.tigeranalytics.com"
MODEL_NAME = "gemini-2.5-pro"

ROOT_FOLDER = "/mnt/c/Users/harshitha.poosar/Downloads/friesland/output"

OUTPUT_JSON_DIR = "json_results"
os.makedirs(OUTPUT_JSON_DIR, exist_ok=True)

MAX_RETRIES = 3
SLEEP_BETWEEN_REQUESTS = 2    # seconds


# --------------------------------------------------------
# 1. Convert Image → Base64
# --------------------------------------------------------
def encode_image(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# --------------------------------------------------------
# 2. FEW-SHOT EXAMPLES (YOUR EXACT CONTENT)
# --------------------------------------------------------
few_shot_examples = """
---EXAMPLE 1---
Flyer Summary: SunCo Minyak 2L, Reg Price "Rp 41.700", Promo "Rp 39.500", Period "23-30 Nov".

Expected JSON:
{
  "image_id": "example_1",
  "period": "23-30 Nov",
  "items": [
    {
      "brand": "SunCo",
      "sku_name": "Minyak Goreng 2L",
      "promo_type": "Hemat Minggu Ini",
      "mechanic": "",
      "regular_price": "Rp 41.700",
      "promo_price": "Rp 39.500",
      "confidence": 0.95
    }
  ]
}
---END---

---EXAMPLE 2---
Flyer Summary: Tango wafer, mechanic "Buy 2", promo price "Rp 5.900".

Expected JSON:
{
  "image_id": "example_2",
  "period": "",
  "items": [
    {
      "brand": "Tango",
      "sku_name": "Tango Wafer 120g",
      "promo_type": "Buy 2",
      "mechanic": "Buy 2",
      "regular_price": "",
      "promo_price": "Rp 5.900",
      "confidence": 0.9
    }
  ]
}
---END---

---EXAMPLE 3---
Flyer Summary: Lifebuoy, 20% off, Reg Price "Rp 18.000", Promo "Rp 14.400".

Expected JSON:
{
  "image_id": "example_3",
  "period": "",
  "items": [
    {
      "brand": "Lifebuoy",
      "sku_name": "Body Wash 250ml",
      "promo_type": "20% off",
      "mechanic": "20% off",
      "regular_price": "Rp 18.000",
      "promo_price": "Rp 14.400",
      "confidence": 0.92
    }
  ]
}
---END---
"""


# --------------------------------------------------------
# 3. MAIN PROMPT (YOUR EXACT CONTENT)
# --------------------------------------------------------
main_prompt = """
Extract promo flyer details from the attached image.

Return JSON with:
- brand
- sku_name
- promo_type
- period
- mechanic
- regular_price (exact as printed)
- promo_price (exact as printed)
- confidence

Rules:
- Return all prices EXACTLY as printed (including "Rp" and dots)
- If something is missing, return ""
- Output JSON only

JSON structure:

{
  "image_id": "",
  "period": "",
  "items": [
    {
      "brand": "",
      "sku_name": "",
      "promo_type": "",
      "mechanic": "",
      "regular_price": "",
      "promo_price": "",
      "confidence": 0.0
    }
  ]
}
"""


# --------------------------------------------------------
# 4. CALL GEMINI WITH RETRY + RATE LIMIT HANDLING
# --------------------------------------------------------
def call_gemini(image_b64):
    url = f"{BASE_URL}/v1/chat/completions"

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": few_shot_examples},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": main_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}}
                ]
            }
        ]
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    # Retry logic
    for attempt in range(MAX_RETRIES):
        response = requests.post(url, headers=headers, json=payload)
        data = response.json()

        # SUCCESS
        if "error" not in data:
            return data

        # RATE LIMIT → WAIT & RETRY
        if data["error"].get("code") == "429":
            wait = (attempt + 1) * 5
            print(f"   🔁 Rate limit hit. Retrying in {wait} sec...")
            time.sleep(wait)
            continue

        # OTHER ERROR → RETURN
        return data

    return data


# --------------------------------------------------------
# 5. CLEAN JSON
# --------------------------------------------------------
def clean_json(text):
    if not text or text.strip() == "":
        return None

    text = text.strip()

    # Remove ``` code fences
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) > 1:
            text = parts[1]

    # Remove "json" label
    if text.strip().startswith("json"):
        text = text.strip()[4:]

    return text.strip()


# --------------------------------------------------------
# 6. PROCESS ALL FOLDERS → ONE JSON PER FOLDER
# --------------------------------------------------------
for folder in os.listdir(ROOT_FOLDER):

    folder_path = os.path.join(ROOT_FOLDER, folder)
    if not os.path.isdir(folder_path):
        continue

    pages_dir = os.path.join(folder_path, "pages")
    if not os.path.exists(pages_dir):
        continue

    print(f"\n Processing folder: {folder}")

    folder_result = {
        "folder": folder,
        "pages": []
    }

    for img_file in sorted(os.listdir(pages_dir)):

        if not img_file.lower().endswith((".png", ".jpg", ".jpeg")):
            continue

        img_path = os.path.join(pages_dir, img_file)
        print(f" Processing {img_file}")

        image_b64 = encode_image(img_path)

        result = call_gemini(image_b64)
        time.sleep(SLEEP_BETWEEN_REQUESTS)

        # API ERROR
        if "error" in result:
            print(f" Error for {img_file}: {result['error']}")
            folder_result["pages"].append({
                "page_name": img_file,
                "error": result["error"]
            })
            continue

        try:
            content = result["choices"][0]["message"]["content"]
            cleaned = clean_json(content)

            if cleaned is None:
                raise ValueError("Empty model output")

            parsed_json = json.loads(cleaned)

            folder_result["pages"].append({
                "page_name": img_file,
                "extracted_data": parsed_json
            })

            print(f" Extracted from {img_file}")

        except Exception as e:
            print(f" JSON error on {img_file}: {e}")
            folder_result["pages"].append({
                "page_name": img_file,
                "error": f"JSON parse error: {str(e)}",
                "raw_output": content
            })

    # SAVE RESULT FOR THIS FOLDER
    out_path = os.path.join(OUTPUT_JSON_DIR, f"{folder}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(folder_result, f, indent=2, ensure_ascii=False)

    print(f" Saved → {out_path}")



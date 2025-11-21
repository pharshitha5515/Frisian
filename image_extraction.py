import asyncio
import re
import os
from pathlib import Path
from typing import Optional
from playwright.async_api import async_playwright, Page, Frame

# ================== CONFIG ==================
PROMO_LIST_URL = "https://www.indomaret.co.id/promosi"
NAV_TIMEOUT = 180_000
DOWNLOAD_TIMEOUT = 120_000
CONVERT_PDF_TO_PNG = True
# ============================================

def safe(name: str):
    name = (name or "").strip()
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name or "untitled"

# ============================================================
# BLOCK NON-FLIP IMAGES
# ============================================================
async def block_nonflip_images(page: Page):
    async def handler(route):
        url = (route.request.url or "").lower()
        if route.request.resource_type == "image":
            # Allow only flipbook/real3d images
            if ("flip" in url or "page" in url or "/uploads/" in url
                or url.endswith((".png", ".jpg", ".jpeg"))):
                await route.continue_()
            else:
                await route.abort()
        else:
            await route.continue_()
    await page.route("**/*", handler)

# ============================================================
# HTML EXTRACTORS
# ============================================================
async def extract_pdf_url_from_html(html: str) -> Optional[str]:
    # Real3D flipbookOptions JSON
    m = re.search(r'"pdfUrl"\s*:\s*"([^"]+)"', html)
    if m:
        return m.group(1).replace("\\/", "/")

    # Generic .pdf
    m = re.search(r'https://[^"\']+\.pdf', html)
    if m:
        return m.group(0)

    return None


async def extract_flipbook_index_html(html: str) -> Optional[str]:
    m = re.search(r'(https://img\.indomaret\.co\.id[^"\']+index\.html)', html)
    if m:
        return m.group(1).replace("\\/", "/")
    return None


# ============================================================
# RECURSIVE FRAME PDF DETECTION (Critical Fix)
# ============================================================
async def find_pdf_in_frames_recursively(frame: Frame) -> Optional[str]:
    try:
        html = await frame.content()
        m = re.search(r'https://[^"\']+\.pdf', html)
        if m:
            return m.group(0)
    except:
        pass

    for child in frame.child_frames:
        pdf = await find_pdf_in_frames_recursively(child)
        if pdf:
            return pdf

    return None


async def extract_pdf_from_iframes(page: Page) -> Optional[str]:
    for frame in page.frames:
        pdf = await find_pdf_in_frames_recursively(frame)
        if pdf:
            return pdf
    return None


# ============================================================
# PDF DOWNLOAD & PDF → PNG CONVERSION
# ============================================================
async def download_pdf(page: Page, url: str, dest: Path) -> bool:
    try:
        print(f"  → Downloading PDF: {url}")
        resp = await page.request.get(url, timeout=DOWNLOAD_TIMEOUT)
        if resp.status != 200:
            print("   PDF download failed")
            return False

        dest.write_bytes(await resp.body())
        print(f"   Saved PDF: {dest}")
        return True

    except Exception as e:
        print(f"  PDF error: {e}")
        return False


def convert_pdf_to_images(pdf_file: Path, out_dir: Path) -> int:
    try:
        import fitz
    except:
        print(" PyMuPDF not installed → skipping conversion.")
        return 0

    doc = fitz.open(pdf_file)
    count = doc.page_count
    out_dir.mkdir(parents=True, exist_ok=True)

    for i in range(count):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        outfile = out_dir / f"page_{i+1:03d}.png"
        pix.save(outfile)
        print(f" Converted page {i+1}/{count}")

    doc.close()
    return count


# ============================================================
# FLIPBOOK (index.html) SCREENSHOTTER
# ============================================================
async def screenshot_flipbook_index(page: Page, url: str, out_dir: Path) -> int:
    await page.goto(url, timeout=NAV_TIMEOUT)
    await page.wait_for_load_state("load")

    selectors = [
        ".flipbook-page-html",
        ".htmlContent",
        ".magazine-page",
        ".RInner .flipbook-page-html"
    ]

    ctx = page
    found = None

    # search main page
    for sel in selectors:
        if await ctx.locator(sel).count() > 0:
            found = sel
            break

    # search frames
    if not found:
        for f in page.frames:
            for sel in selectors:
                if await f.locator(sel).count() > 0:
                    ctx = f
                    found = sel
                    break
            if found:
                break

    if not found:
        print(" No flipbook selector found")
        return 0

    print(f"  → Using flipbook selector: {found}")
    out_dir.mkdir(parents=True, exist_ok=True)

    page_num = 1
    while page_num <= 500:
        locator = ctx.locator(found).first
        await ctx.wait_for_timeout(200)

        outfile = out_dir / f"page_{page_num:03d}.png"

        try:
            await locator.screenshot(path=str(outfile))
            print(f" Saved page {page_num}")
        except:
            break

        # Next page
        next_buttons = [
            ".pageClickAreaRight",
            ".flipbook-right-arrow",
            ".swiper-button-next"
        ]

        clicked = False
        for btn in next_buttons:
            el = ctx.locator(btn).first
            if await el.count() > 0:
                try:
                    await el.click()
                except:
                    await ctx.evaluate(
                        "(sel)=>{let e=document.querySelector(sel);e&&e.click();}",
                        btn
                    )
                clicked = True
                break

        if not clicked:
            break

        page_num += 1

    return page_num - 1


# ============================================================
# CAPTURE SINGLE PROMO (FINAL LOGIC)
# ============================================================
async def capture_single(promo: dict, browser):
    folder = Path("output") / promo["folder"]
    folder.mkdir(parents=True, exist_ok=True)

    print(f"\n Capturing: {promo['title']}")
    print(f"    URL: {promo['link']}")

    page = await browser.new_page()
    await block_nonflip_images(page)

    try:
        await page.goto(promo["link"], timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("load")

        # CRITICAL: wait for dynamic JS/Real3D scripts
        await page.wait_for_timeout(3500)

        # Try to wait for Real3D <script> tag
        try:
            await page.wait_for_selector('script[id^="real3d-flipbook-options"]', timeout=5000)
        except:
            pass

        # Reload fully updated HTML
        html = await page.content()

        # ---- 1) Real3D / direct PDF detection ----
        pdf = await extract_pdf_url_from_html(html)

        # ---- 2) Deep iframe scan ----
        if not pdf:
            pdf = await extract_pdf_from_iframes(page)

        if pdf:
            pdf_dir = folder / "PDF"
            pdf_dir.mkdir(parents=True, exist_ok=True)

            pdf_name = pdf.split("/")[-1].split("?")[0]
            pdf_path = pdf_dir / pdf_name

            if await download_pdf(page, pdf, pdf_path):
                if CONVERT_PDF_TO_PNG:
                    count = convert_pdf_to_images(pdf_path, folder / "pages")
                    print(f"  Converted PDF → {count} pages")
            return

        # ---- 3) index.html flipbooks ----
        idx = await extract_flipbook_index_html(html)
        if idx:
            pages = await screenshot_flipbook_index(page, idx, folder / "pages")
            print(f"  Captured {pages} flipbook pages")
            return

        print("  No PDF or Flipbook found.")

    except Exception as e:
        print(f"  Error: {e}")

    finally:
        await page.close()


# ============================================================
# PROMO LIST SCRAPER
# ============================================================
async def get_promos(page: Page):
    print("Loading promo list...\n")
    await page.goto(PROMO_LIST_URL, timeout=NAV_TIMEOUT)
    await page.wait_for_load_state("load")

    await page.wait_for_selector("div.promotion-page")

    cards = page.locator("div.promotion-page")
    count = await cards.count()
    print(f"Found {count} promo cards\n")

    promos = []

    for i in range(count):
        card = cards.nth(i)

        # ----- title -----
        try:
            title = (await card.locator("h2").inner_text()).strip()
        except:
            title = ""

        promo_id = await card.get_attribute("id")
        if not title and promo_id:
            title = promo_id.replace("-", " ").title()

        # ----- link extraction -----
        link = None

        # 1. <a href="">
        try:
            a = card.locator("a").first
            if await a.count() > 0:
                href = await a.get_attribute("href")
                if href:
                    link = href
        except:
            pass

        # 2. onclick="window.location='...'"
        if not link:
            try:
                onclick = await card.get_attribute("onclick")
                if onclick:
                    m = re.search(r"['\"](/[^'\"]+)['\"]", onclick)
                    if m:
                        link = m.group(1)
            except:
                pass

        # 3. fallback via id
        if not link and promo_id:
            link = f"/{promo_id}/"

        # normalize
        if link and not link.startswith("http"):
            link = "https://www.indomaret.co.id" + link

        if not link:
            continue

        promos.append({
            "title": title or safe(link.split('/')[-2]),
            "link": link,
            "folder": safe(title or link.split('/')[-2])
        })

    # dedupe
    seen = set()
    unique = []
    for p in promos:
        if p["link"] not in seen:
            seen.add(p["link"])
            unique.append(p)

    return unique


# ============================================================
# MAIN
# ============================================================
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        page = await browser.new_page()
        await block_nonflip_images(page)

        promos = await get_promos(page)
        await page.close()

        print(" STARTING CAPTURE")

        for promo in promos:
            await capture_single(promo, browser)

        await browser.close()
        print("\n ALL PROMOS DONE!")


if __name__ == "__main__":
    Path("output").mkdir(exist_ok=True)
    print("Welcome to Indomaret Scraper!")
    print(f"Output will be saved in: {os.path.abspath('output')}\n")

    try:
        import fitz
    except:
        print("PyMuPDF not installed — PDF → PNG disabled.")

    asyncio.run(main())

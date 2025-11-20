import asyncio
import re
import os
from pathlib import Path
from typing import Optional, List, Dict
from playwright.async_api import async_playwright, Page, Frame, Route, Request

# ================== CONFIG ==================
PROMO_LIST_URL = "https://www.indomaret.co.id/promosi"
NAV_TIMEOUT = 180_000
DOWNLOAD_TIMEOUT = 120_000
CONVERT_PDF_TO_PNG = True
OUTPUT_DIR = Path("output")
# ============================================


def safe(name: str):
    name = (name or "").strip()
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name or "untitled"


# ============================================================
# BLOCK NON-FLIP IMAGES
# ============================================================
async def block_nonflip_images(page: Page):
    async def handler(route: Route):
        try:
            req: Request = route.request
            url = (req.url or "").lower()
            rtype = req.resource_type or ""
            if rtype == "image":
                # Allow likely flipbook / uploads images, block others to save bandwidth
                if ("flip" in url or "page" in url or "/uploads/" in url
                        or url.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"))):
                    await route.continue_()
                else:
                    await route.abort()
            else:
                await route.continue_()
        except Exception:
            # If anything goes wrong, continue the request to avoid deadlocks
            try:
                await route.continue_()
            except Exception:
                pass

    # remove existing routes? Playwright will allow multiple but to be safe we just add
    try:
        await page.route("**/*", handler)
    except Exception:
        pass


# ============================================================
# HTML EXTRACTORS
# ============================================================
def extract_pdf_url_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r'"pdfUrl"\s*:\s*"([^"]+)"', text)
    if m:
        return m.group(1).replace("\\/", "/")
    m = re.search(r'https://[^"\']+\.pdf', text)
    if m:
        return m.group(0)
    return None


async def extract_flipbook_index_html(html: str) -> Optional[str]:
    if not html:
        return None
    m = re.search(r'(https://img\.indomaret\.co\.id[^"\']+index\.html)', html)
    if m:
        return m.group(1).replace("\\/", "/")
    return None


# ============================================================
# RECURSIVE FRAME PDF DETECTION
# ============================================================
async def find_pdf_in_frames_recursively(frame: Frame) -> Optional[str]:
    try:
        html = await frame.content()
        m = re.search(r'https://[^"\']+\.pdf', html)
        if m:
            return m.group(0)
    except Exception:
        pass

    for child in frame.child_frames:
        pdf = await find_pdf_in_frames_recursively(child)
        if pdf:
            return pdf

    return None


async def extract_pdf_from_iframes(page: Page) -> Optional[str]:
    for f in page.frames:
        pdf = await find_pdf_in_frames_recursively(f)
        if pdf:
            return pdf
    return None


# ============================================================
# DOWNLOAD & CONVERT
# ============================================================
async def download_pdf(page: Page, url: str, dest: Path) -> bool:
    try:
        print(f"  → Downloading PDF: {url}")
        resp = await page.request.get(url, timeout=DOWNLOAD_TIMEOUT)
        if resp.status != 200:
            print("  PDF download failed:", resp.status)
            return False
        dest.write_bytes(await resp.body())
        print(f"  Saved PDF: {dest}")
        return True
    except Exception as e:
        print(f"  PDF error: {e}")
        return False


def convert_pdf_to_images(pdf_file: Path, out_dir: Path) -> int:
    try:
        import fitz
    except Exception:
        print("⚠ PyMuPDF not installed → skipping conversion.")
        return 0

    doc = fitz.open(pdf_file)
    count = doc.page_count
    out_dir.mkdir(parents=True, exist_ok=True)

    mat = fitz.Matrix(2, 2)
    for i in range(count):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=mat)
        outfile = out_dir / f"page_{i+1:03d}.png"
        pix.save(outfile)
        print(f"  Converted page {i+1}/{count}")

    doc.close()
    return count


# ============================================================
# FLIPBOOK (index.html) SCREENSHOTTER
# ============================================================
async def screenshot_flipbook_index(page: Page, url: str, out_dir: Path) -> int:
    try:
        await page.goto(url, timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("load")
    except Exception as e:
        print("  Failed to open index.html flipbook:", e)
        return 0

    selectors = [
        ".flipbook-page-html",
        ".htmlContent",
        ".magazine-page",
        ".RInner .flipbook-page-html",
        ".page"
    ]

    ctx = page
    found = None

    for sel in selectors:
        try:
            if await page.locator(sel).count() > 0:
                ctx = page
                found = sel
                break
        except Exception:
            continue

    if not found:
        for f in page.frames:
            for sel in selectors:
                try:
                    if await f.locator(sel).count() > 0:
                        ctx = f
                        found = sel
                        break
                except Exception:
                    continue
            if found:
                break

    if not found:
        print(" No flipbook selector found")
        return 0

    print(f"  → Using flipbook selector: {found}")
    out_dir.mkdir(parents=True, exist_ok=True)

    page_num = 1
    while page_num <= 500:
        try:
            locator = ctx.locator(found).first
            await ctx.wait_for_timeout(200)
            outfile = out_dir / f"page_{page_num:03d}.png"
            await locator.screenshot(path=str(outfile))
            print(f"  Saved page {page_num}")
        except Exception:
            break

        # Next page
        next_buttons = [
            ".pageClickAreaRight",
            ".flipbook-right-arrow",
            ".swiper-button-next",
            ".btn-next",
            ".next"
        ]

        clicked = False
        for btn in next_buttons:
            try:
                el = ctx.locator(btn).first
                if await el.count() > 0:
                    try:
                        await el.click()
                    except Exception:
                        try:
                            await ctx.evaluate("(sel)=>{let e=document.querySelector(sel);e&&e.click();}", btn)
                        except Exception:
                            pass
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            break

        page_num += 1

    return page_num - 1


# ============================================================
# CAPTURE SINGLE PROMO
# ============================================================
async def capture_single(promo: dict, browser):
    folder = OUTPUT_DIR / promo["folder"]
    folder.mkdir(parents=True, exist_ok=True)

    print(f"\n📘 Capturing: {promo['title']}")
    print(f"    URL: {promo['link']}")

    page = await browser.new_page()
    await block_nonflip_images(page)

    # intercept network to catch any .pdf request that fires after navigation
    found_pdfs: List[str] = []

    async def on_request(request: Request):
        try:
            url = (request.url or "")
            if url.lower().endswith(".pdf") and url not in found_pdfs:
                found_pdfs.append(url)
        except Exception:
            pass

    page.on("request", on_request)

    try:
        await page.goto(promo["link"], timeout=NAV_TIMEOUT)
        await page.wait_for_load_state("networkidle")
        # wait a bit for any async loads
        await page.wait_for_timeout(3000)

        # try multiple extraction strategies (network-first)
        pdf: Optional[str] = None

        if found_pdfs:
            pdf = found_pdfs[0]

        if not pdf:
            # check inline scripts / html for pdfUrl or .pdf
            html = await page.content()
            pdf = extract_pdf_url_from_text(html)

        if not pdf:
            # search frames for pdf links
            pdf = await extract_pdf_from_iframes(page)

        if pdf:
            pdf_dir = folder / "PDF"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            pdf_name = pdf.split("/")[-1].split("?")[0]
            pdf_path = pdf_dir / pdf_name

            if await download_pdf(page, pdf, pdf_path):
                if CONVERT_PDF_TO_PNG:
                    count = convert_pdf_to_images(pdf_path, folder / "pages")
                    print(f"   Converted PDF → {count} pages")
            return

        # index.html flipbook fallback
        html = await page.content()
        idx = await extract_flipbook_index_html(html)
        if idx:
            pages = await screenshot_flipbook_index(page, idx, folder / "pages")
            print(f"   Captured {pages} flipbook pages")
            return

        print("  No PDF or Flipbook found.")

    except Exception as e:
        print(f"  Error: {e}")

    finally:
        try:
            page.off("request", on_request)
        except Exception:
            pass
        try:
            await page.close()
        except Exception:
            pass


# ============================================================
# PROMO LIST SCRAPER (fixed to avoid thumbnail links)
# ============================================================
async def get_promos(page: Page) -> List[dict]:
    print("Loading promo list...\n")
    await page.goto(PROMO_LIST_URL, timeout=NAV_TIMEOUT)
    await page.wait_for_load_state("networkidle")

    # Primary selector: find anchors inside promo containers (anchors usually point to promo pages)
    anchors = page.locator(
        "div.promosi-item a, div.promotion-page a, div.promo-card a, a.promo-link, .promotion-list a"
    )

    count = await anchors.count()
    print(f" Found {count} promo anchors\n")

    promos: List[dict] = []

    for i in range(count):
        a = anchors.nth(i)
        try:
            href = await a.get_attribute("href")
            if not href:
                continue

            # If href points to an image (thumbnail), try to find a different anchor inside the same card
            if href.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                # inspect parent element for alternative anchors
                card = a.locator("..")  # immediate parent
                # look for other anchors in parent that don't link to uploads
                candidates = card.locator("a")
                ccount = await candidates.count()
                real_href = None
                for j in range(ccount):
                    try:
                        h2 = await candidates.nth(j).get_attribute("href")
                        if h2 and not h2.lower().startswith(("https://www.indomaret.co.id/wp-content/uploads", "http://www.indomaret.co.id/wp-content/uploads")) and not h2.lower().endswith((".png", ".jpg")):
                            real_href = h2
                            break
                    except Exception:
                        continue
                if real_href:
                    href = real_href
                else:
                    # try to scan grandparent
                    try:
                        card2 = card.locator("..")
                        candidates2 = card2.locator("a")
                        c2count = await candidates2.count()
                        for j in range(c2count):
                            try:
                                h2 = await candidates2.nth(j).get_attribute("href")
                                if h2 and not h2.lower().startswith(("https://www.indomaret.co.id/wp-content/uploads")) and not h2.lower().endswith((".png", ".jpg")):
                                    real_href = h2
                                    break
                            except Exception:
                                continue
                        if real_href:
                            href = real_href
                    except Exception:
                        pass

            if not href:
                continue

            # normalize
            if href.startswith("//"):
                href = "https:" + href
            if not href.startswith("http"):
                href = "https://www.indomaret.co.id" + href

            # determine title: prefer img alt, then anchor text, then derive from URL
            title = ""
            try:
                img = a.locator("img")
                if await img.count() > 0:
                    alt = await img.first.get_attribute("alt")
                    if alt and alt.strip():
                        title = alt.strip()
            except Exception:
                pass

            if not title:
                try:
                    inner = (await a.inner_text()).strip()
                    if inner:
                        title = inner
                except Exception:
                    title = ""

            if not title:
                # try to get title from nearby headings inside the card
                try:
                    card_parent = a.locator("..")
                    h = card_parent.locator("h2, h3, .title")
                    if await h.count() > 0:
                        title = (await h.first.inner_text()).strip()
                except Exception:
                    pass

            if not title:
                # fallback from URL path
                parts = href.rstrip("/").split("/")
                if len(parts) >= 2:
                    title = parts[-1].replace("-", " ").title()
                else:
                    title = "untitled"

            promos.append({
                "title": title,
                "link": href,
                "folder": safe(title)
            })
        except Exception:
            continue

    # dedupe by link
    seen = set()
    unique: List[dict] = []
    for p in promos:
        if p["link"] not in seen:
            unique.append(p)
            seen.add(p["link"])

    return unique


# ============================================================
# MAIN
# ============================================================
async def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    print("Welcome to Indomaret Scraper!")
    print(f"Output will be saved in: {os.path.abspath(OUTPUT_DIR)}\n")

    try:
        import fitz  # noqa: F401
    except Exception:
        if CONVERT_PDF_TO_PNG:
            print("PyMuPDF not installed — PDF → PNG disabled. Install with: pip install pymupdf")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        page = await browser.new_page()
        await block_nonflip_images(page)

        promos = await get_promos(page)
        await page.close()
        print("\n STARTING CAPTURE")
        if not promos:
            print("No promos found — if this persists, set headless=False and inspect the page manually.")
        for promo in promos:
            await capture_single(promo, browser)

        await browser.close()
        print("\n ALL PROMOS DONE!")


if __name__ == "__main__":
    asyncio.run(main())

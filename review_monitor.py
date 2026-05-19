"""
review_monitor.py
Scraper bad reviews (bintang 1-3) dari Booking.com dan Agoda
untuk 4 hotel Verse Hotels Group
v2 - improved selectors + debug output
"""

import json
import re
import os
import time
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.sync_api import sync_playwright

# ── Konfigurasi ──────────────────────────────────────────────
BAD_REVIEW_FILE  = "bad_reviews.json"
PREV_SEEN_FILE   = "seen_reviews.json"
LOG_FILE         = "monitor_log.txt"
DEBUG_FILE       = "debug_cards.txt"
MAX_REVIEWS_PER  = 10
BAD_STAR_MAX     = 3

NOTIFY_EMAIL     = "coo.versehotels@gmail.com"
SENDER_EMAIL     = "coo.versehotels@gmail.com"
SENDER_PASSWORD  = os.environ.get("GMAIL_APP_PASSWORD", "")

HOTELS = {
    "Verse Lite Gajah Mada": {
        "booking": "https://www.booking.com/hotel/id/verse-lite-pembangunan.id.html#tab-reviews",
        "agoda":   "https://www.agoda.com/verse-lite-hotel-gajah-mada/reviews/jakarta-id.html",
    },
    "Verse Luxe Wahid Hasyim": {
        "booking": "https://www.booking.com/hotel/id/verse-luxe-wahid-hasyim.id.html#tab-reviews",
        "agoda":   "https://www.agoda.com/verse-luxe-hotel-wahid-hasyim/reviews/jakarta-id.html",
    },
    "Verse Cirebon": {
        "booking": "https://www.booking.com/hotel/id/verse-cirebon.id.html#tab-reviews",
        "agoda":   "https://www.agoda.com/verse-hotel-cirebon/reviews/cirebon-id.html",
    },
    "Oak Tree Mahakam Blok M": {
        "booking": "https://www.booking.com/hotel/id/oak-tree-urban.id.html#tab-reviews",
        "agoda":   "https://www.agoda.com/oak-tree-urban-hotel/reviews/jakarta-id.html",
    },
}

# ── Helper ───────────────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def debug_write(hotel, platform, card_index, raw_text, raw_html=""):
    try:
        with open(DEBUG_FILE, "a", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write(f"HOTEL: {hotel} | PLATFORM: {platform} | CARD #{card_index}\n")
            f.write("-" * 40 + "\n")
            f.write("TEXT:\n")
            f.write((raw_text or "(empty)")[:1500])
            f.write("\n" + "-" * 40 + "\n")
            f.write("HTML:\n")
            f.write((raw_html or "(empty)")[:1500])
            f.write("\n\n")
    except Exception:
        pass

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def make_review_id(hotel, platform, reviewer, comment):
    raw = f"{hotel}|{platform}|{reviewer}|{comment[:50]}"
    return str(abs(hash(raw)))

def normalize(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()

def extract_star_from_text(text):
    """Coba baca bintang/score dari teks"""
    if not text:
        return None, None
    # Format X/10
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*/\s*10", text)
    if m:
        try:
            score = float(m.group(1).replace(",", "."))
            return round(score / 2), score
        except Exception:
            pass
    # Format X/5
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*/\s*5", text)
    if m:
        try:
            score = float(m.group(1).replace(",", "."))
            if 1 <= score <= 5:
                return int(round(score)), score
        except Exception:
            pass
    # Angka tunggal
    m = re.search(r"\b(\d(?:[.,]\d)?)\b", text)
    if m:
        try:
            score = float(m.group(1).replace(",", "."))
            if 1 <= score <= 5:
                return int(round(score)), score
            elif 2 <= score <= 10:
                return round(score / 2), score
        except Exception:
            pass
    return None, None

# ── Scraper Booking.com ──────────────────────────────────────
def scrape_booking_reviews(page, hotel_name, url):
    results = []
    try:
        log(f"  Booking.com -> {hotel_name}")
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)

        for pos in [300, 800, 1400, 2000, 2800, 3500]:
            try:
                page.evaluate(f"window.scrollTo(0, {pos})")
                page.wait_for_timeout(500)
            except Exception:
                pass

        selectors = [
            '[data-testid="review-card"]',
            '.c-review-block',
            '.review_list_new_item_block',
            '.review_item',
            '[class*="ReviewCard"]',
            '[class*="review-card"]',
            'li[data-review-id]',
        ]

        review_cards = []
        used_sel = ""
        for sel in selectors:
            cards = page.query_selector_all(sel)
            if cards:
                log(f"    Selector '{sel}' -> {len(cards)} cards")
                review_cards = cards
                used_sel = sel
                break

        if not review_cards:
            log(f"    Tidak ada card — simpan page text ke debug")
            full_text = normalize(page.inner_text("body"))
            debug_write(hotel_name, "Booking.com", 0, full_text[:3000])
            return results

        for i, card in enumerate(review_cards[:MAX_REVIEWS_PER]):
            try:
                raw_text = normalize(card.inner_text())
                raw_html = card.inner_html()
                debug_write(hotel_name, "Booking.com", i+1, raw_text, raw_html[:800])

                star = None
                score = None

                # Method 1: data-testid
                for sel in ['[data-testid="review-score"]', '[data-testid="review-score-badge"]']:
                    el = card.query_selector(sel)
                    if el:
                        star, score = extract_star_from_text(normalize(el.inner_text()))
                        if star:
                            break

                # Method 2: class-based
                if star is None:
                    for cls in ['bui-review-score__badge', 'review-score-badge',
                                'bui-score', 'c-score', 'score-badge']:
                        el = card.query_selector(f'[class*="{cls}"]')
                        if el:
                            star, score = extract_star_from_text(normalize(el.inner_text()))
                            if star:
                                break

                # Method 3: count filled stars
                if star is None:
                    for star_sel in ['[class*="star"][class*="fill"]',
                                     'svg[aria-label*="star"]',
                                     '[class*="filled"]']:
                        els = card.query_selector_all(star_sel)
                        if els:
                            star = len(els)
                            break

                # Method 4: regex dari raw text
                if star is None:
                    star, score = extract_star_from_text(raw_text[:300])

                log(f"    Card #{i+1}: star={star} score={score} | {raw_text[:70]}")

                reviewer = "Tamu"
                for sel in ['[data-testid="review-author"]', '.bui-avatar-block__title',
                            '.reviewer_name', '[class*="reviewer-name"]']:
                    el = card.query_selector(sel)
                    if el:
                        t = normalize(el.inner_text())
                        if t:
                            reviewer = t
                            break

                comment = ""
                for sel in ['[data-testid="review-negative"]', '[data-testid="review-body"]',
                            '.review_neg', '[class*="review-body"]', '[class*="ReviewBody"]']:
                    el = card.query_selector(sel)
                    if el:
                        t = normalize(el.inner_text())
                        if t:
                            comment = t
                            break
                if not comment:
                    comment = raw_text[:300]

                date_text = ""
                for sel in ['[data-testid="review-date"]', '.c-review-block__date',
                            '[class*="review-date"]', '[class*="ReviewDate"]']:
                    el = card.query_selector(sel)
                    if el:
                        t = normalize(el.inner_text())
                        if t:
                            date_text = t
                            break

                if star and 1 <= star <= BAD_STAR_MAX:
                    results.append({
                        "hotel": hotel_name, "platform": "Booking.com",
                        "reviewer": reviewer, "star": star, "score": score,
                        "comment": comment[:300], "date": date_text, "url": url,
                    })
                    log(f"    BAD REVIEW FOUND: {reviewer} | bintang={star} | {comment[:60]}")

            except Exception as e:
                log(f"    Error card #{i+1}: {str(e)[:80]}")

    except Exception as e:
        log(f"  ERROR Booking {hotel_name}: {str(e)[:100]}")

    log(f"    Bad reviews: {len(results)}")
    return results


# ── Scraper Agoda ────────────────────────────────────────────
def scrape_agoda_reviews(page, hotel_name, url):
    results = []
    try:
        log(f"  Agoda -> {hotel_name}")
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)

        for pos in [500, 1200, 2000, 3000, 4000]:
            try:
                page.evaluate(f"window.scrollTo(0, {pos})")
                page.wait_for_timeout(600)
            except Exception:
                pass

        selectors = [
            '[data-selenium="review-item"]',
            '[class*="ReviewItem"]',
            '[class*="review-item"]',
            '[data-element-name="review-card"]',
            '.review-comment',
            '[class*="Review_"]',
        ]

        review_cards = []
        for sel in selectors:
            cards = page.query_selector_all(sel)
            if cards:
                log(f"    Selector '{sel}' -> {len(cards)} cards")
                review_cards = cards
                break

        if not review_cards:
            log(f"    Tidak ada card — simpan page text ke debug")
            full_text = normalize(page.inner_text("body"))
            debug_write(hotel_name, "Agoda", 0, full_text[:3000])
            return results

        for i, card in enumerate(review_cards[:MAX_REVIEWS_PER]):
            try:
                raw_text = normalize(card.inner_text())
                raw_html = card.inner_html()
                debug_write(hotel_name, "Agoda", i+1, raw_text, raw_html[:800])

                star = None
                score = None

                for sel in ['[data-selenium="review-score"]', '[class*="score"]',
                            '[class*="Score"]', '[class*="rating"]', '[class*="Rating"]']:
                    el = card.query_selector(sel)
                    if el:
                        star, score = extract_star_from_text(normalize(el.inner_text()))
                        if star:
                            break

                if star is None:
                    star, score = extract_star_from_text(raw_text[:200])

                log(f"    Card #{i+1}: star={star} score={score} | {raw_text[:70]}")

                reviewer = "Tamu"
                for sel in ['[data-selenium="reviewer-name"]', '[class*="reviewer"]',
                            '[class*="Reviewer"]', '[class*="traveler"]']:
                    el = card.query_selector(sel)
                    if el:
                        t = normalize(el.inner_text())
                        if t:
                            reviewer = t
                            break

                comment = ""
                for sel in ['[data-selenium="review-comment"]', '[class*="comment"]',
                            '[class*="Comment"]', '[class*="review-text"]', '[class*="body"]']:
                    el = card.query_selector(sel)
                    if el:
                        t = normalize(el.inner_text())
                        if t:
                            comment = t
                            break
                if not comment:
                    comment = raw_text[:300]

                date_text = ""
                for sel in ['[data-selenium="review-date"]', '[class*="date"]', '[class*="Date"]']:
                    el = card.query_selector(sel)
                    if el:
                        t = normalize(el.inner_text())
                        if t:
                            date_text = t
                            break

                if star and 1 <= star <= BAD_STAR_MAX:
                    results.append({
                        "hotel": hotel_name, "platform": "Agoda",
                        "reviewer": reviewer, "star": star, "score": score,
                        "comment": comment[:300], "date": date_text, "url": url,
                    })
                    log(f"    BAD REVIEW FOUND: {reviewer} | bintang={star} | {comment[:60]}")

            except Exception as e:
                log(f"    Error card #{i+1}: {str(e)[:80]}")

    except Exception as e:
        log(f"  ERROR Agoda {hotel_name}: {str(e)[:100]}")

    log(f"    Bad reviews: {len(results)}")
    return results


# ── Filter baru ──────────────────────────────────────────────
def filter_new_reviews(all_reviews, seen):
    new_reviews = []
    for r in all_reviews:
        rid = make_review_id(r["hotel"], r["platform"], r["reviewer"], r["comment"])
        if rid not in seen:
            r["_id"] = rid
            new_reviews.append(r)
    return new_reviews


# ── Kirim Email ──────────────────────────────────────────────
def send_email(new_reviews):
    if not new_reviews or not SENDER_PASSWORD:
        if not SENDER_PASSWORD:
            log("ERROR: GMAIL_APP_PASSWORD tidak ditemukan!")
        return

    total = len(new_reviews)
    hotels_affected = list(set(r["hotel"] for r in new_reviews))
    subject = f"ALERT: {total} Bad Review Baru — Verse Hotels ({datetime.now().strftime('%d %b %Y %H:%M')} WIB)"

    rows_html = ""
    for r in new_reviews:
        stars = "★" * int(r["star"]) if r.get("star") else "—"
        rows_html += f"""
        <tr>
            <td style="padding:10px;border:1px solid #ddd;font-weight:bold">{r['hotel']}</td>
            <td style="padding:10px;border:1px solid #ddd">{r['platform']}</td>
            <td style="padding:10px;border:1px solid #ddd;color:#e74c3c;font-size:18px">{stars}</td>
            <td style="padding:10px;border:1px solid #ddd">{r.get('reviewer','—')}</td>
            <td style="padding:10px;border:1px solid #ddd;font-style:italic">"{r['comment'][:200]}"</td>
            <td style="padding:10px;border:1px solid #ddd;font-size:12px">{r.get('date','—')}</td>
        </tr>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;color:#333">
    <div style="max-width:900px;margin:auto">
        <div style="background:#c0392b;color:white;padding:20px;border-radius:8px 8px 0 0">
            <h2 style="margin:0">⚠️ Bad Review Alert — Verse Hotels Group</h2>
            <p style="margin:5px 0 0">{datetime.now().strftime('%d %B %Y, %H:%M')} WIB</p>
        </div>
        <div style="background:#fff3f3;padding:15px;border:1px solid #e74c3c">
            <strong>{total} bad review baru</strong> ditemukan di: {', '.join(hotels_affected)}
        </div>
        <table style="width:100%;border-collapse:collapse;margin-top:15px">
            <thead>
                <tr style="background:#2c3e50;color:white">
                    <th style="padding:10px;text-align:left">Hotel</th>
                    <th style="padding:10px;text-align:left">Platform</th>
                    <th style="padding:10px;text-align:left">Rating</th>
                    <th style="padding:10px;text-align:left">Tamu</th>
                    <th style="padding:10px;text-align:left">Komentar</th>
                    <th style="padding:10px;text-align:left">Tanggal</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>
        <div style="margin-top:20px;padding:15px;background:#f8f9fa;border-radius:4px;font-size:12px;color:#666">
            Email otomatis — Review Monitor Verse Hotels Group
        </div>
    </div>
    </body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SENDER_EMAIL
        msg["To"]      = NOTIFY_EMAIL
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, NOTIFY_EMAIL, msg.as_string())
        log(f"Email terkirim -> {total} bad review")
    except Exception as e:
        log(f"ERROR kirim email: {str(e)}")


# ── Main ─────────────────────────────────────────────────────
def main():
    try:
        with open(DEBUG_FILE, "w", encoding="utf-8") as f:
            f.write(f"# DEBUG CARDS — {datetime.now()}\n\n")
    except Exception:
        pass

    log("=" * 60)
    log("REVIEW MONITOR v2 — START")
    log("=" * 60)

    seen = load_json(PREV_SEEN_FILE, {})
    all_bad_reviews = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
        ])
        context = browser.new_context(
            locale="id-ID",
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3,4,5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['id-ID','id','en-US','en']});
            window.chrome = {runtime: {}};
        """)
        page = context.new_page()
        page.set_extra_http_headers({"Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"})

        for hotel_name, sources in HOTELS.items():
            log(f"\nHotel: {hotel_name}")
            all_bad_reviews.extend(scrape_booking_reviews(page, hotel_name, sources["booking"]))
            time.sleep(4)
            all_bad_reviews.extend(scrape_agoda_reviews(page, hotel_name, sources["agoda"]))
            time.sleep(4)

        context.close()
        browser.close()

    new_reviews = filter_new_reviews(all_bad_reviews, seen)
    log(f"\nTotal bad reviews : {len(all_bad_reviews)}")
    log(f"Bad reviews BARU  : {len(new_reviews)}")

    for r in new_reviews:
        seen[r["_id"]] = {"hotel": r["hotel"], "platform": r["platform"], "seen_at": str(datetime.now())}
    if len(seen) > 500:
        keys = list(seen.keys())
        for k in keys[:-500]:
            del seen[k]

    save_json(PREV_SEEN_FILE, seen)
    save_json(BAD_REVIEW_FILE, {
        "last_update": str(datetime.now()),
        "total_bad_reviews": len(all_bad_reviews),
        "new_this_run": len(new_reviews),
        "bad_reviews": all_bad_reviews,
        "new_reviews": new_reviews,
    })

    if new_reviews:
        send_email(new_reviews)
    else:
        log("Tidak ada bad review baru — email tidak dikirim")

    log("\nREVIEW MONITOR v2 — SELESAI")


if __name__ == "__main__":
    main()

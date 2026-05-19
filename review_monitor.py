"""
review_monitor.py
Scraper bad reviews (bintang 1-3) dari Booking.com dan Agoda
untuk 4 hotel Verse Hotels Group
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
MAX_REVIEWS_PER  = 10   # Ambil maksimal 10 review terbaru per hotel per platform
BAD_STAR_MAX     = 3    # Bintang 1, 2, 3 dianggap bad review

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
    """Buat ID unik untuk setiap review agar tidak duplikat"""
    raw = f"{hotel}|{platform}|{reviewer}|{comment[:50]}"
    return str(abs(hash(raw)))

def normalize(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()

# ── Scraper Booking.com ──────────────────────────────────────
def scrape_booking_reviews(page, hotel_name, url):
    results = []
    try:
        log(f"  Booking.com → {hotel_name}")
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        # Klik filter "Negatif" jika ada
        try:
            neg_btn = page.query_selector('button[data-testid="review-score-filter-negative"]')
            if not neg_btn:
                neg_btn = page.query_selector('a:has-text("Negatif"), a:has-text("Negative"), button:has-text("Negatif")')
            if neg_btn:
                neg_btn.click()
                page.wait_for_timeout(3000)
        except Exception:
            pass

        # Scroll untuk load review
        for pos in [500, 1000, 1500, 2000, 2500]:
            try:
                page.evaluate(f"window.scrollTo(0, {pos})")
                page.wait_for_timeout(800)
            except Exception:
                pass

        # Extract review cards
        review_cards = page.query_selector_all('[data-testid="review-card"], .c-review-block, .review_list_new_item_block')
        if not review_cards:
            review_cards = page.query_selector_all('.review_item, [class*="review"]')

        log(f"    Ditemukan {len(review_cards)} review cards")

        for card in review_cards[:MAX_REVIEWS_PER]:
            try:
                # Rating/score
                score_el = card.query_selector('[data-testid="review-score"], .bui-review-score__badge, .review-score-badge')
                score_text = normalize(score_el.inner_text()) if score_el else ""
                score = None
                m = re.search(r"(\d+[.,]\d+|\d+)", score_text)
                if m:
                    try:
                        score = float(m.group(1).replace(",", "."))
                        # Booking pakai skala 10, konversi ke bintang 1-5
                        star = round(score / 2)
                    except Exception:
                        star = None
                else:
                    star = None

                # Kalau tidak ada score, cek elemen bintang
                if star is None:
                    star_els = card.query_selector_all('.bk-icon-stars-filled, [class*="star-filled"]')
                    star = len(star_els) if star_els else None

                # Reviewer name
                name_el = card.query_selector('[data-testid="review-author"], .bui-avatar-block__title, .reviewer_name')
                reviewer = normalize(name_el.inner_text()) if name_el else "Tamu"

                # Komentar negatif
                neg_el = card.query_selector('[data-testid="review-negative"], .review_neg, .c-review__body--negative')
                neg_text = normalize(neg_el.inner_text()) if neg_el else ""

                # Komentar positif (fallback)
                pos_el = card.query_selector('[data-testid="review-positive"], .review_pos, .c-review__body--positive')
                pos_text = normalize(pos_el.inner_text()) if pos_el else ""

                comment = neg_text or pos_text
                if not comment:
                    body_el = card.query_selector('.review_body, [class*="review-body"]')
                    comment = normalize(body_el.inner_text()) if body_el else ""

                # Tanggal
                date_el = card.query_selector('[data-testid="review-date"], .c-review-block__date, .review_item_date')
                date_text = normalize(date_el.inner_text()) if date_el else ""

                if star and star <= BAD_STAR_MAX and comment:
                    results.append({
                        "hotel":     hotel_name,
                        "platform":  "Booking.com",
                        "reviewer":  reviewer,
                        "star":      star,
                        "score":     score,
                        "comment":   comment[:300],
                        "date":      date_text,
                        "url":       url,
                    })

            except Exception as e:
                log(f"    Error card: {str(e)[:60]}")
                continue

    except Exception as e:
        log(f"  ERROR Booking.com {hotel_name}: {str(e)[:80]}")

    log(f"    Bad reviews ditemukan: {len(results)}")
    return results


# ── Scraper Agoda ────────────────────────────────────────────
def scrape_agoda_reviews(page, hotel_name, url):
    results = []
    try:
        log(f"  Agoda → {hotel_name}")
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        # Coba filter review negatif / sort by lowest
        try:
            sort_els = page.query_selector_all('select[name*="sort"], [class*="sort"] option, button:has-text("Sort")')
            # Cari opsi "Lowest rated" atau "Paling rendah"
            lowest = page.query_selector('option:has-text("Lowest"), option:has-text("Paling rendah"), option:has-text("Terendah")')
            if lowest:
                lowest.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass

        # Scroll
        for pos in [500, 1000, 1500, 2000, 2500, 3000]:
            try:
                page.evaluate(f"window.scrollTo(0, {pos})")
                page.wait_for_timeout(700)
            except Exception:
                pass

        # Extract review items
        review_cards = page.query_selector_all('[class*="ReviewItem"], [class*="review-item"], [data-element-name="review-card"]')
        if not review_cards:
            review_cards = page.query_selector_all('[class*="Review_"], [class*="reviewCard"]')

        log(f"    Ditemukan {len(review_cards)} review cards")

        for card in review_cards[:MAX_REVIEWS_PER]:
            try:
                # Rating (Agoda pakai skala 10)
                score_el = card.query_selector('[class*="score"], [class*="Score"], [class*="rating"], [class*="Rating"]')
                score_text = normalize(score_el.inner_text()) if score_el else ""
                score = None
                star = None
                m = re.search(r"(\d+[.,]\d+|\d+)", score_text)
                if m:
                    try:
                        score = float(m.group(1).replace(",", "."))
                        if score <= 10:
                            star = round(score / 2)
                        else:
                            star = None
                    except Exception:
                        pass

                # Reviewer name
                name_el = card.query_selector('[class*="reviewer"], [class*="Reviewer"], [class*="name"], [class*="Name"]')
                reviewer = normalize(name_el.inner_text()) if name_el else "Tamu"

                # Komentar
                comment_el = card.query_selector('[class*="comment"], [class*="Comment"], [class*="text"], [class*="Text"], [class*="body"]')
                comment = normalize(comment_el.inner_text()) if comment_el else ""

                # Fallback: ambil semua teks dari card
                if not comment:
                    comment = normalize(card.inner_text())[:300]

                # Tanggal
                date_el = card.query_selector('[class*="date"], [class*="Date"]')
                date_text = normalize(date_el.inner_text()) if date_el else ""

                if star and star <= BAD_STAR_MAX and comment:
                    results.append({
                        "hotel":     hotel_name,
                        "platform":  "Agoda",
                        "reviewer":  reviewer,
                        "star":      star,
                        "score":     score,
                        "comment":   comment[:300],
                        "date":      date_text,
                        "url":       url,
                    })

            except Exception as e:
                log(f"    Error card: {str(e)[:60]}")
                continue

    except Exception as e:
        log(f"  ERROR Agoda {hotel_name}: {str(e)[:80]}")

    log(f"    Bad reviews ditemukan: {len(results)}")
    return results


# ── Filter review baru (belum pernah dikirim) ────────────────
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
    if not new_reviews:
        return
    if not SENDER_PASSWORD:
        log("ERROR: GMAIL_APP_PASSWORD tidak ditemukan di environment!")
        return

    total = len(new_reviews)
    hotels_affected = list(set(r["hotel"] for r in new_reviews))

    subject = f"⚠️ ALERT: {total} Bad Review Baru — Verse Hotels ({datetime.now().strftime('%d %b %Y %H:%M')} WIB)"

    # Build HTML email
    rows_html = ""
    for r in new_reviews:
        stars = "⭐" * r["star"] if r.get("star") else "—"
        rows_html += f"""
        <tr>
            <td style="padding:10px;border:1px solid #ddd;font-weight:bold">{r['hotel']}</td>
            <td style="padding:10px;border:1px solid #ddd">{r['platform']}</td>
            <td style="padding:10px;border:1px solid #ddd;color:#e74c3c">{stars} ({r.get('score','—')})</td>
            <td style="padding:10px;border:1px solid #ddd">{r.get('reviewer','—')}</td>
            <td style="padding:10px;border:1px solid #ddd;font-style:italic">"{r['comment'][:200]}..."</td>
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
            Email ini dikirim otomatis oleh sistem Review Monitor — Verse Hotels Group<br>
            Scraping dilakukan setiap 2 jam sekali.
        </div>
    </div>
    </body></html>
    """

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = SENDER_EMAIL
        msg["To"]      = NOTIFY_EMAIL
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, NOTIFY_EMAIL, msg.as_string())

        log(f"✅ Email terkirim ke {NOTIFY_EMAIL} — {total} bad review")
    except Exception as e:
        log(f"ERROR kirim email: {str(e)}")


# ── Main ─────────────────────────────────────────────────────
def main():
    log("=" * 60)
    log("REVIEW MONITOR — START")
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
        page.set_extra_http_headers({
            "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        })

        for hotel_name, sources in HOTELS.items():
            log(f"\nHotel: {hotel_name}")

            # Booking.com
            booking_reviews = scrape_booking_reviews(page, hotel_name, sources["booking"])
            all_bad_reviews.extend(booking_reviews)
            time.sleep(5)

            # Agoda
            agoda_reviews = scrape_agoda_reviews(page, hotel_name, sources["agoda"])
            all_bad_reviews.extend(agoda_reviews)
            time.sleep(5)

        context.close()
        browser.close()

    # Filter hanya review baru
    new_reviews = filter_new_reviews(all_bad_reviews, seen)
    log(f"\nTotal bad reviews ditemukan : {len(all_bad_reviews)}")
    log(f"Bad reviews BARU (belum dikirim): {len(new_reviews)}")

    # Update seen reviews
    for r in new_reviews:
        seen[r["_id"]] = {
            "hotel":    r["hotel"],
            "platform": r["platform"],
            "date":     r.get("date", ""),
            "seen_at":  str(datetime.now()),
        }

    # Batasi seen tidak terlalu besar (simpan 500 terakhir)
    if len(seen) > 500:
        keys = list(seen.keys())
        for old_key in keys[:-500]:
            del seen[old_key]

    save_json(PREV_SEEN_FILE, seen)

    # Simpan semua bad reviews ke file
    output = {
        "last_update": str(datetime.now()),
        "total_bad_reviews": len(all_bad_reviews),
        "new_this_run": len(new_reviews),
        "bad_reviews": all_bad_reviews,
        "new_reviews": new_reviews,
    }
    save_json(BAD_REVIEW_FILE, output)

    # Kirim email kalau ada yang baru
    if new_reviews:
        send_email(new_reviews)
    else:
        log("Tidak ada bad review baru — email tidak dikirim")

    log("\nREVIEW MONITOR — SELESAI")


if __name__ == "__main__":
    main()

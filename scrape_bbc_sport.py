"""
BBC Sport Scraper  —  在 Colab 執行
用法：python scrape_bbc_sport.py
輸出：./my_dataset/bbc_sport_documents.jsonl
      ./my_dataset/bbc_sport_eval_set.json
"""

import json, re, time, random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────────────────
TARGET_COUNT  = 20
DAYS_BACK     = 30
RANDOM_SEED   = 42
OUT_DIR       = Path("./my_dataset")
JSONL_PATH    = OUT_DIR / "bbc_sport_documents.jsonl"
EVAL_PATH     = OUT_DIR / "bbc_sport_eval_set.json"
REQUEST_DELAY = 1.5
TIMEOUT       = 15

RSS_FEEDS = {
    "football":   "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "cricket":    "https://feeds.bbci.co.uk/sport/cricket/rss.xml",
    "tennis":     "https://feeds.bbci.co.uk/sport/tennis/rss.xml",
    "formula1":   "https://feeds.bbci.co.uk/sport/formula1/rss.xml",
    "rugby-union":"https://feeds.bbci.co.uk/sport/rugby-union/rss.xml",
    "golf":       "https://feeds.bbci.co.uk/sport/golf/rss.xml",
    "athletics":  "https://feeds.bbci.co.uk/sport/athletics/rss.xml",
    "cycling":    "https://feeds.bbci.co.uk/sport/cycling/rss.xml",
    "boxing":     "https://feeds.bbci.co.uk/sport/boxing/rss.xml",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

random.seed(RANDOM_SEED)
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Step 1: RSS → candidate links ──────────────────────────────────
def fetch_rss(days=DAYS_BACK):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    candidates, seen = [], set()
    for sport, url in RSS_FEEDS.items():
        print(f"  RSS [{sport}] ...", end=" ", flush=True)
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"FAILED ({e})"); continue
        count = 0
        for item in root.iter("item"):
            title_el = item.find("title")
            link_el  = item.find("link")
            pub_el   = item.find("pubDate")
            if not title_el or not link_el: continue
            url_art = (link_el.text or "").strip()
            title   = (title_el.text or "").strip()
            pub_dt  = None
            if pub_el and pub_el.text:
                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(pub_el.text.strip())
                    if not pub_dt.tzinfo:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                except Exception: pass
            if pub_dt and pub_dt < cutoff: continue
            if not url_art or url_art in seen: continue
            if not re.search(r"bbc\.(co\.uk|com)/sport/", url_art): continue
            seen.add(url_art)
            candidates.append({
                "url": url_art, "title": title, "sport": sport,
                "pub_date": pub_dt.isoformat() if pub_dt else None,
            })
            count += 1
        print(f"{count}")
    print(f"  Total candidates: {len(candidates)}")
    return candidates

# ── Step 2: fetch & parse article ──────────────────────────────────
def parse_article(item, session):
    try:
        r = session.get(item["url"], timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"    FETCH ERROR: {e}"); return None
    soup = BeautifulSoup(r.text, "lxml")
    # title
    title = ""
    for sel in ["h1[id='main-heading']", "h1.article-headline__text", "h1"]:
        el = soup.select_one(sel)
        if el: title = el.get_text(strip=True); break
    if not title: title = item["title"]
    # date
    date_str = item.get("pub_date", "")[:10]
    for tag, attrs in [("meta",{"property":"article:published_time"}),
                       ("meta",{"name":"article:published_time"}), ("time",{})]:
        el = soup.find(tag, attrs)
        if el:
            v = el.get("content","") or el.get("datetime","") or el.get_text(strip=True)
            if v: date_str = v[:10]; break
    # body
    paragraphs = []
    for sel in ["div[data-component='text-block'] p",
                "article div.article__body-content p",
                "div.story-body__inner p"]:
        found = soup.select(sel)
        if found:
            paragraphs = [p.get_text(strip=True) for p in found if p.get_text(strip=True)]
            if paragraphs: break
    if not paragraphs:
        main = soup.find("main")
        if main:
            paragraphs = [p.get_text(strip=True) for p in main.find_all("p")
                          if len(p.get_text(strip=True)) > 40]
    if not paragraphs:
        print(f"    SKIP (no body)"); return None
    # tags
    tags = [el.get_text(strip=True)
            for el in soup.select("a[data-testid='topic-tag'],ul.tags-list a")]
    if not tags: tags = [item["sport"].replace("-"," ").title()]
    return {
        "title": title, "date": date_str,
        "body": "\n\n".join(paragraphs), "tags": tags,
        "url": item["url"], "sport": item["sport"],
    }

# ── Step 3: build JSONL text ────────────────────────────────────────
def build_text(art):
    parts = []
    if art["title"]: parts.append(art["title"] + ".")
    if art["date"]:  parts.append(f"Published: {art['date']}.")
    if art["tags"]:  parts.append(f"Topics: {', '.join(art['tags'][:5])}.")
    parts.append(art["body"])
    return " ".join(parts)

# ── Step 4: generate QA (3 per article) ────────────────────────────
SPORT_DOMAIN = {
    "football":          "association football (soccer)",
    "cricket":           "cricket",
    "tennis":            "tennis",
    "formula1":          "Formula 1 motor racing",
    "rugby-union":       "rugby union",
    "golf":              "golf",
    "athletics":         "athletics (track and field)",
    "cycling":           "professional road cycling",
    "boxing":            "professional boxing",
    "swimming":          "competitive swimming",
    "commonwealth-games":"multi-sport international competition",
}

def first_sentence(text):
    body = re.sub(r'^.*?Topics:.*?\.', '', text, flags=re.DOTALL).strip()
    m = re.search(r'[.!?]', body)
    return body[:m.start()+1].strip() if m and m.start() > 20 else body[:200]

def make_qa(art):
    title  = art["title"]; date = art["date"]
    sport  = art["sport"]; tags = art["tags"]; url = art["url"]
    body1  = first_sentence(build_text(art))
    domain = SPORT_DOMAIN.get(sport, sport)
    main_tag = tags[1] if len(tags) > 1 else tags[0]
    return [
        {"question": f'According to the BBC Sport article titled "{title}", what happened?',
         "reference_answer": body1,
         "category": "factual_event", "sport": sport, "date": date, "url": url},
        {"question": f'Which athlete or team is the primary focus of the BBC Sport article "{title}"?',
         "reference_answer": main_tag,
         "category": "factual_entity", "sport": sport, "date": date, "url": url},
        {"question": (f'Based on the BBC Sport article "{title}", '
                      f'what sport does the story belong to and what are its broader implications?'),
         "reference_answer": (f"The article belongs to {domain}. "
                               f"Reported on {date}, covering: {', '.join(tags[:3])}."),
         "category": "inferential_implication", "sport": sport, "date": date, "url": url},
    ]

# ── Main ────────────────────────────────────────────────────────────
print("=" * 60)
print("BBC Sport Scraper")
print(f"  Target : {TARGET_COUNT} articles from last {DAYS_BACK} days")
print("=" * 60)

print("\n[Step 1] Fetching RSS feeds...")
candidates = fetch_rss()

# sample with sport diversity
if len(candidates) > TARGET_COUNT:
    selected = random.sample(candidates, TARGET_COUNT)
    for ms in set(RSS_FEEDS) - {s["sport"] for s in selected}:
        pool = [c for c in candidates if c["sport"] == ms and c not in selected]
        if pool:
            swap = random.choice([s for s in selected
                                   if s["sport"] not in (set(RSS_FEEDS) - {s2["sport"] for s2 in selected})])
            selected.remove(swap); selected.append(random.choice(pool))
else:
    selected = candidates

print(f"\n[Step 2] Scraping {len(selected)} articles...")
session = requests.Session(); session.headers.update(HEADERS)
articles = []
for i, item in enumerate(selected, 1):
    print(f"  [{i:2d}/{len(selected)}] {item['title'][:65]}")
    art = parse_article(item, session)
    if art:
        articles.append(art)
        print(f"         ✓ {len(art['body'].split())} words")
    time.sleep(REQUEST_DELAY)

print(f"\n  Done: {len(articles)} articles scraped")

print(f"\n[Step 3] Writing {JSONL_PATH} ...")
with open(JSONL_PATH, "w", encoding="utf-8") as f:
    for idx, art in enumerate(articles):
        record = {"id": idx, "text": build_text(art),
                  "title": art["title"], "url": art["url"],
                  "date": art["date"], "sport": art["sport"], "tags": art["tags"]}
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

print(f"\n[Step 4] Generating eval set...")
all_qa = []
for art in articles:
    # re-read from jsonl to use the same text format
    all_qa.extend(make_qa(art))
with open(EVAL_PATH, "w", encoding="utf-8") as f:
    json.dump(all_qa, f, ensure_ascii=False, indent=2)

print(f"\n{'='*60}")
print(f"✅ {JSONL_PATH}  —  {len(articles)} documents")
print(f"✅ {EVAL_PATH}   —  {len(all_qa)} QA pairs ({len(articles)}×3)")
print(f"{'='*60}")

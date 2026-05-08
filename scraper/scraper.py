import os
import json
import hashlib
import time
import httpx
from datetime import datetime, timezone
from bs4 import BeautifulSoup
import anthropic

# ============================================================
# SOURCES — Google News RSS (jamais bloqué, agrège tout)
# ============================================================
GOOGLE_NEWS_QUERIES = [
    "datacenter construction Europe 2024 2025",
    "data center hyperscale MW investment announced",
    "data centre development EMEA billion",
    "datacenter campus construction UK France Germany Spain",
    "hyperscale data center build announced",
    "colocation data center construction Europe",
    "AI data center campus investment Europe",
    "data center MW construction Africa Middle East",
]

def google_news_rss(query: str) -> str:
    q = query.replace(" ", "+")
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

KEYWORDS = [
    "data center", "datacenter", "data centre", "hyperscale",
    "colocation", " MW", "megawatt", "construction", "campus",
    "billion", "million", "develop", "invest", "build"
]

EXTRACTION_PROMPT = """You are a datacenter construction project analyst for a B2B sales intelligence tool.

Analyze the article and extract ALL datacenter construction, expansion or development projects mentioned.

For EACH project, return a JSON object:
{
  "id": "DC_<6char_hash>",
  "name": "<Owner> <City> Data Center",
  "type": "Parent",
  "value": <USD millions, 0 if unknown>,
  "region": "<Europe|Middle East and Africa|Asia Pacific|Americas>",
  "country": "<country>",
  "city": "<city>",
  "announced": "<YYYY QN>",
  "start": "<YYYY QN or empty>",
  "end": "<YYYY QN or empty>",
  "phase": "<announced|admin|construction|completed>",
  "overview": "<3-4 sentences>",
  "summaryShort": "<max 15 words>",
  "attributes": ["<tags>"],
  "momentum": <0-5>,
  "funding": "<Confirmed|Not Funded / Unconfirmed>",
  "sector": "Data Center",
  "source": "<url>",
  "contacts": [],
  "metrics": [{"p":"<name>","v":"<value>","u":"<unit>"}],
  "tenders": [],
  "updates": ["<date - update>"],
  "scraped_at": "<ISO>",
  "source_hash": "<hash>"
}

Valid attributes: Hyperscale Data Center, Colocation Data Center, AI Infrastructure, Green data center, Renewable Energy, Tier 3 Data Center, Edge Data Center, Waste Heat Recovery, Cloud Infrastructure

Rules:
- Only real construction/expansion projects with named location
- Skip: earnings, layoffs, CEO changes, product launches without construction
- EUR*1.08=USD, GBP*1.26=USD
- momentum: 0=stalled, 1-2=early, 3=planning, 4=construction, 5=fast-tracked
- Return ONLY valid JSON array, no markdown

Article URL: {url}
Article title: {title}
Article date: {date}
Text:
{text}
"""

def fetch_google_news(query: str) -> list[dict]:
    url = google_news_rss(query)
    articles = []
    try:
        r = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        for item in soup.find_all("item")[:15]:
            link = item.find("link")
            title = item.find("title")
            pub = item.find("pubDate")
            source = item.find("source")
            if link and title:
                articles.append({
                    "url": link.get_text(strip=True),
                    "title": title.get_text(strip=True),
                    "date": pub.get_text(strip=True) if pub else "",
                    "source_name": source.get_text(strip=True) if source else ""
                })
    except Exception as e:
        print(f"  Google News error for '{query}': {e}")
    return articles

def is_relevant(title: str) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in KEYWORDS)

def resolve_google_url(url: str) -> str:
    """Google News links redirect — follow to get real URL."""
    try:
        r = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        return str(r.url)
    except Exception:
        return url

def fetch_article(url: str) -> str:
    try:
        time.sleep(2)
        real_url = resolve_google_url(url)
        r = httpx.get(real_url, headers=HEADERS, timeout=25, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script","style","nav","footer","header","aside"]):
            tag.decompose()
        article = (soup.find("article") or
                   soup.find(class_=["article-body","post-content","entry-content","story-body"]) or
                   soup.find("main"))
        text = article.get_text("\n", strip=True) if article else soup.get_text("\n", strip=True)
        lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 30]
        return "\n".join(lines)[:9000]
    except Exception as e:
        print(f"    Fetch error: {e}")
        return ""

def make_hash(url: str, title: str) -> str:
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()[:8]

def extract_projects(client: anthropic.Anthropic, article: dict, text: str) -> list[dict]:
    if not text or len(text) < 150:
        return []
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=3000,
            messages=[{"role": "user", "content": EXTRACTION_PROMPT.format(
                url=article["url"],
                title=article["title"],
                date=article.get("date", ""),
                text=text
            )}]
        )
        raw = msg.content[0].text.strip()
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else raw
            if raw.startswith("json"):
                raw = raw[4:]
        projects = json.loads(raw.strip())
        if isinstance(projects, list):
            for p in projects:
                if not p.get("source_hash"):
                    p["source_hash"] = make_hash(article["url"], article["title"])
                p["scraped_at"] = datetime.now(timezone.utc).isoformat()
            return [p for p in projects if p.get("country")]  # filter empty
        return []
    except Exception as e:
        print(f"    Extract error: {e}")
        return []

def load_existing(path: str) -> list[dict]:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def merge(existing: list, new_list: list) -> tuple[list, int]:
    seen_hashes = {p.get("source_hash","") for p in existing}
    seen_names = {p.get("name","").lower()[:50] for p in existing}
    added = 0
    merged = list(existing)
    for p in new_list:
        h = p.get("source_hash","")
        name = p.get("name","").lower()[:50]
        if (h and h in seen_hashes) or (name and name in seen_names):
            continue
        merged.append(p)
        seen_hashes.add(h)
        seen_names.add(name)
        added += 1
    return merged, added

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key)

    data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    os.makedirs(data_dir, exist_ok=True)
    data_path = os.path.join(data_dir, "projects.json")
    meta_path = os.path.join(data_dir, "meta.json")

    existing = load_existing(data_path)
    print(f"Existing: {len(existing)} projects")

    # Collect articles from Google News
    all_articles = []
    seen_urls = set()
    for query in GOOGLE_NEWS_QUERIES:
        print(f"\nQuery: {query}")
        articles = fetch_google_news(query)
        for a in articles:
            if a["url"] not in seen_urls and is_relevant(a["title"]):
                seen_urls.add(a["url"])
                all_articles.append(a)
                print(f"  + {a['title'][:70]}")
        time.sleep(1)

    print(f"\nTotal articles to process: {len(all_articles)}")

    all_new = []
    for i, article in enumerate(all_articles):
        print(f"\n[{i+1}/{len(all_articles)}] {article['title'][:65]}...")
        text = fetch_article(article["url"])
        if text:
            projects = extract_projects(client, article, text)
            print(f"  → {len(projects)} projects")
            all_new.extend(projects)
        time.sleep(1)

    merged, added = merge(existing, all_new)
    print(f"\nAdded: {added} | Total: {len(merged)}")

    merged.sort(key=lambda p: (p.get("momentum",0), p.get("scraped_at","")), reverse=True)

    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "total_projects": len(merged),
            "new_this_run": added,
            "articles_processed": len(all_articles)
        }, f, indent=2)

    print("Done.")

if __name__ == "__main__":
    main()

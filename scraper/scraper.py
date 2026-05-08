import os
import json
import hashlib
import time
import httpx
from datetime import datetime, timezone
from bs4 import BeautifulSoup
import anthropic

# ============================================================
# SOURCES — RSS feeds (non bloqués) + pages index
# ============================================================
RSS_SOURCES = [
    # DatacenterKnowledge
    "https://www.datacenterknowledge.com/rss.xml",
    "https://www.datacenterknowledge.com/feed",
    # DataCenterDynamics
    "https://www.datacenterdynamics.com/en/rss/",
    "https://www.datacenterdynamics.com/en/news/rss/",
    # DCD (Data Centre Dynamics UK)
    "https://www.datacenterdynamics.com/rss.xml",
    # Bonus sources RSS fiables
    "https://www.theregister.com/data_centre/rss",
    "https://siliconangle.com/feed/",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

KEYWORDS = [
    "data center", "datacenter", "data centre", "hyperscale",
    "colocation", "MW", "megawatt", "construction", "campus",
    "build", "develop", "invest", "billion", "million"
]

EXTRACTION_PROMPT = """You are a datacenter construction project analyst for a B2B sales intelligence tool.

Analyze the article below and extract ALL datacenter construction, expansion, or development projects mentioned.

For EACH project found, output a JSON object with this exact structure:
{
  "id": "DC_<6char_hash>",
  "name": "<Developer/Owner name> <location> Data Center <phase if known>",
  "type": "Parent",
  "value": <budget USD millions as number, 0 if unknown>,
  "region": "<Europe|Middle East and Africa|Asia Pacific|Americas>",
  "country": "<country>",
  "city": "<city or Multiple>",
  "announced": "<YYYY QN>",
  "start": "<YYYY QN or empty>",
  "end": "<YYYY QN or empty>",
  "phase": "<announced|admin|construction|completed>",
  "overview": "<3-4 sentences about the project>",
  "summaryShort": "<max 20 words>",
  "attributes": ["<relevant tags>"],
  "momentum": <0-5>,
  "funding": "<Confirmed|Not Funded / Unconfirmed>",
  "sector": "Data Center",
  "source": "<article url>",
  "contacts": [],
  "metrics": [{"p": "<name>", "v": "<value>", "u": "<unit>"}],
  "tenders": [],
  "updates": ["<date - key update>"],
  "scraped_at": "<ISO datetime>",
  "source_hash": "<md5 of url+title>"
}

Valid attributes: Hyperscale Data Center, Colocation Data Center, AI Infrastructure, Green data center, Renewable Energy, Tier 3 Data Center, Edge Data Center, Waste Heat Recovery, Cloud Infrastructure, Sustainability-Linked Finance

Rules:
- Only extract REAL construction/expansion projects with a named location
- Skip: financial results, layoffs, CEO changes, product launches without construction
- Convert EUR to USD (*1.08), GBP to USD (*1.26)
- phase: "announced" if just announced, "admin" if permits/approvals stage, "construction" if building, "completed" if done
- momentum 0=stalled, 1-2=early, 3=active planning, 4=construction, 5=fast-tracked
- Return ONLY a valid JSON array. No markdown, no explanation, no code blocks.

Article URL: {url}
Article title: {title}
Article date: {date}
Article text:
{text}
"""

def fetch_rss(url: str) -> list[dict]:
    """Fetch RSS feed and return list of {url, title, date} dicts."""
    articles = []
    try:
        r = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        items = soup.find_all("item") or soup.find_all("entry")
        for item in items[:20]:
            link = item.find("link")
            title = item.find("title")
            pub = item.find("pubDate") or item.find("published") or item.find("updated")
            if link and title:
                url_val = link.get_text(strip=True) if link.get_text(strip=True) else link.get("href", "")
                articles.append({
                    "url": url_val,
                    "title": title.get_text(strip=True),
                    "date": pub.get_text(strip=True) if pub else ""
                })
    except Exception as e:
        print(f"  RSS error {url}: {e}")
    return articles

def is_relevant(title: str) -> bool:
    """Check if article title contains datacenter keywords."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in KEYWORDS)

def fetch_article(url: str) -> str:
    """Fetch article text, return cleaned text."""
    try:
        time.sleep(1.5)  # polite delay
        r = httpx.get(url, headers=HEADERS, timeout=25, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        # Remove noise
        for tag in soup(["script", "style", "nav", "footer", "header",
                         "aside", "advertisement", "cookie", "popup"]):
            tag.decompose()
        # Try to get article body
        article = soup.find("article") or soup.find(class_=["article", "post", "content", "entry"])
        if article:
            text = article.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)
        # Clean up blank lines
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        return "\n".join(lines)[:10000]
    except Exception as e:
        print(f"    Fetch error {url}: {e}")
        return ""

def make_hash(url: str, title: str) -> str:
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()[:8]

def extract_projects(client: anthropic.Anthropic, article: dict, text: str) -> list[dict]:
    if not text.strip() or len(text) < 200:
        return []
    try:
        prompt = EXTRACTION_PROMPT.format(
            url=article["url"],
            title=article["title"],
            date=article.get("date", ""),
            text=text
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",  # cheaper model for extraction
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        # Clean possible markdown
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        projects = json.loads(raw.strip())
        if isinstance(projects, list):
            # Add source hash to each
            for p in projects:
                if not p.get("source_hash"):
                    p["source_hash"] = make_hash(article["url"], article["title"])
                if not p.get("scraped_at"):
                    p["scraped_at"] = datetime.now(timezone.utc).isoformat()
            return projects
        return []
    except json.JSONDecodeError as e:
        print(f"    JSON parse error: {e}")
        return []
    except Exception as e:
        print(f"    Extraction error: {e}")
        return []

def load_existing(path: str) -> list[dict]:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return []
    return []

def merge(existing: list[dict], new_list: list[dict]) -> tuple[list[dict], int]:
    seen_hashes = {p.get("source_hash", "") for p in existing if p.get("source_hash")}
    seen_names = {p.get("name", "").lower()[:40] for p in existing}
    added = 0
    merged = list(existing)
    for p in new_list:
        h = p.get("source_hash", "")
        name_key = p.get("name", "").lower()[:40]
        if h and h in seen_hashes:
            continue
        if name_key and name_key in seen_names:
            continue
        merged.append(p)
        seen_hashes.add(h)
        seen_names.add(name_key)
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
    print(f"Existing projects: {len(existing)}")

    # Collect articles from RSS feeds
    all_articles = []
    seen_urls = set()
    for rss_url in RSS_SOURCES:
        print(f"\nRSS: {rss_url}")
        articles = fetch_rss(rss_url)
        for a in articles:
            if a["url"] not in seen_urls and is_relevant(a["title"]):
                seen_urls.add(a["url"])
                all_articles.append(a)
        print(f"  → {len([a for a in articles if is_relevant(a['title'])])} relevant articles")

    print(f"\nTotal relevant articles to process: {len(all_articles)}")

    # Extract projects from articles
    all_new = []
    for i, article in enumerate(all_articles):
        print(f"[{i+1}/{len(all_articles)}] {article['title'][:60]}...")
        text = fetch_article(article["url"])
        if text:
            projects = extract_projects(client, article, text)
            print(f"  → {len(projects)} projects extracted")
            all_new.extend(projects)
        time.sleep(0.5)

    # Merge and save
    merged, added = merge(existing, all_new)
    print(f"\nNew projects added: {added} | Total: {len(merged)}")

    # Sort by momentum desc, then by scraped_at desc
    merged.sort(key=lambda p: (p.get("momentum", 0), p.get("scraped_at", "")), reverse=True)

    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    meta = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_projects": len(merged),
        "new_this_run": added,
        "articles_processed": len(all_articles)
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    print("Done.")

if __name__ == "__main__":
    main()

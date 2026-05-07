import os
import json
import hashlib
import httpx
from datetime import datetime
from bs4 import BeautifulSoup
import anthropic

SOURCES = [
    {
        "name": "DatacenterKnowledge",
        "urls": [
            "https://www.datacenterknowledge.com/data-center-construction",
            "https://www.datacenterknowledge.com/data-center-news",
        ]
    },
    {
        "name": "DataCenterDynamics",
        "urls": [
            "https://www.datacenterdynamics.com/en/news/",
        ]
    },
    {
        "name": "BlackridgeResearch",
        "urls": [
            "https://www.blackridgeresearch.com/blog",
        ]
    },
]

EXTRACTION_PROMPT = """
You are a datacenter project intelligence analyst.

Read the following article text and extract any datacenter construction projects mentioned.
For each project found, return a JSON object with exactly this structure:

{
  "id": "<generated unique id like DC_<hash6>>",
  "name": "<full project name>",
  "type": "Parent",
  "value": <budget in USD millions as number, 0 if unknown>,
  "region": "<Europe|Middle East and Africa|Asia Pacific|Americas>",
  "country": "<country name>",
  "city": "<city name or 'Multiple'>",
  "announced": "<YYYY QN or empty>",
  "start": "<YYYY QN or empty>",
  "end": "<YYYY QN or empty>",
  "phase": "<announced|admin|construction|completed>",
  "overview": "<2-3 sentence description>",
  "summaryShort": "<1 sentence max 20 words>",
  "attributes": ["<tag1>", "<tag2>"],
  "momentum": <0-5 integer based on how active the project seems>,
  "funding": "<Confirmed|Not Funded / Unconfirmed>",
  "sector": "Data Center",
  "source": "<article url>",
  "contacts": [],
  "metrics": [{"p": "<param name>", "v": "<value>", "u": "<unit>"}],
  "tenders": [],
  "updates": ["<date - update text>"],
  "scraped_at": "<ISO datetime>",
  "source_hash": "<hash of source url + title>"
}

Valid attributes include: Hyperscale Data Center, Colocation Data Center, AI Infrastructure, Green data center, Renewable Energy, Tier 3 Data Center, Edge Data Center, Waste Heat Recovery, Cloud Infrastructure, Sustainability-Linked Finance.

Rules:
- Only include real datacenter construction/expansion projects with a location
- Skip news about financials, layoffs, acquisitions without construction
- If budget is in EUR/GBP, convert to USD (EUR*1.08, GBP*1.26)
- Return ONLY a valid JSON array, no markdown, no explanation

Article URL: {url}
Article text:
{text}
"""

def fetch_article_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DCIntelBot/1.0)"}
    try:
        r = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return text[:12000]
    except Exception as e:
        print(f"  Error fetching {url}: {e}")
        return ""

def get_article_links(index_url: str) -> list[str]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DCIntelBot/1.0)"}
    try:
        r = httpx.get(index_url, headers=headers, timeout=20, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        links = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                from urllib.parse import urlparse
                base = urlparse(index_url)
                href = f"{base.scheme}://{base.netloc}{href}"
            if index_url.split("/")[2] in href:
                if any(kw in href.lower() for kw in ["data-center", "datacenter", "construction", "development"]):
                    links.add(href)
        return list(links)[:10]
    except Exception as e:
        print(f"  Error fetching index {index_url}: {e}")
        return []

def extract_projects_from_text(client: anthropic.Anthropic, url: str, text: str) -> list[dict]:
    if not text.strip():
        return []
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": EXTRACTION_PROMPT.format(url=url, text=text)
            }]
        )
        raw = msg.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        projects = json.loads(raw)
        if isinstance(projects, list):
            return projects
        return []
    except Exception as e:
        print(f"  Extraction error for {url}: {e}")
        return []

def load_existing(path: str) -> list[dict]:
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return []

def merge_projects(existing: list[dict], new_projects: list[dict]) -> tuple[list[dict], int]:
    existing_hashes = {p.get("source_hash", p.get("id", "")) for p in existing}
    added = 0
    merged = list(existing)
    for p in new_projects:
        h = p.get("source_hash", "")
        if h and h not in existing_hashes:
            merged.append(p)
            existing_hashes.add(h)
            added += 1
        elif not h:
            merged.append(p)
            added += 1
    return merged, added

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    data_path = os.path.join(os.path.dirname(__file__), "..", "data", "projects.json")
    existing = load_existing(data_path)
    print(f"Loaded {len(existing)} existing projects")

    all_new = []
    for source in SOURCES:
        print(f"\nSource: {source['name']}")
        for index_url in source["urls"]:
            print(f"  Index: {index_url}")
            links = get_article_links(index_url)
            print(f"  Found {len(links)} article links")
            for url in links:
                print(f"    Scraping: {url}")
                text = fetch_article_text(url)
                if text:
                    projects = extract_projects_from_text(client, url, text)
                    print(f"    Extracted {len(projects)} projects")
                    all_new.extend(projects)

    merged, added = merge_projects(existing, all_new)
    print(f"\nAdded {added} new projects. Total: {len(merged)}")

    merged.sort(key=lambda p: p.get("momentum", 0), reverse=True)

    os.makedirs(os.path.dirname(data_path), exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"Saved to {data_path}")

    meta = {
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "total_projects": len(merged),
        "new_this_run": added
    }
    meta_path = os.path.join(os.path.dirname(__file__), "..", "data", "meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print("Meta saved.")

if __name__ == "__main__":
    main()

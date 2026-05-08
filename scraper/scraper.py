import os
import json
import hashlib
import time
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse
import httpx
from bs4 import BeautifulSoup
import anthropic

# ============================================================
# CONFIG
# ============================================================
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

DC_KEYWORDS = [
    "data center", "datacenter", "data centre", "hyperscale",
    "colocation", " MW ", "megawatt", "campus", "construction",
    "billion", "million", "develop", "invest", "build", "facility"
]

# ============================================================
# GOOGLE NEWS QUERIES — projets + acteurs
# ============================================================
GOOGLE_QUERIES = [
    # Projets datacenter EMEA
    "data center construction Europe 2024 2025 MW announced",
    "hyperscale data center campus investment EMEA billion",
    "data centre development UK France Germany Spain billion",
    "AI data center campus construction Europe announced",
    "colocation data center build Europe 2025",
    # Acteurs clés
    "EPC contractor awarded data center construction Europe",
    "engineering consultant appointed data center project",
    "data center planning permission approved UK Europe",
    # Sources riches en contacts
    "site:businesswire.com data center construction",
    "site:prnewswire.com datacenter development announced",
    "site:globenewswire.com data centre investment",
]

# ============================================================
# EXTRACTION PROMPT — enrichi pour contacts et acteurs
# ============================================================
EXTRACTION_PROMPT = """You are a senior datacenter construction intelligence analyst.

Extract ALL datacenter construction, expansion or development projects from the article below.
Be aggressive — extract every project mentioned, even briefly.

For EACH project, return a JSON object. Be thorough on contacts — extract ANY company or person named:

{
  "id": "DC_<6char_hash>",
  "name": "<Developer/Owner> <City> Data Center",
  "type": "Parent",
  "value": <USD millions as number, 0 if unknown>,
  "region": "<Europe|Middle East and Africa|Asia Pacific|Americas>",
  "country": "<country name in English>",
  "city": "<city>",
  "announced": "<YYYY QN or empty>",
  "start": "<YYYY QN or empty>",
  "end": "<YYYY QN or empty>",
  "phase": "<announced|admin|construction|completed>",
  "overview": "<3-4 sentences describing the project>",
  "summaryShort": "<max 15 words>",
  "attributes": ["<relevant tags>"],
  "momentum": <0-5 integer>,
  "funding": "<Confirmed|Not Funded / Unconfirmed>",
  "sector": "Data Center",
  "source": "<article url>",
  "contacts": [
    {
      "role": "<Project Owner|Developer|EPC / Main Contractor|Engineering Design Consultant|Project Management Consultant|Planning Authority|Investor|Partner>",
      "company": "<company name>",
      "person": "<person name if mentioned, else empty>",
      "title": "<job title if mentioned, else empty>",
      "email": "",
      "tel": "",
      "linkedin": "",
      "source_verified": "article"
    }
  ],
  "metrics": [
    {"p": "<param name>", "v": "<value>", "u": "<unit>"}
  ],
  "tenders": [
    {
      "name": "<tender name>",
      "type": "<EPC|Design Build|Consultancy>",
      "issue": "<YYYY QN>",
      "award": "<YYYY QN>",
      "awardedTo": "<company if known>"
    }
  ],
  "updates": ["<date - key development>"],
  "scraped_at": "<ISO datetime>",
  "source_hash": "<hash>"
}

CONTACT EXTRACTION RULES — be very thorough:
- Extract EVERY company mentioned in relation to the project
- Role mapping: "developer/owner/client" → "Project Owner", "contractor/builder/construction" → "EPC / Main Contractor", "architect/engineer/designer" → "Engineering Design Consultant", "project manager/PM" → "Project Management Consultant", "investor/fund/capital" → "Investor"
- If a person is named (CEO, MD, director), include them with their company
- Include planning authorities if mentioned

VALID ATTRIBUTES: Hyperscale Data Center, Colocation Data Center, AI Infrastructure, Green data center, Renewable Energy, Tier 3 Data Center, Edge Data Center, Waste Heat Recovery, Cloud Infrastructure, Sustainability-Linked Finance

VALID PHASES:
- "announced": just announced, planning stage
- "admin": permits, approvals, environmental studies
- "construction": actively building
- "completed": finished

MOMENTUM (0-5):
- 0: stalled/cancelled
- 1-2: early announcement, no funding
- 3: funded, in planning/admin
- 4: under construction
- 5: fast-tracked, ahead of schedule

CURRENCY: EUR*1.08=USD, GBP*1.26=USD

Return ONLY a valid JSON array. No markdown, no explanation, no code blocks.
If no datacenter projects found, return []

Article URL: {url}
Article title: {title}
Article date: {date}
Article text:
{text}
"""

# ============================================================
# COMPANY ENRICHMENT PROMPT
# ============================================================
ENRICH_PROMPT = """You are a business intelligence analyst specializing in the datacenter construction industry.

For the company "{company}" which is a {role} on a datacenter project in {country}:

Search your knowledge to provide:
1. The company's full official name
2. Known senior contacts in datacenter/construction (name, title) — only if you are confident they are real
3. Company website
4. Parent company if applicable
5. LinkedIn company URL if known

Return ONLY a JSON object:
{{
  "full_name": "<official company name>",
  "website": "<website url or empty>",
  "linkedin_company": "<linkedin.com/company/... or empty>",
  "parent_company": "<parent company name or empty>",
  "known_contacts": [
    {{"name": "<real person name>", "title": "<title>", "confidence": "<high|medium>"}}
  ]
}}

Only include contacts you are highly confident are real and currently associated with this company.
Return empty array for known_contacts if uncertain.
"""

# ============================================================
# PLANNING PORTAL SOURCES — permis de construire publics
# ============================================================
PLANNING_SOURCES = [
    # UK Planning Portal — recherche datacenter
    "https://www.planningportal.co.uk/applications?term=data+centre",
    # UK Planning applications RSS
    "https://news.google.com/rss/search?q=data+center+planning+permission+approved+site:planningportal.co.uk+OR+site:planning.gov.uk&hl=en",
    # Companies House — recherche datacenters UK
    "https://news.google.com/rss/search?q=data+center+director+appointed+Companies+House+UK&hl=en",
]

# ============================================================
# FETCH FUNCTIONS
# ============================================================
def fetch_google_news(query: str) -> list[dict]:
    if query.startswith("site:"):
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    else:
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    
    articles = []
    try:
        r = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "xml")
        for item in soup.find_all("item")[:12]:
            link = item.find("link")
            title = item.find("title")
            pub = item.find("pubDate")
            source = item.find("source")
            if link and title:
                t = title.get_text(strip=True)
                if is_relevant(t):
                    articles.append({
                        "url": link.get_text(strip=True),
                        "title": t,
                        "date": pub.get_text(strip=True) if pub else "",
                        "source_name": source.get_text(strip=True) if source else ""
                    })
    except Exception as e:
        print(f"  Google News error: {e}")
    return articles

def is_relevant(title: str) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in DC_KEYWORDS)

def resolve_url(url: str) -> str:
    try:
        r = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        return str(r.url)
    except Exception:
        return url

def fetch_article(url: str) -> tuple[str, str]:
    """Returns (text, real_url)"""
    try:
        time.sleep(2)
        real_url = resolve_url(url)
        r = httpx.get(real_url, headers=HEADERS, timeout=25, follow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        article = (
            soup.find("article") or
            soup.find(attrs={"class": re.compile(r"article|post|entry|story|content", re.I)}) or
            soup.find("main")
        )
        text = article.get_text("\n", strip=True) if article else soup.get_text("\n", strip=True)
        lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 40]
        return "\n".join(lines)[:9000], real_url
    except Exception as e:
        print(f"    Fetch error: {e}")
        return "", url

def make_hash(url: str, title: str) -> str:
    return hashlib.md5(f"{url}{title}".encode()).hexdigest()[:8]

# ============================================================
# OPENCORPORATES ENRICHMENT
# ============================================================
def search_opencorporates(company_name: str, country_code: str = "") -> dict:
    """Search OpenCorporates public API for company officers."""
    try:
        time.sleep(0.5)
        q = quote_plus(company_name)
        url = f"https://api.opencorporates.com/v0.4/companies/search?q={q}&fields=name,officers"
        if country_code:
            url += f"&jurisdiction_code={country_code}"
        r = httpx.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        companies = data.get("results", {}).get("companies", [])
        if companies:
            company = companies[0].get("company", {})
            officers = []
            for o in company.get("officers", [])[:5]:
                officer = o.get("officer", {})
                if officer.get("name"):
                    officers.append({
                        "name": officer.get("name", ""),
                        "title": officer.get("position", ""),
                        "source": "opencorporates"
                    })
            return {
                "official_name": company.get("name", company_name),
                "jurisdiction": company.get("jurisdiction_code", ""),
                "company_number": company.get("company_number", ""),
                "opencorporates_url": company.get("opencorporates_url", ""),
                "officers": officers
            }
    except Exception as e:
        print(f"    OpenCorporates error for {company_name}: {e}")
    return {}

def search_companies_house(company_name: str) -> dict:
    """Search UK Companies House API (free, no auth needed for basic search)."""
    try:
        time.sleep(0.5)
        q = quote_plus(company_name)
        url = f"https://api.company-information.service.gov.uk/search/companies?q={q}&items_per_page=1"
        r = httpx.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        items = data.get("items", [])
        if items:
            company = items[0]
            cn = company.get("company_number", "")
            result = {
                "official_name": company.get("title", company_name),
                "company_number": cn,
                "status": company.get("company_status", ""),
                "officers": []
            }
            # Get officers
            if cn:
                time.sleep(0.5)
                off_url = f"https://api.company-information.service.gov.uk/company/{cn}/officers"
                r2 = httpx.get(off_url, headers=HEADERS, timeout=10)
                if r2.status_code == 200:
                    off_data = r2.json()
                    for item in off_data.get("items", [])[:5]:
                        if item.get("name") and item.get("resigned_on") is None:
                            result["officers"].append({
                                "name": item.get("name", "").title(),
                                "title": item.get("officer_role", "").replace("_", " ").title(),
                                "source": "companies_house_uk"
                            })
            return result
    except Exception as e:
        print(f"    Companies House error for {company_name}: {e}")
    return {}

# ============================================================
# AI EXTRACTION
# ============================================================
def extract_projects(client: anthropic.Anthropic, article: dict, text: str) -> list[dict]:
    if not text or len(text) < 200:
        return []
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4000,
            messages=[{"role": "user", "content": EXTRACTION_PROMPT.format(
                url=article.get("url", ""),
                title=article.get("title", ""),
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
        if not isinstance(projects, list):
            return []
        
        now = datetime.now(timezone.utc).isoformat()
        valid = []
        for p in projects:
            if not p.get("country"):
                continue
            if not p.get("source_hash"):
                p["source_hash"] = make_hash(article.get("url",""), article.get("title",""))
            p["scraped_at"] = now
            valid.append(p)
        return valid
    except Exception as e:
        print(f"    Extraction error: {e}")
        return []

def enrich_contacts(client: anthropic.Anthropic, project: dict) -> dict:
    """Enrich contacts with Companies House / OpenCorporates data."""
    if not project.get("contacts"):
        return project
    
    enriched_contacts = []
    country = project.get("country", "")
    country_map = {
        "United Kingdom": "gb", "France": "fr", "Germany": "de",
        "Spain": "es", "Netherlands": "nl", "Ireland": "ie",
        "Sweden": "se", "Finland": "fi", "Italy": "it", "Portugal": "pt"
    }
    cc = country_map.get(country, "")
    
    for contact in project.get("contacts", []):
        company = contact.get("company", "")
        if not company or len(company) < 3:
            enriched_contacts.append(contact)
            continue
        
        # Try Companies House for UK companies
        if country == "United Kingdom" or cc == "gb":
            ch_data = search_companies_house(company)
            if ch_data.get("officers"):
                contact["official_name"] = ch_data.get("official_name", company)
                # Add officers as additional contact entries
                for officer in ch_data["officers"][:2]:
                    if officer.get("name") and not any(
                        c.get("person") == officer["name"] for c in enriched_contacts
                    ):
                        enriched_contacts.append({
                            "role": contact.get("role", ""),
                            "company": company,
                            "person": officer.get("name", ""),
                            "title": officer.get("title", ""),
                            "email": "",
                            "tel": "",
                            "linkedin": "",
                            "source_verified": "companies_house_uk"
                        })
        
        # Try OpenCorporates for EU companies
        elif cc:
            oc_data = search_opencorporates(company, cc)
            if oc_data.get("officers"):
                contact["opencorporates_url"] = oc_data.get("opencorporates_url", "")
                for officer in oc_data["officers"][:2]:
                    if officer.get("name") and not any(
                        c.get("person") == officer["name"] for c in enriched_contacts
                    ):
                        enriched_contacts.append({
                            "role": contact.get("role", ""),
                            "company": company,
                            "person": officer.get("name", "").title(),
                            "title": officer.get("title", ""),
                            "email": "",
                            "tel": "",
                            "linkedin": "",
                            "source_verified": "opencorporates"
                        })
        
        enriched_contacts.append(contact)
    
    project["contacts"] = enriched_contacts
    return project

# ============================================================
# MERGE & DEDUP
# ============================================================
def merge(existing: list, new_list: list) -> tuple[list, int]:
    seen_hashes = {p.get("source_hash", "") for p in existing if p.get("source_hash")}
    seen_names = {normalize_name(p.get("name", "")) for p in existing}
    added = 0
    merged = list(existing)
    
    for p in new_list:
        h = p.get("source_hash", "")
        name_key = normalize_name(p.get("name", ""))
        
        if h and h in seen_hashes:
            continue
        if name_key and name_key in seen_names:
            # Update existing project with new contacts if any
            for i, ep in enumerate(merged):
                if normalize_name(ep.get("name", "")) == name_key:
                    existing_companies = {c.get("company","") for c in ep.get("contacts",[])}
                    for nc in p.get("contacts", []):
                        if nc.get("company","") not in existing_companies:
                            merged[i].setdefault("contacts", []).append(nc)
                    break
            continue
        
        merged.append(p)
        seen_hashes.add(h)
        seen_names.add(name_key)
        added += 1
    
    return merged, added

def normalize_name(name: str) -> str:
    """Normalize project name for dedup."""
    n = name.lower().strip()
    n = re.sub(r'[^\w\s]', '', n)
    n = re.sub(r'\s+', ' ', n)
    return n[:60]

def load_existing(path: str) -> list:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

# ============================================================
# MAIN
# ============================================================
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
    
    # Step 1: Collect articles from Google News
    print("\n=== STEP 1: Collecting articles ===")
    all_articles = []
    seen_urls = set()
    
    for query in GOOGLE_QUERIES:
        print(f"Query: {query[:60]}...")
        articles = fetch_google_news(query)
        new_count = 0
        for a in articles:
            if a["url"] not in seen_urls:
                seen_urls.add(a["url"])
                all_articles.append(a)
                new_count += 1
        print(f"  → {new_count} new relevant articles")
        time.sleep(1)
    
    print(f"\nTotal articles to process: {len(all_articles)}")
    
    # Step 2: Extract projects from articles
    print("\n=== STEP 2: Extracting projects ===")
    all_new = []
    for i, article in enumerate(all_articles):
        print(f"[{i+1}/{len(all_articles)}] {article['title'][:65]}...")
        text, real_url = fetch_article(article["url"])
        article["url"] = real_url  # use real URL
        if text:
            projects = extract_projects(client, article, text)
            print(f"  → {len(projects)} projects found")
            all_new.extend(projects)
        time.sleep(1)
    
    print(f"\nTotal raw projects extracted: {len(all_new)}")
    
    # Step 3: Enrich contacts with public registries
    print("\n=== STEP 3: Enriching contacts ===")
    enriched = []
    for i, p in enumerate(all_new):
        if p.get("contacts") and p.get("country") in [
            "United Kingdom", "France", "Germany", "Spain",
            "Netherlands", "Ireland", "Sweden", "Finland", "Italy", "Portugal"
        ]:
            print(f"  Enriching: {p['name'][:50]}...")
            p = enrich_contacts(client, p)
        enriched.append(p)
    
    # Step 4: Merge with existing
    print("\n=== STEP 4: Merging ===")
    merged, added = merge(existing, enriched)
    print(f"New projects added: {added} | Total: {len(merged)}")
    
    # Sort by momentum desc
    merged.sort(key=lambda p: (p.get("momentum", 0), p.get("scraped_at", "")), reverse=True)
    
    # Save
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    
    # Count total contacts
    total_contacts = sum(len(p.get("contacts", [])) for p in merged)
    
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "total_projects": len(merged),
            "new_this_run": added,
            "articles_processed": len(all_articles),
            "total_contacts": total_contacts
        }, f, indent=2)
    
    print(f"\nDone! {len(merged)} projects, {total_contacts} contacts total.")

if __name__ == "__main__":
    main()

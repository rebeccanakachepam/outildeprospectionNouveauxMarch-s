# DC Intel PAM — Datacenter Project Intelligence

Outil de veille et prospection commerciale pour les projets datacenters EMEA.  
Se met à jour automatiquement toutes les 6h via GitHub Actions + Claude API.

## Architecture

```
GitHub Actions (cron 6h)
  └── scraper/scraper.py
        ├── Scrap DatacenterKnowledge, DataCenterDynamics, BlackridgeResearch
        ├── Claude API → extrait et structure les projets en JSON
        └── data/projects.json  (commit auto sur main)
              └── GitHub Pages → index.html charge le JSON au démarrage
```

## Setup en 5 minutes

### 1. Forker ce repo

```bash
git clone https://github.com/VOTRE_USERNAME/dc-intel-pam.git
cd dc-intel-pam
```

### 2. Ajouter le secret ANTHROPIC_API_KEY

Dans GitHub : **Settings → Secrets and variables → Actions → New repository secret**

```
Name:  ANTHROPIC_API_KEY
Value: sk-ant-...
```

### 3. Activer GitHub Pages

**Settings → Pages → Source : GitHub Actions**

### 4. Déclencher le premier scraping

**Actions → Scrape & Deploy → Run workflow**

L'outil sera ensuite accessible sur :
```
https://VOTRE_USERNAME.github.io/dc-intel-pam/
```

### 5. (Optionnel) Modifier la fréquence

Dans `.github/workflows/scrape.yml`, changer le cron :
```yaml
- cron: '0 */6 * * *'   # toutes les 6h (défaut)
- cron: '0 8 * * *'     # une fois par jour à 8h UTC
- cron: '0 8 * * 1'     # une fois par semaine le lundi
```

## Ajouter des sources

Dans `scraper/scraper.py`, ajouter des entrées dans `SOURCES` :

```python
SOURCES = [
    {
        "name": "MaSourcPersonnelle",
        "urls": [
            "https://www.exemple.com/news/datacenters",
        ]
    },
    # ...
]
```

Le scraper récupère les liens d'articles des pages index, puis Claude extrait
les projets de chaque article automatiquement.

## Structure des données

Chaque projet dans `data/projects.json` suit ce schéma :

```json
{
  "id": "DC_a1b2c3",
  "name": "Nom du projet",
  "type": "Parent",
  "value": 500,
  "region": "Europe",
  "country": "France",
  "city": "Paris",
  "announced": "2024 Q3",
  "start": "2025 Q1",
  "end": "2027 Q4",
  "phase": "construction",
  "overview": "Description complète...",
  "summaryShort": "Résumé court.",
  "attributes": ["Hyperscale Data Center", "AI Infrastructure"],
  "momentum": 4,
  "funding": "Confirmed",
  "sector": "Data Center",
  "source": "https://...",
  "contacts": [],
  "metrics": [{"p": "IT Capacity", "v": "100", "u": "MW"}],
  "tenders": [],
  "updates": ["2025 Q1 - Construction démarrée."],
  "scraped_at": "2025-01-15T08:00:00Z",
  "source_hash": "abc123"
}
```

## Coût estimé

- GitHub Actions : gratuit (2 000 min/mois incluses)
- Claude API : ~$0.02 par article traité (~10 articles/run × 4 runs/jour = $0.80/jour max)
- Recommandation : utiliser `claude-haiku-4-5` dans le scraper pour réduire les coûts

## Stack

- Python 3.11 + `anthropic` + `httpx` + `beautifulsoup4`
- Vanilla JS + CSS (zero dépendance frontend)
- GitHub Actions + GitHub Pages

# ⚡ StreamScout & GigHunt

**A zero-cost streamer discovery engine and editor gig finder for content creators and video editors.**

StreamScout scans Twitch for high-tier streamers (1,000+ viewers, 100,000+ followers) who have **no YouTube presence** — meaning no one is editing their content for YouTube yet. These are untapped opportunities for video editors looking for clients.

GigHunt complements this by scraping Twitter/X and Reddit for "hiring video editor" posts in real time, giving you a live feed of job opportunities.

---

## 🎯 What Problem Does This Solve?

If you're a **freelance video editor** specializing in gaming/streaming content, finding clients is hard:

- **Manual scouting is slow** — you'd have to browse Twitch, then manually check YouTube for each streamer, one by one.
- **Hiring posts are scattered** — gigs are posted across Twitter, Reddit, Discord, and other platforms with no central feed.
- **Timing matters** — the best leads go fast. You need to find streamers *before* they hire someone else.

StreamScout automates the entire discovery pipeline:

1. **Scans Twitch** for top-tier live streamers.
2. **Cross-references YouTube** with a 3-tier verification system to confirm they have no active YouTube channel.
3. **Detects clipper channels** — if third-party channels are already clipping a streamer, someone's covering that niche.
4. **Creates leads** — streamers who pass all 3 tiers become actionable prospects in a mini CRM.
5. **Finds gigs** — searches Twitter/X and Reddit for "hiring editor" posts and aggregates them into a single feed.

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    FastAPI Backend (main.py)                  │
│        REST API + SSE streaming + Static dashboard           │
├──────────────┬──────────────┬────────────────────────────────┤
│  Twitch API  │  YouTube API │  Twitter (twikit) + Reddit     │
│  (Helix)     │  (Hybrid)    │  (asyncpraw)                   │
├──────────────┴──────────────┴────────────────────────────────┤
│            Cross-Reference Pipeline (services/)              │
│     Tier 1 → Tier 2 → Tier 3 → Lead Creation                │
├──────────────────────────────────────────────────────────────┤
│           SQLite (default) / PostgreSQL (optional)            │
├──────────────────────────────────────────────────────────────┤
│     Celery + Filesystem Broker (or Redis) for scheduling     │
└──────────────────────────────────────────────────────────────┘
```

### 3-Tier YouTube Verification

| Tier | Check | Window | Result |
|------|-------|--------|--------|
| **Tier 1** | YouTube link in Twitch profile panels | 1 week | Reject if channel posted within 1 week |
| **Tier 2** | YouTube channel name search | 1 month | Reject if any matching channel is active |
| **Tier 3** | YouTube highlight/clip search | 1 month | Reject if clip channels with 10k+ views exist |

Only streamers who pass **all 3 tiers** become verified leads.

### YouTube API Quota Optimization

The system uses a hybrid approach that saves ~99% of YouTube API quota:
- **Step 1:** Free scraping via `youtube-search-python` ($0) to find channel candidates.
- **Step 2:** Official YouTube Data API (1 quota unit) only to check last upload date.
- **Result:** ~10,000 channels/day capacity vs. ~100 with search-only.

---

## 📋 Prerequisites

- **Python 3.10+**
- **Twitch Developer App** (free) — [Create one here](https://dev.twitch.tv/console/apps)
- **YouTube Data API v3 key** (free tier: 10,000 units/day) — [Google Cloud Console](https://console.cloud.google.com)
- **Twitter/X cookies** (optional, for GigHunt) — extract from browser DevTools
- **Reddit "script" app** (optional, for GigHunt) — [Create one here](https://www.reddit.com/prefs/apps)

> **Note:** Only Twitch + YouTube are required for the core StreamScout scanner. Twitter and Reddit are optional and power the GigHunt feature.

---

## 🚀 Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/Content_Creator_Helper.git
cd Content_Creator_Helper
```

### 2. Create a virtual environment

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Copy the example file and fill in your API keys:

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# ── Required for StreamScout ──
TWITCH_CLIENT_ID=your_twitch_client_id
TWITCH_CLIENT_SECRET=your_twitch_client_secret
YOUTUBE_API_KEY=your_youtube_api_key

# ── Optional for GigHunt (Twitter) ──
TWITTER_AUTH_TOKEN=your_auth_token_cookie
TWITTER_CT0=your_ct0_cookie

# ── Optional for GigHunt (Reddit) ──
REDDIT_CLIENT_ID=your_reddit_client_id
REDDIT_CLIENT_SECRET=your_reddit_client_secret
REDDIT_USER_AGENT=StreamScout/1.0 (by u/your_username)
```

### 5. Start the application

```bash
uvicorn main:app --reload --port 8000
```

Open your browser to **http://localhost:8000** — you'll see the Command Center dashboard.

---

## 💻 Usage

### Web Dashboard (Command Center)

The dashboard has 3 modules:

| Module | Description |
|--------|-------------|
| **📡 Streamer Scanner** | Scan Twitch, view verified leads, filter by YouTube status, sort by viewers/followers. Click "Start Scan" to run the pipeline. |
| **💼 Gig Hunt** | Browse hiring posts from Twitter and Reddit. Filter by platform, set timeframe (1 week – 6 months), click "Search Gigs". |
| **⚙️ Settings** | View which API integrations are configured and their connection status. |

### REST API

The backend exposes a full REST API (auto-documented at `/docs`):

```
GET  /api/streamers              → Paginated streamer list with filters
GET  /api/streamers/{id}         → Detailed streamer view (YouTube channels, lead info)
POST /api/leads?streamer_id={id} → Save a streamer as a lead/prospect
PATCH /api/leads/{id}            → Update lead status & notes
POST /api/scan/trigger           → Manually trigger a Twitch scan (via Celery)
GET  /api/scan/stream            → SSE stream of real-time scan progress
GET  /api/gigs                   → Paginated gig feed
POST /api/gigs/search            → Trigger on-demand gig search (via Celery)
GET  /api/gigs/search/stream     → SSE stream of gig search progress
GET  /api/status                 → Check which APIs are configured
GET  /health                     → Health check
```

### Background Tasks (Celery)

For automated daily scans, start the Celery worker and scheduler:

```bash
# Terminal 1 — Start the Celery worker
celery -A celery_app worker --loglevel=info --concurrency=2

# Terminal 2 — Start the beat scheduler (runs daily scan at 03:00 UTC)
celery -A celery_app beat --loglevel=info
```

> **Note:** Celery works out of the box with a filesystem broker (no Redis required). If you set `REDIS_URL` in `.env`, it will use Redis for better performance.

---

## 📂 Project Structure

```
Content_Creator_Helper/
├── main.py                  # FastAPI app — routes, SSE endpoints, dashboard
├── config.py                # Pydantic-settings — loads .env variables
├── database.py              # SQLAlchemy async/sync engines & sessions
├── models.py                # ORM models (Streamer, YouTubeChannel, Lead, SocialPost)
├── tasks.py                 # Celery background tasks
├── celery_app.py            # Celery factory + beat schedule
│
├── scrapers/
│   ├── twitch_scanner.py    # Twitch Helix API scanner
│   ├── youtube_zero_cost.py # Hybrid YouTube verifier (scraper + API)
│   ├── twitter_gig_finder.py# Twitter/X gig scraper (twikit, $0)
│   └── reddit_gig_finder.py # Reddit gig scraper (asyncpraw)
│
├── services/
│   ├── cross_reference.py   # 3-tier Twitch → YouTube pipeline (the brain)
│   └── clipper_checker.py   # YouTube clipper detection
│
├── static/
│   ├── index.html           # Command Center dashboard
│   ├── styles.css           # Dashboard styles
│   └── app.js               # Dashboard logic (vanilla JS)
│
├── .env.example             # Template for environment variables
├── .gitignore               # Excludes .env, database, cache files
└── requirements.txt         # Python dependencies
```

---

## 🔑 Getting API Keys

### Twitch (Required)

1. Go to [dev.twitch.tv/console/apps](https://dev.twitch.tv/console/apps)
2. Click **"Register Your Application"**
3. Set **OAuth Redirect URL** to `http://localhost`
4. Copy the **Client ID** and **Client Secret** into `.env`

### YouTube Data API v3 (Required)

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a new project (or select existing)
3. Navigate to **APIs & Services → Library**
4. Enable **"YouTube Data API v3"**
5. Go to **Credentials → Create Credentials → API Key**
6. Copy the API key into `.env`

### Twitter / X (Optional — for GigHunt)

1. Log in to [x.com](https://x.com) in your browser
2. Open DevTools (F12) → **Application** tab → **Cookies** → `https://x.com`
3. Copy the values of `auth_token` and `ct0` into `.env`

### Reddit (Optional — for GigHunt)

1. Go to [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps)
2. Click **"create another app..."**
3. Select **"script"** type
4. Set **redirect uri** to `http://localhost`
5. Copy the **Client ID** (under the app name) and **Secret** into `.env`

---

## ⚙️ Configuration

All settings are in `.env`. Key tuning parameters:

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_VIEWERS` | `1000` | Minimum concurrent Twitch viewers to qualify |
| `MIN_FOLLOWERS` | `100000` | Minimum Twitch followers to qualify |
| `DORMANCY_DAYS` | `180` | Days without upload → "dormant" YouTube channel |
| `DATABASE_URL` | `sqlite+aiosqlite:///./streamscout.db` | Database connection (SQLite or PostgreSQL) |
| `REDIS_URL` | *(empty)* | Optional Redis for Celery broker + deduplication |

---

## 📄 License

This project is provided as-is for personal and educational use.

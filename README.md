# 🎬 StreamScout & GigHunt

> **A dual-purpose backend application built to automate client acquisition for freelance video editors.**

---

## ⚠️ Important Notice

> **This repository contains a partial upload of the project.**
> The full source code is not publicly available due to personal use interests.

---

## 📌 What Is This?

**StreamScout & GigHunt** is a FastAPI + Celery backend that eliminates the manual grind of finding freelance video editing clients. It does two things automatically:

| Module | What It Does |
|---|---|
| 🔭 **StreamScout** | Discovers Twitch streamers who likely need a video editor and Social Media presence |
| 🎯 **GigHunt** | Scrapes Reddit & Twitter/X for active "hiring" posts in real time |

---

## 🔭 StreamScout — Streamer Lead Generation

StreamScout acts as an intelligence engine for client prospecting.

**How the pipeline works:**

1. **Data Ingestion** — Scans active Twitch streamers filtered by viewership/follower metrics using the Twitch API
2. **Cross-Referencing** — Looks up each streamer's associated YouTube channel via the YouTube API
3. **Qualification** — Evaluates whether the streamer already uploads edited content or has dedicated clippers
4. **Lead Tracking** — Streamers without a strong YouTube presence are flagged as actionable **LEADS** in the database

**What you can do with a lead:**
- Set a status (e.g., `PROSPECT`, `CONTACTED`, `CONVERTED`)
- Add personal notes
- Predict estimated monthly revenue

---

## 🎯 GigHunt — Automated Job Board

GigHunt replaces manual scrolling across platforms by aggregating editing gigs into one feed.

**How it works:**

1. **Smart Searching** — Queries Reddit and Twitter/X using custom keywords (e.g., *"looking for video editor"*, *"need a thumbnail designer"*)
2. **Continuous Monitoring** — Asynchronously scrapes platforms for posts within a configurable time window (e.g., last 30 days)
3. **Unified Feed** — Results are normalized, sorted chronologically, and paginated — complete with interaction metrics (likes, replies) and direct links

---

## 🏗️ Technical Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     FastAPI Backend                      │
│         Exposes REST endpoints for the dashboard         │
└────────────────────────┬────────────────────────────────┘
                         │
          ┌──────────────┴──────────────┐
          │                             │
┌─────────▼──────────┐       ┌──────────▼─────────┐
│  Celery Workers     │       │   SSE Streaming     │
│  (Background jobs)  │       │  (Live progress UI) │
└─────────┬──────────┘       └────────────────────┘
          │
┌─────────▼──────────┐
│   SQLite Database   │
│   (streamscout.db)  │
│   via SQLAlchemy    │
└────────────────────┘
```

| Component | Role |
|---|---|
| **FastAPI** | Provides fast, robust REST endpoints consumed by the frontend dashboard |
| **Celery** | Offloads slow scraping tasks (Twitch scans, Twitter scrapes) to background workers |
| **Server-Sent Events (SSE)** | Streams live progress bars and status updates to the UI during active scans |
| **SQLite + SQLAlchemy** | Persists all streamers, gigs, and lead conversions locally in `streamscout.db` |

---

## 🗂️ Project Structure (Partial)

```
streamscout-gighunt/
├── main.py                  # FastAPI app entry point
├── celery_worker.py         # Celery worker configuration
├── streamscout.db           # SQLite database (auto-generated)
├── routers/
│   ├── streamscout.py       # Twitch/YouTube lead generation endpoints
│   └── gighunt.py           # Reddit/Twitter job scraping endpoints
├── models/
│   └── ...                  # SQLAlchemy models
├── tasks/
│   └── ...                  # Celery background tasks
└── ...                      # Additional modules (not uploaded)
```

> 🔒 Several modules, configuration files, and credential files are **excluded from this upload**, for exemple Frontend and Backend.

---

## 🚀 Running the App (Requires Your Own API Keys)

**Prerequisites:**
- Python 3.10+
- Redis (for Celery broker)

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Start the FastAPI server:**
```bash
uvicorn main:app --reload
```

**Start the Celery worker:**
```bash
celery -A celery_worker worker --loglevel=info
```

> ⚠️ You must configure your own API credentials in the appropriate files before running. The application will not work without valid keys for Twitch, YouTube, Reddit, and Twitter/X.

---

## 🛠️ Tech Stack

- **Backend Framework:** FastAPI
- **Task Queue:** Celery + Redis
- **Database:** SQLite via SQLAlchemy
- **Real-time Streaming:** Server-Sent Events (SSE)
- **External APIs:** Twitch API, YouTube Data API v3, Reddit (PRAW), Twitter/X API

---

## 📄 If necessary, I will upload all the files and release them to the public!

---

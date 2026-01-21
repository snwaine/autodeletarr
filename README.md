![Docker Pulls](https://img.shields.io/docker/pulls/snwaine/mediareaparr)
![Docker Version](https://img.shields.io/docker/v/snwaine/mediareaparr/latest)
![Build Status](https://img.shields.io/github/actions/workflow/status/snwaine/mediareaparr/docker.yml)
![License](https://img.shields.io/github/license/snwaine/mediareaparr)
# 🪦 MediaReaparr
**Schedule the inevitable.**

MediaReaparr is a self-hosted automation tool that intelligently cleans your media library by removing movies and episodes that no longer meet your quality or score standards.  
It integrates with **Radarr** and **Sonarr**, supports scheduled jobs, dry-runs, and score-based decisions — all through a clean, dark-themed web UI.

---

## ✨ Features

- 🎯 **Score-based cleanup**
  - Fetches ratings from IMDb, OMDb, and TMDb
  - Normalizes all ratings to a **0–100** scale
  - Averages available scores and compares against a threshold

- ⏱ **Scheduled jobs**
  - Cron-style schedules
  - Manual **Run Now**
  - Enable / disable per job

- 🎬 **Radarr & Sonarr support**
  - Per-app configuration
  - Tag-based targeting
  - Sonarr delete modes (files / episodes / series)

- 🧪 **Dry-Run mode**
  - Preview deletions before committing

- 🌑 **Modern Web UI**
  - Dark theme with green MediaReaparr accents
  - Responsive job cards
  - Modal-based editing and confirmations

- 🐳 **Docker-first**
  - Designed for Docker & Unraid
  - Persistent config & state via mounted volumes

---

## 🚀 Quick Start (Docker)

```bash
docker run -d \
  --name mediareaparr \
  -p 8787:8787 \
  -v /path/to/config:/config \
  -e FLASK_SECRET_KEY=change-me \
  ghcr.io/snwaine/mediareaparr:latest



![Docker Pulls](https://img.shields.io/docker/pulls/snwaine/mediareaparr)
![Docker Version](https://img.shields.io/docker/v/snwaine/mediareaparr/latest)
![Build Status](https://img.shields.io/github/actions/workflow/status/snwaine/mediareaparr/docker.yml)
![License](https://img.shields.io/github/license/snwaine/mediareaparr)

# 🪦 MediaReaparr
**Schedule the inevitable.**

> ⚠️ **VERY EARLY ALPHA — NOT FOR PUBLIC USE**
>
> Breaking changes are expected. Config formats, UI flows, and job behavior may change without notice.

---

## 🧠 What is MediaReaparr?

**MediaReaparr** is a self-hosted automation tool that intelligently cleans your media library by removing movies and TV content that no longer meet your quality or score standards.

It integrates directly with **Radarr** and **Sonarr**, supports **dry-runs**, **score-based deletion**, and **scheduled execution** — all managed through a modern, dark-themed Web UI.

There is **no external cron dependency**.  
All scheduling and execution happens **inside the container**.

---

## ✨ Features

### 🎯 Score-based cleanup (Radarr)
- Uses ratings already available in Radarr (IMDb, TMDb, OMDb)
- Normalizes all scores to a **0–100** scale
- Averages available ratings
- Optional rule: *delete only if score is below X*

### 🎬 Radarr & Sonarr support
- Multiple app instances
- Tag-based targeting
- Per-job configuration
- Sonarr delete modes:
  - Episodes only
  - Episodes → remove empty series
  - Whole series

### ⏱ Job execution
- Built-in **internal scheduler**
- Manual **Run Now**
- Enable / disable per job
- Per-job execution history

### 🧪 Dry-Run mode
- Preview exactly what *would* be deleted
- Shows age, score, title, and path
- No filesystem or API changes

### 🌑 Modern Web UI
- Dark theme with green MediaReaparr accents
- Clean job cards
- Modal-based editing and confirmations
- Live status + log viewer

### 🐳 Docker-first
- Designed for Docker & Unraid
- No host-level cron required
- Persistent config, state, and logs via volumes

---

## 🚀 Quick Start (Docker)

```bash
docker run -d \
  --name mediareaparr \
  -p 7575:7575 \
  -v /path/to/config:/config \
  -e FLASK_SECRET_KEY=change-me \
  snwaine/mediareaparr:latest

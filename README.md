# WellAtlas by Henry Suden

A Flask web app to manage Customers → Sites → timeline entries (with photos/docs), shown on a Leaflet map. Includes login, soft-delete, public share links, KML import, and optional Google Drive backup.

## Files in this repo
- `app.py` – the whole application (models, routes, inline templates)
- `requirements.txt` – Python dependencies
- `Procfile` – process definition for Render/Heroku style platforms
- `README.md` – these instructions

## Quick Start (Render)
1. Create a **new Render Web Service** from this repo.
2. **Start Command:** `gunicorn app:app`
3. **Environment Variables** (Settings → Environment → Add):
   - `SECRET_KEY` = any non-empty string (e.g., `supersecret123`)
   - `DATA_DIR` = `/var/data`
   - *(optional, for Drive backups)*  
     - `GDRIVE_FOLDER_ID` = your Google Drive folder id  
     - `GDRIVE_SERVICE_JSON` = `service-account.json`
4. *(Optional)* **Secret Files** (Environment → Secret Files → Add):  
   - **Name:** `service-account.json`  
   - **Contents:** paste your Google service account JSON
5. *(Optional)* **Disk** (Disks tab) – for persistence between deploys:
   - Name: `data` (anything)
   - Mount Path: `/var/data`
   - Size: 5GB+  
   *(If you skip the disk, data is ephemeral; use Drive backup to keep snapshots.)*
6. Deploy. After it’s live:
   - Visit `/admin/ensure_schema` → should return `schema ok`
   - Then visit `/` → sign up and use the app

## Local run (dev)
```bash
python -m venv .venv
. .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export SECRET_KEY=dev123
export DATA_DIR=./data
python app.py
# open http://localhost:5000

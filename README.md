# WellAtlas (All-in-One Fixed Build)

Upload these 4 files to a new GitHub repo and deploy on Render.

Render:
- Start Command: gunicorn app:app
- Env: SECRET_KEY, DATA_DIR=/var/data
- Disk: mount at /var/data
- (Optional) Google Drive: add GDRIVE_SERVICE_JSON=service-account.json, GDRIVE_FOLDER_ID=<id>, and Secret File `service-account.json`.

First run:
- Visit /admin/ensure_schema (should print "schema ok")
- Then / to use the app

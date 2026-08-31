"""
Deploys a static site directory to Netlify via their Deploy API directly
(https://docs.netlify.com/api/get-started/#deploy-with-a-zip-file) — no
Node.js/netlify-cli dependency, since this needs to run identically from a
local machine or a plain Python GitHub Actions runner.
"""

import io
import zipfile

import requests

API_BASE = "https://api.netlify.com/api/v1"


def _zip_directory(site_dir):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in site_dir.rglob("*"):
            if path.is_file():
                zf.write(path, arcname=path.relative_to(site_dir))
    buf.seek(0)
    return buf


def deploy(site_dir, site_id, auth_token):
    zip_buf = _zip_directory(site_dir)
    resp = requests.post(
        f"{API_BASE}/sites/{site_id}/deploys",
        headers={"Authorization": f"Bearer {auth_token}", "Content-Type": "application/zip"},
        data=zip_buf,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()

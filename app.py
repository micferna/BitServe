import os
import json
import shutil
import logging
import asyncio
import sqlite3
from typing import List, Optional
from time import time

import libtorrent as lt
import psutil
import httpx

from fastapi import (
    FastAPI, HTTPException, UploadFile, File, BackgroundTasks, 
    Depends, Query
)
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from dotenv import load_dotenv


# -----------------------------------------------------------------------------
# 1. Chargement des variables d'environnement
# -----------------------------------------------------------------------------
load_dotenv()  # charge éventuellement un fichier .env

# -----------------------------------------------------------------------------
# 2. Configuration du logger
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# 3. Paramètres de sécurité
# -----------------------------------------------------------------------------
API_TOKEN = os.getenv("API_TOKEN", "secret-token-here")
security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Vérifie la présence d'un token d'API."""
    if credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Token d'API invalide ou manquant.")
    return credentials

# -----------------------------------------------------------------------------
# 4. Configuration de l'application FastAPI
# -----------------------------------------------------------------------------
app = FastAPI(title="BitTorrent Manager with SQLite (Optimized)")

# -----------------------------------------------------------------------------
# 5. Répertoires de travail
# -----------------------------------------------------------------------------
bitserve_dir = "./.bitserve"
os.makedirs(bitserve_dir, exist_ok=True)

downloads_path = "./downloads"
os.makedirs(downloads_path, exist_ok=True)

torrent_files_dir = os.path.join(bitserve_dir, "torrent_files")
os.makedirs(torrent_files_dir, exist_ok=True)

state_file_path = os.path.join(bitserve_dir, "session_state.dat")

resume_data_directory = os.path.join(bitserve_dir, "resume_data")
os.makedirs(resume_data_directory, exist_ok=True)

# -----------------------------------------------------------------------------
# 6. Configuration avancée libtorrent
# -----------------------------------------------------------------------------
session_params = lt.session_params()
session_params.settings = {
    'listen_interfaces': '0.0.0.0:6881',
    'connection_limit': 200,        # max 200 connexions simultanées
    'upload_rate_limit': 0,         # aucune limite d'upload
    'download_rate_limit': 0,       # aucune limite de download
}
session_params.settings['resume_save_path'] = resume_data_directory

session = lt.session(session_params)

# -----------------------------------------------------------------------------
# 7. Base de données SQLite
# -----------------------------------------------------------------------------
DB_PATH = os.path.join(bitserve_dir, "bitserve.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS torrents (
            info_hash TEXT PRIMARY KEY,
            name TEXT,
            total_uploaded INTEGER DEFAULT 0,
            total_downloaded INTEGER DEFAULT 0,
            last_access REAL DEFAULT 0,
            active INTEGER DEFAULT 0  -- 0 = inactif/arrêté, 1 = actif dans la session
        )
    """)
    conn.commit()
    conn.close()

def db_insert_torrent(info_hash: str, name: str):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT INTO torrents (info_hash, name, last_access, active)
            VALUES (?, ?, ?, ?)
        """, (info_hash, name, time(), 1))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()

def db_update_torrent_stats(info_hash: str, uploaded: int, downloaded: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE torrents
        SET total_uploaded = ?, total_downloaded = ?
        WHERE info_hash = ?
    """, (uploaded, downloaded, info_hash))
    conn.commit()
    conn.close()

def db_update_torrent_access(info_hash: str, active: int):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        UPDATE torrents
        SET last_access = ?, active = ?
        WHERE info_hash = ?
    """, (time(), active, info_hash))
    conn.commit()
    conn.close()

def db_delete_torrent(info_hash: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM torrents WHERE info_hash = ?", (info_hash,))
    conn.commit()
    conn.close()

def db_get_torrent(info_hash: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT info_hash, name, total_uploaded, total_downloaded, last_access, active
        FROM torrents
        WHERE info_hash = ?
    """, (info_hash,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {
            "info_hash": row[0],
            "name": row[1],
            "total_uploaded": row[2],
            "total_downloaded": row[3],
            "last_access": row[4],
            "active": row[5],
        }
    return None

def db_list_torrents(offset=0, limit=50):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT info_hash, name, total_uploaded, total_downloaded, last_access, active
        FROM torrents
        ORDER BY rowid
        LIMIT ? OFFSET ?
    """, (limit, offset))
    rows = cursor.fetchall()
    conn.close()
    results = []
    for row in rows:
        results.append({
            "info_hash": row[0],
            "name": row[1],
            "total_uploaded": row[2],
            "total_downloaded": row[3],
            "last_access": row[4],
            "active": row[5],
        })
    return results

def db_list_active_torrents():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.execute("""
        SELECT info_hash
        FROM torrents
        WHERE active = 1
    """)
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]

# -----------------------------------------------------------------------------
# 8. Gestion mémoire des torrents : LRU & Limite d'actifs
# -----------------------------------------------------------------------------
MAX_ACTIVE_TORRENTS = 500  # <--- Ajustez selon vos ressources
torrents_actifs = {}       # info_hash -> handle libtorrent

def ensure_memory_limit():
    """
    Si le nombre de torrents actifs dépasse la limite autorisée,
    on met en pause (retire) les torrents les moins récemment utilisés (LRU).
    """
    active_list = sorted(
        db_list_active_torrents(),
        key=lambda h: db_get_torrent(h)["last_access"]
    )
    # S'il y a trop de torrents actifs, on 'désactive' les plus anciens
    while len(active_list) > MAX_ACTIVE_TORRENTS:
        oldest_hash = active_list[0]
        pause_torrent(oldest_hash)
        active_list.pop(0)

def pause_torrent(info_hash: str):
    """
    Met un torrent en pause dans libtorrent et met à jour la DB pour
    marquer 'active=0'. Retire aussi le handle de la mémoire.
    """
    if info_hash in torrents_actifs:
        handle = torrents_actifs[info_hash]
        session.remove_torrent(handle, False)
        torrents_actifs.pop(info_hash, None)

    db_update_torrent_access(info_hash, active=0)
    logger.info(f"[PAUSE] Torrent {info_hash} mis hors session.")

def resume_torrent(info_hash: str):
    """
    Réactive un torrent 'inactif' depuis la DB si besoin.
    """
    record = db_get_torrent(info_hash)
    if not record:
        raise HTTPException(status_code=404, detail="Torrent introuvable.")

    if record["active"] == 1 and info_hash in torrents_actifs:
        # Déjà actif
        db_update_torrent_access(info_hash, active=1)
        logger.info(f"[RESUME] Torrent {info_hash} déjà actif.")
        return

    # Charger le .torrent depuis le disque
    torrent_file_path = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
    if not os.path.exists(torrent_file_path):
        raise HTTPException(status_code=404, detail="Fichier .torrent introuvable sur le disque.")

    try:
        with open(torrent_file_path, 'rb') as f:
            decoded = lt.bdecode(f.read())
            info = lt.torrent_info(decoded)
            params = {
                'ti': info,
                'save_path': downloads_path,
            }
            handle = session.add_torrent(params)
        torrents_actifs[info_hash] = handle
        db_update_torrent_access(info_hash, active=1)
        logger.info(f"[RESUME] Torrent {info_hash} réactivé.")
    except Exception as e:
        logger.error(f"[RESUME] Erreur lors de la réactivation du torrent {info_hash}: {e}")

# -----------------------------------------------------------------------------
# 9. Modèles Pydantic
# -----------------------------------------------------------------------------
class Webhook(BaseModel):
    event: str
    url: str

class TorrentRemovalRequest(BaseModel):
    info_hashes: List[str] = Field(..., min_items=1)
    remove_files: Optional[bool] = False

# -----------------------------------------------------------------------------
# 10. Fonctions utilitaires libtorrent
# -----------------------------------------------------------------------------
def save_session_state():
    """Sauvegarde l'état global de la session (fichier .dat)."""
    try:
        with open(state_file_path, "wb") as f:
            f.write(lt.bencode(session.save_state()))
        logger.info("Session state saved.")
    except Exception as e:
        logger.error(f"Error saving session state: {e}")

def load_session_state():
    """Restaure l'état global de la session si le fichier existe."""
    if os.path.exists(state_file_path):
        try:
            with open(state_file_path, "rb") as f:
                session.load_state(lt.bdecode(f.read()))
            logger.info("Session state restored.")
        except Exception as e:
            logger.error(f"Error restoring session state: {e}")

def add_torrent_from_file(file_path, info_hash, total_uploaded=0, total_downloaded=0, name="Unknown"):
    """
    Ajoute un torrent ACTIF à la session, + enregistre dans la DB (ou maj si existe déjà).
    Assure aussi le respect de la limite de torrents actifs via LRU si besoin.
    """
    try:
        with open(file_path, 'rb') as f:
            decoded = lt.bdecode(f.read())
            info = lt.torrent_info(decoded)
            params = {'ti': info, 'save_path': downloads_path}
            handle = session.add_torrent(params)

        torrents_actifs[info_hash] = handle
        db_update_torrent_stats(info_hash, total_uploaded, total_downloaded)
        db_update_torrent_access(info_hash, active=1)
        logger.info(f"Torrent activé : {name} ({info_hash})")

        # Contrôle du nombre max de torrents actifs
        ensure_memory_limit()

    except Exception as e:
        logger.error(f"Error adding torrent from file {file_path}: {e}")

# -----------------------------------------------------------------------------
# 11. Événements startup / shutdown
# -----------------------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    init_db()
    load_session_state()

    # On va réactiver uniquement les torrents marqués active=1,
    # mais attention si on en a trop, on appliquera le LRU
    active_hashes = db_list_active_torrents()
    for h in active_hashes:
        record = db_get_torrent(h)
        if not record:
            continue
        torrent_file_path = os.path.join(torrent_files_dir, f"{h}.torrent")
        if os.path.exists(torrent_file_path):
            add_torrent_from_file(
                file_path=torrent_file_path,
                info_hash=h,
                total_uploaded=record["total_uploaded"],
                total_downloaded=record["total_downloaded"],
                name=record["name"]
            )
        else:
            logger.warning(f"[STARTUP] .torrent manquant pour {h}, DB le signale actif.")
    logger.info("Application démarrée.")

@app.on_event("shutdown")
def shutdown_event():
    # Sauvegarder la session
    save_session_state()

    # Mettre à jour la DB pour sauvegarder stats finales
    for info_hash, handle in torrents_actifs.copy().items():
        st = handle.status()
        db_update_torrent_stats(info_hash, st.total_upload, st.total_done)

    logger.info("Application arrêtée.")

# -----------------------------------------------------------------------------
# 12. Endpoints BitTorrent
# -----------------------------------------------------------------------------
@app.post("/add-torrents/")
async def add_torrents(
    files: List[UploadFile] = File(..., max_length=5_000_000),
    credentials: HTTPAuthorizationCredentials = Depends(verify_token),
    background_tasks: BackgroundTasks = None
):
    """
    Ajoute un ou plusieurs torrents depuis des fichiers .torrent.
    - Sauvegarde en DB + activation immédiate (si on ne dépasse pas la limite).
    - Fichiers de .torrent stockés dans `torrent_files_dir`.
    """
    results = {"success": [], "errors": []}
    allowed_mime_types = ["application/x-bittorrent", "application/octet-stream"]

    async def process_torrent_file(file: UploadFile):
        if file.content_type not in allowed_mime_types:
            return {"filename": file.filename, "error": f"MIME type non autorisé ({file.content_type})."}

        try:
            contents = await file.read()
            try:
                info_obj = lt.torrent_info(lt.bdecode(contents))
            except Exception as e:
                return {"filename": file.filename, "error": f"Fichier invalide: {e}"}

            info_hash = str(info_obj.info_hash())
            existing = db_get_torrent(info_hash)
            if existing:
                return {"filename": file.filename, "error": f"Torrent déjà présent: {info_hash}"}

            # On sauvegarde le .torrent
            torrent_path = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
            with open(torrent_path, 'wb') as tf:
                tf.write(contents)

            # Enregistrement en DB + marquer actif
            db_insert_torrent(info_hash, info_obj.name())

            # Activation dans la session
            add_torrent_from_file(
                file_path=torrent_path,
                info_hash=info_hash,
                name=info_obj.name()
            )
            return {"filename": file.filename, "info_hash": info_hash}

        except Exception as e:
            logger.error(f"Error uploading file {file.filename}: {e}")
            return {"filename": file.filename, "error": str(e)}

    tasks = [process_torrent_file(f) for f in files]
    results_list = await asyncio.gather(*tasks)

    for r in results_list:
        if "error" in r:
            results["errors"].append(r)
        else:
            results["success"].append(r)

    if background_tasks:
        await trigger_webhooks("torrent_added", {"results": results_list}, background_tasks)

    return results

@app.get("/torrents/")
async def list_torrents(
    credentials: HTTPAuthorizationCredentials = Depends(verify_token),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=1000)
):
    """
    Liste paginée des torrents depuis la DB.
    - Renvoie quelques stats si le torrent est actif dans la session.
    """
    db_t = db_list_torrents(offset=offset, limit=limit)
    results = []

    for row in db_t:
        info_hash = row["info_hash"]
        handle = torrents_actifs.get(info_hash)
        if handle:
            st = handle.status()
            ratio = st.total_upload / st.total_done if st.total_done > 0 else 0
            results.append({
                "info_hash": info_hash,
                "name": st.name,
                "status": str(st.state),
                "progress_percent": round(st.progress * 100, 2),
                "download_rate_kbps": st.download_rate / 1000,
                "upload_rate_kbps": st.upload_rate / 1000,
                "seedtime_hours": round(st.seeding_time / 3600, 2),
                "num_peers": st.num_peers,
                "ratio": f"{ratio:.6f}",
                "total_uploaded": st.total_upload,
                "total_downloaded": st.total_done,
                "active": 1,
            })
        else:
            # Torrent inactif
            results.append({
                "info_hash": info_hash,
                "name": row["name"],
                "status": "inactive",
                "progress_percent": 0.0,
                "download_rate_kbps": 0.0,
                "upload_rate_kbps": 0.0,
                "seedtime_hours": 0,
                "num_peers": 0,
                "ratio": "0.000000",
                "total_uploaded": row["total_uploaded"],
                "total_downloaded": row["total_downloaded"],
                "active": row["active"],
            })

    return {
        "offset": offset,
        "limit": limit,
        "count": len(results),
        "torrents": results
    }

@app.post("/remove-torrents/")
async def remove_torrents(
    request: TorrentRemovalRequest,
    credentials: HTTPAuthorizationCredentials = Depends(verify_token)
):
    """
    Supprime un ou plusieurs torrents, et éventuellement leurs fichiers du disque.
    """
    removed = []
    not_found = []
    files_removed = []

    for info_hash in request.info_hashes:
        record = db_get_torrent(info_hash)
        if not record:
            not_found.append(info_hash)
            continue

        # Si actif, on retire le handle
        if info_hash in torrents_actifs:
            handle = torrents_actifs[info_hash]
            session.remove_torrent(handle, request.remove_files)
            torrents_actifs.pop(info_hash, None)

        # Supprime le .torrent
        tpath = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
        if os.path.exists(tpath):
            os.remove(tpath)
            files_removed.append(tpath)

        # Supprime fichiers réels si demandé
        if request.remove_files and record["name"] and record["name"] != "Unknown":
            potential_path = os.path.join(downloads_path, record["name"])
            if os.path.exists(potential_path):
                shutil.rmtree(potential_path, ignore_errors=True)

        # Enfin, suppression de la DB
        db_delete_torrent(info_hash)
        removed.append(info_hash)

    if not removed and not not_found:
        raise HTTPException(status_code=404, detail="Aucun torrent trouvé pour les info_hash fournis.")

    return {
        "message": "Suppression terminée.",
        "removed": removed,
        "not_found": not_found,
        "files_removed": files_removed
    }

# -----------------------------------------------------------------------------
# 13. Nouveaux endpoints pour gérer pause/resume
# -----------------------------------------------------------------------------
@app.post("/pause-torrent/{info_hash}")
def pause_torrent_endpoint(info_hash: str, credentials: HTTPAuthorizationCredentials = Depends(verify_token)):
    """Met un torrent en pause (suppression du handle + active=0)."""
    record = db_get_torrent(info_hash)
    if not record:
        raise HTTPException(status_code=404, detail="Torrent introuvable.")

    if record["active"] == 0:
        return {"message": "Déjà inactif."}

    pause_torrent(info_hash)
    return {"message": f"Torrent {info_hash} mis en pause."}

@app.post("/resume-torrent/{info_hash}")
def resume_torrent_endpoint(info_hash: str, credentials: HTTPAuthorizationCredentials = Depends(verify_token)):
    """Réactive un torrent en pause, en tenant compte de la limite de torrents actifs."""
    resume_torrent(info_hash)
    ensure_memory_limit()
    return {"message": f"Torrent {info_hash} réactivé (si .torrent présent)."}

# -----------------------------------------------------------------------------
# 14. Endpoint d'informations système (avec cache)
# -----------------------------------------------------------------------------
system_info_cache = {
    "data": None,
    "last_fetch": 0,
    "ttl": 5
}

@app.get("/system-info/")
async def system_info(credentials: HTTPAuthorizationCredentials = Depends(verify_token)):
    now = time()
    if system_info_cache["data"] is not None and (now - system_info_cache["last_fetch"]) < system_info_cache["ttl"]:
        return system_info_cache["data"]

    disk_usage = psutil.disk_usage('/')
    mem_info = psutil.virtual_memory()
    data = {
        "disk_total_gb": f"{disk_usage.total / (1024**3):.2f} Go",
        "disk_used_gb": f"{disk_usage.used / (1024**3):.2f} Go",
        "disk_free_gb": f"{disk_usage.free / (1024**3):.2f} Go",
        "disk_percent_used": f"{disk_usage.percent}%",
        "cpu_usage_percent": psutil.cpu_percent(),
        "memory_total_gb": f"{mem_info.total / (1024**3):.2f} Go",
        "memory_available_gb": f"{mem_info.available / (1024**3):.2f} Go",
        "memory_used_gb": f"{mem_info.used / (1024**3):.2f} Go",
        "memory_free_gb": f"{mem_info.free / (1024**3):.2f} Go",
        "memory_percent_used": f"{mem_info.percent}%"
    }
    system_info_cache["data"] = data
    system_info_cache["last_fetch"] = now
    return data

# -----------------------------------------------------------------------------
# 15. Webhooks
# -----------------------------------------------------------------------------
webhooks = []

@app.post("/register-webhook/")
async def register_webhook(webhook: Webhook, credentials: HTTPAuthorizationCredentials = Depends(verify_token)):
    for w in webhooks:
        if w.url == webhook.url and w.event == webhook.event:
            raise HTTPException(status_code=400, detail="Webhook déjà enregistré pour cet event et cette URL.")
    webhooks.append(webhook)
    logger.info(f"Webhook registered: {webhook}")
    return {"message": "Webhook enregistré avec succès."}

async def trigger_webhooks(event: str, data: dict, background_tasks: BackgroundTasks):
    for w in webhooks:
        if w.event == event:
            background_tasks.add_task(send_webhook, w.url, data)

async def send_webhook(url: str, data: dict):
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=data, timeout=10.0)
            logger.info(f"Webhook sent to {url}, response status code: {response.status_code}")
    except httpx.RequestError as e:
        logger.error(f"Failed to send webhook to {url}: {e}")

# -----------------------------------------------------------------------------
# 16. Lancement direct (pour dev)
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    init_db()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

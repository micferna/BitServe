from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import httpx
from pydantic import BaseModel, Field
from typing import List, Optional
import libtorrent as lt
import psutil
import os
import json
import logging

# ----------------------------------------
# Configuration du logger
# ----------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
)
logger = logging.getLogger(__name__)

# ----------------------------------------
# Paramètres de sécurité
# ----------------------------------------
API_TOKEN = "secret-token-here"  # À adapter / stocker en variable d'environnement ou autre

security = HTTPBearer()

def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    Vérifie la présence d'un token d'API dans l'en-tête d'autorisation.
    Ex: Autorization: Bearer secret-token-here
    """
    if credentials.credentials != API_TOKEN:
        raise HTTPException(
            status_code=401, detail="Token d'API invalide ou manquant."
        )
    return credentials

# ----------------------------------------
# Configuration de l'application FastAPI
# ----------------------------------------
app = FastAPI(title="BitTorrent Manager Secure")

# ----------------------------------------
# Répertoires de travail
# ----------------------------------------
bitserve_dir = "./.bitserve"
os.makedirs(bitserve_dir, exist_ok=True)

downloads_path = "./downloads"
os.makedirs(downloads_path, exist_ok=True)

torrent_files_dir = os.path.join(bitserve_dir, "torrent_files")
os.makedirs(torrent_files_dir, exist_ok=True)

state_file_path = os.path.join(bitserve_dir, "session_state.dat")
torrents_file_path = os.path.join(bitserve_dir, "torrents_data.json")

# ----------------------------------------
# Configuration avancée libtorrent
# ----------------------------------------
# Exemple de limites : on force 200 connexions max, etc.
session_params = lt.session_params()
session_params.settings = {
    'listen_interfaces': '0.0.0.0:6881',
    'connection_limit': 200,        # max 200 connexions
    'upload_rate_limit': 0,         # pas de limite d'upload (0 = illimité), ajustez si besoin
    'download_rate_limit': 0,       # pas de limite de download (0 = illimité)
    # 'alert_mask': lt.alert.category_t.all_categories,  # Pour déboguer si besoin
    # ...
}
# Possibilité de définir un répertoire de resume data
resume_data_directory = os.path.join(bitserve_dir, "resume_data")
os.makedirs(resume_data_directory, exist_ok=True)

# On active la sauvegarde automatique de resume data dans ce répertoire
# (fonctionne mieux avec libtorrent >= 2.0)
session_params.settings['resume_save_path'] = resume_data_directory

session = lt.session(session_params)

# Dictionnaire pour suivre les torrents
torrents = {}
# Liste des webhooks
webhooks = []

# ----------------------------------------
# Modèles Pydantic
# ----------------------------------------
class Webhook(BaseModel):
    event: str
    url: str

class TorrentRemovalRequest(BaseModel):
    info_hashes: List[str] = Field(..., min_items=1, description="Liste des info_hash à supprimer.")
    remove_files: Optional[bool] = False

# ----------------------------------------
# Fonctions de gestion
# ----------------------------------------
def save_session_state():
    """
    Sauvegarde l'état global de la session (fichier .dat).
    """
    try:
        with open(state_file_path, "wb") as f:
            f.write(lt.bencode(session.save_state()))
        logger.info("Session state saved.")
    except Exception as e:
        logger.error(f"Error saving session state: {e}")

def load_session_state():
    """
    Restaure l'état global de la session si le fichier existe.
    """
    if os.path.exists(state_file_path):
        try:
            with open(state_file_path, "rb") as f:
                session.load_state(lt.bdecode(f.read()))
            logger.info("Session state restored.")
        except Exception as e:
            logger.error(f"Error restoring session state: {e}")

def save_torrents_data():
    """
    Sauvegarde dans un .json les infos essentielles de chaque torrent (upload/download/name).
    """
    for info_hash, torrent_data in torrents.items():
        torrent_status = torrent_data['handle'].status()
        torrent_data['total_uploaded'] = torrent_status.total_upload
        torrent_data['total_downloaded'] = torrent_status.total_done

    data_to_save = {
        info_hash: {
            "info_hash": info_hash,
            "name": torrent_data['handle'].status().name,
            "total_uploaded": torrent_data['total_uploaded'],
            "total_downloaded": torrent_data['total_downloaded'],
        } for info_hash, torrent_data in torrents.items()
    }

    try:
        with open(torrents_file_path, "w", encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        logger.info("Torrents data saved to JSON.")
    except Exception as e:
        logger.error(f"Error saving torrents data: {e}")

def load_torrents_data():
    """
    Charge le .json des torrents et ré-ajoute chaque torrent via add_torrent_from_file.
    """
    if os.path.exists(torrents_file_path):
        try:
            with open(torrents_file_path, encoding='utf-8') as f:
                loaded_torrents = json.load(f)

            for info_hash, torrent_data in loaded_torrents.items():
                torrent_file_path = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
                if os.path.exists(torrent_file_path):
                    add_torrent_from_file(
                        file_path=torrent_file_path,
                        info_hash=info_hash,
                        total_uploaded=torrent_data.get('total_uploaded', 0),
                        total_downloaded=torrent_data.get('total_downloaded', 0),
                        name=torrent_data.get('name', "Unknown")
                    )
            logger.info("Torrents data loaded.")
        except Exception as e:
            logger.error(f"Error loading torrents data: {e}")

def add_torrent_from_file(file_path, info_hash, total_uploaded=0, total_downloaded=0, name="Unknown"):
    """
    Ajoute un torrent à la session à partir d'un fichier .torrent,
    tout en initialisant les données de suivi.
    """
    try:
        with open(file_path, 'rb') as f:
            e = lt.bdecode(f.read())
            info = lt.torrent_info(e)
            # IMPORTANT: on force save_path dans un dossier contrôlé
            # pour éviter que le torrent aille écrire ailleurs.
            params = {
                'ti': info,
                'save_path': downloads_path,  # Chemin contrôlé
                # 'storage_mode': lt.storage_mode_t.storage_mode_sparse, # Option si besoin
            }
            handle = session.add_torrent(params)

        torrents[info_hash] = {
            'handle': handle,
            'total_uploaded': total_uploaded,
            'total_downloaded': total_downloaded,
            'name': name,
        }
    except Exception as e:
        logger.error(f"Error adding torrent from file {file_path}: {e}")

# ----------------------------------------
# Événements startup / shutdown
# ----------------------------------------
@app.on_event("startup")
async def startup_event():
    load_session_state()
    load_torrents_data()

@app.on_event("shutdown")
def shutdown_event():
    save_session_state()
    save_torrents_data()

# ----------------------------------------
# Endpoints BitTorrent
# ----------------------------------------
@app.post("/add-torrents/")
async def add_torrents(
    files: List[UploadFile] = File(..., description="Fichiers .torrent à uploader", max_length=5_000_000),
    credentials: HTTPAuthorizationCredentials = Depends(verify_token)
):
    """
    Ajout de torrents à partir de fichiers .torrent.
    - Limitation de 5 Mo par fichier.
    - Vérification basique du MIME type.
    - Enregistrement de chaque .torrent dans torrent_files_dir.
    """
    results = {"success": [], "errors": []}

    allowed_mime_types = ["application/x-bittorrent", "application/octet-stream"]
    for file in files:
        # Vérification du MIME type
        if file.content_type not in allowed_mime_types:
            msg = f"MIME type non autorisé ({file.content_type})."
            results["errors"].append({"filename": file.filename, "error": msg})
            continue

        try:
            contents = await file.read()

            # Vérification structure torrent (bdecode)
            try:
                info_obj = lt.torrent_info(lt.bdecode(contents))
            except Exception as e:
                msg = f"Le fichier n'est pas un torrent valide: {e}"
                results["errors"].append({"filename": file.filename, "error": msg})
                continue

            info_hash = str(info_obj.info_hash())

            if info_hash in torrents:
                msg = "Torrent déjà ajouté."
                results["errors"].append({"filename": file.filename, "error": msg})
                continue

            # Sauvegarde du fichier .torrent
            torrent_file_path = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
            with open(torrent_file_path, 'wb') as torrent_file:
                torrent_file.write(contents)

            add_torrent_from_file(torrent_file_path, info_hash)
            results["success"].append({"filename": file.filename, "info_hash": info_hash})

        except Exception as e:
            logger.error(f"Error uploading file {file.filename}: {e}")
            results["errors"].append({"filename": file.filename, "error": str(e)})

    return results


@app.get("/torrents/")
async def list_torrents(credentials: HTTPAuthorizationCredentials = Depends(verify_token)):
    """
    Retourne la liste des torrents, avec infos de progression, ratio, etc.
    """
    torrents_list = []
    for info_hash, torrent_data in torrents.items():
        handle = torrent_data['handle']
        status = handle.status()
        # Calcul ratio
        ratio = (
            torrent_data['total_uploaded'] / torrent_data['total_downloaded']
            if torrent_data['total_downloaded'] > 0 else 0
        )
        formatted_ratio = f"{ratio:.6f}"

        torrents_list.append({
            "info_hash": info_hash,
            "name": status.name,
            "progress_percent": status.progress * 100,
            "download_rate_kbps": status.download_rate / 1000,
            "upload_rate_kbps": status.upload_rate / 1000,
            "status": str(status.state),
            "seedtime_hours": status.seeding_time / 3600,
            "num_peers": status.num_peers,
            "ratio": formatted_ratio
        })
    return torrents_list


@app.post("/remove-torrents/")
async def remove_torrents(
    request: TorrentRemovalRequest,
    credentials: HTTPAuthorizationCredentials = Depends(verify_token)
):
    """
    Supprime un ou plusieurs torrents, et éventuellement les fichiers du disque.
    """
    removed = []
    not_found = []
    files_removed = []

    for info_hash in request.info_hashes:
        if info_hash in torrents:
            handle = torrents[info_hash]['handle']
            # Supprime le torrent de la session
            session.remove_torrent(handle, request.remove_files)

            # On supprime la référence locale
            del torrents[info_hash]

            # Supprime le fichier .torrent
            torrent_file_path = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
            if os.path.exists(torrent_file_path):
                os.remove(torrent_file_path)
                files_removed.append(torrent_file_path)

            removed.append(info_hash)
        else:
            not_found.append(info_hash)

    if not removed:
        raise HTTPException(status_code=404, detail="Aucun torrent trouvé pour les info_hash fournis.")

    return {
        "message": "Suppression terminée.",
        "removed": removed,
        "not_found": not_found,
        "files_removed": files_removed
    }

# ----------------------------------------
# Endpoint d'informations système
# ----------------------------------------
@app.get("/system-info/")
async def system_info(credentials: HTTPAuthorizationCredentials = Depends(verify_token)):
    disk_usage = psutil.disk_usage('/')
    mem_info = psutil.virtual_memory()
    return {
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

# ----------------------------------------
# Webhooks
# ----------------------------------------
@app.post("/register-webhook/")
async def register_webhook(
    webhook: Webhook,
    credentials: HTTPAuthorizationCredentials = Depends(verify_token)
):
    """
    Enregistre un webhook pour un événement donné.
    Exemple : { "event": "torrent_added", "url": "https://..." }
    """
    webhooks.append(webhook)
    logger.info(f"Webhook registered: {webhook}")
    return {"message": "Webhook enregistré avec succès."}

async def trigger_webhooks(event: str, data: dict, background_tasks: BackgroundTasks):
    """
    Déclenche l'envoi d'un webhook si l'événement correspond à un webhook enregistré.
    """
    for w in webhooks:
        if w.event == event:
            background_tasks.add_task(send_webhook, w.url, data)

async def send_webhook(url: str, data: dict):
    """
    Envoie les données à l'URL spécifiée via requête POST asynchrone.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=data, timeout=10.0)
            logger.info(f"Webhook sent to {url}, response status code: {response.status_code}")
    except httpx.RequestError as e:
        logger.error(f"Failed to send webhook to {url}: {e}")

# ----------------------------------------
# Lancement direct
# ----------------------------------------
if __name__ == "__main__":
    import uvicorn
    # Pour activer TLS, utilisez un reverse proxy ou spécifiez vos cert/key ici.
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")

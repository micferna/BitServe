from fastapi import FastAPI, HTTPException, UploadFile, File, BackgroundTasks
import httpx
from pydantic import BaseModel
from typing import List, Optional
import libtorrent as lt
import psutil
import os
import json

app = FastAPI(title="BitTorrent Manager")

# Configuration et gestion de la session libtorrent
session_params = lt.session_params()
session_params.settings = {'listen_interfaces': '0.0.0.0:6881'}
session = lt.session(session_params)

# Structures de données pour stocker les torrents et webhooks
torrents = {}
webhooks = []

# Répertoires pour la sauvegarde et les téléchargements
bitserve_dir = "./.bitserve"
state_file_path = os.path.join(bitserve_dir, "session_state.dat")
torrents_file_path = os.path.join(bitserve_dir, "torrents_data.json")
downloads_path = "./downloads"
torrent_files_dir = os.path.join(bitserve_dir, "torrent_files")

# S'assurer que les répertoires nécessaires existent
os.makedirs(bitserve_dir, exist_ok=True)
os.makedirs(downloads_path, exist_ok=True)
os.makedirs(torrent_files_dir, exist_ok=True)

# Modèles Pydantic pour la validation des données
class Webhook(BaseModel):
    event: str
    url: str

class TorrentRemovalRequest(BaseModel):
    info_hashes: List[str]
    remove_files: Optional[bool] = False

# Fonctions de gestion des torrents et de la session
def save_session_state():
    with open(state_file_path, "wb") as f:
        f.write(lt.bencode(session.save_state()))
    print("Session state saved.")

def load_session_state():
    if os.path.exists(state_file_path):
        with open(state_file_path, "rb") as f:
            session.load_state(lt.bdecode(f.read()))
        print("Session state restored.")

def save_torrents_data():
    for info_hash, torrent in torrents.items():
        torrent_status = torrent['handle'].status()
        # Mise à jour des valeurs avec les données actuelles
        torrent['total_uploaded'] = torrent_status.total_upload
        torrent['total_downloaded'] = torrent_status.total_done

    data_to_save = {
        info_hash: {
            "info_hash": info_hash,
            "name": torrent['handle'].status().name,
            "total_uploaded": torrent['total_uploaded'],
            "total_downloaded": torrent['total_downloaded'],
        } for info_hash, torrent in torrents.items()
    }
    with open(torrents_file_path, "w") as f:
        json.dump(data_to_save, f)


def load_torrents_data():
    # Charge les données des torrents et recrée la structure `torrents`
    if os.path.exists(torrents_file_path):
        with open(torrents_file_path) as f:
            loaded_torrents = json.load(f)
        
        for info_hash, torrent_data in loaded_torrents.items():
            add_torrent_from_file(
                file_path=os.path.join(torrent_files_dir, f"{info_hash}.torrent"),
                info_hash=info_hash,
                total_uploaded=torrent_data.get('total_uploaded', 0),
                total_downloaded=torrent_data.get('total_downloaded', 0),
                name=torrent_data.get('name', "Unknown")
            )
    print("Torrents data loaded.")

def add_torrent_from_file(file_path, info_hash, total_uploaded=0, total_downloaded=0, name="Unknown"):
    # Ajoute un torrent à partir d'un fichier et initialise les données
    with open(file_path, 'rb') as f:
        e = lt.bdecode(f.read())
        info = lt.torrent_info(e)
        params = {
            'ti': info,
            'save_path': downloads_path,
        }
        handle = session.add_torrent(params)
        torrents[info_hash] = {
            'handle': handle,
            # Utilise les valeurs fournies pour initialiser les totaux
            'total_uploaded': total_uploaded,
            'total_downloaded': total_downloaded,
            'name': name,
        }

# Gestion des événements de l'application
@app.on_event("startup")
async def startup_event():
    load_session_state()
    load_torrents_data()

@app.on_event("shutdown")
def shutdown_event():
    save_session_state()
    save_torrents_data()

# Endpoints API pour la gestion des torrents
@app.post("/add-torrents/")
async def add_torrents(files: List[UploadFile] = File(...)):
    results = {"success": [], "errors": []}
    for file in files:
        try:
            contents = await file.read()
            info = lt.torrent_info(lt.bdecode(contents))
            info_hash = str(info.info_hash())

            if info_hash in torrents:
                results["errors"].append({"filename": file.filename, "error": "Torrent already added."})
                continue

            torrent_file_path = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
            with open(torrent_file_path, 'wb') as torrent_file:
                torrent_file.write(contents)

            add_torrent_from_file(torrent_file_path, info_hash)
            results["success"].append({"filename": file.filename, "info_hash": info_hash})
        except Exception as e:
            results["errors"].append({"filename": file.filename, "error": str(e)})
    return results

@app.get("/torrents/")
async def list_torrents():
    torrents_list = []
    for info_hash, torrent in torrents.items():
        handle = torrent['handle']
        status = handle.status()
        ratio = (torrent['total_uploaded'] / torrent['total_downloaded']) if torrent['total_downloaded'] > 0 else 0
        formatted_ratio = f"{ratio:.6f}"
        torrents_list.append({
            "info_hash": info_hash,
            "name": status.name,
            "progress": status.progress * 100,
            "download_rate": status.download_rate / 1000,
            "upload_rate": status.upload_rate / 1000,
            "status": str(status.state),
            "seedtime_hours": status.seeding_time / 3600,
            "num_peers": status.num_peers,
            "ratio": formatted_ratio
        })
    return torrents_list


@app.post("/remove-torrents/")
async def remove_torrents(request: TorrentRemovalRequest):
    removed = []
    not_found = []
    files_removed = []

    for info_hash in request.info_hashes:
        if info_hash in torrents:
            handle = torrents[info_hash]
            session.remove_torrent(handle, request.remove_files)
            del torrents[info_hash]

            torrent_file_path = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
            if os.path.exists(torrent_file_path):
                os.remove(torrent_file_path)
                files_removed.append(torrent_file_path)

            removed.append(info_hash)
        else:
            not_found.append(info_hash)

    if not removed:
        raise HTTPException(status_code=404, detail="Torrents not found.")

    return {"message": "Torrents removal process completed.", "removed": removed, "not_found": not_found, "files_removed": files_removed}

# Informations système
@app.get("/system-info/")
async def system_info():
    disk_usage = psutil.disk_usage('/')
    return {
        "disk_total_gb": f"{disk_usage.total / (1024**3):.2f} Go",
        "disk_used_gb": f"{disk_usage.used / (1024**3):.2f} Go",
        "disk_free_gb": f"{disk_usage.free / (1024**3):.2f} Go",
        "disk_percent_used": f"{disk_usage.percent}%",
        "cpu_usage_percent": psutil.cpu_percent(),
        "memory_total_gb": f"{psutil.virtual_memory().total / (1024**3):.2f} Go",
        "memory_available_gb": f"{psutil.virtual_memory().available / (1024**3):.2f} Go",
        "memory_used_gb": f"{psutil.virtual_memory().used / (1024**3):.2f} Go",
        "memory_free_gb": f"{psutil.virtual_memory().free / (1024**3):.2f} Go",
        "memory_percent_used": f"{psutil.virtual_memory().percent}%"
    }

# Enregistrement et déclenchement de webhooks
@app.post("/register-webhook/")
async def register_webhook(webhook: Webhook):
    webhooks.append(webhook)
    return {"message": "Webhook registered successfully."}

async def trigger_webhooks(event: str, data: dict, background_tasks: BackgroundTasks):
    for webhook in webhooks:
        if webhook.event == event:
            background_tasks.add_task(send_webhook, webhook.url, data)

async def send_webhook(url: str, data: dict):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=data)
    except httpx.RequestError as e:
        print(f"Failed to send webhook: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
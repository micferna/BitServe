from fastapi import FastAPI, HTTPException, UploadFile, File
from typing import List, Optional
import libtorrent as lt
import psutil
import httpx
import os
import json
from pydantic import BaseModel
import atexit

app = FastAPI()

# Configuration initiale de la session libtorrent
session_params = lt.session_params()
session_params.settings = {'listen_interfaces': '0.0.0.0:6881'}
session = lt.session(session_params)

torrents = {}

# Chemin vers le dossier .bitserve pour les fichiers de sauvegarde
bitserve_dir = "./.bitserve"

# Chemins vers les fichiers de sauvegarde
state_file_path = os.path.join(bitserve_dir, "session_state.dat")
torrents_file_path = os.path.join(bitserve_dir, "torrents_data.json")

# Chemin vers le répertoire de téléchargement, directement dans le répertoire courant de l'application
downloads_path = "./downloads"

# Définition du chemin vers le répertoire où seront stockés les fichiers .torrent
torrent_files_dir = os.path.join(bitserve_dir, "torrent_files")  # Ajout de cette ligne

# Assurez-vous que les répertoires existent
os.makedirs(bitserve_dir, exist_ok=True)
os.makedirs(downloads_path, exist_ok=True)
os.makedirs(torrent_files_dir, exist_ok=True)  # Assurez-vous que ce répertoire existe également


class Webhook(BaseModel):
    event: str
    url: str

webhooks = []

class TorrentRemovalRequest(BaseModel):
    info_hashes: List[str]
    remove_files: Optional[bool] = False

def save_session_state():
    with open(state_file_path, "wb") as f:
        f.write(lt.bencode(session.save_state()))
    print("État de la session sauvegardé.")

def load_session_state():
    if os.path.exists(state_file_path):
        with open(state_file_path, "rb") as f:
            session.load_state(lt.bdecode(f.read()))
        print("État de la session restauré.")

def save_torrents_data():
    data_to_save = {info_hash: {"info_hash": info_hash, "name": handle.status().name}
                    for info_hash, handle in torrents.items()}
    with open(torrents_file_path, "w") as f:
        json.dump(data_to_save, f)


def load_torrents_data():
    if os.path.exists(torrents_file_path):
        with open(torrents_file_path, "r") as f:
            loaded_torrents = json.load(f)
        for info_hash, torrent_data in loaded_torrents.items():
            torrent_file_path = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
            if os.path.exists(torrent_file_path):
                add_torrent_from_file(torrent_file_path, info_hash)

def add_torrent_from_file(file_path, info_hash):
    with open(file_path, 'rb') as f:
        e = lt.bdecode(f.read())
        info = lt.torrent_info(e)
        params = {
            'ti': info,
            'save_path': downloads_path,
        }
        handle = session.add_torrent(params)
        torrents[info_hash] = handle  # Assurez-vous que cette ligne ne crée pas de conflit avec votre logique existante


@app.on_event("startup")
async def startup_event():
    load_session_state()
    load_torrents_data()

@app.on_event("shutdown")
def shutdown_event():
    save_session_state()
    save_torrents_data()


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

            # Sauvegarde du fichier .torrent dans le répertoire torrent_files
            torrent_file_path = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
            with open(torrent_file_path, 'wb') as torrent_file:
                torrent_file.write(contents)

            # Ajout du torrent à la session
            params = {'ti': info, 'save_path': downloads_path}
            handle = session.add_torrent(params)
            torrents[info_hash] = handle
            results["success"].append({"filename": file.filename, "info_hash": info_hash})
        except Exception as e:
            results["errors"].append({"filename": file.filename, "error": str(e)})
    
    return results


@app.get("/torrents/")
async def list_torrents():
    torrents_list = []
    for info_hash, handle in torrents.items():
        status = handle.status()
        torrents_list.append({
            "info_hash": info_hash,
            "name": status.name,
            "progress": status.progress * 100,
            "download_rate": status.download_rate / 1000,
            "upload_rate": status.upload_rate / 1000,
            "status": str(status.state),
            "seedtime_hours": status.seeding_time / 3600,
            "num_peers": status.num_peers
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
            # Suppression du torrent de la session libtorrent
            session.remove_torrent(handle, request.remove_files)
            del torrents[info_hash]

            # Tentative de suppression du fichier .torrent associé
            torrent_file_path = os.path.join(torrent_files_dir, f"{info_hash}.torrent")
            try:
                if os.path.exists(torrent_file_path):
                    os.remove(torrent_file_path)
                    files_removed.append(torrent_file_path)
            except Exception as e:
                # Gérer l'erreur si nécessaire, par exemple en loggant l'erreur
                print(f"Erreur lors de la suppression du fichier .torrent : {e}")

            removed.append(info_hash)
        else:
            not_found.append(info_hash)

    if not removed:
        raise HTTPException(status_code=404, detail="Torrents not found.")

    return {"message": "Torrents removal process completed.", "removed": removed, "not_found": not_found, "files_removed": files_removed}


@app.get("/system-info/")
async def system_info():
    disk_usage = psutil.disk_usage('/')
    # Convertir en Go et arrondir à 2 décimales
    disk_total_gb = round(disk_usage.total / (1024**3), 2)
    disk_used_gb = round(disk_usage.used / (1024**3), 2)
    disk_free_gb = round(disk_usage.free / (1024**3), 2)
    disk_percent_used = disk_usage.percent

    # Convertir la mémoire en Go et arrondir à 2 décimales
    memory_info = psutil.virtual_memory()
    memory_total_gb = round(memory_info.total / (1024**3), 2)
    memory_available_gb = round(memory_info.available / (1024**3), 2)
    memory_used_gb = round(memory_info.used / (1024**3), 2)
    memory_free_gb = round(memory_info.free / (1024**3), 2)
    memory_percent_used = memory_info.percent

    return {
        "disk_total_gb": f"{disk_total_gb} Go",
        "disk_used_gb": f"{disk_used_gb} Go",
        "disk_free_gb": f"{disk_free_gb} Go",
        "disk_percent_used": f"{disk_percent_used}%",
        "cpu_usage_percent": psutil.cpu_percent(),
        "memory_total_gb": f"{memory_total_gb} Go",
        "memory_available_gb": f"{memory_available_gb} Go",
        "memory_used_gb": f"{memory_used_gb} Go",
        "memory_free_gb": f"{memory_free_gb} Go",
        "memory_percent_used": f"{memory_percent_used}%"
    }

# Endpoint pour enregistrer un webhook
@app.post("/register-webhook/")
async def register_webhook(event: str, url: str):
    webhooks.append(Webhook(event, url))
    return {"message": "Webhook registered successfully."}

# Fonction pour déclencher des webhooks (non connectée à un endpoint)
async def trigger_webhooks(event: str, data: dict):
    for webhook in webhooks:
        if webhook.event == event:
            async with httpx.AsyncClient() as client:
                try:
                    await client.post(webhook.url, json=data)
                except httpx.RequestError as e:
                    print(f"Failed to send webhook: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="DEBUG")
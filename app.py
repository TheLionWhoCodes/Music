from flask import Flask, request, jsonify, session, send_file, Response
import tidalapi
import os, re, json, uuid, shutil, subprocess, tempfile, threading, time, base64
import requests as req_lib

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tidal-web-secret-2024")

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_dur(sec):
    return f"{int(sec)//60}:{int(sec)%60:02d}"

def parse_tidal_url(url):
    patterns = [
        (r"tidal\.com/(?:\w+/)?track/(\d+)",              "track"),
        (r"tidal\.com/(?:\w+/)?album/(\d+)",               "album"),
        (r"tidal\.com/(?:\w+/)?playlist/([\w-]+)",         "playlist"),
        (r"listen\.tidal\.com/(?:\w+/)?track/(\d+)",       "track"),
        (r"listen\.tidal\.com/(?:\w+/)?album/(\d+)",       "album"),
        (r"listen\.tidal\.com/(?:\w+/)?playlist/([\w-]+)", "playlist"),
    ]
    for pattern, tipo in patterns:
        m = re.search(pattern, url)
        if m:
            return tipo, m.group(1)
    return None, None

def safe_fn(s):
    return re.sub(r'[\\/*?:"<>|]', "", str(s or "")).strip()

def get_tidal_session(access_token):
    for attr in ["master","hi_res_lossless","hi_res","high","hifi","lossless","low"]:
        if hasattr(tidalapi.Quality, attr):
            best = getattr(tidalapi.Quality, attr)
            break
    else:
        best = None
    try:
        config = tidalapi.Config(quality=best) if best else tidalapi.Config()
        tidal  = tidalapi.Session(config)
        tidal.load_oauth_session(
            token_type="Bearer", access_token=access_token,
            refresh_token=None, expiry_time=None,
        )
        if tidal.check_login():
            return tidal, None
        return None, "Token inválido o expirado"
    except Exception as e:
        return None, str(e)

def detect_plan(tidal):
    try:
        sub  = tidal.user.subscription
        plan = str(getattr(sub,"type","") or getattr(sub,"highestSoundQuality","")).upper()
    except:
        plan = ""
    if any(x in plan for x in ["HI_RES","HIRES","MASTER","DOLBY"]):
        return "HiFi Plus", "MASTER"
    elif any(x in plan for x in ["HIFI","HI_FI","LOSSLESS"]):
        return "HiFi", "HiFi"
    elif any(x in plan for x in ["PREMIUM","HIGH"]):
        return "Premium", "High"
    else:
        return "Básico", "Normal"

def write_tidal_dl_config(token, work_dir, quality="HiFi", lyrics=True):
    """Escribe el config de tidal-dl para esta sesión."""
    cfg = {
        "downloadPath": work_dir,
        "quality": quality,
        "addLyrics": lyrics,
        "lyricFile": lyrics,
        "usePlaylistFolder": False,
        "albumFolderFormat": "",
        "trackFileFormat": "{ArtistName} - {TrackTitle}",
        "accessToken": token,
        "tokenType": "Bearer",
    }
    cfg_path = os.path.join(work_dir, "tidal-dl.json")
    with open(cfg_path, "w") as f:
        json.dump(cfg, f)
    return cfg_path

def find_audio_file(directory):
    EXTS = {".flac", ".m4a", ".mp3", ".aac", ".opus"}
    for root, _, files in os.walk(directory):
        for f in files:
            if os.path.splitext(f)[1].lower() in EXTS:
                return os.path.join(root, f)
    return None

def find_lrc_file(directory):
    for root, _, files in os.walk(directory):
        for f in files:
            if f.lower().endswith(".lrc"):
                return os.path.join(root, f)
    return None

# ── Token ─────────────────────────────────────────────────────────────────────

@app.route("/api/token", methods=["POST"])
def set_token():
    token = (request.json or {}).get("token", "").strip()
    if not token: return jsonify({"error": "Token vacío"}), 400
    tidal, err = get_tidal_session(token)
    if err: return jsonify({"error": err}), 401
    plan_name, quality = detect_plan(tidal)
    session["access_token"] = token
    session["plan"]         = plan_name
    session["quality"]      = quality
    try:
        u    = tidal.user
        name = (getattr(u,"first_name","") + " " + getattr(u,"last_name","")).strip()
    except:
        name = "Usuario Tidal"
    return jsonify({"ok": True, "user": name or "Usuario Tidal", "plan": plan_name, "quality": quality})

@app.route("/api/token", methods=["DELETE"])
def clear_token():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/token/status")
def token_status():
    token = session.get("access_token")
    if not token: return jsonify({"logged_in": False})
    tidal, err = get_tidal_session(token)
    if err:
        session.clear()
        return jsonify({"logged_in": False})
    try:
        u    = tidal.user
        name = (getattr(u,"first_name","") + " " + getattr(u,"last_name","")).strip()
    except:
        name = "Usuario Tidal"
    return jsonify({
        "logged_in": True,
        "user":    name or "Usuario Tidal",
        "plan":    session.get("plan", "Básico"),
        "quality": session.get("quality", "Normal"),
    })

# ── Debug ─────────────────────────────────────────────────────────────────────

@app.route("/api/debug")
def debug():
    token = session.get("access_token")
    if not token: return jsonify({"error": "Sin sesión"}), 401
    tidal, err = get_tidal_session(token)
    if err: return jsonify({"error": err}), 401
    try:
        u   = tidal.user
        sub = u.subscription
        return jsonify({
            "subscription_dir":  [x for x in dir(sub) if not x.startswith("_")],
            "subscription_dict": sub.__dict__ if hasattr(sub,"__dict__") else str(sub),
            "user_dir":          [x for x in dir(u) if not x.startswith("_")],
        })
    except Exception as e:
        return jsonify({"error": str(e)})

# ── Info ──────────────────────────────────────────────────────────────────────

@app.route("/api/info", methods=["POST"])
def get_info():
    token = session.get("access_token")
    if not token: return jsonify({"error": "Sin sesión activa"}), 401
    url = (request.json or {}).get("url", "").strip()
    tipo, eid = parse_tidal_url(url)
    if not tipo: return jsonify({"error": "URL de Tidal no reconocida"}), 400
    tidal, err = get_tidal_session(token)
    if err: return jsonify({"error": err}), 401
    try:
        if tipo == "track":
            t = tidal.track(int(eid))
            return jsonify({
                "type": "track", "title": t.name, "artist": t.artist.name, "cover": None,
                "tracks": [{"id": t.id, "title": t.name, "artist": t.artist.name,
                            "album": getattr(getattr(t,"album",None),"name",""),
                            "track_num": getattr(t,"track_num",1),
                            "duration": fmt_dur(t.duration), "url": url}]
            })
        elif tipo == "album":
            a  = tidal.album(int(eid)); ts = list(a.tracks())
            cover = None
            try: cover = a.image(320)
            except: pass
            return jsonify({
                "type": "album", "title": a.name, "artist": a.artist.name, "cover": cover,
                "tracks": [{"id": t.id, "title": t.name, "artist": t.artist.name,
                            "album": a.name, "track_num": getattr(t,"track_num",i+1),
                            "duration": fmt_dur(t.duration),
                            "url": f"https://tidal.com/browse/track/{t.id}"}
                           for i,t in enumerate(ts)]
            })
        elif tipo == "playlist":
            p  = tidal.playlist(eid); ts = list(p.tracks())
            return jsonify({
                "type": "playlist", "title": p.name, "artist": "", "cover": None,
                "tracks": [{"id": t.id, "title": t.name, "artist": t.artist.name,
                            "album": getattr(getattr(t,"album",None),"name",""),
                            "track_num": getattr(t,"track_num",i+1),
                            "duration": fmt_dur(t.duration),
                            "url": f"https://tidal.com/browse/track/{t.id}"}
                           for i,t in enumerate(ts)]
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Download audio via tidal-dl ───────────────────────────────────────────────

@app.route("/api/download/<int:track_id>")
def download_audio(track_id):
    token   = session.get("access_token")
    quality = session.get("quality", "Normal")
    if not token: return jsonify({"error": "Sin sesión activa"}), 401

    do_lyrics = request.args.get("lyrics", "true").lower() == "true"

    work_dir     = tempfile.mkdtemp(prefix="tidal_")
    home_cfg     = os.path.expanduser("~/.tidal-dl.json")
    backup_cfg   = home_cfg + ".bak"
    restored     = False

    try:
        # Backup del config global si existe
        if os.path.exists(home_cfg):
            shutil.copy2(home_cfg, backup_cfg)

        # Escribir config con token del usuario
        cfg = {
            "downloadPath": work_dir,
            "quality":      quality,
            "addLyrics":    do_lyrics,
            "lyricFile":    do_lyrics,
            "usePlaylistFolder": False,
            "albumFolderFormat": "",
            "trackFileFormat":   "{ArtistName} - {TrackTitle}",
            "accessToken":  token,
            "tokenType":    "Bearer",
        }
        with open(home_cfg, "w") as f:
            json.dump(cfg, f)

        track_url = f"https://tidal.com/browse/track/{track_id}"
        result = subprocess.run(
            ["tidal-dl", "-l", track_url, "-q", quality],
            capture_output=True, text=True, timeout=120,
            cwd=work_dir
        )

        # Restaurar config global
        if os.path.exists(backup_cfg):
            shutil.copy2(backup_cfg, home_cfg)
            os.remove(backup_cfg)
        restored = True

        audio_path = find_audio_file(work_dir)
        if not audio_path:
            print(f"[stdout] {result.stdout}\n[stderr] {result.stderr}")
            return jsonify({"error": f"No se pudo descargar. Error: {result.stderr[-300:]}"}), 500

        ext      = os.path.splitext(audio_path)[1].lower()
        basename = os.path.splitext(os.path.basename(audio_path))[0]

        with open(audio_path, "rb") as f:
            audio_data = f.read()

        mime = {
            ".flac": "audio/flac",
            ".m4a":  "audio/mp4",
            ".mp3":  "audio/mpeg",
            ".aac":  "audio/aac",
            ".opus": "audio/opus",
        }.get(ext, "audio/flac")

        return Response(audio_data, content_type=mime, headers={
            "Content-Disposition": f'attachment; filename="{basename}{ext}"',
            "Content-Length": str(len(audio_data)),
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout al descargar"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if not restored and os.path.exists(backup_cfg):
            shutil.copy2(backup_cfg, home_cfg)
            try: os.remove(backup_cfg)
            except: pass
        try: shutil.rmtree(work_dir, ignore_errors=True)
        except: pass

# ── Download LRC ──────────────────────────────────────────────────────────────

@app.route("/api/lrc/<int:track_id>")
def download_lrc(track_id):
    token   = session.get("access_token")
    quality = session.get("quality", "Normal")
    if not token: return jsonify({"error": "Sin sesión activa"}), 401

    work_dir   = tempfile.mkdtemp(prefix="tidal_lrc_")
    home_cfg   = os.path.expanduser("~/.tidal-dl.json")
    backup_cfg = home_cfg + ".bak"
    restored   = False

    try:
        if os.path.exists(home_cfg):
            shutil.copy2(home_cfg, backup_cfg)

        cfg = {
            "downloadPath": work_dir,
            "quality":      quality,
            "addLyrics":    True,
            "lyricFile":    True,
            "usePlaylistFolder": False,
            "albumFolderFormat": "",
            "trackFileFormat":   "{ArtistName} - {TrackTitle}",
            "accessToken":  token,
            "tokenType":    "Bearer",
        }
        with open(home_cfg, "w") as f:
            json.dump(cfg, f)

        track_url = f"https://tidal.com/browse/track/{track_id}"
        subprocess.run(
            ["tidal-dl", "-l", track_url, "-q", quality],
            capture_output=True, text=True, timeout=120,
            cwd=work_dir
        )

        if os.path.exists(backup_cfg):
            shutil.copy2(backup_cfg, home_cfg)
            os.remove(backup_cfg)
        restored = True

        lrc_path = find_lrc_file(work_dir)
        if not lrc_path:
            return jsonify({"error": "No hay letras disponibles para esta canción"}), 404

        basename = os.path.splitext(os.path.basename(lrc_path))[0]
        with open(lrc_path, "r", encoding="utf-8", errors="replace") as f:
            lrc_content = f.read()

        return Response(lrc_content.encode("utf-8"), content_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{basename}.lrc"'})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout al obtener letras"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if not restored and os.path.exists(backup_cfg):
            shutil.copy2(backup_cfg, home_cfg)
            try: os.remove(backup_cfg)
            except: pass
        try: shutil.rmtree(work_dir, ignore_errors=True)
        except: pass

# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

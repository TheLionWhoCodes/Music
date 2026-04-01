import os, re, json, shutil, subprocess, tempfile, threading
from flask import Flask, request, jsonify, session, send_file, Response
import tidalapi

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tidalget-secret-2024")

# Lock para evitar conflictos de config cuando hay descargas simultáneas
dl_lock = threading.Lock()

# ── Utilidades ────────────────────────────────────────────────────────────────

def parse_url(url):
    for pat, tipo in [
        (r"tidal\.com/(?:\w+/)?track/(\d+)",          "track"),
        (r"tidal\.com/(?:\w+/)?album/(\d+)",           "album"),
        (r"tidal\.com/(?:\w+/)?playlist/([\w-]+)",     "playlist"),
        (r"listen\.tidal\.com/(?:\w+/)?track/(\d+)",   "track"),
        (r"listen\.tidal\.com/(?:\w+/)?album/(\d+)",   "album"),
        (r"listen\.tidal\.com/(?:\w+/)?playlist/([\w-]+)", "playlist"),
    ]:
        m = re.search(pat, url)
        if m: return tipo, m.group(1)
    return None, None

def fmt(sec):
    return f"{int(sec)//60}:{int(sec)%60:02d}"

def safe(s):
    return re.sub(r'[\\/*?:"<>|]', "", str(s or "")).strip()

def tidal_session(token):
    """Crea sesión tidalapi con el accessToken."""
    for attr in ["master","hi_res_lossless","hi_res","high","hifi","lossless","low"]:
        if hasattr(tidalapi.Quality, attr):
            q = getattr(tidalapi.Quality, attr)
            break
    else:
        q = None
    try:
        cfg    = tidalapi.Config(quality=q) if q else tidalapi.Config()
        tidal  = tidalapi.Session(cfg)
        tidal.load_oauth_session(
            token_type="Bearer", access_token=token,
            refresh_token=None, expiry_time=None
        )
        return (tidal, None) if tidal.check_login() else (None, "Token inválido o expirado")
    except Exception as e:
        return None, str(e)

def detect_plan(tidal):
    """Retorna (nombre_plan, calidad_tidal_dl)."""
    try:
        sub  = tidal.user.subscription
        plan = str(getattr(sub,"type","") or getattr(sub,"highestSoundQuality","")).upper()
    except:
        plan = ""
    if any(x in plan for x in ["HI_RES","HIRES","MASTER","DOLBY"]):
        return "HiFi Plus 🎵", "Master"
    if any(x in plan for x in ["HIFI","HI_FI","LOSSLESS"]):
        return "HiFi 💎", "HiFi"
    if any(x in plan for x in ["PREMIUM","HIGH"]):
        return "Premium 🟢", "High"
    return "Básico ⚪", "Normal"

def write_cfg(token, dl_path, quality, lyrics):
    """Escribe ~/.tidal-dl.json con el token del usuario."""
    cfg = {
        "downloadPath":     dl_path,
        "quality":          quality,
        "addLyrics":        lyrics,
        "lyricFile":        lyrics,
        "usePlaylistFolder": False,
        "albumFolderFormat": "",
        "trackFileFormat":  "{ArtistName} - {TrackTitle}",
        "accessToken":      token,
        "tokenType":        "Bearer",
        "refreshToken":     "",
    }
    path = os.path.expanduser("~/.tidal-dl.json")
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)

def find_file(directory, extensions):
    for root, _, files in os.walk(directory):
        for f in files:
            if os.path.splitext(f)[1].lower() in extensions:
                return os.path.join(root, f)
    return None

def run_tidal_dl(token, quality, lyrics, track_url):
    """Corre tidal-dl y retorna la carpeta temporal con los archivos."""
    tmp = tempfile.mkdtemp(prefix="tg_")
    with dl_lock:
        write_cfg(token, tmp, quality, lyrics)
        result = subprocess.run(
            ["tidal-dl", "-l", track_url],
            capture_output=True, text=True, timeout=180, cwd=tmp
        )
    return tmp, result

# ── Token ─────────────────────────────────────────────────────────────────────

@app.route("/api/token", methods=["POST"])
def set_token():
    token = (request.json or {}).get("token", "").strip()
    if not token:
        return jsonify({"error": "Token vacío"}), 400

    tidal, err = tidal_session(token)
    if err:
        return jsonify({"error": err}), 401

    plan_name, quality = detect_plan(tidal)
    session["token"]   = token
    session["plan"]    = plan_name
    session["quality"] = quality

    try:
        u    = tidal.user
        name = (getattr(u,"first_name","")+" "+getattr(u,"last_name","")).strip()
    except:
        name = "Usuario Tidal"

    return jsonify({"ok": True, "user": name or "Usuario Tidal",
                    "plan": plan_name, "quality": quality})

@app.route("/api/token", methods=["DELETE"])
def del_token():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/token/status")
def token_status():
    token = session.get("token")
    if not token:
        return jsonify({"logged_in": False})
    tidal, err = tidal_session(token)
    if err:
        session.clear()
        return jsonify({"logged_in": False})
    try:
        u    = tidal.user
        name = (getattr(u,"first_name","")+" "+getattr(u,"last_name","")).strip()
    except:
        name = "Usuario Tidal"
    return jsonify({"logged_in": True, "user": name or "Usuario Tidal",
                    "plan": session.get("plan","Básico ⚪"),
                    "quality": session.get("quality","Normal")})

# ── Info ──────────────────────────────────────────────────────────────────────

@app.route("/api/info", methods=["POST"])
def get_info():
    token = session.get("token")
    if not token:
        return jsonify({"error": "Sin sesión activa"}), 401

    url = (request.json or {}).get("url", "").strip()
    tipo, eid = parse_url(url)
    if not tipo:
        return jsonify({"error": "URL de Tidal no reconocida"}), 400

    tidal, err = tidal_session(token)
    if err:
        return jsonify({"error": err}), 401

    try:
        if tipo == "track":
            t = tidal.track(int(eid))
            return jsonify({
                "type": "track", "title": t.name,
                "artist": t.artist.name, "cover": None,
                "tracks": [{"id": t.id, "title": t.name,
                            "artist": t.artist.name,
                            "album": getattr(getattr(t,"album",None),"name",""),
                            "duration": fmt(t.duration),
                            "url": url}]
            })
        elif tipo == "album":
            a = tidal.album(int(eid))
            ts = list(a.tracks())
            cover = None
            try: cover = a.image(320)
            except: pass
            return jsonify({
                "type": "album", "title": a.name,
                "artist": a.artist.name, "cover": cover,
                "tracks": [{"id": t.id, "title": t.name,
                            "artist": t.artist.name, "album": a.name,
                            "duration": fmt(t.duration),
                            "url": f"https://tidal.com/browse/track/{t.id}"}
                           for t in ts]
            })
        elif tipo == "playlist":
            p = tidal.playlist(eid)
            ts = list(p.tracks())
            return jsonify({
                "type": "playlist", "title": p.name,
                "artist": "", "cover": None,
                "tracks": [{"id": t.id, "title": t.name,
                            "artist": t.artist.name,
                            "album": getattr(getattr(t,"album",None),"name",""),
                            "duration": fmt(t.duration),
                            "url": f"https://tidal.com/browse/track/{t.id}"}
                           for t in ts]
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Download audio ────────────────────────────────────────────────────────────

@app.route("/api/download/<int:track_id>")
def download_audio(track_id):
    token   = session.get("token")
    quality = session.get("quality", "Normal")
    if not token:
        return jsonify({"error": "Sin sesión activa"}), 401

    lyrics    = request.args.get("lyrics","true").lower() == "true"
    track_url = f"https://tidal.com/browse/track/{track_id}"
    tmp       = None

    try:
        tmp, result = run_tidal_dl(token, quality, lyrics, track_url)

        audio = find_file(tmp, {".flac",".m4a",".mp3",".aac",".opus"})
        if not audio:
            err_msg = (result.stderr or result.stdout or "Sin output")[-400:]
            return jsonify({"error": f"tidal-dl falló: {err_msg}"}), 500

        ext  = os.path.splitext(audio)[1].lower()
        name = os.path.splitext(os.path.basename(audio))[0]
        mime = {".flac":"audio/flac",".m4a":"audio/mp4",
                ".mp3":"audio/mpeg",".aac":"audio/aac",".opus":"audio/opus"}.get(ext,"audio/flac")

        with open(audio,"rb") as f:
            data = f.read()

        return Response(data, content_type=mime, headers={
            "Content-Disposition": f'attachment; filename="{name}{ext}"',
            "Content-Length": str(len(data)),
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout (180s). Intenta de nuevo."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if tmp: shutil.rmtree(tmp, ignore_errors=True)

# ── Download LRC ──────────────────────────────────────────────────────────────

@app.route("/api/lrc/<int:track_id>")
def download_lrc(track_id):
    token   = session.get("token")
    quality = session.get("quality", "Normal")
    if not token:
        return jsonify({"error": "Sin sesión activa"}), 401

    track_url = f"https://tidal.com/browse/track/{track_id}"
    tmp       = None

    try:
        tmp, result = run_tidal_dl(token, quality, True, track_url)

        lrc = find_file(tmp, {".lrc"})
        if not lrc:
            return jsonify({"error": "No hay letras para esta canción"}), 404

        name = os.path.splitext(os.path.basename(lrc))[0]
        with open(lrc,"r",encoding="utf-8",errors="replace") as f:
            content = f.read()

        return Response(content.encode("utf-8"),
                        content_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}.lrc"'})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timeout. Intenta de nuevo."}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if tmp: shutil.rmtree(tmp, ignore_errors=True)

# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

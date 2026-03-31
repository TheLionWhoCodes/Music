from flask import Flask, request, jsonify, session, send_file, Response
import tidalapi
import os, re, io, uuid, requests as req_lib, tempfile, threading, time, json, base64

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tidal-web-secret-2024")

# Jobs en memoria por sesión
jobs = {}
jobs_lock = threading.Lock()

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

def get_session_from_token(access_token):
    """Crea una sesión tidalapi a partir del accessToken del usuario."""
    tidal = tidalapi.Session()
    try:
        tidal.load_oauth_session(
            token_type="Bearer",
            access_token=access_token,
            refresh_token=None,
            expiry_time=None,
        )
        if tidal.check_login():
            return tidal, None
        return None, "Token inválido o expirado"
    except Exception as e:
        return None, str(e)

# ── Rutas de Token ────────────────────────────────────────────────────────────

@app.route("/api/token", methods=["POST"])
def set_token():
    data = request.json or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "Token vacío"}), 400

    tidal, err = get_session_from_token(token)
    if err:
        return jsonify({"error": err}), 401

    session["access_token"] = token
    try:
        user = tidal.user
        name = getattr(user, "first_name", "") + " " + getattr(user, "last_name", "")
        return jsonify({"ok": True, "user": name.strip() or "Usuario Tidal"})
    except:
        return jsonify({"ok": True, "user": "Usuario Tidal"})

@app.route("/api/token", methods=["DELETE"])
def clear_token():
    session.pop("access_token", None)
    return jsonify({"ok": True})

@app.route("/api/token/status", methods=["GET"])
def token_status():
    token = session.get("access_token")
    if not token:
        return jsonify({"logged_in": False})
    tidal, err = get_session_from_token(token)
    if err:
        session.pop("access_token", None)
        return jsonify({"logged_in": False})
    try:
        user = tidal.user
        name = getattr(user, "first_name", "") + " " + getattr(user, "last_name", "")
        return jsonify({"logged_in": True, "user": name.strip() or "Usuario Tidal"})
    except:
        return jsonify({"logged_in": True, "user": "Usuario Tidal"})

# ── Info ──────────────────────────────────────────────────────────────────────

@app.route("/api/info", methods=["POST"])
def get_info():
    token = session.get("access_token")
    if not token:
        return jsonify({"error": "Sin sesión activa"}), 401

    url = (request.json or {}).get("url", "").strip()
    tipo, eid = parse_tidal_url(url)
    if not tipo:
        return jsonify({"error": "URL de Tidal no reconocida"}), 400

    tidal, err = get_session_from_token(token)
    if err:
        return jsonify({"error": err}), 401

    try:
        if tipo == "track":
            t = tidal.track(int(eid))
            return jsonify({
                "type": "track", "title": t.name, "artist": t.artist.name,
                "cover": None,
                "tracks": [{"id": t.id, "title": t.name, "artist": t.artist.name,
                             "duration": fmt_dur(t.duration), "url": url}]
            })
        elif tipo == "album":
            a = tidal.album(int(eid))
            ts = a.tracks()
            return jsonify({
                "type": "album", "title": a.name, "artist": a.artist.name,
                "cover": a.image(320) if hasattr(a, "image") else None,
                "tracks": [{"id": t.id, "title": t.name, "artist": t.artist.name,
                             "duration": fmt_dur(t.duration),
                             "url": f"https://tidal.com/browse/track/{t.id}"} for t in ts]
            })
        elif tipo == "playlist":
            p = tidal.playlist(eid)
            ts = list(p.tracks())
            return jsonify({
                "type": "playlist", "title": p.name, "artist": "",
                "cover": None,
                "tracks": [{"id": t.id, "title": t.name, "artist": t.artist.name,
                             "duration": fmt_dur(t.duration),
                             "url": f"https://tidal.com/browse/track/{t.id}"} for t in ts]
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Download (stream directo al navegador) ────────────────────────────────────

@app.route("/api/download/<int:track_id>", methods=["GET"])
def download_track(track_id):
    token = session.get("access_token")
    if not token:
        return jsonify({"error": "Sin sesión activa"}), 401

    tidal, err = get_session_from_token(token)
    if err:
        return jsonify({"error": err}), 401

    try:
        track = tidal.track(track_id)
        stream_url = track.get_url()
        artist = re.sub(r'[\\/*?:"<>|]', "", track.artist.name)
        title  = re.sub(r'[\\/*?:"<>|]', "", track.name)
        filename = f"{artist} - {title}.flac"

        # Proxy stream al navegador
        r = req_lib.get(stream_url, stream=True, timeout=30)
        content_type = r.headers.get("Content-Type", "audio/flac")

        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        response = Response(generate(), content_type=content_type)
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        if "Content-Length" in r.headers:
            response.headers["Content-Length"] = r.headers["Content-Length"]
        return response

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file("index.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

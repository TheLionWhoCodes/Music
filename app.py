from flask import Flask, request, jsonify, session, send_file, Response
import tidalapi
import os, re, requests as req_lib

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tidal-web-secret-2024")

QUALITY_MAP = {
    "Master": tidalapi.Quality.hi_res,
    "HiFi":   tidalapi.Quality.lossless,
    "High":   tidalapi.Quality.high,
    "Normal": tidalapi.Quality.low,
}

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

def get_tidal_session(access_token, quality="HiFi"):
    config = tidalapi.Config(quality=QUALITY_MAP.get(quality, tidalapi.Quality.high))
    tidal  = tidalapi.Session(config)
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

def safe_filename(s):
    return re.sub(r'[\\/*?:"<>|]', "", str(s)).strip()

def add_metadata(data: bytes, content_type: str, track, cover_data=None, lyrics=None) -> bytes:
    try:
        import io as _io
        buf = _io.BytesIO(data)

        if "flac" in content_type:
            from mutagen.flac import FLAC, Picture
            audio = FLAC(buf)
            audio["title"]  = track.name
            audio["artist"] = track.artist.name
            try: audio["album"] = track.album.name
            except: pass
            try: audio["tracknumber"] = str(track.track_num)
            except: pass
            if lyrics:
                audio["lyrics"] = lyrics
            if cover_data:
                pic = Picture()
                pic.type = 3
                pic.mime = "image/jpeg"
                pic.data = cover_data
                audio.add_picture(pic)
            out = _io.BytesIO()
            audio.save(out)
            return out.getvalue()

        elif "mp4" in content_type or "m4a" in content_type or "aac" in content_type:
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(buf)
            audio["\xa9nam"] = [track.name]
            audio["\xa9ART"] = [track.artist.name]
            try: audio["\xa9alb"] = [track.album.name]
            except: pass
            try: audio["trkn"] = [(track.track_num, 0)]
            except: pass
            if lyrics:
                audio["\xa9lyr"] = [lyrics]
            if cover_data:
                audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
            out = _io.BytesIO()
            audio.save(out)
            return out.getvalue()
    except:
        pass
    return data

# ── Token ─────────────────────────────────────────────────────────────────────

@app.route("/api/token", methods=["POST"])
def set_token():
    data  = request.json or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify({"error": "Token vacío"}), 400
    tidal, err = get_tidal_session(token)
    if err:
        return jsonify({"error": err}), 401
    session["access_token"] = token
    try:
        user = tidal.user
        name = (getattr(user, "first_name", "") + " " + getattr(user, "last_name", "")).strip()
        return jsonify({"ok": True, "user": name or "Usuario Tidal"})
    except:
        return jsonify({"ok": True, "user": "Usuario Tidal"})

@app.route("/api/token", methods=["DELETE"])
def clear_token():
    session.pop("access_token", None)
    return jsonify({"ok": True})

@app.route("/api/token/status")
def token_status():
    token = session.get("access_token")
    if not token:
        return jsonify({"logged_in": False})
    tidal, err = get_tidal_session(token)
    if err:
        session.pop("access_token", None)
        return jsonify({"logged_in": False})
    try:
        user = tidal.user
        name = (getattr(user, "first_name", "") + " " + getattr(user, "last_name", "")).strip()
        return jsonify({"logged_in": True, "user": name or "Usuario Tidal"})
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
    tidal, err = get_tidal_session(token)
    if err:
        return jsonify({"error": err}), 401
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
            a  = tidal.album(int(eid))
            ts = list(a.tracks())
            cover = None
            try: cover = a.image(320)
            except: pass
            return jsonify({
                "type": "album", "title": a.name, "artist": a.artist.name, "cover": cover,
                "tracks": [{"id": t.id, "title": t.name, "artist": t.artist.name,
                            "album": a.name, "track_num": getattr(t,"track_num",i+1),
                            "duration": fmt_dur(t.duration),
                            "url": f"https://tidal.com/browse/track/{t.id}"}
                           for i, t in enumerate(ts)]
            })
        elif tipo == "playlist":
            p  = tidal.playlist(eid)
            ts = list(p.tracks())
            return jsonify({
                "type": "playlist", "title": p.name, "artist": "", "cover": None,
                "tracks": [{"id": t.id, "title": t.name, "artist": t.artist.name,
                            "album": getattr(getattr(t,"album",None),"name",""),
                            "track_num": getattr(t,"track_num",i+1),
                            "duration": fmt_dur(t.duration),
                            "url": f"https://tidal.com/browse/track/{t.id}"}
                           for i, t in enumerate(ts)]
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Download ──────────────────────────────────────────────────────────────────

@app.route("/api/download/<int:track_id>")
def download_track(track_id):
    token = session.get("access_token")
    if not token:
        return jsonify({"error": "Sin sesión activa"}), 401

    quality   = request.args.get("quality", "HiFi")
    do_lyrics = request.args.get("lyrics", "true").lower() == "true"

    tidal, err = get_tidal_session(token, quality)
    if err:
        return jsonify({"error": err}), 401

    try:
        track      = tidal.track(track_id)
        stream_url = track.get_url()
        r          = req_lib.get(stream_url, timeout=60)
        content_type = r.headers.get("Content-Type", "audio/flac")
        audio_data   = r.content

        # Portada
        cover_data = None
        try:
            cover_url = track.album.image(1280)
            cr = req_lib.get(cover_url, timeout=10)
            if cr.status_code == 200:
                cover_data = cr.content
        except: pass

        # Letras
        lyrics_text = None
        if do_lyrics:
            try:
                lyr = track.lyrics()
                lyrics_text = getattr(lyr,"subtitles",None) or getattr(lyr,"text",None)
            except: pass

        # Metadatos
        audio_data = add_metadata(audio_data, content_type, track, cover_data, lyrics_text)

        # Extensión
        ext = "flac"
        if "mp4" in content_type or "m4a" in content_type: ext = "m4a"
        elif "aac" in content_type:  ext = "aac"
        elif "mpeg" in content_type or "mp3" in content_type: ext = "mp3"
        elif "opus" in content_type: ext = "opus"

        filename = f"{safe_filename(track.artist.name)} - {safe_filename(track.name)}.{ext}"

        return Response(audio_data, content_type=content_type, headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(audio_data)),
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

from flask import Flask, request, jsonify, session, send_file, Response
import tidalapi
import os, re, io, tempfile, requests as req_lib

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tidal-web-secret-2024")

# ── Plan detection ────────────────────────────────────────────────────────────

def detect_plan(tidal):
    """Detecta el plan de la cuenta y retorna (plan_name, quality_attr, ext)."""
    # Intentar obtener info de suscripción
    try:
        sub = tidal.user.subscription
        plan = str(getattr(sub, "type", "") or getattr(sub, "highestSoundQuality", "")).upper()
    except:
        plan = ""

    # Detectar por calidad máxima disponible
    if any(x in plan for x in ["HI_RES", "HIRES", "MASTER", "DOLBY", "SONY"]):
        return "HiFi Plus", "master", "flac"
    elif any(x in plan for x in ["HIFI", "HI_FI", "LOSSLESS", "HIGH"]):
        return "HiFi", "high", "flac"
    elif any(x in plan for x in ["PREMIUM", "HIGH"]):
        return "Premium", "low", "mp3"
    else:
        return "Básico", "low_96k", "mp3"

def get_best_quality():
    """Obtiene el mejor atributo Quality disponible en esta versión de tidalapi."""
    for attr in ["master", "hi_res_lossless", "hi_res", "high", "hifi", "lossless", "low"]:
        if hasattr(tidalapi.Quality, attr):
            return getattr(tidalapi.Quality, attr)
    try:    return list(tidalapi.Quality)[0]
    except: return None

def get_quality_for_plan(plan_name):
    """Retorna el atributo Quality según el plan."""
    if plan_name in ["HiFi Plus", "HiFi"]:
        for attr in ["master", "hi_res_lossless", "hi_res", "high", "hifi", "lossless"]:
            if hasattr(tidalapi.Quality, attr):
                return getattr(tidalapi.Quality, attr)
    else:
        for attr in ["low", "low_96k", "normal"]:
            if hasattr(tidalapi.Quality, attr):
                return getattr(tidalapi.Quality, attr)
    return get_best_quality()

def get_tidal_session(access_token, quality=None):
    try:
        q = quality or get_best_quality()
        config = tidalapi.Config(quality=q) if q else tidalapi.Config()
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

def fetch_cover(track):
    try:
        r = req_lib.get(track.album.image(1280), timeout=15)
        if r.status_code == 200:
            return r.content
    except: pass
    return None

def fetch_lyrics(track):
    try:
        lyr = track.lyrics()
        return getattr(lyr, "subtitles", None), getattr(lyr, "text", None)
    except:
        return None, None

def embed_flac_metadata(audio_bytes, track, cover=None, lyrics=None):
    from mutagen.flac import FLAC, Picture
    tmp = tempfile.NamedTemporaryFile(suffix=".flac", delete=False)
    try:
        tmp.write(audio_bytes); tmp.flush(); tmp.close()
        audio = FLAC(tmp.name)
        audio["title"]       = track.name
        audio["artist"]      = track.artist.name
        audio["albumartist"] = track.artist.name
        try:    audio["album"] = track.album.name
        except: pass
        try:    audio["tracknumber"] = str(track.track_num)
        except: pass
        try:    audio["date"] = str(track.album.release_date.year)
        except: pass
        if lyrics: audio["lyrics"] = lyrics
        if cover:
            pic = Picture()
            pic.type = 3; pic.mime = "image/jpeg"; pic.desc = "Cover"; pic.data = cover
            audio.clear_pictures(); audio.add_picture(pic)
        audio.save(tmp.name)
        with open(tmp.name, "rb") as f:
            return f.read()
    except Exception as e:
        print(f"[flac meta error] {e}")
        return audio_bytes
    finally:
        try: os.unlink(tmp.name)
        except: pass

def embed_mp3_metadata(audio_bytes, track, cover=None, lyrics=None):
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, APIC, USLT, ID3NoHeaderError
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    try:
        tmp.write(audio_bytes); tmp.flush(); tmp.close()
        try:    audio = ID3(tmp.name)
        except: audio = ID3()
        audio.add(TIT2(encoding=3, text=track.name))
        audio.add(TPE1(encoding=3, text=track.artist.name))
        try:    audio.add(TALB(encoding=3, text=track.album.name))
        except: pass
        try:    audio.add(TRCK(encoding=3, text=str(track.track_num)))
        except: pass
        if lyrics: audio.add(USLT(encoding=3, lang="spa", desc="", text=lyrics))
        if cover:  audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
        audio.save(tmp.name)
        with open(tmp.name, "rb") as f:
            return f.read()
    except Exception as e:
        print(f"[mp3 meta error] {e}")
        return audio_bytes
    finally:
        try: os.unlink(tmp.name)
        except: pass

# ── Token ─────────────────────────────────────────────────────────────────────

@app.route("/api/token", methods=["POST"])
def set_token():
    token = (request.json or {}).get("token", "").strip()
    if not token: return jsonify({"error": "Token vacío"}), 400

    tidal, err = get_tidal_session(token)
    if err: return jsonify({"error": err}), 401

    plan_name, _, ext = detect_plan(tidal)
    session["access_token"] = token
    session["plan"]         = plan_name
    session["dl_ext"]       = ext

    try:
        u    = tidal.user
        name = (getattr(u,"first_name","") + " " + getattr(u,"last_name","")).strip()
    except:
        name = "Usuario Tidal"

    return jsonify({"ok": True, "user": name or "Usuario Tidal", "plan": plan_name, "ext": ext})

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
    plan = session.get("plan", "Desconocido")
    ext  = session.get("dl_ext", "flac")
    return jsonify({"logged_in": True, "user": name or "Usuario Tidal", "plan": plan, "ext": ext})

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
                            "duration": fmt_dur(t.duration)}]
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
                            "duration": fmt_dur(t.duration)}
                           for i,t in enumerate(ts)]
            })
        elif tipo == "playlist":
            p  = tidal.playlist(eid); ts = list(p.tracks())
            return jsonify({
                "type": "playlist", "title": p.name, "artist": "", "cover": None,
                "tracks": [{"id": t.id, "title": t.name, "artist": t.artist.name,
                            "album": getattr(getattr(t,"album",None),"name",""),
                            "track_num": getattr(t,"track_num",i+1),
                            "duration": fmt_dur(t.duration)}
                           for i,t in enumerate(ts)]
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Download audio ────────────────────────────────────────────────────────────

@app.route("/api/download/<int:track_id>")
def download_audio(track_id):
    token    = session.get("access_token")
    plan     = session.get("plan", "Básico")
    dl_ext   = session.get("dl_ext", "flac")
    if not token: return jsonify({"error": "Sin sesión activa"}), 401

    do_lyrics = request.args.get("lyrics", "true").lower() == "true"

    q       = get_quality_for_plan(plan)
    tidal, err = get_tidal_session(token, q)
    if err: return jsonify({"error": err}), 401

    try:
        track        = tidal.track(track_id)
        stream_url   = track.get_url()
        r            = req_lib.get(stream_url, timeout=120)
        content_type = r.headers.get("Content-Type", "audio/flac")
        audio_data   = r.content

        cover = fetch_cover(track)
        plain_lyrics = None
        if do_lyrics:
            _, plain_lyrics = fetch_lyrics(track)

        # Determinar formato real recibido
        ct = content_type.lower()
        if "flac" in ct:
            audio_data = embed_flac_metadata(audio_data, track, cover, plain_lyrics)
            ext = "flac"
        elif any(x in ct for x in ["mpeg", "mp3"]):
            audio_data = embed_mp3_metadata(audio_data, track, cover, plain_lyrics)
            ext = "mp3"
        else:
            # m4a/aac — devolver sin metadata (mutagen MP4 en tmpfile)
            ext = "m4a"

        basename = f"{safe_fn(track.artist.name)} - {safe_fn(track.name)}"
        return Response(audio_data, content_type=content_type, headers={
            "Content-Disposition": f'attachment; filename="{basename}.{ext}"',
            "Content-Length": str(len(audio_data)),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Download LRC ──────────────────────────────────────────────────────────────

@app.route("/api/lrc/<int:track_id>")
def download_lrc(track_id):
    token = session.get("access_token")
    if not token: return jsonify({"error": "Sin sesión activa"}), 401
    tidal, err = get_tidal_session(token)
    if err: return jsonify({"error": err}), 401
    try:
        track = tidal.track(track_id)
        lrc, plain = fetch_lyrics(track)
        content = lrc or plain
        if not content:
            return jsonify({"error": "No hay letras disponibles"}), 404
        basename = f"{safe_fn(track.artist.name)} - {safe_fn(track.name)}"
        ext = "lrc" if lrc else "txt"
        return Response(content.encode("utf-8"), content_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{basename}.{ext}"'})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

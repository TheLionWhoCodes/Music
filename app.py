from flask import Flask, request, jsonify, session, send_file, Response
import tidalapi
import os, re, io, requests as req_lib

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tidal-web-secret-2024")

# ── Quality detection ─────────────────────────────────────────────────────────

def get_tidal_session(access_token, quality="HiFi"):
    quality_options = {
        "Master": ["master", "hi_res_lossless", "hi_res"],
        "HiFi":   ["high", "hifi", "lossless"],
        "High":   ["low", "high_aac"],
        "Normal": ["low_96k", "normal"],
    }
    q = None
    for name in quality_options.get(quality, ["high", "hifi", "lossless"]):
        if hasattr(tidalapi.Quality, name):
            q = getattr(tidalapi.Quality, name)
            break
    if q is None:
        try:    q = list(tidalapi.Quality)[0]
        except: q = None
    try:
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
    """Retorna (lrc_synced, plain_text)"""
    try:
        lyr = track.lyrics()
        return getattr(lyr, "subtitles", None), getattr(lyr, "text", None)
    except:
        return None, None

def get_ext(ct):
    ct = ct.lower()
    if "flac" in ct: return "flac"
    if "mp4"  in ct or "m4a" in ct or "aac" in ct: return "m4a"
    if "mpeg" in ct or "mp3" in ct: return "mp3"
    if "opus" in ct: return "opus"
    return "flac"

def embed_metadata(data: bytes, ct: str, track, cover: bytes = None, lyrics: str = None) -> bytes:
    """Incrusta título, artista, álbum, número de pista, portada y letras."""
    try:
        buf       = io.BytesIO(data)
        title     = track.name
        artist    = track.artist.name
        album     = getattr(getattr(track, "album", None), "name", "") or ""
        track_num = str(getattr(track, "track_num", 1))

        if "flac" in ct:
            from mutagen.flac import FLAC, Picture
            audio = FLAC(buf)
            audio["title"]       = title
            audio["artist"]      = artist
            audio["album"]       = album
            audio["tracknumber"] = track_num
            if lyrics:
                audio["lyrics"] = lyrics
            if cover:
                pic = Picture()
                pic.type = 3; pic.mime = "image/jpeg"; pic.data = cover
                audio.clear_pictures(); audio.add_picture(pic)
            out = io.BytesIO(); audio.save(out); return out.getvalue()

        elif any(x in ct for x in ["mp4","m4a","aac"]):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(buf)
            audio["\xa9nam"] = [title]
            audio["\xa9ART"] = [artist]
            audio["\xa9alb"] = [album]
            audio["trkn"]    = [(int(track_num), 0)]
            if lyrics:   audio["\xa9lyr"] = [lyrics]
            if cover:    audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
            out = io.BytesIO(); audio.save(out); return out.getvalue()

        elif any(x in ct for x in ["mpeg","mp3"]):
            from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, APIC, USLT, ID3NoHeaderError
            try:    audio = ID3(buf)
            except: audio = ID3()
            audio.add(TIT2(encoding=3, text=title))
            audio.add(TPE1(encoding=3, text=artist))
            audio.add(TALB(encoding=3, text=album))
            audio.add(TRCK(encoding=3, text=track_num))
            if lyrics: audio.add(USLT(encoding=3, lang="spa", desc="", text=lyrics))
            if cover:  audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover))
            out = io.BytesIO(data); audio.save(out); return out.getvalue()

    except Exception as e:
        print(f"[metadata error] {e}")
    return data

# ── Token ─────────────────────────────────────────────────────────────────────

@app.route("/api/token", methods=["POST"])
def set_token():
    token = (request.json or {}).get("token", "").strip()
    if not token: return jsonify({"error": "Token vacío"}), 400
    tidal, err = get_tidal_session(token)
    if err: return jsonify({"error": err}), 401
    session["access_token"] = token
    try:
        u = tidal.user
        name = (getattr(u,"first_name","") + " " + getattr(u,"last_name","")).strip()
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
    if not token: return jsonify({"logged_in": False})
    tidal, err = get_tidal_session(token)
    if err:
        session.pop("access_token", None)
        return jsonify({"logged_in": False})
    try:
        u = tidal.user
        name = (getattr(u,"first_name","") + " " + getattr(u,"last_name","")).strip()
        return jsonify({"logged_in": True, "user": name or "Usuario Tidal"})
    except:
        return jsonify({"logged_in": True, "user": "Usuario Tidal"})

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
            a = tidal.album(int(eid)); ts = list(a.tracks())
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
            p = tidal.playlist(eid); ts = list(p.tracks())
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

# ── Download audio (con metadatos + portada) ──────────────────────────────────

@app.route("/api/download/<int:track_id>")
def download_audio(track_id):
    token = session.get("access_token")
    if not token: return jsonify({"error": "Sin sesión activa"}), 401

    quality   = request.args.get("quality", "HiFi")
    do_lyrics = request.args.get("lyrics", "true").lower() == "true"

    tidal, err = get_tidal_session(token, quality)
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

        audio_data = embed_metadata(audio_data, content_type, track, cover, plain_lyrics)

        ext      = get_ext(content_type)
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
            return jsonify({"error": "No hay letras disponibles para esta canción"}), 404

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

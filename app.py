from flask import Flask, request, jsonify, session, send_file, Response
import tidalapi
import os, re, io, requests as req_lib
from mutagen.flac import FLAC, Picture

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "tidal-web-secret-2024")

# ───────────────── Helpers ─────────────────

def fmt_dur(sec):
    return f"{int(sec)//60}:{int(sec)%60:02d}"

def safe_filename(s):
    return re.sub(r'[\\/*?:"<>|]', "", str(s or "")).strip()

def parse_tidal_url(url):
    patterns = [
        (r"tidal\.com/(?:\w+/)?track/(\d+)", "track"),
        (r"tidal\.com/(?:\w+/)?album/(\d+)", "album"),
        (r"tidal\.com/(?:\w+/)?playlist/([\w-]+)", "playlist"),
    ]
    for p,t in patterns:
        m = re.search(p,url)
        if m:
            return t,m.group(1)
    return None,None

def get_tidal_session(token, quality="HiFi"):
    tidal = tidalapi.Session()
    tidal.load_oauth_session(
        token_type="Bearer",
        access_token=token,
        refresh_token=None,
        expiry_time=None,
    )
    if tidal.check_login():
        return tidal,None
    return None,"Token inválido"

def get_cover(track):
    try:
        url = track.album.image(1280)
        r = req_lib.get(url,timeout=15)
        if r.status_code == 200:
            return r.content
    except:
        pass
    return None

def get_lyrics(track):
    try:
        lyr = track.lyrics()
        synced = getattr(lyr,"subtitles",None)
        plain  = getattr(lyr,"text",None)
        return synced,plain
    except:
        return None,None


# ───────────────── Metadata ─────────────────

def add_flac_metadata(data, track, cover_data=None, lyrics=None):

    buf = io.BytesIO(data)
    audio = FLAC(buf)

    audio["title"] = track.name
    audio["artist"] = track.artist.name
    audio["album"] = getattr(track.album,"name","")
    audio["tracknumber"] = str(getattr(track,"track_num",1))

    if lyrics:
        audio["lyrics"] = lyrics

    if cover_data:
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.data = cover_data
        audio.clear_pictures()
        audio.add_picture(pic)

    out = io.BytesIO()
    audio.save(out)

    return out.getvalue()


# ───────────────── TOKEN ─────────────────

@app.route("/api/token", methods=["POST"])
def set_token():

    data = request.json or {}
    token = data.get("token","").strip()

    if not token:
        return jsonify({"error":"Token vacío"}),400

    tidal,err = get_tidal_session(token)

    if err:
        return jsonify({"error":err}),401

    session["access_token"] = token

    return jsonify({"ok":True})


@app.route("/api/token/status")
def token_status():

    token = session.get("access_token")

    if not token:
        return jsonify({"logged_in":False})

    tidal,err = get_tidal_session(token)

    if err:
        return jsonify({"logged_in":False})

    return jsonify({"logged_in":True})


# ───────────────── INFO ─────────────────

@app.route("/api/info", methods=["POST"])
def get_info():

    token = session.get("access_token")

    if not token:
        return jsonify({"error":"Sin sesión"}),401

    url = request.json.get("url","")

    tipo,eid = parse_tidal_url(url)

    if not tipo:
        return jsonify({"error":"URL no válida"}),400

    tidal,err = get_tidal_session(token)

    if err:
        return jsonify({"error":err}),401

    if tipo == "track":

        t = tidal.track(int(eid))

        return jsonify({
            "type":"track",
            "title":t.name,
            "artist":t.artist.name,
            "tracks":[{
                "id":t.id,
                "title":t.name,
                "artist":t.artist.name,
                "album":t.album.name,
                "track_num":t.track_num,
                "duration":fmt_dur(t.duration)
            }]
        })


# ───────────────── DOWNLOAD ─────────────────

@app.route("/api/download/<int:track_id>")
def download_track(track_id):

    token = session.get("access_token")

    if not token:
        return jsonify({"error":"Sin sesión"}),401

    do_lyrics = request.args.get("lyrics","false") == "true"

    tidal,err = get_tidal_session(token)

    if err:
        return jsonify({"error":err}),401

    try:

        track = tidal.track(track_id)

        stream_url = track.get_url()

        r = req_lib.get(stream_url,timeout=120)

        audio_data = r.content

        cover_data = get_cover(track)

        lrc_content = None
        plain_lyrics = None

        if do_lyrics:
            lrc_content,plain_lyrics = get_lyrics(track)

        audio_data = add_flac_metadata(
            audio_data,
            track,
            cover_data,
            plain_lyrics
        )

        artist = safe_filename(track.artist.name)
        title  = safe_filename(track.name)

        basename = f"{artist} - {title}"

        # enviar FLAC
        response = Response(
            audio_data,
            content_type="audio/flac",
            headers={
                "Content-Disposition":f'attachment; filename="{basename}.flac"'
            }
        )

        # guardar LRC aparte
        if do_lyrics and lrc_content:

            with open(f"/tmp/{basename}.lrc","w",encoding="utf-8") as f:
                f.write(lrc_content)

        return response

    except Exception as e:
        return jsonify({"error":str(e)}),500


# ───────────────── FRONTEND ─────────────────

@app.route("/")
def index():
    return send_file("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port)

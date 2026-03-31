from flask import Flask, request, jsonify, session, Response, send_file
import tidalapi
import os, re, io
import requests
from mutagen.flac import FLAC, Picture

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY","tidal-web")

# ---------------- helpers ----------------

def safe_filename(name):
    return re.sub(r'[\\/*?:"<>|]',"",str(name))

def parse_tidal_url(url):

    patterns = [
        r"tidal\.com/(?:browse/)?track/(\d+)",
        r"listen\.tidal\.com/(?:browse/)?track/(\d+)"
    ]

    for p in patterns:

        m = re.search(p,url)

        if m:
            return int(m.group(1))

    return None


def get_cover(track):

    try:
        url = track.album.image(1280)

        r = requests.get(url,timeout=15)

        if r.status_code == 200:
            return r.content

    except:
        pass

    return None


def get_lyrics(track):

    try:

        lyr = track.lyrics()

        synced = getattr(lyr,"subtitles",None)

        return synced

    except:

        return None


# ---------------- metadata ----------------

def add_metadata(audio_bytes,track,cover,lyrics):

    audio = FLAC(io.BytesIO(audio_bytes))

    audio["title"] = track.name
    audio["artist"] = track.artist.name
    audio["album"] = track.album.name
    audio["tracknumber"] = str(track.track_num)

    if lyrics:
        audio["lyrics"] = lyrics

    if cover:

        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.data = cover

        audio.clear_pictures()
        audio.add_picture(pic)

    out = io.BytesIO()

    audio.save(out)

    return out.getvalue()


# ---------------- TOKEN ----------------

@app.route("/api/token",methods=["POST"])
def set_token():

    data = request.json or {}

    token = data.get("token","").strip()

    if not token:
        return jsonify({"error":"token vacío"}),400

    session["token"] = token

    return jsonify({"ok":True})


# ---------------- INFO ----------------

@app.route("/api/info",methods=["POST"])
def info():

    if "token" not in session:
        return jsonify({"error":"sin token"}),401

    data = request.json or {}

    url = data.get("url","")

    track_id = parse_tidal_url(url)

    if not track_id:
        return jsonify({"error":"URL de Tidal no reconocida"}),400

    tidal = tidalapi.Session()

    tidal.load_oauth_session(
        token_type="Bearer",
        access_token=session["token"]
    )

    try:

        track = tidal.track(track_id)

        return jsonify({
            "id":track.id,
            "title":track.name,
            "artist":track.artist.name,
            "album":track.album.name,
            "duration":track.duration
        })

    except Exception as e:

        return jsonify({"error":str(e)}),500


# ---------------- DOWNLOAD SONG ----------------

@app.route("/api/download/<int:track_id>")
def download(track_id):

    if "token" not in session:
        return jsonify({"error":"sin token"}),401

    tidal = tidalapi.Session()

    tidal.load_oauth_session(
        token_type="Bearer",
        access_token=session["token"]
    )

    try:

        track = tidal.track(track_id)

        stream = track.get_url()

        r = requests.get(stream,timeout=120)

        audio = r.content

        cover = get_cover(track)

        lyrics = get_lyrics(track)

        audio = add_metadata(audio,track,cover,lyrics)

        artist = safe_filename(track.artist.name)
        title = safe_filename(track.name)

        filename = f"{artist} - {title}.flac"

        return Response(
            audio,
            content_type="audio/flac",
            headers={
                "Content-Disposition":f'attachment; filename="{filename}"'
            }
        )

    except Exception as e:

        return jsonify({"error":str(e)}),500


# ---------------- DOWNLOAD LYRICS ----------------

@app.route("/api/lyrics/<int:track_id>")
def lyrics(track_id):

    if "token" not in session:
        return jsonify({"error":"sin token"}),401

    tidal = tidalapi.Session()

    tidal.load_oauth_session(
        token_type="Bearer",
        access_token=session["token"]
    )

    try:

        track = tidal.track(track_id)

        lrc = get_lyrics(track)

        if not lrc:
            return jsonify({"error":"no hay letras"}),404

        artist = safe_filename(track.artist.name)
        title = safe_filename(track.name)

        filename = f"{artist} - {title}.lrc"

        return Response(
            lrc,
            content_type="text/plain",
            headers={
                "Content-Disposition":f'attachment; filename="{filename}"'
            }
        )

    except Exception as e:

        return jsonify({"error":str(e)}),500


# ---------------- FRONTEND ----------------

@app.route("/")
def index():

    return send_file("index.html")


# ---------------- RUN ----------------

if __name__ == "__main__":

    port = int(os.environ.get("PORT",10000))

    app.run(host="0.0.0.0",port=port)

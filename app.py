from flask import Flask, render_template, request, jsonify, session, Response, stream_with_context
import yt_dlp
import threading
import time
import uuid
import requests
import os

app = Flask(__name__)
app.secret_key = os.urandom(24)

download_status = {}
COOLDOWN_SECONDS = 30


def get_quality_format(quality):
    quality_map = {
        'best': 'best',
        '1080': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        '720': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
        '480': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
        '360': 'bestvideo[height<=360]+bestaudio/best[height<=360]',
        'audio': 'bestaudio/best',
    }
    return quality_map.get(quality, 'best')


def download_video(task_id, url, quality, format_type):
    try:
        download_status[task_id]['status'] = 'starting'
        
        if format_type in ['mp3', 'm4a']:
            format_selection = 'bestaudio/best'
            ext = format_type
        else:
            format_selection = get_quality_format(quality)
            ext = format_type
        
        ydl_opts = {
            'format': format_selection,
            'quiet': True,
            'no_warnings': True,
            'no_check_certificate': True,
            'extractor_args': {
                'youtube': {
                    'client': ['android', 'ios'],
                    'player_client': ['android', 'ios'],
                }
            },
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            download_url = info.get('url', '')
            
            if not download_url and 'formats' in info:
                for fmt in reversed(info.get('formats', [])):
                    if fmt.get('url'):
                        download_url = fmt['url']
                        break
            
            download_status[task_id].update({
                'download_url': download_url,
                'ext': ext,
                'title': info.get('title', 'Unknown'),
                'status': 'ready' if download_url else 'error',
                'error': 'No download URL found' if not download_url else '',
                'cooldown_until': time.time() + COOLDOWN_SECONDS
            })
            
    except Exception as e:
        download_status[task_id].update({
            'status': 'error',
            'error': str(e),
            'cooldown_until': time.time() + COOLDOWN_SECONDS
        })


@app.route('/')
def index():
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
    
    session_id = session['session_id']
    can_download = True
    cooldown_remaining = 0
    
    if session_id in download_status:
        last_download = download_status[session_id]
        if 'cooldown_until' in last_download:
            remaining = last_download['cooldown_until'] - time.time()
            if remaining > 0:
                can_download = False
                cooldown_remaining = int(remaining)
    
    return render_template('index.html', can_download=can_download, cooldown_remaining=cooldown_remaining)


@app.route('/get_info', methods=['POST'])
def get_info():
    url = request.json.get('url', '').strip()
    
    if not url:
        return jsonify({'error': 'Please enter a valid URL'}), 400
    
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extractor_args': {'youtube': {'client': ['android', 'ios']}}
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = info.get('duration', 0)
            hours, remainder = divmod(duration, 3600)
            minutes, seconds = divmod(remainder, 60)
            
            return jsonify({
                'title': info.get('title', 'N/A'),
                'uploader': info.get('uploader', 'N/A'),
                'duration': f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}",
                'thumbnail': info.get('thumbnail', ''),
                'view_count': info.get('view_count', 0)
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 400


@app.route('/download', methods=['POST'])
def download():
    session_id = session.get('session_id')
    
    if not session_id:
        return jsonify({'error': 'Invalid session'}), 400
    
    if session_id in download_status and 'cooldown_until' in download_status[session_id]:
        remaining = download_status[session_id]['cooldown_until'] - time.time()
        if remaining > 0:
            return jsonify({'error': f'Please wait {int(remaining)} seconds', 'cooldown': int(remaining)}), 429
    
    data = request.json
    url = data.get('url', '').strip()
    quality = data.get('quality', 'best')
    format_type = data.get('format', 'mp4')
    
    if not url:
        return jsonify({'error': 'Please enter a valid URL'}), 400
    
    task_id = str(uuid.uuid4())[:8]
    download_status[task_id] = {
        'status': 'queued', 'progress': 0, 'speed': '0 MB/s',
        'title': '', 'error': '', 'download_url': '', 'ext': 'mp4'
    }
    
    threading.Thread(target=download_video, args=(task_id, url, quality, format_type), daemon=True).start()
    download_status[session_id] = download_status[task_id]
    
    return jsonify({'task_id': task_id})


@app.route('/status/<task_id>')
def status(task_id):
    if task_id not in download_status:
        return jsonify({'error': 'Task not found'}), 404
    
    task = download_status[task_id]
    return jsonify({
        'status': task['status'],
        'progress': task.get('progress', 0),
        'title': task['title'],
        'error': task.get('error', ''),
        'filename': f"{task['title']}.{task.get('ext', 'mp4')}" if task.get('title') else '',
        'download_url': task.get('download_url', '')
    })


@app.route('/download_file/<task_id>')
def download_file(task_id):
    if task_id not in download_status:
        return jsonify({'error': 'Task not found'}), 404
    
    task = download_status[task_id]
    if task['status'] != 'ready' or not task.get('download_url'):
        return jsonify({'error': 'Download not ready'}), 400
    
    def generate():
        try:
            response = requests.get(task['download_url'], stream=True)
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        except Exception as e:
            print(f"Streaming error: {e}")
    
    return Response(
        stream_with_context(generate()),
        mimetype='application/octet-stream',
        headers={'Content-Disposition': f'attachment; filename="{task["title"]}.{task.get("ext", "mp4")}"'}
    )


if __name__ == '__main__':
    print("\n" + "=" * 50)
    print("  YouTube Downloader - Web Version")
    print("=" * 50)
    print(f"  Cooldown: {COOLDOWN_SECONDS} seconds")
    print("=" * 50)
    print("\n  Starting server at http://localhost:5000\n")
    app.run(debug=True, host='0.0.0.0', port=5000)

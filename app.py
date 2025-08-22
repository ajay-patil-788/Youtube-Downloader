from flask import Flask, render_template, request, jsonify, send_file, session
import yt_dlp
import os
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse, parse_qs
import json
import re

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

# Global dictionary to store download progress
download_progress = {}

class DownloadProgressHook:
    def __init__(self, download_id):
        self.download_id = download_id
        self.last_update = time.time()
    
    def __call__(self, d):
        try:
            current_time = time.time()
            if current_time - self.last_update < 0.5 and d['status'] == 'downloading':
                return
            
            self.last_update = current_time
            status = d.get('status', 'unknown')
            print(f"Progress hook called with status: {status}")
            
            if status == 'downloading':
                percent = 0
                speed = 'N/A'
                eta = 'N/A'
                
                if 'downloaded_bytes' in d and 'total_bytes' in d and d['total_bytes']:
                    percent = (d['downloaded_bytes'] / d['total_bytes']) * 100
                elif 'downloaded_bytes' in d and 'total_bytes_estimate' in d and d['total_bytes_estimate']:
                    percent = (d['downloaded_bytes'] / d['total_bytes_estimate']) * 100
                elif '_percent_str' in d:
                    try:
                        percent_str = str(d['_percent_str']).replace('%', '').strip()
                        percent = float(percent_str)
                    except (ValueError, TypeError):
                        pass
                
                if '_speed_str' in d:
                    speed = str(d['_speed_str'])
                elif 'speed' in d and d['speed']:
                    speed = format_bytes(d['speed']) + '/s'
                
                if '_eta_str' in d:
                    eta = str(d['_eta_str'])
                elif 'eta' in d and d['eta']:
                    eta = f"{int(d['eta'])}s"
                
                download_progress[self.download_id] = {
                    'status': 'downloading',
                    'percent': min(max(percent, 0), 100),
                    'speed': speed,
                    'eta': eta,
                    'timestamp': current_time
                }
                
            elif status == 'finished':
                filename = d.get('filename', '')
                download_progress[self.download_id] = {
                    'status': 'finished',
                    'percent': 100,
                    'filename': filename,
                    'timestamp': current_time
                }
                
            elif status == 'error':
                error_msg = d.get('error', 'Unknown download error')
                download_progress[self.download_id] = {
                    'status': 'error',
                    'error': str(error_msg),
                    'timestamp': current_time
                }
                
        except Exception as e:
            print(f"Error in progress hook: {e}")
            download_progress[self.download_id] = {
                'status': 'error',
                'error': f'Progress tracking error: {str(e)}',
                'timestamp': time.time()
            }

def format_bytes(bytes_value):
    if bytes_value is None:
        return "Unknown"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_value < 1024.0:
            return f"{bytes_value:.1f} {unit}"
        bytes_value /= 1024.0
    return f"{bytes_value:.1f} TB"

def get_video_info(url):
    """Extract video information with multiple fallback methods"""
    methods = [
        # Method 1: Android Creator (works best)
        {
            'user_agent': 'Mozilla/5.0 (Linux; Android 11; SM-G973F)',
            'extractor_args': {'youtube': {'player_client': ['android_creator']}}
        },
        # Method 2: iOS
        {
            'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)',
            'extractor_args': {'youtube': {'player_client': ['ios']}}
        },
        # Method 3: Android Music
        {
            'user_agent': 'Mozilla/5.0 (Linux; Android 11)',
            'extractor_args': {'youtube': {'player_client': ['android_music']}}
        }
    ]
    
    for i, method in enumerate(methods):
        try:
            print(f"Trying method {i+1} for: {url}")
            ydl_opts = {
                'quiet': False,
                'no_warnings': False,
                'extract_flat': False,
                'writeinfojson': False,
                'writedescription': False,
                'writesubtitles': False,
                'writeautomaticsub': False,
                **method
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    print(f"SUCCESS with method {i+1}!")
                    
                    # Basic video info
                    video_data = {
                        'title': info.get('title', 'Unknown'),
                        'thumbnail': info.get('thumbnail', ''),
                        'duration': info.get('duration', 0),
                        'uploader': info.get('uploader', 'Unknown'),
                        'view_count': info.get('view_count', 0),
                        'upload_date': info.get('upload_date', ''),
                        'description': info.get('description', '')[:500] + '...' if len(info.get('description', '')) > 500 else info.get('description', ''),
                    }
                    
                    # Format duration
                    if video_data['duration']:
                        minutes = video_data['duration'] // 60
                        seconds = video_data['duration'] % 60
                        video_data['duration_formatted'] = f"{minutes}:{seconds:02d}"
                    else:
                        video_data['duration_formatted'] = "Unknown"
                    
                    # Format view count
                    if video_data['view_count']:
                        if video_data['view_count'] >= 1000000:
                            video_data['view_count_formatted'] = f"{video_data['view_count']/1000000:.1f}M"
                        elif video_data['view_count'] >= 1000:
                            video_data['view_count_formatted'] = f"{video_data['view_count']/1000:.1f}K"
                        else:
                            video_data['view_count_formatted'] = str(video_data['view_count'])
                    else:
                        video_data['view_count_formatted'] = "Unknown"
                    
                    # Simple formats (works better with fallback methods)
                    video_formats = [{
                        'format_id': 'best',
                        'ext': 'mp4',
                        'quality': 'Best Available',
                        'filesize': 'Unknown',
                        'fps': 'N/A',
                        'vcodec': 'auto',
                        'acodec': 'auto',
                        'type': 'best'
                    }]
                    
                    audio_formats = [{
                        'format_id': 'bestaudio',
                        'ext': 'mp3',
                        'quality': 'Best Audio',
                        'filesize': 'Unknown',
                        'acodec': 'auto',
                        'type': 'audio_best'
                    }]
                    
                    video_data['formats'] = video_formats
                    video_data['audio_formats'] = audio_formats
                    
                    print(f"Video info extracted successfully: {video_data['title']}")
                    return video_data
                    
        except Exception as e:
            print(f"Method {i+1} failed: {str(e)}")
            continue
    
    # If all methods fail
    raise Exception("Unable to extract video info. YouTube may be blocking this video or the URL is invalid.")

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_info', methods=['POST'])
def get_info():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Invalid JSON data'}), 400
        
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'error': 'Please provide a valid URL'}), 400
        
        print(f"Processing URL: {url}")
        
        if not re.match(r'^https?://', url):
            url = 'https://' + url
        
        if 'youtube.com' not in url and 'youtu.be' not in url:
            return jsonify({'error': 'Please provide a valid YouTube URL'}), 400
        
        video_info = get_video_info(url)
        return jsonify(video_info)
        
    except Exception as e:
        error_msg = str(e)
        print(f"Error in get_info: {error_msg}")
        return jsonify({'error': error_msg}), 500

@app.route('/download', methods=['POST'])
def download():
    try:
        data = request.json
        url = data.get('url', '').strip()
        format_id = data.get('format_id', '')
        download_type = data.get('type', 'video')
        format_info = data.get('format_info', {})
        
        if not url or not format_id:
            return jsonify({'error': 'Missing required parameters'}), 400
        
        download_id = str(uuid.uuid4())
        download_progress[download_id] = {
            'status': 'starting',
            'percent': 0,
            'timestamp': time.time()
        }
        
        temp_dir = tempfile.mkdtemp()
        
        base_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'progress_hooks': [DownloadProgressHook(download_id)],
            'no_warnings': False,
            'extract_flat': False,
        }
        
        if download_type == 'audio':
            ydl_opts = {
                **base_opts,
                'format': 'bestaudio',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'prefer_ffmpeg': True,
            }
        else:
            ydl_opts = {
                **base_opts,
                'format': 'best[height<=1080]/best',
                'merge_output_format': 'mp4',
            }
        
        def download_video():
            try:
                download_progress[download_id]['status'] = 'downloading'
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
                
                files = [f for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
                
                if files:
                    largest_file = max(files, key=lambda f: os.path.getsize(os.path.join(temp_dir, f)))
                    filename = os.path.join(temp_dir, largest_file)
                    file_size = os.path.getsize(filename)
                    
                    download_progress[download_id] = {
                        'status': 'finished',
                        'percent': 100,
                        'filename': filename,
                        'file_size': format_bytes(file_size),
                        'timestamp': time.time()
                    }
                else:
                    raise Exception("Download completed but no files found")
                    
            except Exception as e:
                download_progress[download_id] = {
                    'status': 'error',
                    'error': str(e),
                    'timestamp': time.time()
                }
        
        thread = threading.Thread(target=download_video, daemon=True)
        thread.start()
        
        return jsonify({'download_id': download_id})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/progress/<download_id>')
def get_progress(download_id):
    progress = download_progress.get(download_id, {'status': 'not_found'})
    return jsonify(progress)

@app.route('/download_file/<download_id>')
def download_file(download_id):
    try:
        progress = download_progress.get(download_id)
        if not progress or progress['status'] != 'finished':
            return jsonify({'error': 'Download not finished'}), 400
        
        filename = progress['filename']
        if os.path.exists(filename):
            return send_file(filename, as_attachment=True)
        else:
            return jsonify({'error': 'File not found'}), 404
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/cleanup/<download_id>')
def cleanup(download_id):
    try:
        progress = download_progress.get(download_id)
        if progress and progress['status'] == 'finished':
            filename = progress['filename']
            if os.path.exists(filename):
                os.remove(filename)
            
            temp_dir = os.path.dirname(filename)
            if os.path.exists(temp_dir):
                try:
                    os.rmdir(temp_dir)
                except OSError:
                    pass
        
        if download_id in download_progress:
            del download_progress[download_id]
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

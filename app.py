from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import os
import tempfile
import threading
import time
import uuid
import json
import re
import logging
from urllib.parse import urlparse
import atexit
import shutil
import random
import requests

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-in-production')

# Global dictionary to store download progress
download_progress = {}
temp_dirs = set()

# Proxy rotation list (add working proxies here)
PROXY_LIST = [
    # Add residential proxies here if available
    # 'http://username:password@proxy1:port',
    # 'http://username:password@proxy2:port',
]

# Multiple User-Agent strings
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/121.0',
]

def cleanup_temp_dirs():
    """Clean up all temporary directories on app shutdown"""
    for temp_dir in list(temp_dirs):
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
                logger.info(f"Cleaned up temp directory: {temp_dir}")
        except Exception as e:
            logger.error(f"Failed to cleanup {temp_dir}: {e}")
        temp_dirs.discard(temp_dir)

atexit.register(cleanup_temp_dirs)

class DownloadProgressHook:
    def __init__(self, download_id):
        self.download_id = download_id
        self.last_update = time.time()

    def __call__(self, d):
        try:
            current_time = time.time()
            if current_time - self.last_update < 0.5 and d.get('status') == 'downloading':
                return
            
            self.last_update = current_time
            status = d.get('status', 'unknown')
            logger.info(f"Progress hook called with status: {status}")
            
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
            logger.error(f"Error in progress hook: {e}")
            download_progress[self.download_id] = {
                'status': 'error',
                'error': f'Progress tracking error: {str(e)}',
                'timestamp': time.time()
            }

def format_bytes(bytes_value):
    """Convert bytes to human readable format"""
    if bytes_value is None:
        return "Unknown"
    try:
        bytes_value = float(bytes_value)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_value < 1024.0:
                return f"{bytes_value:.1f} {unit}"
            bytes_value /= 1024.0
        return f"{bytes_value:.1f} TB"
    except (ValueError, TypeError):
        return "Unknown"

def try_alternative_extractor(url):
    """Try alternative methods to extract video info"""
    try:
        # Method 1: Try with OAuth2 plugin
        oauth2_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'user_agent': random.choice(USER_AGENTS),
            'extractor_retries': 3,
            'sleep_interval': 2,
            'force_ipv4': True,
            # Try OAuth2 method
            'username': 'oauth2',
            'password': '',
        }
        
        with yt_dlp.YoutubeDL(oauth2_opts) as ydl:
            return ydl.extract_info(url, download=False)
            
    except Exception as e:
        logger.warning(f"OAuth2 method failed: {e}")
        
    # Method 2: Try with different extractors
    try:
        invidious_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'user_agent': random.choice(USER_AGENTS),
            'force_generic_extractor': False,
            'extractor_retries': 3,
        }
        
        # Try converting to invidious URL
        if 'youtube.com/watch?v=' in url:
            video_id = url.split('watch?v=')[1].split('&')[0]
            invidious_url = f"https://invidious.io/watch?v={video_id}"
            
            with yt_dlp.YoutubeDL(invidious_opts) as ydl:
                return ydl.extract_info(invidious_url, download=False)
                
    except Exception as e:
        logger.warning(f"Alternative extractor failed: {e}")
        
    return None

def get_video_info(url):
    """Extract video information with multiple fallback methods"""
    try:
        logger.info(f"Extracting info from: {url}")
        
        # Add random delay
        time.sleep(random.uniform(2, 5))
        
        # Primary method with enhanced bypass
        user_agent = random.choice(USER_AGENTS)
        proxy = random.choice(PROXY_LIST) if PROXY_LIST else None
        
        ydl_opts = {
            'quiet': False,
            'no_warnings': False,
            'extract_flat': False,
            'writeinfojson': False,
            'writedescription': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
            'user_agent': user_agent,
            'headers': {
                'User-Agent': user_agent,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'Sec-Fetch-Dest': 'document',
                'Sec-Fetch-Mode': 'navigate',
                'Sec-Fetch-Site': 'none',
            },
            'extractor_retries': 5,
            'fragment_retries': 5,
            'retry_sleep_functions': {'http': lambda n: min(4 ** n, 30)},
            'sleep_interval_requests': random.uniform(1, 3),
            'force_ipv4': True,
            'nocheckcertificate': True,
            'ignoreerrors': False,
        }
        
        if proxy:
            ydl_opts['proxy'] = proxy
            logger.info(f"Using proxy: {proxy}")

        # Try primary method
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info:
                    logger.info("Primary extraction method succeeded")
                    return process_video_info(info)
        except Exception as e:
            logger.warning(f"Primary method failed: {e}")
            
        # Try alternative methods
        logger.info("Trying alternative extraction methods...")
        alternative_info = try_alternative_extractor(url)
        if alternative_info:
            return process_video_info(alternative_info)
            
        # If all methods fail, return error with suggestion
        raise Exception("Video extraction failed. This may be due to YouTube's bot detection. Try again in a few minutes or use a VPN.")

    except Exception as e:
        error_msg = f"Error extracting video info: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)

def process_video_info(info):
    """Process extracted video info into standard format"""
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

    # Process formats (simplified for reliability)
    available_formats = info.get('formats', [])
    
    video_formats = []
    audio_formats = []
    
    # Get best available formats only to avoid complexity
    video_formats = [{
        'format_id': 'best[height<=1080]',
        'ext': 'mp4',
        'quality': 'Best Quality (1080p max)',
        'filesize': 'Unknown',
        'fps': 'auto',
        'vcodec': 'auto',
        'acodec': 'auto',
        'type': 'best'
    }, {
        'format_id': 'best[height<=720]',
        'ext': 'mp4',
        'quality': 'Good Quality (720p max)',
        'filesize': 'Unknown',
        'fps': 'auto',
        'vcodec': 'auto',
        'acodec': 'auto',
        'type': 'best'
    }]

    audio_formats = [{
        'format_id': 'bestaudio[ext=m4a]',
        'ext': 'm4a',
        'quality': 'Best Quality (M4A)',
        'filesize': 'Unknown',
        'acodec': 'auto',
        'type': 'audio_best',
        'audio_format': 'M4A'
    }, {
        'format_id': 'bestaudio[ext=mp3]',
        'ext': 'mp3',
        'quality': 'Best Quality (MP3)',
        'filesize': 'Unknown',
        'acodec': 'auto',
        'type': 'audio_best',
        'audio_format': 'MP3'
    }]

    video_data['formats'] = video_formats
    video_data['audio_formats'] = audio_formats
    
    return video_data

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

        logger.info(f"Processing URL: {url}")

        if not re.match(r'^https?://', url):
            url = 'https://' + url

        if 'youtube.com' not in url and 'youtu.be' not in url:
            return jsonify({'error': 'Please provide a valid YouTube URL'}), 400

        video_info = get_video_info(url)
        logger.info(f"Video info extracted successfully: {video_info.get('title', 'Unknown')}")

        return jsonify(video_info)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in get_info: {error_msg}")
        
        # Provide helpful error message
        if "bot" in error_msg.lower():
            error_msg = "YouTube has blocked this server's IP address due to bot detection. Please try again in a few minutes, or contact support to enable proxy/VPN support."
        
        return jsonify({'error': error_msg}), 500

# [Rest of your existing routes remain the same - download, progress, etc.]
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

        logger.info(f"Starting download - URL: {url}, Format: {format_id}, Type: {download_type}")

        download_id = str(uuid.uuid4())
        download_progress[download_id] = {
            'status': 'starting',
            'percent': 0,
            'timestamp': time.time()
        }

        temp_dir = tempfile.mkdtemp()
        temp_dirs.add(temp_dir)

        user_agent = random.choice(USER_AGENTS)
        proxy = random.choice(PROXY_LIST) if PROXY_LIST else None
        
        ydl_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'progress_hooks': [DownloadProgressHook(download_id)],
            'user_agent': user_agent,
            'headers': {'User-Agent': user_agent},
            'format': format_id,
            'force_ipv4': True,
            'nocheckcertificate': True,
            'extractor_retries': 3,
            'sleep_interval_requests': 2,
        }
        
        if proxy:
            ydl_opts['proxy'] = proxy

        def download_video():
            try:
                download_progress[download_id]['status'] = 'downloading'
                time.sleep(random.uniform(2, 5))

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
                    shutil.rmtree(temp_dir)
                    temp_dirs.discard(temp_dir)
                except OSError as e:
                    logger.warning(f"Could not remove temp directory {temp_dir}: {e}")

        if download_id in download_progress:
            del download_progress[download_id]

        return jsonify({'success': True})

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.errorhandler(404)
def not_found_error(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'error': 'Internal server error'}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=False, host='0.0.0.0', port=port)

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

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'your-secret-key-change-in-production')

# Global dictionary to store download progress
download_progress = {}
temp_dirs = set()  # Track temp directories for cleanup

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

# Register cleanup function
atexit.register(cleanup_temp_dirs)

class DownloadProgressHook:
    def __init__(self, download_id):
        self.download_id = download_id
        self.last_update = time.time()

    def __call__(self, d):
        try:
            current_time = time.time()
            # Update progress every 0.5 seconds to avoid too frequent updates
            if current_time - self.last_update < 0.5 and d.get('status') == 'downloading':
                return
            
            self.last_update = current_time
            status = d.get('status', 'unknown')
            logger.info(f"Progress hook called with status: {status}")
            
            if status == 'downloading':
                percent = 0
                speed = 'N/A'
                eta = 'N/A'
                
                # Try different ways to get progress
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
                
                # Get speed
                if '_speed_str' in d:
                    speed = str(d['_speed_str'])
                elif 'speed' in d and d['speed']:
                    speed = format_bytes(d['speed']) + '/s'
                
                # Get ETA
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
                
                logger.info(f"Progress updated: {percent:.1f}% - Speed: {speed} - ETA: {eta}")
                
            elif status == 'finished':
                filename = d.get('filename', '')
                download_progress[self.download_id] = {
                    'status': 'finished',
                    'percent': 100,
                    'filename': filename,
                    'timestamp': current_time
                }
                
                logger.info(f"Download finished: {filename}")
                
            elif status == 'error':
                error_msg = d.get('error', 'Unknown download error')
                download_progress[self.download_id] = {
                    'status': 'error',
                    'error': str(error_msg),
                    'timestamp': current_time
                }
                
                logger.error(f"Download error: {error_msg}")
                
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

def get_video_info(url):
    """Extract video information and available formats"""
    try:
        logger.info(f"Extracting info from: {url}")
        ydl_opts = {
            'quiet': False,
            'no_warnings': False,
            'extract_flat': False,
            'writeinfojson': False,
            'writedescription': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info("Starting extraction...")
            info = ydl.extract_info(url, download=False)
            logger.info("Extraction completed")

            if not info:
                raise Exception("No video information found")

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

            logger.info(f"Basic info extracted: {video_data['title']}")

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

            # Extract formats with better logic
            available_formats = info.get('formats', [])
            logger.info(f"Found {len(available_formats)} formats")

            # Get the best combined formats (video+audio)
            combined_formats = {}
            video_only_formats = {}
            audio_only_formats = {}

            for fmt in available_formats:
                try:
                    format_id = fmt.get('format_id', '')
                    height = fmt.get('height')
                    vcodec = fmt.get('vcodec', 'none')
                    acodec = fmt.get('acodec', 'none')
                    filesize = fmt.get('filesize') or fmt.get('filesize_approx')

                    # Combined video+audio formats
                    if vcodec != 'none' and acodec != 'none' and height:
                        quality_key = f"{height}p"
                        if quality_key not in combined_formats or (filesize and filesize > combined_formats[quality_key].get('raw_filesize', 0)):
                            combined_formats[quality_key] = {
                                'format_id': format_id,
                                'ext': fmt.get('ext', 'mp4'),
                                'quality': quality_key,
                                'filesize': format_bytes(filesize),
                                'raw_filesize': filesize or 0,
                                'fps': fmt.get('fps', 'N/A'),
                                'vcodec': vcodec[:15],
                                'acodec': acodec[:15],
                                'type': 'combined'
                            }

                    # Video-only formats (for higher quality)
                    elif vcodec != 'none' and acodec == 'none' and height:
                        quality_key = f"{height}p"
                        video_only_formats[quality_key] = {
                            'format_id': format_id,
                            'ext': fmt.get('ext', 'mp4'),
                            'quality': quality_key,
                            'filesize': format_bytes(filesize),
                            'raw_filesize': filesize or 0,
                            'fps': fmt.get('fps', 'N/A'),
                            'vcodec': vcodec[:15],
                            'acodec': 'separate',
                            'type': 'video_only',
                            'needs_audio_merge': True
                        }

                    # Audio-only formats - Only M4A and MP3
                    elif acodec != 'none' and vcodec == 'none':
                        ext = fmt.get('ext', '').lower()
                        # Only include M4A and MP3 formats
                        if ext in ['m4a', 'mp3'] or 'mp4a' in acodec.lower() or 'mp3' in acodec.lower():
                            abr = fmt.get('abr', 0)
                            if abr and abr > 0:
                                # Determine output format based on codec/extension
                                if ext == 'm4a' or 'mp4a' in acodec.lower():
                                    output_ext = 'm4a'
                                    format_type = 'M4A'
                                else:
                                    output_ext = 'mp3'
                                    format_type = 'MP3'

                                quality_key = f"{int(abr)}kbps_{format_type}"
                                if quality_key not in audio_only_formats or (filesize and filesize > audio_only_formats[quality_key].get('raw_filesize', 0)):
                                    audio_only_formats[quality_key] = {
                                        'format_id': format_id,
                                        'ext': output_ext,
                                        'quality': f"{int(abr)}kbps ({format_type})",
                                        'filesize': format_bytes(filesize),
                                        'raw_filesize': filesize or 0,
                                        'acodec': acodec,
                                        'type': 'audio_only',
                                        'audio_format': format_type
                                    }

                except Exception as e:
                    logger.error(f"Error processing format {fmt.get('format_id', 'unknown')}: {e}")
                    continue

            # Merge formats prioritizing combined formats, then video-only with audio
            all_video_formats = {}
            # Add combined formats first
            all_video_formats.update(combined_formats)
            # Add video-only formats for qualities not available in combined
            for quality, fmt in video_only_formats.items():
                if quality not in all_video_formats:
                    all_video_formats[quality] = fmt

            # Convert to lists and sort
            video_formats = list(all_video_formats.values())
            video_formats.sort(key=lambda x: int(x['quality'].replace('p', '')), reverse=True)

            audio_formats = list(audio_only_formats.values())
            audio_formats.sort(key=lambda x: int(x['quality'].split('kbps')[0]), reverse=True)

            # Add fallback formats if none found
            if not video_formats:
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

            if not audio_formats:
                audio_formats = [
                    {
                        'format_id': 'bestaudio[ext=m4a]',
                        'ext': 'm4a',
                        'quality': 'Best Quality (M4A)',
                        'filesize': 'Unknown',
                        'acodec': 'auto',
                        'type': 'audio_best',
                        'audio_format': 'M4A'
                    },
                    {
                        'format_id': 'bestaudio[ext=mp3]',
                        'ext': 'mp3',
                        'quality': 'Best Quality (MP3)',
                        'filesize': 'Unknown',
                        'acodec': 'auto',
                        'type': 'audio_best',
                        'audio_format': 'MP3'
                    }
                ]

            video_data['formats'] = video_formats
            video_data['audio_formats'] = audio_formats

            logger.info(f"Found {len(video_formats)} video formats and {len(audio_formats)} audio formats")
            return video_data

    except Exception as e:
        error_msg = f"Error extracting video info: {str(e)}"
        logger.error(error_msg)
        raise Exception(error_msg)

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

        # Validate URL
        if not re.match(r'^https?://', url):
            url = 'https://' + url

        # Basic URL validation for YouTube
        if 'youtube.com' not in url and 'youtu.be' not in url:
            return jsonify({'error': 'Please provide a valid YouTube URL'}), 400

        video_info = get_video_info(url)
        logger.info(f"Video info extracted successfully: {video_info.get('title', 'Unknown')}")
        
        return jsonify(video_info)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in get_info: {error_msg}")
        return jsonify({'error': error_msg}), 500

@app.route('/download', methods=['POST'])
def download():
    try:
        data = request.json
        url = data.get('url', '').strip()
        format_id = data.get('format_id', '')
        download_type = data.get('type', 'video')  # 'video' or 'audio'
        format_info = data.get('format_info', {})

        if not url or not format_id:
            return jsonify({'error': 'Missing required parameters'}), 400

        logger.info(f"Starting download - URL: {url}, Format: {format_id}, Type: {download_type}")

        # Generate unique download ID
        download_id = str(uuid.uuid4())

        # Initialize progress
        download_progress[download_id] = {
            'status': 'starting',
            'percent': 0,
            'timestamp': time.time()
        }

        # Create temp directory
        temp_dir = tempfile.mkdtemp()
        temp_dirs.add(temp_dir)  # Track for cleanup
        logger.info(f"Temp directory: {temp_dir}")

        # Configure yt-dlp options based on format type
        base_opts = {
            'outtmpl': os.path.join(temp_dir, '%(title)s.%(ext)s'),
            'progress_hooks': [DownloadProgressHook(download_id)],
            'no_warnings': False,
            'extract_flat': False,
        }

        if download_type == 'audio':
            # Audio download - M4A or MP3 only
            audio_format = format_info.get('audio_format', 'MP3')
            
            if 'bestaudio[ext=m4a]' in format_id:
                format_selector = 'bestaudio[ext=m4a]/bestaudio[acodec*=mp4a]/bestaudio'
                preferred_codec = 'm4a'
            elif 'bestaudio[ext=mp3]' in format_id:
                format_selector = 'bestaudio[ext=mp3]/bestaudio[acodec*=mp3]/bestaudio'
                preferred_codec = 'mp3'
            elif audio_format == 'M4A' or format_info.get('ext') == 'm4a':
                format_selector = format_id if format_id != 'bestaudio' else 'bestaudio[ext=m4a]/bestaudio'
                preferred_codec = 'm4a'
            else:
                format_selector = format_id if format_id != 'bestaudio' else 'bestaudio[ext=mp3]/bestaudio'
                preferred_codec = 'mp3'

            ydl_opts = {
                **base_opts,
                'format': format_selector,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': preferred_codec,
                    'preferredquality': '192' if preferred_codec == 'mp3' else '128',
                }],
                'postprocessor_args': ['-ar', '44100'] if preferred_codec == 'mp3' else [],
                'prefer_ffmpeg': True,
            }

        else:
            # Video download with proper audio handling
            if format_id == 'best':
                # Use best combined format or merge video+audio
                format_selector = 'best[height<=1080][ext=mp4]/best[height<=1080]/bestvideo[height<=1080]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best'
            elif format_info.get('type') == 'video_only' or format_info.get('needs_audio_merge'):
                # Video-only format needs audio merging
                height = format_info.get('quality', '720').replace('p', '')
                format_selector = f"{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio/best[height<={height}]"
            else:
                # Combined format - ensure it has audio
                format_selector = f"{format_id}/best[height<=1080]"

            ydl_opts = {
                **base_opts,
                'format': format_selector,
                'merge_output_format': 'mp4',
                'postprocessors': [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }],
                # Ensure audio is preserved during conversion
                'postprocessor_args': {
                    'FFmpegVideoConvertor': ['-c:v', 'libx264', '-c:a', 'aac', '-strict', 'experimental']
                },
            }

        logger.info(f"Format selector: {ydl_opts['format']}")

        # Start download in background thread
        def download_video():
            try:
                logger.info(f"Starting download thread for {download_id}")
                download_progress[download_id]['status'] = 'downloading'
                download_progress[download_id]['timestamp'] = time.time()

                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    logger.info(f"Downloading with format: {ydl_opts['format']}")
                    ydl.download([url])

                # Check if download completed successfully
                logger.info(f"Download thread completed, checking files in: {temp_dir}")
                files = [f for f in os.listdir(temp_dir) if os.path.isfile(os.path.join(temp_dir, f))]
                logger.info(f"Files found: {files}")

                if files:
                    # Get the largest file (likely the download)
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

                    logger.info(f"Download completed successfully: {filename} ({format_bytes(file_size)})")
                else:
                    raise Exception("Download completed but no files found in temp directory")

            except Exception as e:
                error_msg = str(e)
                logger.error(f"Download thread error: {error_msg}")
                download_progress[download_id] = {
                    'status': 'error',
                    'error': error_msg,
                    'timestamp': time.time()
                }

        # Start download thread
        thread = threading.Thread(target=download_video, daemon=True)
        thread.start()

        return jsonify({'download_id': download_id})

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error in download endpoint: {error_msg}")
        return jsonify({'error': error_msg}), 500

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
        logger.error(f"Error in download_file: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/cleanup/<download_id>')
def cleanup(download_id):
    try:
        progress = download_progress.get(download_id)
        if progress and progress['status'] == 'finished':
            filename = progress['filename']
            if os.path.exists(filename):
                os.remove(filename)
                
            # Also remove the temp directory
            temp_dir = os.path.dirname(filename)
            if os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    temp_dirs.discard(temp_dir)  # Remove from tracking
                except OSError as e:
                    logger.warning(f"Could not remove temp directory {temp_dir}: {e}")

        # Remove from progress dict
        if download_id in download_progress:
            del download_progress[download_id]

        return jsonify({'success': True})

    except Exception as e:
        logger.error(f"Error in cleanup: {e}")
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

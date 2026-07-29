# BIMBO v4.0 — Torrent/Magnet Download (aria2c-based)
# Fixed: Uses aria2c for reliable magnet/torrent downloads on cloud platforms
# Fallback: libtorrent if aria2c is not available
# Bot safe — only this file changed, nothing else affected

import os
import re
import asyncio
import logging
import time
import shutil
from urllib.parse import urlparse

from pyrogram import filters, Client
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from config import Config, BIMBO_DOWNLOAD_LOCATION
from database.adduser import AddUser
from helper_funcs.display_progress import register_task, update_task, humanbytes

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# Libtorrent fallback (agar aria2c nahi hai to ye use hoga)
# ──────────────────────────────────────────────────────────
try:
    import libtorrent as lt
    LIBTORRENT_AVAILABLE = True
except ImportError:
    lt = None
    LIBTORRENT_AVAILABLE = False

# ──────────────────────────────────────────────────────────
# Aria2c XMLRPC connection
# ──────────────────────────────────────────────────────────
import xmlrpc.client

ARIA2_URI = "http://localhost:6800/jsonrpc"
ARIA2_SECRET = ""  # No secret by default for local daemon


def get_aria2_client():
    """Get aria2c RPC client"""
    try:
        server = xmlrpc.client.ServerProxy(ARIA2_URI, allow_none=True)
        version = server.aria2.getVersion(ARIA2_SECRET)
        logger.info(f"✅ Aria2 connected for torrent: v{version.get('version', '?')}")
        return server
    except Exception as e:
        logger.warning(f"⚠️ Aria2 not available for torrent: {e}")
        return None


# ──────────────────────────────────────────────────────────
# Link detection
# ──────────────────────────────────────────────────────────
def is_torrent_link(url: str) -> bool:
    """Check if URL is a torrent or magnet link"""
    url = url.strip()
    if url.startswith('magnet:'):
        return True
    if url.endswith('.torrent'):
        return True
    torrent_domains = [
        'thepiratebay', '1337x', 'rarbg', 'yts', 'eztv',
        'torrentz', 'limetorrents', 'torlock', 'demonoid'
    ]
    try:
        domain = urlparse(url).netloc.lower()
        return any(t in domain for t in torrent_domains)
    except Exception:
        return False


# ──────────────────────────────────────────────────────────
# Method 1: Download via aria2c (PRIMARY - reliable on cloud)
# ──────────────────────────────────────────────────────────
async def download_torrent_aria2(url: str, download_path: str,
                                  progress_msg: Message, chat_id: int) -> dict:
    """Download torrent/magnet using aria2c — works on Koyeb/Heroku/VPS"""
    server = get_aria2_client()
    if not server:
        return None  # Signal to try fallback

    os.makedirs(download_path, exist_ok=True)

    options = {
        'dir': download_path,
        'max-connection-per-server': '16',
        'split': '16',
        'min-split-size': '10M',
        'continue': 'true',
        'allow-overwrite': 'true',
        'auto-file-renaming': 'false',
        'file-allocation': 'none',
        'bt-max-peers': '100',
        'bt-stop-timeout': '300',       # 5 min timeout for peers
        'seed-ratio': '0',              # Don't seed after download
        'seed-time': '0',
        'follow-torrent': 'true',
        'bt-tracker': (
            'udp://tracker.opentrackr.org:1337/announce,'
            'udp://open.demonii.com:1337/announce,'
            'udp://tracker.openbittorrent.com:80,'
            'udp://exodus.desync.com:6969,'
            'udp://tracker.coppersurfer.tk:6969,'
            'udp://tracker.leechers-paradise.org:6969,'
            'udp://9.rarbg.to:2710/announce,'
            'udp://tracker.internetwarriors.net:1337,'
            'udp://tracker.torrent.eu.org:451/announce,'
            'udp://tracker.tiny-vps.com:6969/announce,'
            'udp://opentor.org:2710,'
            'udp://tracker.ds.is:6969/announce,'
            'udp://open.stealth.si:80/announce'
        ),
        'enable-dht': 'true',
        'enable-peer-exchange': 'true',
    }

    gid = None
    is_magnet = url.strip().startswith('magnet:')

    try:
        # Add download based on type
        if is_magnet:
            # Direct magnet URI — aria2c handles natively
            logger.info(f"🧲 Adding magnet to aria2c...")
            await asyncio.to_thread(
                server.aria2.addUri, ARIA2_SECRET, [url.strip()], options
            )
            # Get the GID from active downloads (latest added)
            active = server.aria2.tellActive(ARIA2_SECRET, ['gid', 'bittorrent'])
            waiting = server.aria2.tellWaiting(ARIA2_SECRET, -10, 10, ['gid', 'bittorrent'])
            all_downloads = active + waiting
            if all_downloads:
                # Find our torrent by checking bittorrent info
                for dl in all_downloads:
                    bt = dl.get('bittorrent', {})
                    if bt or is_magnet:
                        gid = dl.get('gid')
                        break
            if not gid and all_downloads:
                gid = all_downloads[0].get('gid')

            # Wait a moment and poll for GID if not found
            if not gid:
                for _ in range(10):
                    await asyncio.sleep(2)
                    all_dl = server.aria2.tellActive(ARIA2_SECRET, ['gid']) + \
                             server.aria2.tellWaiting(ARIA2_SECRET, -10, 10, ['gid'])
                    if all_dl:
                        gid = all_dl[-1].get('gid')
                        break

        elif url.strip().endswith('.torrent'):
            # Download .torrent file first, then add to aria2c
            import requests
            logger.info(f"📥 Downloading .torrent file from URL...")
            resp = await asyncio.to_thread(requests.get, url.strip(), timeout=30)
            if resp.status_code != 200:
                return {'success': False, 'error': f'Failed to download .torrent file (HTTP {resp.status_code})'}

            torrent_file_path = os.path.join(download_path, '_temp.torrent')
            with open(torrent_file_path, 'wb') as f:
                f.write(resp.content)

            logger.info(f"📥 Adding .torrent to aria2c...")
            result = await asyncio.to_thread(
                server.aria2.addTorrent, ARIA2_SECRET, torrent_file_path, [], options
            )
            gid = result
            # Clean up temp torrent file
            try:
                os.remove(torrent_file_path)
            except Exception:
                pass
        else:
            # URL to a .torrent file (not ending in .torrent but is one)
            import requests
            resp = await asyncio.to_thread(requests.get, url.strip(), timeout=30)
            if resp.status_code != 200:
                return {'success': False, 'error': f'Failed to download file (HTTP {resp.status_code})'}
            torrent_file_path = os.path.join(download_path, '_temp.torrent')
            with open(torrent_file_path, 'wb') as f:
                f.write(resp.content)
            result = await asyncio.to_thread(
                server.aria2.addTorrent, ARIA2_SECRET, torrent_file_path, [], options
            )
            gid = result
            try:
                os.remove(torrent_file_path)
            except Exception:
                pass

        if not gid:
            return {'success': False, 'error': 'Could not get download GID from aria2c'}

        logger.info(f"✅ Torrent added to aria2c: GID={gid}")

        # Register in task tracker
        task_id = f"torrent_{gid}"
        register_task(task_id, chat_id, "Fetching metadata...", 0, 'download', 'aria2')

        # ── Progress monitoring loop ──
        filename = "Fetching metadata..."
        total_size = 0
        metadata_wait = 0
        max_metadata_wait = 180  # 3 minutes for metadata

        while True:
            try:
                status = await asyncio.to_thread(
                    server.aria2.tellStatus, ARIA2_SECRET, gid,
                    ['gid', 'status', 'totalLength', 'completedLength',
                     'downloadSpeed', 'uploadSpeed', 'connections',
                     'dir', 'files', 'bittorrent', 'errorCode',
                     'errorMessage', 'numSeeders']
                )
            except Exception as e:
                logger.error(f"Aria2 status error: {e}")
                await asyncio.sleep(3)
                continue

            current_status = status.get('status', '')
            completed = int(status.get('completedLength', 0))
            total = int(status.get('totalLength', 0))
            speed = int(status.get('downloadSpeed', 0))
            connections = int(status.get('connections', 0))
            seeders = status.get('numSeeders', '0')

            # Update filename when available
            bt = status.get('bittorrent', {})
            if bt and bt.get('info', {}).get('name'):
                filename = bt['info']['name']
            files = status.get('files', [])
            if files and filename == "Fetching metadata...":
                for f in files:
                    path = f.get('path', '')
                    if path:
                        filename = os.path.basename(path)
                        break

            # Update task tracker
            update_task(task_id, completed, total, speed, 'downloading', 'aria2')
            total_size = total

            # ── Update progress message ──
            if total > 0:
                progress = (completed / total) * 100
                downloaded_mb = completed / (1024 * 1024)
                total_mb = total / (1024 * 1024)
                speed_mb = speed / (1024 * 1024)

                bar_len = 20
                filled = int(bar_len * progress / 100)
                bar = '█' * filled + '░' * (bar_len - filled)

                text = (
                    f"🧲 **Torrent Download**\n\n"
                    f"📁 **File:** `{filename[:60]}{'...' if len(filename) > 60 else ''}`\n\n"
                    f"📊 **Progress:** {bar} {progress:.1f}%\n"
                    f"💾 **Size:** {downloaded_mb:.2f} MB / {total_mb:.2f} MB\n"
                    f"⚡ **Speed:** {speed_mb:.2f} MB/s\n"
                    f"👥 **Peers:** {connections} (Seeds: {seeders})\n"
                    f"🔄 **Status:** Downloading..."
                )
            else:
                metadata_wait += 3
                text = (
                    f"🧲 **Torrent Download**\n\n"
                    f"📁 **File:** `{filename[:60]}`\n\n"
                    f"📊 **Progress:** Waiting for metadata...\n"
                    f"👥 **Peers:** {connections}\n"
                    f"⏳ **Timeout:** {max_metadata_wait - metadata_wait}s remaining\n"
                    f"🔄 **Status:** Connecting to swarm..."
                )

            try:
                await progress_msg.edit_text(text)
            except Exception:
                pass

            # Check completion
            if current_status == 'complete':
                logger.info(f"✅ Torrent download complete: {gid}")
                update_task(task_id, total, total, 0, 'completed', 'aria2')

                # Get downloaded file path
                if files:
                    if len(files) == 1:
                        file_path = files[0].get('path', '')
                    else:
                        # Multiple files — return the download directory
                        file_path = download_path
                else:
                    # Fallback: find files in download_path
                    found_files = []
                    for root, dirs, fnames in os.walk(download_path):
                        for fname in fnames:
                            fpath = os.path.join(root, fname)
                            if not fname.startswith('_temp') and not fname.startswith('.aria2'):
                                found_files.append(fpath)
                    file_path = found_files[0] if len(found_files) == 1 else download_path

                return {
                    'success': True,
                    'file_path': file_path,
                    'file_name': filename,
                    'size': total_size,
                    'engine': 'aria2c'
                }

            if current_status == 'error':
                error = status.get('errorMessage', 'Unknown error')
                return {'success': False, 'error': f'Aria2 error: {error}'}

            if current_status == 'removed':
                return {'success': False, 'error': 'Download was removed'}

            # Metadata timeout check
            if total == 0 and metadata_wait > max_metadata_wait:
                # Try to remove and report error
                try:
                    server.aria2.forceRemove(ARIA2_SECRET, gid)
                except Exception:
                    pass
                return {
                    'success': False,
                    'error': f'Metadata timeout ({max_metadata_wait}s) — no peers found. '
                             'Torrent might be dead or network blocked.'
                }

            await asyncio.sleep(3)

    except Exception as e:
        logger.error(f"Torrent aria2 download error: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


# ──────────────────────────────────────────────────────────
# Method 2: Download via libtorrent (FALLBACK)
# ──────────────────────────────────────────────────────────
async def download_torrent_libtorrent(url: str, download_path: str,
                                       progress_msg: Message) -> dict:
    """Fallback: Download torrent using libtorrent (needs DHT connectivity)"""
    if not LIBTORRENT_AVAILABLE:
        return {'success': False, 'error': 'libtorrent not installed'}

    try:
        session = lt.session({
            'user_agent': 'BIMBO-Bot/1.0',
            'listen_interfaces': '0.0.0.0:6881,[::]:6881',
            'download_rate_limit': 0,
            'upload_rate_limit': 0,
            'connections_limit': 200,
            'alert_mask': lt.alert.category_t.status_notification | lt.alert.category_t.error_notification,
        })

        # Add DHT for peer discovery
        try:
            session.add_dht_router('router.bittorrent.com', 6881)
            session.add_dht_router('router.utorrent.com', 6881)
            session.add_dht_router('dht.transmissionbt.com', 6881)
        except Exception:
            pass

        os.makedirs(download_path, exist_ok=True)

        # Parse magnet or torrent
        if url.startswith('magnet:'):
            # libtorrent 2.x API
            try:
                params = lt.parse_magnet_uri(url)
                params.save_path = download_path
                handle = session.add_torrent(params)
            except AttributeError:
                # Fallback for older versions
                handle = lt.add_magnet_uri(session, url, {'save_path': download_path})
        else:
            import requests as req
            response = await asyncio.to_thread(req.get, url, timeout=30)
            torrent_data = lt.bdecode(response.content)
            info = lt.torrent_info(torrent_data)
            handle = session.add_torrent({'ti': info, 'save_path': download_path})

        # Add extra trackers
        trackers = [
            'udp://tracker.opentrackr.org:1337/announce',
            'udp://open.demonii.com:1337/announce',
            'udp://tracker.openbittorrent.com:80',
            'udp://exodus.desync.com:6969',
            'udp://tracker.torrent.eu.org:451/announce',
        ]
        for tracker in trackers:
            try:
                handle.add_tracker({'url': tracker, 'tier': 0})
            except Exception:
                pass

        # Wait for metadata
        logger.info("Torrent (libtorrent): Waiting for metadata...")
        timeout = 120
        while not handle.has_metadata() and timeout > 0:
            await asyncio.sleep(1)
            timeout -= 1
            s = handle.status()
            try:
                await progress_msg.edit_text(
                    f"🧲 **Torrent Download** (libtorrent fallback)\n\n"
                    f"📊 **Status:** Finding peers... ({timeout}s remaining)\n"
                    f"👥 **Peers:** {s.num_peers}"
                )
            except Exception:
                pass

        if not handle.has_metadata():
            return {'success': False, 'error': 'Metadata timeout — no peers (libtorrent)'}

        torrent_info = handle.torrent_file()
        file_name = torrent_info.name()
        total_size = torrent_info.total_size()

        logger.info(f"Torrent (libtorrent): {file_name} ({total_size / (1024*1024):.1f} MB)")

        # Download with progress
        while not handle.is_seed():
            status = handle.status()
            progress = status.progress * 100
            downloaded = status.total_done
            speed = status.download_rate

            bar_len = 20
            filled = int(bar_len * progress / 100)
            bar = '█' * filled + '░' * (bar_len - filled)

            text = (
                f"🧲 **Torrent Download** (libtorrent)\n\n"
                f"📁 **File:** `{file_name[:60]}{'...' if len(file_name) > 60 else ''}`\n\n"
                f"📊 **Progress:** {bar} {progress:.1f}%\n"
                f"💾 **Size:** {downloaded / (1024*1024):.2f} / {total_size / (1024*1024):.2f} MB\n"
                f"⚡ **Speed:** {speed / (1024*1024):.2f} MB/s\n"
                f"👥 **Peers:** {status.num_peers} (Seeds: {status.num_seeds})\n"
            )
            try:
                await progress_msg.edit_text(text)
            except Exception:
                pass

            await asyncio.sleep(2)

        file_path = os.path.join(download_path, file_name)
        return {
            'success': True,
            'file_path': file_path,
            'file_name': file_name,
            'size': total_size,
            'engine': 'libtorrent'
        }

    except Exception as e:
        logger.error(f"libtorrent download error: {e}", exc_info=True)
        return {'success': False, 'error': str(e)}


# ──────────────────────────────────────────────────────────
# Upload helper
# ──────────────────────────────────────────────────────────
async def upload_to_telegram(client, chat_id: int, file_path: str, caption: str):
    """Upload file(s) to Telegram"""
    if os.path.isdir(file_path):
        # Upload all files in directory
        uploaded = 0
        for root, dirs, files in os.walk(file_path):
            # Skip .aria2 hidden files
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for file in files:
                if file.startswith('.'):
                    continue
                file_full_path = os.path.join(root, file)
                file_size = os.path.getsize(file_full_path)

                # Determine upload type based on extension
                ext = os.path.splitext(file)[1].lower()
                try:
                    if ext in ['.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.m4v']:
                        await client.send_video(
                            chat_id=chat_id,
                            video=file_full_path,
                            caption=f"🧲 {file}\n{caption}",
                            supports_streaming=True
                        )
                    elif ext in ['.mp3', '.m4a', '.wav', '.flac', '.ogg', '.aac']:
                        await client.send_audio(
                            chat_id=chat_id,
                            audio=file_full_path,
                            caption=f"🧲 {file}\n{caption}"
                        )
                    else:
                        await client.send_document(
                            chat_id=chat_id,
                            document=file_full_path,
                            caption=f"🧲 {file}\n{caption}"
                        )
                    uploaded += 1
                except Exception as e:
                    logger.error(f"Upload error for {file}: {e}")
                    # Try as document
                    try:
                        await client.send_document(
                            chat_id=chat_id,
                            document=file_full_path,
                            caption=f"🧲 {file}\n{caption}"
                        )
                        uploaded += 1
                    except Exception as e2:
                        logger.error(f"Document upload also failed for {file}: {e2}")

        return uploaded
    else:
        # Single file upload
        ext = os.path.splitext(file_path)[1].lower()
        file_name = os.path.basename(file_path)
        try:
            if ext in ['.mp4', '.mkv', '.webm', '.avi', '.mov', '.flv', '.m4v']:
                await client.send_video(
                    chat_id=chat_id,
                    video=file_path,
                    caption=f"🧲 {file_name}\n{caption}",
                    supports_streaming=True
                )
            elif ext in ['.mp3', '.m4a', '.wav', '.flac', '.ogg', '.aac']:
                await client.send_audio(
                    chat_id=chat_id,
                    audio=file_path,
                    caption=f"🧲 {file_name}\n{caption}"
                )
            else:
                await client.send_document(
                    chat_id=chat_id,
                    document=file_path,
                    caption=f"🧲 {file_name}\n{caption}"
                )
            return 1
        except Exception as e:
            logger.error(f"Single file upload error: {e}")
            # Last resort: try document
            try:
                await client.send_document(
                    chat_id=chat_id,
                    document=file_path,
                    caption=f"🧲 {file_name}\n{caption}"
                )
                return 1
            except Exception as e2:
                logger.error(f"Document fallback also failed: {e2}")
                return 0


# ──────────────────────────────────────────────────────────
# Main handler — catches magnet: and .torrent links
# ──────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.regex(r'(?i)^magnet:\?|^https?://.*\.torrent'))
async def handle_torrent(client: Client, message: Message):
    """Handle torrent/magnet links — aria2c primary, libtorrent fallback"""
    await AddUser(client, message)

    url = message.text.strip()

    if not is_torrent_link(url):
        return

    user_id = message.from_user.id
    download_path = os.path.join(BIMBO_DOWNLOAD_LOCATION, f"torrent_{user_id}_{int(time.time())}")

    # Send initial message
    progress_msg = await message.reply_text(
        "🧲 **Torrent Detected**\n\n"
        "🔄 Connecting to aria2c...\n"
        "⏳ Please wait..."
    )

    # Ensure download directory exists
    os.makedirs(download_path, exist_ok=True)

    result = None

    # ── Try aria2c first (PRIMARY — works on cloud platforms) ──
    try:
        await progress_msg.edit_text(
            "🧲 **Torrent Download**\n\n"
            "⚡ Using **aria2c** (primary engine)\n"
            "🔄 Adding torrent..."
        )
        result = await download_torrent_aria2(url, download_path, progress_msg, user_id)
    except Exception as e:
        logger.error(f"aria2c torrent failed: {e}")
        result = None

    # ── If aria2c failed/unavailable, try libtorrent fallback ──
    if result is None or (isinstance(result, dict) and not result.get('success')):
        aria2_error = result.get('error', 'aria2c not available') if result else 'aria2c returned None'

        if LIBTORRENT_AVAILABLE:
            logger.info(f"aria2c torrent failed ({aria2_error}), trying libtorrent fallback...")
            await progress_msg.edit_text(
                "🧲 **Torrent Download**\n\n"
                f"⚠️ aria2c failed: {aria2_error[:80]}\n"
                "🔄 Switching to libtorrent (fallback)...\n"
                "⏳ This may take longer on cloud platforms..."
            )
            await asyncio.sleep(2)
            result = await download_torrent_libtorrent(url, download_path, progress_msg)
        else:
            result = {
                'success': False,
                'error': f'aria2c failed ({aria2_error}) and libtorrent is not installed.\n\n'
                         'Make sure aria2c is running or install libtorrent.'
            }

    # ── Handle result ──
    if not result or not result.get('success'):
        error = result.get('error', 'Unknown error') if result else 'Download failed'
        await progress_msg.edit_text(
            f"❌ **Torrent Download Failed**\n\n"
            f"Error: `{error[:500]}`\n\n"
            "💡 **Tips:**\n"
            "• Magnet link dead ho sakta hai (no seeders)\n"
            "• Koyeb/Heroku pe DHT limited hota hai\n"
            "• VPS pe zyada reliable chalega\n"
            "• Torrent search command `/ts` se active torrent dhundho"
        )
        # Cleanup
        try:
            shutil.rmtree(download_path, ignore_errors=True)
        except Exception:
            pass
        return

    # ── Download complete — upload to Telegram ──
    engine_name = result.get('engine', 'unknown')
    file_name = result.get('file_name', 'Unknown')
    file_size = result.get('size', 0)
    file_path = result.get('file_path', '')

    await progress_msg.edit_text(
        f"✅ **Download Complete!** ({engine_name})\n\n"
        f"📁 **File:** `{file_name}`\n"
        f"💾 **Size:** {humanbytes(file_size)}\n"
        f"🔧 **Engine:** {engine_name}\n\n"
        f"📤 Uploading to Telegram..."
    )

    # Upload to Telegram
    caption = f"✅ Downloaded by BIMBO Bot"
    uploaded = await upload_to_telegram(client, message.chat.id, file_path, caption)

    if uploaded > 0:
        # Delete progress message
        try:
            await progress_msg.delete()
        except Exception:
            pass
    else:
        await progress_msg.edit_text(
            f"❌ **Upload Failed**\n\n"
            f"Downloaded successfully but upload to Telegram failed.\n"
            f"📁 File: `{file_name}`\n"
            f"💾 Size: {humanbytes(file_size)}"
        )

    # ── Cleanup downloaded files ──
    try:
        shutil.rmtree(download_path, ignore_errors=True)
        logger.info(f"🧹 Cleaned up torrent download: {download_path}")
    except Exception:
        pass


# ──────────────────────────────────────────────────────────
# Handler: .torrent file document received directly
# ──────────────────────────────────────────────────────────
@Client.on_message(filters.private & filters.document)
async def handle_torrent_document(client: Client, message: Message):
    """Handle .torrent file sent as document"""
    doc = message.document
    if not doc or not doc.file_name:
        return
    if not doc.file_name.lower().endswith('.torrent'):
        return

    await AddUser(client, message)
    user_id = message.from_user.id

    progress_msg = await message.reply_text(
        "🧲 **Torrent File Detected**\n\n"
        f"📄 **File:** `{doc.file_name}`\n"
        f"💾 **Size:** {humanbytes(doc.file_size)}\n\n"
        "🔄 Downloading torrent file..."
    )

    # Download the .torrent file from Telegram
    temp_dir = os.path.join(BIMBO_DOWNLOAD_LOCATION, f"torrent_temp_{user_id}")
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        torrent_file_path = await message.download(file_name=os.path.join(temp_dir, doc.file_name))
        logger.info(f"📥 Torrent file downloaded: {torrent_file_path}")
    except Exception as e:
        await progress_msg.edit_text(f"❌ Failed to download .torrent file: {e}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    download_path = os.path.join(BIMBO_DOWNLOAD_LOCATION, f"torrent_{user_id}_{int(time.time())}")
    os.makedirs(download_path, exist_ok=True)

    result = None

    # Try aria2c first
    try:
        server = get_aria2_client()
        if server:
            options = {
                'dir': download_path,
                'max-connection-per-server': '16',
                'split': '16',
                'min-split-size': '10M',
                'continue': 'true',
                'allow-overwrite': 'true',
                'bt-max-peers': '100',
                'bt-stop-timeout': '300',
                'seed-ratio': '0',
                'seed-time': '0',
                'follow-torrent': 'true',
                'bt-tracker': (
                    'udp://tracker.opentrackr.org:1337/announce,'
                    'udp://open.demonii.com:1337/announce,'
                    'udp://tracker.openbittorrent.com:80,'
                    'udp://exodus.desync.com:6969,'
                    'udp://tracker.coppersurfer.tk:6969,'
                    'udp://tracker.torrent.eu.org:451/announce,'
                    'udp://opentor.org:2710'
                ),
                'enable-dht': 'true',
                'enable-peer-exchange': 'true',
            }

            gid = await asyncio.to_thread(
                server.aria2.addTorrent, ARIA2_SECRET, torrent_file_path, [], options
            )

            if gid:
                logger.info(f"✅ Torrent file added to aria2c: GID={gid}")
                task_id = f"torrent_{gid}"
                register_task(task_id, user_id, doc.file_name, 0, 'download', 'aria2')

                # Monitor progress
                filename = doc.file_name.replace('.torrent', '')
                total_size = 0
                metadata_wait = 0
                max_metadata_wait = 180

                while True:
                    try:
                        status = await asyncio.to_thread(
                            server.aria2.tellStatus, ARIA2_SECRET, gid,
                            ['gid', 'status', 'totalLength', 'completedLength',
                             'downloadSpeed', 'connections', 'bittorrent',
                             'errorCode', 'errorMessage', 'numSeeders']
                        )
                    except Exception:
                        await asyncio.sleep(3)
                        continue

                    current_status = status.get('status', '')
                    completed = int(status.get('completedLength', 0))
                    total = int(status.get('totalLength', 0))
                    speed = int(status.get('downloadSpeed', 0))
                    connections = int(status.get('connections', 0))
                    seeders = status.get('numSeeders', '0')

                    bt = status.get('bittorrent', {})
                    if bt and bt.get('info', {}).get('name'):
                        filename = bt['info']['name']

                    update_task(task_id, completed, total, speed, 'downloading', 'aria2')
                    total_size = total

                    if total > 0:
                        progress = (completed / total) * 100
                        bar_len = 20
                        filled = int(bar_len * progress / 100)
                        bar = '█' * filled + '░' * (bar_len - filled)
                        text = (
                            f"🧲 **Torrent Download**\n\n"
                            f"📁 **File:** `{filename[:60]}`\n\n"
                            f"📊 **Progress:** {bar} {progress:.1f}%\n"
                            f"💾 **Size:** {completed / (1024*1024):.1f} / {total / (1024*1024):.1f} MB\n"
                            f"⚡ **Speed:** {speed / (1024*1024):.2f} MB/s\n"
                            f"👥 **Peers:** {connections} (Seeds: {seeders})"
                        )
                    else:
                        metadata_wait += 3
                        text = (
                            f"🧲 **Torrent Download**\n\n"
                            f"📁 **File:** `{filename[:60]}`\n\n"
                            f"⏳ Finding peers... ({max_metadata_wait - metadata_wait}s)\n"
                            f"👥 **Peers:** {connections}"
                        )

                    try:
                        await progress_msg.edit_text(text)
                    except Exception:
                        pass

                    if current_status == 'complete':
                        update_task(task_id, total, total, 0, 'completed', 'aria2')
                        # Find downloaded files
                        found_files = []
                        for root, dirs, files in os.walk(download_path):
                            dirs[:] = [d for d in dirs if not d.startswith('.')]
                            for f in files:
                                if not f.startswith('.'):
                                    found_files.append(os.path.join(root, f))
                        file_path = found_files[0] if len(found_files) == 1 else download_path
                        result = {
                            'success': True,
                            'file_path': file_path,
                            'file_name': filename,
                            'size': total_size,
                            'engine': 'aria2c'
                        }
                        break

                    if current_status == 'error':
                        result = {'success': False, 'error': status.get('errorMessage', 'Error')}
                        break

                    if total == 0 and metadata_wait > max_metadata_wait:
                        try:
                            server.aria2.forceRemove(ARIA2_SECRET, gid)
                        except Exception:
                            pass
                        result = {'success': False, 'error': 'Metadata timeout - no peers'}
                        break

                    await asyncio.sleep(3)
    except Exception as e:
        logger.error(f"aria2c torrent file error: {e}")

    # Fallback to libtorrent
    if result is None or (isinstance(result, dict) and not result.get('success')):
        if LIBTORRENT_AVAILABLE:
            try:
                session = lt.session({
                    'listen_interfaces': '0.0.0.0:6881,[::]:6881',
                    'alert_mask': lt.alert.category_t.status_notification | lt.alert.category_t.error_notification,
                })
                try:
                    session.add_dht_router('router.bittorrent.com', 6881)
                    session.add_dht_router('router.utorrent.com', 6881)
                except Exception:
                    pass

                with open(torrent_file_path, 'rb') as f:
                    torrent_data = lt.bdecode(f.read())
                info = lt.torrent_info(torrent_data)
                handle = session.add_torrent({'ti': info, 'save_path': download_path})
                handle.resume()

                # Wait for metadata
                for _ in range(60):
                    if handle.has_metadata():
                        break
                    await asyncio.sleep(1)

                if handle.has_metadata():
                    ti = handle.torrent_file()
                    filename = ti.name()
                    total_size = ti.total_size()

                    while not handle.is_seed():
                        st = handle.status()
                        progress = st.progress * 100
                        bar = '█' * int(progress / 5) + '░' * (20 - int(progress / 5))
                        try:
                            await progress_msg.edit_text(
                                f"🧲 **Torrent** (libtorrent)\n\n"
                                f"📁 `{filename[:60]}`\n"
                                f"{bar} {progress:.1f}%\n"
                                f"💾 {st.total_done / (1024*1024):.1f} / {total_size / (1024*1024):.1f} MB\n"
                                f"⚡ {st.download_rate / (1024*1024):.2f} MB/s"
                            )
                        except Exception:
                            pass
                        await asyncio.sleep(2)

                    file_path = os.path.join(download_path, filename)
                    result = {
                        'success': True,
                        'file_path': file_path,
                        'file_name': filename,
                        'size': total_size,
                        'engine': 'libtorrent'
                    }
                else:
                    result = {'success': False, 'error': 'Metadata timeout (libtorrent)'}
            except Exception as e:
                result = {'success': False, 'error': str(e)}
        else:
            result = result or {'success': False, 'error': 'aria2c failed and libtorrent not available'}

    # Handle result
    if not result or not result.get('success'):
        error = result.get('error', 'Unknown') if result else 'Failed'
        await progress_msg.edit_text(
            f"❌ **Torrent Download Failed**\n\nError: `{error[:300]}`"
        )
    else:
        await progress_msg.edit_text(
            f"✅ **Download Complete!**\n\n"
            f"📁 `{result['file_name']}`\n"
            f"💾 {humanbytes(result.get('size', 0))}\n\n"
            f"📤 Uploading..."
        )
        uploaded = await upload_to_telegram(client, message.chat.id, result['file_path'], "✅ via BIMBO Bot")
        if uploaded > 0:
            try:
                await progress_msg.delete()
            except Exception:
                pass
        else:
            await progress_msg.edit_text("❌ Upload failed. File downloaded but couldn't send.")

    # Cleanup
    try:
        shutil.rmtree(download_path, ignore_errors=True)
        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception:
        pass

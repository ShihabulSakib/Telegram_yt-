#!/usr/bin/env python3
"""
Telegram Link Harvester - Main CLI Application
A tool to scan Telegram channels/groups and download linked content
NOW WITH PARALLEL DOWNLOADS! 🚀
"""

import asyncio
import json
import os
import sys
import re
import time
from datetime import datetime
from pathlib import Path
import argparse
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

try:
    from telethon import TelegramClient
    from telethon.tl.types import Message
    from telethon.errors import SessionPasswordNeededError
except ImportError:
    print("Error: telethon not installed. Run: pip install telethon")
    sys.exit(1)

try:
    import yt_dlp
except ImportError:
    print("Warning: yt-dlp not installed. YouTube downloads won't work.")
    print("Install with: pip install yt-dlp")

try:
    import gdown
except ImportError:
    print("Warning: gdown not installed. Google Drive downloads won't work.")
    print("Install with: pip install gdown")

try:
    from tqdm import tqdm
except ImportError:
    print("Warning: tqdm not installed. Installing for better progress bars...")
    print("Run: pip install tqdm")
    tqdm = None


class Config:
    """Configuration manager"""
    
    def __init__(self, config_path='config.json'):
        self.config_path = config_path
        self.data = self.load_config()
    
    def load_config(self) -> dict:
        """Load configuration from JSON file"""
        if not os.path.exists(self.config_path):
            print(f"Error: {self.config_path} not found!")
            print("Create a config.json with the following structure:")
            print(json.dumps({
                "api_id": "your_api_id",
                "api_hash": "your_api_hash",
                "phone": "your_phone_number"
            }, indent=2))
            sys.exit(1)
        
        with open(self.config_path, 'r') as f:
            return json.load(f)
    
    @property
    def api_id(self):
        return self.data.get('api_id')
    
    @property
    def api_hash(self):
        return self.data.get('api_hash')
    
    @property
    def phone(self):
        return self.data.get('phone')


class LinkExtractor:
    """Extract and classify links from text"""
    
    YOUTUBE_PATTERNS = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=[\w-]+',
        r'(?:https?://)?(?:www\.)?youtu\.be/[\w-]+',
        r'(?:https?://)?(?:www\.)?youtube\.com/playlist\?list=[\w-]+',
    ]
    
    DRIVE_PATTERNS = [
        r'(?:https?://)?drive\.google\.com/file/d/[\w-]+',
        r'(?:https?://)?drive\.google\.com/drive/folders/[\w-]+',
        r'(?:https?://)?drive\.google\.com/open\?id=[\w-]+',
    ]
    
    @staticmethod
    def extract_links(text: str) -> List[Dict[str, str]]:
        """Extract all supported links from text"""
        if not text:
            return []
        
        links = []
        
        # Extract YouTube links
        for pattern in LinkExtractor.YOUTUBE_PATTERNS:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                links.append({
                    'url': match.group(0),
                    'type': 'youtube'
                })
        
        # Extract Google Drive links
        for pattern in LinkExtractor.DRIVE_PATTERNS:
            matches = re.finditer(pattern, text, re.IGNORECASE)
            for match in matches:
                links.append({
                    'url': match.group(0),
                    'type': 'drive'
                })
        
        return links


class TelegramScanner:
    """Scan Telegram channels/groups for links"""
    
    def __init__(self, config: Config):
        self.config = config
        self.client = None
        self.session_file = 'session.session'
    
    async def connect(self):
        """Connect to Telegram"""
        self.client = TelegramClient(
            self.session_file,
            self.config.api_id,
            self.config.api_hash
        )
        
        await self.client.start(phone=self.config.phone)
        
        if not await self.client.is_user_authorized():
            await self.client.send_code_request(self.config.phone)
            try:
                await self.client.sign_in(self.config.phone, input('Enter code: '))
            except SessionPasswordNeededError:
                await self.client.sign_in(password=input('Enter 2FA password: '))
        
        print("✓ Connected to Telegram")
    
    async def scan_channel(self, channel: str, limit: Optional[int] = None) -> List[Dict]:
        """Scan a channel/group for links"""
        if not self.client:
            await self.connect()
        
        print(f"\nScanning channel: {channel}")
        print(f"Limit: {limit if limit else 'All messages'}")
        
        collected = []
        count = 0
        
        try:
            async for message in self.client.iter_messages(channel, limit=limit):
                if not isinstance(message, Message):
                    continue
                
                count += 1
                if count % 100 == 0:
                    print(f"  Scanned {count} messages, found {len(collected)} links...")
                
                text = message.text or message.message or ''
                links = LinkExtractor.extract_links(text)
                
                if links:
                    for link in links:
                        collected.append({
                            'url': link['url'],
                            'type': link['type'],
                            'caption': text[:500],  # First 500 chars
                            'date': message.date.isoformat(),
                            'message_id': message.id,
                            'channel': channel,
                            'sender': getattr(message.sender, 'username', 'unknown') if message.sender else 'unknown'
                        })
        
        except Exception as e:
            print(f"Error scanning channel: {e}")
            return collected
        
        print(f"\n✓ Scan complete: {count} messages scanned, {len(collected)} links found")
        return collected
    
    async def close(self):
        """Close Telegram connection"""
        if self.client:
            await self.client.disconnect()


class DataManager:
    """Handles all file and directory operations for harvester data."""

    def __init__(self, data_dir='.harvester_data'):
        self.data_dir = Path(data_dir)
        self.channels_dir = self.data_dir / 'channels'
        self.channel_info_path = self.data_dir / 'channel_info.json'
        
        # Create directories if they don't exist
        self.data_dir.mkdir(exist_ok=True)
        self.channels_dir.mkdir(exist_ok=True)

    def _get_channel_id_str(self, channel_entity) -> str:
        """Converts a channel entity (username or ID) to a safe string for filenames."""
        channel_id = str(channel_entity).lstrip('@')
        # Sanitize for filename
        return re.sub(r'[^\w\.\-]', '_', channel_id)

    def get_db_path(self, channel_entity) -> str:
        """Gets the database file path for a specific channel."""
        safe_channel_id = self._get_channel_id_str(channel_entity)
        return str(self.channels_dir / f"{safe_channel_id}.json")

    def save_channel_list(self, channel_list: List[Dict]):
        """Saves the list of user's channels to a file."""
        with open(self.channel_info_path, 'w', encoding='utf-8') as f:
            json.dump(channel_list, f, indent=2, ensure_ascii=False)
        print(f"✓ Channel list saved to {self.channel_info_path}")

    def load_channel_list(self) -> List[Dict]:
        """Loads the list of user's channels from a file."""
        if not self.channel_info_path.exists():
            return []
        with open(self.channel_info_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def get_all_db_paths(self) -> List[str]:
        """Returns a list of all channel database file paths."""
        return [str(p) for p in self.channels_dir.glob('*.json')]

    def mark_link_downloaded(self, link: Dict, success: bool):
        """Marks a single link as downloaded in its source channel database."""
        if 'channel' not in link:
            return  # Cannot update if we don't know the source
        
        db_path = self.get_db_path(link['channel'])
        db = LinkDatabase(db_path)
        db.mark_downloaded(link['url'], success)


class LinkDatabase:
    """Manage collected links database for a single file."""
    
    def __init__(self, db_path: Optional[str]):
        self.db_path = db_path
        self.links = self.load()
        # Only create a lock if this is a file-based database
        self.lock = threading.Lock() if db_path else None
    
    def load(self) -> List[Dict]:
        """Load links from database"""
        if self.db_path and os.path.exists(self.db_path):
            with open(self.db_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return []
    
    def save(self):
        """Save links to database"""
        if not self.db_path or not self.lock:
            # This is an in-memory DB, cannot be saved.
            return

        with self.lock:
            # Ensure parent directory exists
            Path(self.db_path).parent.mkdir(exist_ok=True)
            with open(self.db_path, 'w', encoding='utf-8') as f:
                json.dump(self.links, f, indent=2, ensure_ascii=False)
    
    def add_links(self, new_links: List[Dict]):
        """Add new links to database (with deduplication)"""
        existing_urls = {link['url'] for link in self.links}
        added = 0
        
        for link in new_links:
            if link['url'] not in existing_urls:
                link['status'] = 'pending'
                link['collected_at'] = datetime.now().isoformat()
                self.links.append(link)
                existing_urls.add(link['url'])
                added += 1
        
        if added > 0:
            self.save()

        print(f"\n✓ Added {added} new links to channel database.")
        print(f"  Total links for this channel: {len(self.links)}")
    
    def get_pending_links(self, link_type: Optional[str] = None) -> List[Dict]:
        """Get links that haven't been downloaded or have failed."""
        pending = [
            link for link in self.links 
            if link.get('status', 'pending') in ['pending', 'failed']
        ]
        
        if link_type:
            pending = [link for link in pending if link['type'] == link_type]
        
        return pending
    
    def mark_downloaded(self, url: str, success: bool = True):
        """Mark a link as downloaded (thread-safe)"""
        if not self.db_path or not self.lock:
            # In-memory, just update the list
            for link in self.links:
                if link['url'] == url:
                    link['status'] = 'completed' if success else 'failed'
                    link['downloaded_at'] = datetime.now().isoformat()
                    break
            return

        # For file-based DB, it's safer to load, update, save.
        with self.lock:
            # Re-load to ensure we have the latest data before writing
            links = self.load()
            for link in links:
                if link['url'] == url:
                    link['status'] = 'completed' if success else 'failed'
                    link['downloaded_at'] = datetime.now().isoformat()
                    break
            
            with open(self.db_path, 'w', encoding='utf-8') as f:
                json.dump(links, f, indent=2, ensure_ascii=False)

    def list_links(self, filter_text: Optional[str] = None):
        """List all links with optional filter"""
        links = self.links
        
        if filter_text:
            links = [l for l in links if filter_text.lower() in l['caption'].lower()]
        
        if not links:
            print("No links found")
            return
        
        print(f"\nTotal links: {len(links)}\n")
        for i, link in enumerate(links, 1):
            status_emoji = {'pending': '⏳', 'completed': '✓', 'failed': '✗'}.get(link.get('status', 'pending'), '?')
            print(f"{i}. [{status_emoji}] {link['type'].upper()}: {link['url']}")
            print(f"   Caption: {link['caption'][:80]}...")
            print(f"   Date: {link['date']} | Status: {link.get('status', 'pending')}\n")


class ProgressHook:
    """Custom progress hook for yt-dlp with tqdm"""
    
    def __init__(self, pbar: Optional[tqdm] = None):
        self.pbar = pbar
        self.last_downloaded = 0
    
    def __call__(self, d):
        if not self.pbar:
            return
        
        if d['status'] == 'downloading':
            # Extract downloaded bytes
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            
            # Update progress bar
            if total > 0:
                self.pbar.total = total
                self.pbar.n = downloaded
                self.pbar.refresh()
        
        elif d['status'] == 'finished':
            if self.pbar and self.pbar.total:
                self.pbar.n = self.pbar.total
                self.pbar.refresh()


class Downloader:
    """Download content from collected links with parallel support"""
    
    def __init__(self, output_dir='downloads', workers=4):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        
        self.youtube_dir = self.output_dir / 'youtube'
        self.drive_dir = self.output_dir / 'drive'
        self.youtube_dir.mkdir(exist_ok=True)
        self.drive_dir.mkdir(exist_ok=True)
        
        self.workers = workers
        self.download_lock = threading.Lock()  # Rate limiting
    
    def sanitize_filename(self, text: str, max_length: int = 100) -> str:
        """Create a safe filename from text"""
        # Remove special characters
        safe = re.sub(r'[^\w\s-]', '', text)
        safe = re.sub(r'[-\s]+', '_', safe)
        return safe[:max_length]
    
    def download_youtube(self, url: str, caption: str, pbar: Optional[tqdm] = None) -> tuple[bool, str]:
        """Download YouTube video"""
        try:
            safe_name = self.sanitize_filename(caption) if caption else 'video'
            
            # Custom progress hook
            progress_hook = ProgressHook(pbar) if pbar else None
            
            ydl_opts = {
                'format': 'best[height<=1080]',
                'outtmpl': str(self.youtube_dir / f'{safe_name}_%(id)s.%(ext)s'),
                'quiet': True,  # Suppress yt-dlp output
                'no_warnings': True,
                'progress_hooks': [progress_hook] if progress_hook else [],
            }
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            
            return True, "Success"
        
        except Exception as e:
            error_msg = str(e)
            # Shorten error message
            if len(error_msg) > 100:
                error_msg = error_msg[:100] + "..."
            return False, error_msg
    
    def download_drive(self, url: str, caption: str, pbar: Optional[tqdm] = None) -> tuple[bool, str]:
        """Download Google Drive file"""
        try:
            # Extract file ID from various Drive URL formats
            file_id = None
            
            if '/file/d/' in url:
                file_id = url.split('/file/d/')[1].split('/')[0]
            elif '/folders/' in url:
                file_id = url.split('/folders/')[1].split('/')[0]
            elif 'id=' in url:
                file_id = parse_qs(urlparse(url).query).get('id', [None])[0]
            
            if not file_id:
                return False, "Could not extract file ID"
            
            safe_name = self.sanitize_filename(caption) if caption else f'drive_{file_id}'
            output_path = str(self.drive_dir / safe_name)
            
            # gdown doesn't have great progress support, but we can update pbar
            if pbar:
                pbar.set_description(f"⬇️  Downloading Drive file")
            
            gdown.download(id=file_id, output=output_path, quiet=True, fuzzy=True)
            
            return True, "Success"
        
        except Exception as e:
            error_msg = str(e)
            if len(error_msg) > 100:
                error_msg = error_msg[:100] + "..."
            return False, error_msg
    
    def download_single_link(self, link: Dict, position: int = 0, total: int = 0) -> tuple[Dict, bool, str]:
        """Download a single link with progress tracking"""
        url = link['url']
        link_type = link['type']
        caption = link.get('caption', '')
        
        # Create a progress bar for this specific download if tqdm is available
        desc = f"[{position}/{total}] {link_type.upper()}"
        
        if tqdm:
            with tqdm(total=100, desc=desc, position=position, leave=False, 
                     bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt}', dynamic_ncols=True) as pbar:
                
                # Small delay to prevent rate limiting
                with self.download_lock:
                    time.sleep(0.1)  # 100ms delay between starting downloads
                
                if link_type == 'youtube':
                    success, msg = self.download_youtube(url, caption, pbar)
                elif link_type == 'drive':
                    success, msg = self.download_drive(url, caption, pbar)
                else:
                    success, msg = False, "Unsupported type"
                
                # Complete the progress bar
                if success:
                    pbar.n = pbar.total
                    pbar.set_description(f"✓ {desc}")
                else:
                    pbar.set_description(f"✗ {desc} - {msg[:30]}")
                pbar.refresh()
        else:
            # Fallback without tqdm
            with self.download_lock:
                time.sleep(0.1)
            
            if link_type == 'youtube':
                success, msg = self.download_youtube(url, caption)
            elif link_type == 'drive':
                success, msg = self.download_drive(url, caption)
            else:
                success, msg = False, "Unsupported type"
        
        return link, success, msg
    
    def download_batch(self, links: List[Dict], data_manager: DataManager):
        """Download multiple links in parallel"""
        total = len(links)
        
        if total == 0:
            print("No links to download")
            return
        
        print(f"\n{'='*70}")
        print(f"🚀 Starting parallel download: {total} links with {self.workers} workers")
        print(f"{'='*70}\n")
        
        results = {
            'success': 0,
            'failed': 0,
            'errors': []
        }
        
        start_time = time.time()
        
        # Use ThreadPoolExecutor for parallel downloads
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            # Submit all download tasks
            future_to_link = {
                executor.submit(self.download_single_link, link, i+1, total): link 
                for i, link in enumerate(links)
            }
            
            # Create main progress bar if tqdm available
            if tqdm:
                main_pbar = tqdm(total=total, desc="Overall Progress", position=self.workers+1,
                               bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]')
            
            # Process completed downloads
            for future in as_completed(future_to_link):
                link, success, msg = future.result()
                
                # Update database
                data_manager.mark_link_downloaded(link, success)
                
                # Update statistics
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
                    results['errors'].append({
                        'url': link['url'],
                        'error': msg
                    })
                
                # Update main progress bar
                if tqdm:
                    main_pbar.update(1)
                    main_pbar.set_postfix({
                        'Success': results['success'],
                        'Failed': results['failed']
                    })
            
            if tqdm:
                main_pbar.close()
        
        # Calculate statistics
        elapsed = time.time() - start_time
        avg_time = elapsed / total if total > 0 else 0
        
        # Print summary
        print(f"\n{'='*70}")
        print(f"📊 DOWNLOAD SUMMARY")
        print(f"{'='*70}")
        print(f"  Total Links:     {total}")
        print(f"  ✓ Success:       {results['success']} ({results['success']/total*100:.1f}%)")
        print(f"  ✗ Failed:        {results['failed']} ({results['failed']/total*100:.1f}%)")
        print(f"  ⏱️  Total Time:    {elapsed:.1f}s")
        print(f"  📈 Avg per file:  {avg_time:.1f}s")
        print(f"  ⚡ Workers used:  {self.workers}")
        print(f"{'='*70}")
        
        # Show errors if any
        if results['errors'] and len(results['errors']) <= 10:
            print(f"\n❌ Failed Downloads:")
            for err in results['errors'][:10]:
                print(f"  • {err['url'][:50]}... - {err['error'][:50]}")
            if len(results['errors']) > 10:
                print(f"  ... and {len(results['errors']) - 10} more")
        
        print()


async def main():
    """Main CLI application"""
    parser = argparse.ArgumentParser(
        description='Telegram Link Harvester - Scan and download content from Telegram channels',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py scan @channel --limit 100
  python main.py list --filter "tutorial"
  python main.py download --all --workers 5
  python main.py download --type youtube --workers 3
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Scan command
    scan_parser = subparsers.add_parser('scan', help='Scan a channel for links')
    scan_parser.add_argument('channel', help='Channel username or ID (e.g., @channelname)')
    scan_parser.add_argument('--limit', type=int, help='Limit number of messages to scan')
    
    # List command
    list_parser = subparsers.add_parser('list', help='List collected links')
    list_parser.add_argument('--filter', help='Filter links by caption text')
    
    # Download command
    download_parser = subparsers.add_parser('download', help='Download collected links')
    download_parser.add_argument('--all', action='store_true', help='Download all pending links')
    download_parser.add_argument('--type', choices=['youtube', 'drive'], help='Download only specific type')
    download_parser.add_argument('--filter', help='Download links matching filter text')
    download_parser.add_argument('--workers', type=int, default=4, 
                                help='Number of parallel downloads (default: 4, recommended: 3-5)')
    
    # Status command
    subparsers.add_parser('status', help='Show statistics')
    
    # List channels command
    subparsers.add_parser('list-channels', help='List all your channels/groups with IDs')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # Initialize data manager
    data_manager = DataManager()
    
    # Handle commands
    if args.command == 'scan':
        config = Config()
        scanner = TelegramScanner(config)
        
        try:
            # Try to convert channel to int for IDs
            channel_entity = int(args.channel)
        except ValueError:
            # It's a username
            channel_entity = args.channel

        try:
            links = await scanner.scan_channel(channel_entity, args.limit)
            db_path = data_manager.get_db_path(channel_entity)
            db = LinkDatabase(db_path)
            db.add_links(links)
        finally:
            await scanner.close()
    
    elif args.command == 'list':
        all_links = []
        for db_path in data_manager.get_all_db_paths():
            db = LinkDatabase(db_path)
            all_links.extend(db.links)
        
        agg_db = LinkDatabase(None)
        agg_db.links = all_links
        agg_db.list_links(args.filter)
    
    elif args.command == 'download':
        # Validate worker count
        workers = args.workers
        if workers < 1:
            print("Error: Workers must be at least 1")
            return
        if workers > 10:
            print("⚠️  Warning: More than 10 workers may cause rate limiting!")
            print("   Recommended: 3-5 workers for best results")
            response = input("Continue anyway? (y/n): ")
            if response.lower() != 'y':
                return
        
        downloader = Downloader(workers=workers)
        
        # Aggregate links from all databases
        all_links = []
        for db_path in data_manager.get_all_db_paths():
            db = LinkDatabase(db_path)
            all_links.extend(db.links)
        
        agg_db = LinkDatabase(None)
        agg_db.links = all_links

        if args.all:
            pending = agg_db.get_pending_links(args.type)
        elif args.filter:
            all_pending = agg_db.get_pending_links(args.type)
            pending = [l for l in all_pending if args.filter.lower() in l['caption'].lower()]
        else:
            print("Error: Specify --all or --filter")
            return
        
        if not pending:
            print("No pending links to download")
            return
        
        downloader.download_batch(pending, data_manager)
    
    elif args.command == 'status':
        all_links = []
        for db_path in data_manager.get_all_db_paths():
            db = LinkDatabase(db_path)
            all_links.extend(db.links)

        total = len(all_links)
        pending = len([l for l in all_links if l.get('status', 'pending') in ['pending', 'failed']])
        completed = len([l for l in all_links if l.get('status') == 'completed'])
        
        youtube = len([l for l in all_links if l['type'] == 'youtube'])
        drive = len([l for l in all_links if l['type'] == 'drive'])
        
        print("\n" + "="*50)
        print("TELEGRAM LINK HARVESTER - STATUS")
        print("="*50)
        print(f"Total Links (all channels): {total}")
        print(f"  ⏳ Pending/Failed: {pending}")
        print(f"  ✓ Completed: {completed}")
        print("\nBy Type:")
        print(f"  YouTube: {youtube}")
        print(f"  Google Drive: {drive}")
        print("="*50 + "\n")
    
    elif args.command == 'list-channels':
        config = Config()
        scanner = TelegramScanner(config)
        
        try:
            await scanner.connect()
            print("\n" + "="*70)
            print("YOUR TELEGRAM CHANNELS & GROUPS")
            print("="*70 + "\n")
            
            channel_list_data = []
            async for dialog in scanner.client.iter_dialogs():
                if dialog.is_channel or dialog.is_group:
                    channel_type = "📢 Channel" if dialog.is_channel else "👥 Group"
                    username = f"@{dialog.entity.username}" if dialog.entity.username else "(no username)"
                    
                    print(f"{channel_type}: {dialog.name}")
                    print(f"  ID: {dialog.id}")
                    print(f"  Username: {username}")
                                        
                    usage_id = f"@{dialog.entity.username}" if dialog.entity.username else dialog.id
                    print(f"  Use: python main.py scan {usage_id}\n")

                    channel_list_data.append({
                        'name': dialog.name,
                        'id': dialog.id,
                        'username': dialog.entity.username,
                        'type': 'channel' if dialog.is_channel else 'group'
                    })
            
            data_manager.save_channel_list(channel_list_data)

            print("="*70)
            print("💡 Tip: Copy the ID number and use it with 'scan' command")
            print("="*70 + "\n")
        
        finally:
            await scanner.close()


if __name__ == '__main__':
    asyncio.run(main())
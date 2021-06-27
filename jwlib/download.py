import hashlib
import os
import shutil
import time
import urllib.parse
import urllib.request
from sys import stderr
from typing import List

from .common import Path, Settings, msg
from .parse import Media, get_languages, url_basename


class MissingTimestampError(Exception):
    pass


class DiskLimitReached(Exception):
    pass


def download_all(s: Settings, media_list: List[Media]):
    """Download/check media files"""

    if s.quiet < 1:
        msg('scanning local files')

    # Check existing local media before initiating the download (to get correct progress info)
    download_list = [media for media in media_list if not check_media(s, media)]

    for num, media in enumerate(download_list):
        # Make room
        if s.keep_free > 0:
            try:
                disk_cleanup(s, media)
            except MissingTimestampError:
                if s.quiet < 2:
                    msg('low disk space and missing metadata, skipping: {}'.format(media.name))
                continue
            except DiskLimitReached:
                return

        # Download the video
        if s.quiet < 2:
            print('[{}/{}]'.format(num + 1, len(download_list)), end=' ', file=stderr)
        download_media(s, media)


def download_all_subtitles(s: Settings, media_list: List[Media]):
    """Download all VTT files giving them the correct name and language code"""

    queue = []
    for media in media_list:
        mediafile = media.find_file(s.media_dir)
        for lang, url in media.subtitles.items():
            if lang in s.download_subtitles or (lang == s.lang and True in s.download_subtitles):
                # ISO 639 language code (some language codes have extra suffix, we remove that)
                isolang = get_languages()[lang][1].split('_')[0]
                name = mediafile.stem + '.' + isolang + os.path.splitext(url_basename(url))[1]
                subfile = mediafile.with_name(name)
                # --fix-broken will re-download subtitle files
                if s.overwrite_bad or not subfile.exists():
                    queue.append((url, subfile, media.name))

    for i, queue_entry in enumerate(queue):
        url, file, name = queue_entry
        if s.quiet < 2:
            msg('[{}/{}] downloading: {} ({})'.format(i + 1, len(queue), url_basename(url), name))
        download_file(url, file)


def check_media(s: Settings, media: Media):
    """Download media file and check it.

    Download file, check MD5 sum and size, delete file if it missmatches.

    :param s: Global settings
    :param media: a Media instance
    :return: True if check is successful
    """
    file = media.find_file(s.media_dir)
    if not file.exists():
        return False

    # If we are going to fix bad files, check the existing ones
    if s.overwrite_bad:

        if media.size and file.size != media.size:
            if s.quiet < 2:
                msg('size mismatch: {}'.format(file))
            return False

        if s.checksums and media.md5 and _md5(file) != media.md5:
            if s.quiet < 2:
                msg('checksum mismatch: {}'.format(file))
            return False

    return True


def download_media(s: Settings, media: Media):
    """Download media file and check it.

    :param s: Global settings
    :param media: a Media instance
    :return: True if download was successful
    """
    file = media.find_file(s.media_dir)
    tmpfile = file.with_name(file.name + '.part')

    # Check for partially downloaded files
    if tmpfile.exists():

        # If file is smaller, resume download
        if media.size and tmpfile.size < media.size:
            if s.quiet < 2:
                msg('resuming: {} ({})'.format(media.filename, media.name))
            download_file(media.url, tmpfile, resume=True, rate_limit=s.rate_limit, progress=s.quiet < 1)

        # Always validate size and MD5 on resumed downloads
        if media.size and tmpfile.size != media.size:
            if s.quiet < 2:
                msg('size mismatch, deleting: {}'.format(tmpfile))
            # Always remove resumed files that have wrong size
            tmpfile.unlink()
        elif media.md5 and _md5(tmpfile) != media.md5:
            if s.quiet < 2:
                msg('checksum mismatch, deleting: {}'.format(tmpfile))
            # Always remove resumed files that are broken
            tmpfile.unlink()

        # Set timestamp to date of publishing, move and return success
        else:
            if media.date:
                tmpfile.set_mtime(media.date)
            tmpfile.rename(file)
            return True

    # Continuing to regular download
    if s.quiet < 2:
        msg('downloading: {} ({})'.format(media.filename, media.name))
    download_file(media.url, tmpfile, rate_limit=s.rate_limit, progress=s.quiet < 1)

    # Check exist and non-empty
    try:
        if tmpfile.size == 0:
            tmpfile.unlink()
            raise FileNotFoundError
    except FileNotFoundError:
        if s.quiet < 2:
            msg('download failed: {}'.format(media.filename))
        return False

    # Set timestamp to date of publishing, move and approve
    if media.date:
        tmpfile.set_mtime(media.date)
    tmpfile.rename(file)

    # Check size (log only)
    if media.size and file.size != media.size:
        if s.quiet < 2:
            msg('size mismatch: {}'.format(file))
        return False
    # Check MD5 if size was correct (optional, log only)
    elif s.checksums and media.md5 and _md5(file) != media.md5:
        if s.quiet < 2:
            msg('checksum mismatch: {}'.format(file))

    return True


def disk_usage_info(s: Settings):
    """Display information about disk usage and maybe a warning"""

    free = shutil.disk_usage(str(s.work_dir)).free

    if s.quiet < 1:
        msg('note: old MP4 files in target directory will be deleted if space runs low')
        msg('free space: {:} MiB, minimum limit: {:} MiB'.format(free // 1024 ** 2, s.keep_free // 1024 ** 2))

    if s.warning and free < s.keep_free:
        message = '\nWarning:\n' \
                  'The disk usage currently exceeds the limit by {} MiB.\n' \
                  'If the limit was set too high by mistake, many or ALL \n' \
                  'currently downloaded videos may get deleted.\n'
        msg(message.format((s.keep_free - free) // 1024 ** 2))
        try:
            if not input('Do you want to proceed anyway? [y/N]: ') in ('y', 'Y'):
                exit(1)
        except EOFError:
            exit(1)


def _md5(file: Path):
    """Return MD5 of a file."""

    hash_md5 = hashlib.md5()
    with file.open('rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def download_file(url: str, file: Path, resume=False, rate_limit=0.0, progress=False):
    """Throttled download with progress bar

    :param url: URL to download
    :param file: Output file
    :param resume: Append instead of overwrite
    :param rate_limit: Rate limit in MB/s
    :param progress: Show progress bar
    """

    if resume and file.exists():
        file_mode = 'ab'
        done_bytes = file.size
    else:
        file_mode = 'wb'
        done_bytes = 0

    # To be downloaded each second
    if rate_limit != 0:
        chunk_size = int(rate_limit * 1024 * 1024)
    else:
        # Default chunk size of 1 MB means we do not loose whole file if download gets aborted
        chunk_size = 1024 * 1024

    # Ask server to skip the first N bytes
    request = urllib.request.Request(url)
    request.add_header('Range', 'bytes={}-'.format(done_bytes))

    with urllib.request.urlopen(request) as response:
        if progress:
            # Get size of download
            total_bytes = int(response.headers['content-length']) + done_bytes
            # Avoid ZeroDivisionError and only print progress bar if we are in a terminal
            if total_bytes == 0 or not stderr.isatty():
                progress = False

        with file.open(file_mode) as f:
            while True:
                # Print a progress bar
                if progress:
                    percent = 100 * (done_bytes / total_bytes)
                    # Never more than 70 hash signs
                    bar = '#' * min(70 * done_bytes // total_bytes, 70)
                    ####----- (padded to 70 chars) NNN.N (padded to 5 chars) %
                    print('\r{:-<70} {: >5.1f}%'.format(bar, percent), end='', flush=True, file=stderr)

                # Download and write a chunk
                started = time.time()
                chunk = response.read(chunk_size)
                done_bytes += len(chunk)
                if not chunk:
                    if progress:
                        print()  # newline when done
                    break
                f.write(chunk)

                if rate_limit:
                    try:
                        # Every chunk should take 1 second, sleep for the time that's left
                        time.sleep(1 + started - time.time())
                    except ValueError:
                        pass


def disk_cleanup(s: Settings, reference_media: Media):
    """Clean up old videos until there is enough space"""
    assert s.keep_free
    assert reference_media.size

    while True:
        space = shutil.disk_usage(str(s.media_dir)).free
        needed = reference_media.size + s.keep_free
        if space > needed:
            break
        if s.quiet < 1:
            msg('free space: {:} MiB, needed: {:} MiB'.format(space // 1024 ** 2, needed // 1024 ** 2))

        # We dare not delete files if we don't know if they are older or newer than this one
        if not reference_media.date:
            raise MissingTimestampError

        try:
            # Get the oldest .mp4 file in the working directory
            oldest = min((f for f in s.media_dir.iterdir() if f.is_mp4()), key=lambda f: f.mtime)
        except ValueError:
            msg('cannot free more disk space, no videos in {}'.format(s.media_dir))
            exit(1)
            raise

        # If the reference date is older than the oldest file, exit the program.
        if reference_media.date <= oldest.mtime:
            if s.quiet < 1:
                msg('disk limit reached, all videos up to date')
            raise DiskLimitReached

        # Delete the file and add a "deleted" marker
        if s.quiet < 2:
            msg('removing old video: {}'.format(oldest))
        oldest.unlink()


def copy_files(s: Settings):
    """jwb-index --import

    Fancy copy of files from one directory to another
    """

    # Create a list of all mp4 files to be copied
    source_files = []
    for source in s.import_dir.iterdir():
        try:
            # Just a simple size check, no checksum etc
            if source.is_mp4() and source.size != (s.media_dir / source.name).size:
                source_files.append(source)
        except OSError:
            pass

    # Newest file first
    source_files.sort(key=lambda x: x.date, reverse=True)

    total = len(source_files)
    for source_file in source_files:
        if s.keep_free > 0:
            disk_cleanup(s, reference_media=source_file)

        if s.quiet < 1:
            i = source_files.index(source_file)
            msg('copying [{}/{}]: {}'.format(i + 1, total, source_file.name))

        shutil.copy2(source_file.path, s.media_dir / source_file.name)

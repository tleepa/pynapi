#!/usr/bin/env python
# -*- coding: UTF-8 -*-
#
# Copyright (C) 2009-2015 Arkadiusz Miśkiewicz <arekm@pld-linux.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# napiprojekt.pl API is used with napiproject administration consent
# (given by Marek <kontakt@napiprojekt.pl> at Wed, 24 Feb 2010 14:43:00 +0100)
#
# napisy24.pl API access granted by napisy24 admins at 15 Feb 2015
#
#
# Copyright (C) 2022 TLeepa <tleepa@gmail.com>
#


from hashlib import md5
from urllib.parse import urlencode

import aiofiles
import asyncio
import argparse
import base64
import io
import os
import requests
import struct
import sys
import xml.etree.ElementTree as etree
import zipfile


prog = os.path.basename(sys.argv[0])
languages = {"pl": "PL", "en": "ENG"}
video_files = [
    ".asf",
    ".avi",
    ".divx",
    ".m2ts",
    ".mkv",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogm",
    ".rm",
    ".rmvb",
    ".wmv",
]


async def calculate_digest(filename: str) -> str:
    d = md5()
    try:
        async with aiofiles.open(filename, mode="rb") as f:
            d.update(await f.read(10485760))
    except (IOError, OSError) as e:
        raise Exception(f"Hashing video file failed: {e}")
    return d.hexdigest()


async def napisy24_hash(filename: str) -> str:
    try:
        longlongformat = "<q"  # little-endian long long
        bytesize = struct.calcsize(longlongformat)

        async with aiofiles.open(filename, mode="rb") as f:
            filesize = os.path.getsize(filename)
            hash = filesize

            if filesize < 65536 * 2:
                raise Exception(
                    f"Hashing (napisy24) video file failed: '{filename}': File too small"
                )

            for _ in range(int(65536 / bytesize)):
                buffer = await f.read(bytesize)
                (l_value,) = struct.unpack(longlongformat, buffer)
                hash += l_value
                hash = hash & 0xFFFFFFFFFFFFFFFF  # to remain as 64bit number

            await f.seek(max(0, filesize - 65536), 0)
            for _ in range(int(65536 / bytesize)):
                buffer = await f.read(bytesize)
                (l_value,) = struct.unpack(longlongformat, buffer)
                hash += l_value
                hash = hash & 0xFFFFFFFFFFFFFFFF

        returnedhash = "%016x" % hash
        return returnedhash

    except IOError as e:
        raise Exception(f"Hashing (napisy24) video file failed: {e}")


async def get_subtitle_napisy24(
    filename: str, digest: str = None, lang: str = "pl"
) -> bytes:
    url = "http://napisy24.pl/run/CheckSubAgent.php"
    headers = {"Content-type": "application/x-www-form-urlencoded"}

    pdata = []
    pdata.append(("postAction", "CheckSub"))
    pdata.append(("ua", "pynapi"))
    pdata.append(("ap", "XaA!29OkF5Pe"))
    pdata.append(("nl", lang))
    pdata.append(("fn", filename))
    pdata.append(("fh", await napisy24_hash(filename)))
    pdata.append(("fs", os.path.getsize(filename)))
    if digest:
        pdata.append(("md5", digest))

    repeat = 3
    error = "Fetching subtitle (napisy24) failed:"
    while repeat > 0:
        repeat = repeat - 1
        try:
            r = requests.post(url, headers=headers, data=urlencode(pdata))
            subdata = r.content
        except (IOError, OSError) as e:
            error = f"{error} {e}"
            await asyncio.sleep(0.5)
            continue

        if not r.ok:
            error = f"{error}, HTTP code: {str(r.status_code)}"
            await asyncio.sleep(0.5)
            continue

        if subdata.startswith(b"OK-2|"):
            pos = subdata.find(b"||")
            if pos >= 2 and len(subdata) > (pos + 2):
                subdata = subdata[pos + 2 :]

                try:
                    subzip = zipfile.ZipFile(io.BytesIO(subdata))
                    sub = subzip.read(subzip.namelist()[0])
                except Exception as e:
                    raise Exception(f"Subtitle NOT FOUND {e}")
            else:
                raise Exception("Subtitle NOT FOUND (subtitle too short)")
        elif subdata.startswith(b"OK-"):
            raise Exception("Subtitle NOT FOUND")
        else:
            raise Exception("Subtitle NOT FOUND (unknown error)")

        repeat = 0

    if sub is None or sub == "":
        raise Exception(error)

    return sub


async def get_subtitle_napiprojekt(digest: str, lang: str = "PL") -> bytes:
    url = "http://napiprojekt.pl/api/api-napiprojekt3.php"
    headers = {"Content-type": "application/x-www-form-urlencoded"}

    data = {
        "downloaded_subtitles_id": digest,
        "mode": "1",
        "client": "pynapi",
        "client_ver": "0",
        "downloaded_subtitles_lang": lang,
        "downloaded_subtitles_txt": "1",
    }

    repeat = 3
    sub = None
    error = "Fetching subtitle (napiprojekt) failed:"
    while repeat > 0:
        repeat = repeat - 1
        try:
            r = requests.post(
                url,
                headers=headers,
                data=urlencode(data),
            )
            subdata = r.content.decode()
        except (IOError, OSError) as e:
            error = f"{error} {e}"
            await asyncio.sleep(0.5)
            continue

        if not r.ok:
            error = f"{error}, HTTP code: {str(r.status_code)}"
            await asyncio.sleep(0.5)
            continue

        try:
            root = etree.fromstring(subdata)
            status = root.find("status")
            if status is not None and status.text == "success":
                content = root.find("subtitles/content")
                sub = base64.b64decode(content.text)
                break
            else:
                raise Exception("Subtitle NOT FOUND")
        except Exception as e:
            error = f"{error} XML parsing: {e}"
            await asyncio.sleep(0.5)
            continue

    if sub is None or sub == "":
        raise Exception(error)

    return sub


async def process_file(
    index: int, total_files: int, file: str, args: argparse.Namespace
) -> None:
    digest = None

    if file.startswith("napiprojekt:"):
        digest = file.split(":")[-1]
        vfile = digest + ".txt"
    else:
        vfile = file + ".txt"
        basefile = file
        if len(file) > 4:
            basefile = file[:-4]
            vfile = basefile + ".txt"

    if args.dest:
        vfile = os.path.join(args.dest, os.path.split(vfile)[1])

    if not args.update and os.path.exists(vfile):
        print(
            f"{prog}: {index}/{total_files}: Skipping because update flag not set and '{vfile}' already exists"
        )
        return

    if not args.nobackup and os.path.exists(vfile):
        vfile_bak = vfile + "-bak"
        try:
            os.rename(vfile, vfile_bak)
        except (IOError, OSError) as e:
            print(
                f"{prog}: {index}/{total_files}: Skipping due to backup of '{vfile}' as '{vfile_bak}' failure: {e}"
            )
            return
        else:
            print(
                f"{prog}: {index}/{total_files}: Old subtitle backed up as '{vfile_bak}'"
            )

    print(f"{prog}: {index}/{total_files}: Processing subtitle for {file}")

    try:
        if not digest:
            digest = await calculate_digest(file)
    except:
        print(f"{prog}: {sys.exc_info()[1]}")
        return

    try:
        sub = await get_subtitle_napiprojekt(digest, languages[args.lang])
    except:
        try:
            sub = await get_subtitle_napisy24(file, digest, args.lang)
        except:
            print(f"{prog}: {index}/{total_files}: {sys.exc_info()[1]}")
            return

    async with aiofiles.open(vfile, "wb") as fp:
        await fp.write(sub)

    print(f"{prog}: {index}/{total_files}: SUBTITLE STORED ({len(sub)} bytes)")


async def main(args: argparse.Namespace) -> None:

    print(f"{prog}: Subtitles language '{args.lang}'. Looking for video files...")

    files = []
    for f in args.file:
        if os.path.isdir(f):
            for dirpath, _, filenames in os.walk(f, topdown=False):
                for file in filenames:
                    if os.path.splitext(file)[1] in video_files:
                        files.append(os.path.join(dirpath, file))
        elif os.path.splitext(f)[1] in video_files or f.startswith("napiprojekt:"):
            files.append(f)

    files.sort()

    cors = []
    for index, file in enumerate(files, start=1):
        cors.append(process_file(index, len(files), file, args))

    if cors:
        await asyncio.gather(*cors)
    else:
        print(f"{prog}: No video files found...")


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(
            prog=prog,
            description="Find video files and download matching subtitles from napiprojekt/napisy24 server.",
        )
        parser.add_argument(
            "file", help="file or directory", nargs="*", metavar="FILE/DIR"
        )
        parser.add_argument(
            "-l",
            "--lang",
            choices=list(languages.keys()),
            help="subtitles language",
            default="pl",
        )
        parser.add_argument(
            "-n",
            "--nobackup",
            action="store_true",
            help="make no subtitle backup when in update mode",
        )
        parser.add_argument(
            "-u",
            "--update",
            action="store_true",
            help="fetch new and also update existing subtitles",
        )
        parser.add_argument("-d", "--dest", help="destination directory")

        args = parser.parse_args()
        if not args.file:
            parser.print_help()
        else:
            asyncio.run(main(args))

    except SystemExit:
        pass
    except Exception as e:
        print(f"Error: {e}\n")

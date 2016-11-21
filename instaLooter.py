# coding: utf-8
import argparse
import copy
import datetime
import gzip
import json
import os
import progressbar
import re
import six
import sys
import threading
import time

from contextlib import closing
from bs4 import BeautifulSoup

try:
    import PIL.Image
    import piexif
except ImportError:
    PIL = None



class InstaDownloader(threading.Thread):

    def __init__(self, owner):
        super(InstaDownloader, self).__init__()
        self.medias = owner._medias_queue
        self.directory = owner.directory
        self.use_metadata = owner.use_metadata
        self.owner = owner

        self._killed = False

    def run(self):

        while not self._killed:
            media = self.medias.get()
            if media is None:
                break
            if not media['is_video']:
                self._download_photo(media)
            else:
                self._download_video(media)

            self.owner.dl_count += 1

    def _add_metadata(self, path, metadata):
        """
        Tag downloaded photos with metadata from associated Instagram post.

        If GExiv2 is not installed, do nothing.
        """

        if PIL is not None:

            img = PIL.Image.open(path)

            exif_dict = {"0th":{}, "Exif":{}, "GPS":{}, "1st":{}, "thumbnail":None}

            exif_dict['0th'] = {
                piexif.ImageIFD.Artist: "Image creator, {}".format(self.owner.metadata['full_name']),
            }

            exif_dict['1st'] = {
                piexif.ImageIFD.Artist: "Image creator, {}".format(self.owner.metadata['full_name']),
            }

            exif_dict['Exif'] = {
                piexif.ExifIFD.DateTimeOriginal: datetime.datetime.fromtimestamp(metadata['date']).isoformat(),
                #piexif.ExifIFD.UserComment: metadata.get('caption', ''),
            }

            img.save(path, exif=piexif.dump(exif_dict))


    def _download_photo(self, media):

        photo_url = media.get('display_src')
        photo_basename = os.path.basename(photo_url.split('?')[0])
        photo_name = os.path.join(self.directory, photo_basename)

        # save full-resolution photo
        self._dl(photo_url, photo_name)

        # put info from Instagram post into image metadata
        if self.use_metadata:
            self._add_metadata(photo_name, media)

    def _download_video(self, media):
        """
        Given source code for loaded Instagram page:
        - discover all video wrapper links
        - activate all links to load video url
        - extract and download video url
        """

        url = "https://www.instagram.com/p/{}/".format(media['code'])
        req = six.moves.urllib.request.Request(url, headers=self.owner._headers)
        con = six.moves.urllib.request.urlopen(req)

        if con.headers.get('Content-Encoding', '') == 'gzip':
            con = gzip.GzipFile(fileobj=con)

        data = self.owner._get_shared_data(con)

        video_url = data["entry_data"]["PostPage"][0]["media"]["video_url"]
        video_basename = os.path.basename(video_url.split('?')[0])
        video_name = os.path.join(self.directory, video_basename)

        # save full-resolution photo
        self._dl(video_url, video_name)

        # put info from Instagram post into image metadata
        # if self.use_metadata:
        #     self._add_metadata(video_name, data["entry_data"]["PostPage"][0]["media"])

    @staticmethod
    def _dl(source, dest):
        with closing(six.moves.urllib.request.urlopen(source)) as source_con:
            with open(dest, 'wb') as dest_file:
                dest_file.write(source_con.read())

    def kill(self):
        self._killed = True




class InstaLooter(object):

    _RX_SHARED_DATA = re.compile(r'window._sharedData = ({[^\n]*});')

    def __init__(self, name, directory, num_to_download=None, log_level='info', use_metadata=True, get_videos=True, jobs=16):
        self.name = name
        self.directory = directory
        self.use_metadata = use_metadata
        self.get_videos = get_videos
        self.num_to_download=num_to_download or float("inf")
        self.jobs = jobs

        self.dl_count = 0

        self.metadata = {}

        self._cookies = None
        self._pbar = None
        self._headers =  {
            'User-Agent':"Mozilla/5.0 (Windows NT 10.0; WOW64; rv:50.0) Gecko/20100101 Firefox/50.0",
            'Accept': 'text/html',
            'Accept-Encoding': 'gzip' if six.PY3 else 'identity',
            'Connection': 'keep-alive',
            'Host':'www.instagram.com',
            'DNT': '1',
            'Upgrade-Insecure-Requests': '1',
        }

    def __del__(self):
        for worker in self._workers:
            worker.kill()
        if hasattr(self, '_pbar'):
            self._pbar.finish()

    def _init_workers(self):
        self._shared_map = {}
        self._workers = []
        self._medias_queue = six.moves.queue.Queue()
        for _ in six.moves.range(self.jobs):
            worker = InstaDownloader(self)
            worker.start()
            self._workers.append(worker)

    def pages(self, pbar=False):

        url = "/{}/".format(self.name)
        with closing(six.moves.http_client.HTTPSConnection("www.instagram.com")) as con:
            while True:
                con.request("GET", url, headers=self._headers)
                res = con.getresponse()
                self._cookies = res.getheader('Set-Cookie')
                self._headers['Cookie'] = self._cookies

                if res.getheader('Content-Encoding', '') == 'gzip':
                    res = gzip.GzipFile(fileobj=res)
                data = self._get_shared_data(res)

                if self.num_to_download == float('inf'):
                    media_count = data['entry_data']['ProfilePage'][0]['user']['media']['count']
                else:
                    media_count = self.num_to_download

                if pbar:
                    if not 'max_id' in url: # First page: init pbar
                        self._init_pbar(1, media_count//12 + 1, 'Loading pages |')
                    else: # Other pages: update pbar
                        if self._pbar.value < self._pbar.max_value:
                            self._pbar.update(self._pbar.value+1)

                if not 'max_id' in url:
                    self._parse_metadata(data)

                yield data

                try:
                    max_id = data['entry_data']['ProfilePage'][0]['user']['media']['nodes'][-1]['id']
                    url = '/{}/?max_id={}'.format(self.name, max_id)
                except IndexError:
                    break

    def medias(self, pbar=False):
        for page in self.pages(pbar=pbar):
            for media in page['entry_data']['ProfilePage'][0]['user']['media']['nodes']:
                yield media

    def download_photos(self, pbar=False):
        self.download(pbar=pbar, condition=lambda media: not media['is_video'])

    def download_videos(self, pbar=False):
        self.download(pbar=pbar, condition=lambda media: media['is_video'])

    def download(self, pbar=False, condition=None):
        self._init_workers()
        if condition is None:
            condition = lambda media: (not media['is_video'] or self.get_videos)
        medias_queued = self._fill_media_queue(pbar=pbar, condition=condition)
        if pbar:
            self._init_pbar(self.dl_count, medias_queued, 'Downloading |')
        self._poison_workers()
        self._join_workers(pbar=pbar)

    @classmethod
    def _get_shared_data(cls, res):
        soup = BeautifulSoup(res.read().decode('utf-8'), 'lxml')
        script = soup.find('body').find('script', {'type':'text/javascript'})
        return json.loads(cls._RX_SHARED_DATA.match(script.text).group(1))

    def _fill_media_queue(self, pbar, condition):
        medias_queued = 0
        for media in self.medias(pbar=pbar):
            if condition(media):
                media_url = media.get('display_src')
                media_basename = os.path.basename(media_url.split('?')[0])
                if not os.path.exists(os.path.join(self.directory, media_basename)):
                    self._medias_queue.put(media)
                    medias_queued += 1
            if medias_queued >= self.num_to_download:
                break
        return medias_queued

    def _join_workers(self, pbar=False):
        while any(w.is_alive() for w in self._workers):
            if pbar:
                self._pbar.update(self.dl_count)
        self._pbar.update(self.dl_count)

    def _init_pbar(self, ini_val, max_val, label):
        self._pbar = progressbar.ProgressBar(
            min_value=0,
            max_value=max_val,
            initial_value=ini_val,
            widgets=[
                label,
                progressbar.Percentage(),
                '(', progressbar.SimpleProgress(), ')',
                progressbar.Bar(),
                progressbar.Timer(), ' ',
                '|', progressbar.ETA(),
            ]
        )
        self._pbar.start()

    def _poison_workers(self):
        for worker in self._workers:
            self._medias_queue.put(None)

    def _parse_metadata(self, data):
        user = data["entry_data"]["ProfilePage"][0]["user"]
        for k,v in six.iteritems(user):
            self.metadata[k] = copy.copy(v)
        self.metadata['follows'] = self.metadata['follows']['count']
        self.metadata['followed_by'] = self.metadata['followed_by']['count']
        del self.metadata['media']['nodes']


def main(args=sys.argv):
    # parse arguments
    parser = argparse.ArgumentParser(description='InstaLooter')
    parser.add_argument('username', help='Instagram username')
    parser.add_argument('directory', help='Where to save the images')
    parser.add_argument('-n', '--num-to-download',
                        help='Number of posts to download', type=int)
    parser.add_argument('-m', '--add_metadata',
                        help=("Add metadata (caption/date) from Instagram "
                              "post into downloaded images' exif tags "
                              "(requires GExiv2 python module)"),
                        action='store_true', dest='use_metadata')
    parser.add_argument('-v', '--get_videos',
                        help="Download videos",
                        action='store_true', dest='get_videos')
    parser.add_argument('-j', '--jobs',
                        help="Number of concurrent threads to use",
                        action='store', dest='jobs',
                        type=int, default=64)
    parser.add_argument('-q', '--quiet',
                        help="Do not display any output",
                        action='store_true')

    args = parser.parse_args()

    looter = InstaLooter(name=args.username,
                         directory=os.path.expanduser(args.directory),
                         num_to_download=args.num_to_download,
                         use_metadata=args.use_metadata,
                         get_videos=args.get_videos,
                         jobs=args.jobs)

    try:
        looter.download(pbar=not args.quiet)
    except KeyboardInterrupt:
        looter.__del__()

if __name__=="__main__":
    main(sys.argv)
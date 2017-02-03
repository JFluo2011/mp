import cgi
import time
import asyncio
import sqlite3
import logging
from asyncio import Queue
from datetime import datetime

import aiohttp
from elasticsearch_dsl.connections import connections

from models import User, Live, session
from client import ZhihuClient
from utils import flatten_live_dict
from config import SPEAKER_KEYS, LIVE_KEYS

LIVE_API_URL = 'https://api.zhihu.com/lives/{type}?purchasable=0&limit=10&offset={offset}'  # noqa
LIVE_TYPE = frozenset(['ongoing', 'ended'])
es = connections.get_connection(Live._doc_type.using)
index = Live._doc_type.index
used_words = set()

# logging.basicConfig(level=logging.DEBUG)

def analyze_tokens(text):
    if not text:
        return []
    global used_words
    result = es.indices.analyze(index=index, analyzer='ik_max_word',
                                params={'filter': ['lowercase']}, body=text)

    words = set([r['token'] for r in result['tokens'] if len(r['token']) > 1])

    new_words = words.difference(used_words)
    used_words.update(words)
    return new_words


def gen_suggests(topics, tags, outline, username, subject):
    global used_words
    used_words = set()
    suggests = []

    for item, weight in ((topics, 10), (subject, 5), (outline, 3),
                         (tags, 3), (username, 2)):
        item = analyze_tokens(item)
        if item:
            suggests.append({'input': list(item), 'weight': weight})
    return suggests


class Crawler:
    def __init__(self, max_redirect=10, max_tries=4,
                 max_tasks=10, *, loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self.max_redirect = max_redirect
        self.max_tries = max_tries
        self.max_tasks = max_tasks
        self.q = Queue(loop=self.loop)
        self.seen_urls = set()
        for t in LIVE_TYPE:
            for offset in range(max_tasks):
                self.add_url(LIVE_API_URL.format(type=t, offset=offset * 10))
        self.t0 = time.time()
        self.t1 = None
        client = ZhihuClient()
        self.headers = {}
        client.auth(self)
        self._session = None

    @property
    def session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers=self.headers, loop=self.loop)
        return self._session

    def close(self):
        self.session.close()

    async def parse_link(self, response):
        rs = await response.json()

        if response.status == 200:
            for live in rs['data']:
                speaker = live.pop('speaker')
                speaker_id = speaker['member']['id']
                user = User.add(speaker_id=speaker_id,
                                **flatten_live_dict(speaker, SPEAKER_KEYS))
                live_dict = flatten_live_dict(live, LIVE_KEYS)
                topics = [t['name'] for t in live_dict.pop('topics')]
                tags = ' '.join(set(sum([(t['name'], t['short_name'])
                                         for t in live_dict.pop('tags')], ())))
                live_dict['speaker_id'] = user.id
                live_dict['speaker_name'] = user.name
                live_dict['topics'] = topics
                live_dict['topic_names'] = ' '.join(topics)
                live_dict['seats_taken'] = live_dict.pop('seats')['taken']
                live_dict['amount'] = live_dict.pop('fee')['amount'] / 100
                live_dict['status'] = live_dict['status'] == 'public'
                live_dict['tag_names'] = tags
                live_dict['starts_at'] = datetime.fromtimestamp(
                    live_dict['starts_at'])
                live_dict['live_suggest'] = gen_suggests(
                    live_dict['topic_names'], tags, live_dict['outline'],
                    user.name, live_dict['subject'])
                Live.add(**live_dict)

            paging = rs['paging']
            if not paging['is_end']:
                next_url = paging['next']
                return paging['next']
        else:
            print('HTTP status_code is {}'.format(response.status))

    async def fetch(self, url, max_redirect):
        tries = 0
        exception = None
        while tries < self.max_tries:
            try:
                response = await self.session.get(
                    url, allow_redirects=False)
                break
            except aiohttp.ClientError as client_error:
                exception = client_error

            tries += 1
        else:
            return

        try:
            next_url = await self.parse_link(response)
            print('{} has finished'.format(url))
            if next_url is not None:
                self.add_url(next_url, max_redirect)
        finally:
            response.release()

    async def work(self):
        try:
            while 1:
                url, max_redirect = await self.q.get()
                assert url in self.seen_urls
                await self.fetch(url, max_redirect)
                self.q.task_done()
                asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    def add_url(self, url, max_redirect=None):
        if max_redirect is None:
            max_redirect = self.max_redirect
        if url not in self.seen_urls:
            self.seen_urls.add(url)
            self.q.put_nowait((url, max_redirect))

    async def crawl(self):
        self.__workers = [asyncio.Task(self.work(), loop=self.loop)
                          for _ in range(self.max_tasks)]
        self.t0 = time.time()
        await self.q.join()
        self.t1 = time.time()
        for w in self.__workers:
            w.cancel()


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    crawler = Crawler()
    loop.run_until_complete(crawler.crawl())
    print('Finished in {:.3f} secs'.format(crawler.t1 - crawler.t0))
    crawler.close()

    loop.close()

import logging
from datetime import datetime, timedelta
from typing import TypedDict

import arrow
import gevent
import grequests
from bs4 import BeautifulSoup
from redis import Redis

from . import settings


class Serie(TypedDict):
    title: str
    url: str
    store_key: str
    check_interval: int


class SerieChapterData(TypedDict):
    chapter_number: int
    chapter_description: str
    chapter_url: str


def load_serie_data(serie: Serie, redis: Redis) -> SerieChapterData:
    return redis.hgetall(f"{serie['store_key']}-last-chapter")  # type: ignore


def save_serie_data(serie: Serie, data: SerieChapterData, redis: Redis) -> None:
    redis.hset(f"{serie['store_key']}-last-chapter", mapping=data)  # type: ignore


def check_new_chapter(serie: Serie) -> SerieChapterData:
    request = grequests.get(serie["url"], headers={"User-Agent": settings.USER_AGENT})
    page_content = request.send().response.text
    soup = BeautifulSoup(page_content, "lxml")
    chapter_element = soup.select(".wp-manga-chapter:nth-child(1) a")[0]
    chapter_description = chapter_element.text.strip()
    _, chapter_number, *_ = chapter_description.split()
    chapter_link = chapter_element.attrs["href"]
    return {
        "chapter_description": chapter_description,
        "chapter_number": int(chapter_number),
        "chapter_url": chapter_link,
    }


def make_worker(serie: Serie, redis: Redis) -> gevent.Greenlet:
    logger = logging.getLogger("manhuaplus_scraping")

    def _error_notifier(job):
        logger.error(repr(job.exception), extra={"author": serie["title"]})

    def _success_notifier(last_chapter: SerieChapterData):
        try:
            serie_data: SerieChapterData = load_serie_data(serie, redis) or {
                **last_chapter,
                "chapter_number": 0,
                "chapter_url": "",
            }

            if last_chapter["chapter_number"] <= int(serie_data["chapter_number"]):
                # logger.info("No New Chapter Available", extra={"author": serie["title"]})
                return

            logger.info(
                "**New Chapter Available "
                f"[{serie_data['chapter_number']} => {last_chapter['chapter_number']}]**\n"
                f"{last_chapter['chapter_description']} \n"
                f"{last_chapter['chapter_url']}",
                extra={"author": serie["title"]},
            )
            save_serie_data(serie, last_chapter, redis)
        except Exception as error:
            logger.error(repr(error), extra={"author": serie["title"]})

    def _wait_for_next_checking():
        try:
            now = datetime.now()
            next_checking_at = now + timedelta(minutes=serie["check_interval"])
            logger.info(
                f"Next checking {arrow.get(next_checking_at).humanize(other=now)}.",
                extra={"author": serie["title"]},
            )
            wait_time_seconds = (next_checking_at - now).total_seconds()
            gevent.sleep(wait_time_seconds)
        except Exception as error:
            logger.error(repr(error), extra={"author": serie["title"]})

    def _loop():
        while True:
            task = gevent.Greenlet(check_new_chapter, serie)
            # task.link_value(_success_notifier)
            task.link_exception(_error_notifier)
            task.start()
            task.join()
            result: SerieChapterData = task.get()
            _success_notifier(result)
            _wait_for_next_checking()

    return gevent.spawn(_loop)

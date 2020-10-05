try:
    import sqlalchemy
except ImportError:
    raise ImportError('Requires sqlalchemy')

import logging
import os
import arrow
from sqlalchemy import Column, Integer, create_engine, DateTime, UnicodeText, Text, desc, asc
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session, load_only
from sqlalchemy import event
from sqlalchemy.engine import Engine
from csirtg_smrt.constants import PYVERSION, RUNTIME_PATH, CACHE_PATH
from sqlalchemy.sql.expression import func
from pprint import pprint

TRACE = os.environ.get('CSIRTG_SMRT_SQLITE_TRACE')
CLEANUP_DAYS = os.getenv('CSIRTG_SMRT_ARCHIVER_CLEANUP_DAYS', 60)

DB_FILE = os.path.join(CACHE_PATH, 'smrt.db')
Base = declarative_base()

if PYVERSION > 2:
    basestring = (str, bytes)

logger = logging.getLogger(__name__)

if not TRACE:
    logger.setLevel(logging.ERROR)

# http://stackoverflow.com/q/9671490/7205341
SYNC = os.environ.get('SMRT_SQLITE_SYNC', 'NORMAL')

# https://www.sqlite.org/pragma.html#pragma_cache_size
CACHE_SIZE = os.environ.get('SMRT_SQLITE_CACHE_SIZE', 512000000)  # 512MB

AUTO_VACUUM = True
if os.getenv('SMRT_SQLITE_AUTO_VACUUM', '1') == '0':
    AUTO_VACUUM = False

@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode = MEMORY")
    cursor.execute("PRAGMA synchronous = {}".format(SYNC))
    cursor.execute("PRAGMA temp_store = MEMORY")
    cursor.execute("PRAGMA cache_size = {}".format(CACHE_SIZE))

    if AUTO_VACUUM:
        cursor.execute("PRAGMA auto_vacuum = INCREMENTAL")

    cursor.close()

class Indicator(Base):
    __tablename__ = "indicators"

    id = Column(Integer, primary_key=True)
    indicator = Column(UnicodeText, index=True)
    group = Column(Text)
    provider = Column(Text)
    firsttime = Column(DateTime)
    lasttime = Column(DateTime)
    tags = Column(Text)
    created_at = Column(DateTime, default=func.now())

    def __init__(self, indicator=None, group='everyone', provider=None, firsttime=None, lasttime=None, tags=None):

        self.indicator = indicator
        self.group = group
        self.provider = provider
        self.firsttime = firsttime
        self.lasttime = lasttime
        self.tags = tags

        if isinstance(group, list):
            self.group = group[0]

        if isinstance(self.tags, list):
            self.tags.sort()
            self.tags = ','.join(self.tags)

        if self.lasttime and isinstance(self.lasttime, basestring):
            self.lasttime = arrow.get(self.lasttime).datetime

        if self.firsttime and isinstance(self.firsttime, basestring):
            self.firsttime = arrow.get(self.firsttime).datetime


class Feed(Base):
    __tablename__ = "feeds"

    id = Column(Integer, primary_key=True)
    feed_name = Column(UnicodeText, index=True)
    feed_params_md5 = Column(Text)
    file_path = Column(UnicodeText)
    file_md5 = Column(Text)
    created_at = Column(DateTime, default=func.now())

    def __init__(self, feed_name=None, feed_params=None, feed_params_md5=None, file_path=None, file_md5=None):

        self.feed_name = feed_name
        self.feed_params_md5 = feed_params_md5
        self.file_path = file_path
        self.file_md5 = file_md5

        if PYVERSION == 2:
            if self.file_path and isinstance(self.file_path, str):
                self.file_path = unicode(self.file_path)

            if self.feed_name and isinstance(self.feed_name, str):
                self.feed_name = unicode(self.feed_name)
        
# http://www.pythoncentral.io/sqlalchemy-orm-examples/
class Archiver(object):
    def __init__(self, dbfile=DB_FILE, autocommit=False, dictrows=True, **kwargs):

        self.dbfile = dbfile
        self.autocommit = autocommit
        self.dictrows = dictrows
        self.path = "sqlite:///{0}".format(self.dbfile)

        echo = False
        if TRACE:
            echo = True

        # http://docs.sqlalchemy.org/en/latest/orm/contextual.html
        self.engine = create_engine(self.path, echo=echo)
        self.handle = sessionmaker(bind=self.engine)
        self.handle = scoped_session(self.handle)
        self._session = None
        self._tx_count = 0
        self.feedcache = {}

        Base.metadata.create_all(self.engine)

        logger.debug('database path: {}'.format(self.path))

        self.clear_memcache()

    def begin(self):
        self._tx_count += 1
        if self._session:
            return self._session

        self._session = self.handle()
        return self._session

    def commit(self):
        if self._tx_count == 0:
            raise Exception("commit outside of transaction")
        self._tx_count -= 1
        if self._tx_count == 0:
            self._session.commit()
            self._session = None

    def clear_memcache(self):
        self.memcache = {}
        self.memcached_provider = None

        self.clear_feedcache()

    def clear_feedcache(self):
        self.feedcache = {}

    def cache_provider(self, provider):
        if self.memcached_provider == provider:
            return

        self.memcached_provider = provider
        self.memcache = {}
        logger.info("Caching archived indicators for provider {}".format(provider))
        q = self.handle().query(Indicator) \
            .filter_by(provider=provider) \
            .order_by(asc(Indicator.lasttime), asc(Indicator.firsttime), asc(Indicator.created_at))

        q = q.options(load_only("indicator", "group", "tags", "firsttime", "lasttime"))
        q = q.yield_per(1000)
        for i in q:
            self.memcache[i.indicator] = (i.group, i.tags, i.firsttime, i.lasttime)

        logger.info("Cached provider {} in memory, {} objects".format(provider, len(self.memcache)))

    def cache_feed_from_db(self, feed_name, feed_params_md5, file_md5):
        logger.info("Querying smrtdb for feed {}".format(feed_name))
        q = self.handle().query(Feed) \
            .filter_by(feed_params_md5=feed_params_md5) \
            .order_by(desc(Feed.created_at)) 

        q = q.options(load_only('feed_params_md5', 'file_md5'))
        q = q.first()
        
        if q:
            self.feedcache[q.feed_params_md5] = q.file_md5
            logger.info("Cached feed {} in memory, {} objects".format(feed_name, len(self.feedcache)))
            return q.file_md5
        else:
            logger.info('No smrtdb entry found for feed {}'.format(feed_name))
            return None

    def is_feed_archived(self, feed_name, feed_params_md5, file_md5):
        # is current run of rule/feed already in mem and do its cache contents match that of prior run?
        if self.feedcache.get(feed_params_md5) and self.feedcache[feed_params_md5] == file_md5:
            return True
        
        # if not in mem, check if it's in smrtdb
        prior_file_md5 = self.cache_feed_from_db(feed_name, feed_params_md5, file_md5)
        
        logger.info('Checking feed {} with rule md5 {}'.format(feed_name, feed_params_md5))

        if prior_file_md5:
            logger.info('Found matching feed {} params_md5 in feedcache. Checking to see if prior file contents the same'.format(feed_name))
            if prior_file_md5 == file_md5:
                logger.info('md5 file match for feed {} on lastrun and this run'.format(feed_name))
                return True

        # If we made it here, the latest feed is diff from last run.
        logger.info('File md5s for feed {} different. Lastrun: {} this run: {}'.format(feed_name, prior_file_md5, file_md5))
        return False

    def search(self, indicator):
        tags = indicator.tags
        if isinstance(tags, list):
            tags.sort()
            tags = ','.join(tags)

        self.cache_provider(indicator.provider)

        # Is there any cached record?
        if indicator.indicator not in self.memcache:
            return False

        (ex_group, ex_tags, ex_ft, ex_lt) = self.memcache[indicator.indicator]

        # Is the indicator or tags different?
        if (ex_group, ex_tags) != (indicator.group, tags):
            return False

        timestamp_comparisons = (
            (ex_ft, indicator.firsttime),
            (ex_lt, indicator.lasttime),
        )

        for existing_ts, indicator_ts in timestamp_comparisons:
            # If the new indicator does not have this ts, ignore it
            if indicator_ts is None:
                continue
            # Cache has no old ts, but there is a new one, so we are out of date
            if existing_ts is None:
                return False
            # otherwise, compare timestamps to see if we are out of date
            if indicator_ts.replace(tzinfo=None) > existing_ts.replace(tzinfo=None):
                return False

        # If we made it here, the cached indicator is >= to the one in the feed.
        return True

    def create_feed_record(self, feed_name, feed_params_md5, file_md5, cache_file_path):
        logger.info('Creating record for feed {} rule_md5: {}, file_md5: {}'.format(feed_name, feed_params_md5, file_md5))

        f = Feed(feed_name=feed_name, feed_params_md5=feed_params_md5, file_path=cache_file_path,
                      file_md5=file_md5)

        s = self.begin()
        s.add(f)
        s.commit()

        # cache in mem
        self.feedcache[feed_params_md5] = file_md5

        return f.id

    def create(self, indicator):
        tags = indicator.tags
        if isinstance(indicator.tags, list):
            indicator.tags.sort()
            tags = ','.join(indicator.tags)

        i = indicator.indicator
        if PYVERSION == 2:
            if isinstance(i, str):
                i = unicode(i)

        i = Indicator(indicator=i, provider=indicator.provider, group=indicator.group,
                      lasttime=indicator.lasttime, tags=tags, firsttime=indicator.firsttime)

        s = self.begin()
        s.add(i)
        s.commit()

        firsttime = None
        if indicator.firsttime:
            firsttime = indicator.firsttime.replace(tzinfo=None)

        lasttime = None
        if indicator.lasttime:
            lasttime = indicator.lasttime.replace(tzinfo=None)

        self.memcache[indicator.indicator] = (
            indicator.group,
            tags,
            firsttime,
            lasttime
        )

        return i.id

    def cleanup(self, days=CLEANUP_DAYS):
        days = int(days)
        date = arrow.utcnow()
        date = date.shift(days=-days)

        s = self.begin()
        count = s.query(Indicator).filter(Indicator.created_at < date.datetime).delete()
        self.commit()

        s = self.begin()
        count = s.query(Feed).filter(Feed.created_at < date.datetime).delete()
        self.commit()

        if AUTO_VACUUM:
            logger.info('running database vacuum')
            s.execute('PRAGMA incremental_vacuum')

        return count

class NOOPArchiver:
    def __init__(self, *args, **kwargs):
        pass

    def begin(self):
        pass

    def commit(self):
        pass

    def clear_memcache(self):
        pass

    def search(self, indicator):
        return False

    def is_feed_archived(self, fetcher):
        return False

    def create(self, indicator):
        pass

    def create_feed_record(self, fetcher):
        return -1

    def cleanup(self, days=180):
        return 0

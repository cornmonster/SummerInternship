'''
database.py - this file is part of S3QL.

Copyright © 2008 Nikolaus Rath <Nikolaus@rath.org>

This work can be distributed under the terms of the GNU GPLv3.


Module Attributes:
-----------

:initsql:      SQL commands that are executed whenever a new
               connection is created.
'''

from .logging import logging, QuietError # Ensure use of custom logger class
import pymysql
import os

log = logging.getLogger(__name__)

# *modified*: comments are added
# most of commands in initsql are not necessary when using a mysql database.
# initsql = (
#            # WAL mode causes trouble with e.g. copy_tree, so we don't use it at the moment
#            # (cf. http://article.gmane.org/gmane.comp.db.sqlite.general/65243).
#            # However, if we start using it we must initiaze it *before* setting
#            # locking_mode to EXCLUSIVE, otherwise we can't switch the locking
#            # mode without first disabling WAL.

#            # synchronous pragma sets the current disk synchronization mode, which controls
#            # how aggressively SQLite will write data all the way out to physical storage.
#            # Setting Pragma synchronous to OFF means no syncs at all.
#            'PRAGMA synchronous = OFF',
#            # journal_mode pragma controls how the journal file is stored and processed.
#            # Setting Pragma journal_mode to OFF means no journal record is kept.
#            'PRAGMA journal_mode = OFF',

#            #'PRAGMA synchronous = NORMAL',
#            #'PRAGMA journal_mode = WAL',

#            # foreign_keys pragma disables foreign key constrain
#            'PRAGMA foreign_keys = OFF',
#            # locking_mode pragma being set to EXCLUSIVE means the connection will not release
#            # the lock until the end of the connection. 
#            'PRAGMA locking_mode = EXCLUSIVE',
#            # This may be a typo, it should be recursive_triggers. This pragma enables recursive
#            # triggers.
#            'PRAGMA recursize_triggers = on',
#            # This pragma sets the page size of the database.
#            'PRAGMA page_size = 4096',
#            # This pragma sets write-ahead log auto-checkpoint interval.
#            'PRAGMA wal_autocheckpoint = 25000',
#            # This pragma makes temporary tables and indices stored in a file.
#            'PRAGMA temp_store = FILE',
#            # This pragma is off, new databases are created using the latest file format.
#            'PRAGMA legacy_file_format = off',
#            )

class Connection(object):
    '''
    This class wraps an APSW connection object. It should be used instead of any
    native APSW cursors.

    It provides methods to directly execute SQL commands and creates apsw
    cursors dynamically.

    Instances are not thread safe. They can be passed between threads,
    but must not be called concurrently.

    Attributes
    ----------

    :conn:     apsw connection object
    '''

    # *modified*: changed the argument list and the way to handle the arguments
    def __init__(self):
        self.conn = pymysql.connect(host='10.2.72.85',
                                    user='user-002',
                                    password='password',
                                    db='S3QL')

    # def __init__(self, host, user, password, db):
    #     self.host = host
    #     self.user = user;
    #     self.password = password
    #     self.db = db
    #     self.conn = mysql.connector.connect(host=self.host,
    #                                         user=self.user,
    #                                         password=self.password,
    #                                         db=self.db)

        # not sure if pragmas are needed
        # cur = self.conn.cursor()

        # for s in initsql:
        #     cur.execute(s)

    def close(self):
        self.conn.close()

    # *modified* : Since there is no local db, we just return a constant
    def get_size(self):
        '''Return size of database file'''

        # if self.file is not None and self.file not in ('', ':memory:'):
        #     return os.path.getsize(self.file)
        # else:
        #     return 0

        return 0

    def query(self, *a, **kw):
        '''Return iterator over results of given SQL statement

        If the caller does not retrieve all rows the iterator's close() method
        should be called as soon as possible to terminate the SQL statement
        (otherwise it may block execution of other statements). To this end,
        the iterator may also be used as a context manager.
        '''

        return ResultSet(self.conn.cursor().execute(*a, **kw))

    # *modified*: Since mysql does't provide changes(), we need to return the cursor
    # attribute rowcount.
    def execute(self, *a, **kw):
        '''Execute the given SQL statement. Return number of affected rows '''

        # self.conn.cursor().execute(*a, **kw)
        # return self.changes()

        cur = self.conn.cursor()
        cur.execute(*a, **kw)
        self.conn.sommit()
        return cur.rowcount

    # *need to be modified*: mysql doesn't provide rowid. So we have to use
    # 'SELECT LAST_INSERT_ID()' to get the last inserted rowid
    def rowid(self, *a, **kw):
        """Execute SQL statement and return last inserted rowid"""

        self.conn.cursor().execute(*a, **kw)
        self.conn.commit()
        # return self.conn.last_insert_rowid()
        sql = 'SELECT LAST_INSERT_ID()'
        return self.getval(sql)

    def has_val(self, *a, **kw):
        '''Execute statement and check if it gives result rows'''

        res = self.conn.cursor().execute(*a, **kw)
        try:
            next(res)
        except StopIteration:
            return False
        else:
            # Finish the active SQL statement
            res.close()
            return True

    def get_val(self, *a, **kw):
        """Execute statement and return first element of first result row.

        If there is no result row, raises `NoSuchRowError`. If there is more
        than one row, raises `NoUniqueValueError`.
        """

        return self.get_row(*a, **kw)[0]

    def get_list(self, *a, **kw):
        """Execute select statement and returns result list"""

        return list(self.query(*a, **kw))

    def get_row(self, *a, **kw):
        """Execute select statement and return first row.

        If there are no result rows, raises `NoSuchRowError`. If there is more
        than one result row, raises `NoUniqueValueError`.
        """

        res = self.conn.cursor().execute(*a, **kw)
        try:
            row = next(res)
        except StopIteration:
            raise NoSuchRowError()
        try:
            next(res)
        except StopIteration:
            # Fine, we only wanted one row
            pass
        else:
            # Finish the active SQL statement
            res.close()
            raise NoUniqueValueError()

        return row

    # *need to be modified*: mysql doesn't provide rowid. Since there is no reference
    # to this method, we just comment it.
    # def last_rowid(self):
    #     """Return rowid most recently inserted in the current thread"""

    #     return self.conn.last_insert_rowid()


    # *modified*: mysql doesn't provide the methon changes(), so we don't use this method any more.
    # def changes(self):
    #     """Return number of rows affected by most recent sql statement"""

    #     return self.conn.changes()


class NoUniqueValueError(Exception):
    '''Raised if get_val or get_row was called with a query
    that generated more than one result row.
    '''

    def __str__(self):
        return 'Query generated more than 1 result row'


class NoSuchRowError(Exception):
    '''Raised if the query did not produce any result rows'''

    def __str__(self):
        return 'Query produced 0 result rows'


class ResultSet(object):
    '''
    Provide iteration over encapsulated apsw cursor. Additionally,
    `ResultSet` instances may be used as context managers to terminate
    the query before all result rows have been retrieved.
    '''

    def __init__(self, cur):
        self.cur = cur

    def __next__(self):
        return next(self.cur)

    def __iter__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.cur.close()

    def close(self):
        '''Terminate query'''

        self.cur.close()

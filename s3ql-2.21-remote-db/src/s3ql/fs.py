'''
fs.py - this file is part of S3QL.

Copyright © 2008 Nikolaus Rath <Nikolaus@rath.org>

This work can be distributed under the terms of the GNU GPLv3.
'''

from .logging import logging
from . import deltadump, CTRL_NAME, CTRL_INODE
from .backends.common import NoSuchObject, CorruptedObjectError
from .common import get_path, parse_literal, time_ns
from .database import NoSuchRowError
from .inode_cache import OutOfInodesError
from io import BytesIO
from llfuse import FUSEError
import collections
import errno
import llfuse
import math
import os
import stat
import struct
import time
import sys

# We work in bytes
CTRL_NAME = CTRL_NAME.encode('us-ascii')

# standard logger for this module
log = logging.getLogger(__name__)

# For long requests, we force a GIL release in the following interval
GIL_RELEASE_INTERVAL = 0.05

# ACL_ERRNO is the error code returned for requests that try
# to modify or access extendeda attributes associated with ACL.
# Since we currently don't know how to keep these in sync
# with permission bits, we cannot support ACLs despite having
# full support for extended attributes.
#
# What errno to use for ACL_ERRNO is a bit tricky. acl_set_fd(3)
# returns ENOTSUP if the file system does not support ACLs. However,
# this function uses the setxattr() syscall internally, and
# setxattr(3) states that ENOTSUP means that the file system does not
# support extended attributes at all. A test with btrfs mounted with
# -o noacl shows that the actual errno returned by setxattr() is
# EOPNOTSUPP. Also, some Python versions do not know about
# errno.ENOTSUPP and errno(3) says that on Linux, EOPNOTSUPP and ENOTSUP
# have the same value (despite this violating POSIX).
#
# All in all, the situation seems complicated, so we try to use
# EOPNOTSUPP with a fallback on ENOTSUP just in case.

if not hasattr(errno, 'EOPNOTSUPP'):
    ACL_ERRNO = errno.ENOTSUP
else:
    ACL_ERRNO = errno.EOPNOTSUPP

class Operations(llfuse.Operations):
    """A full-featured file system for online data storage

    This class implements low-level FUSE operations and is meant to be passed to
    llfuse.init().

    The ``access`` method of this class always gives full access, independent of
    file permissions. If the FUSE library is initialized with ``allow_other`` or
    ``allow_root``, the ``default_permissions`` option should therefore always
    be passed as well.


    Attributes:
    -----------

    :cache:       Holds information about cached blocks
    :inode_cache: A cache for the attributes of the currently opened inodes.
    :open_inodes: dict of currently opened inodes. This is used to not remove
                  the blocks of unlinked inodes that are still open.
    :upload_event: If set, triggers a metadata upload
    :failsafe: Set when backend problems are encountered. In that case, fs only
               allows read access.
    :broken_blocks: Caches information about corrupted blocks to avoid repeated (pointless)
                    attempts to retrieve them. This attribute is a dict (indexed by inodes)
                    of sets of block indices. Broken blocks are removed from the cache
                    when an inode is forgotten.

    Multithreading
    --------------

    All methods are reentrant and may release the global lock while they
    are running.


    Directory Entry Types
    ----------------------

    S3QL is quite agnostic when it comes to directory entry types. Every
    directory entry can contain other entries *and* have a associated data,
    size, link target and device number. However, S3QL makes some provisions for
    users relying on unlink()/rmdir() to fail for a directory/file. For that, it
    explicitly checks the st_mode attribute.
    """

    def __init__(self, block_cache, db, max_obj_size, inode_cache,
                 upload_event=None):
        super().__init__()

        self.inodes = inode_cache
        self.db = db
        self.upload_event = upload_event
        self.open_inodes = collections.defaultdict(lambda: 0)
        self.max_obj_size = max_obj_size
        self.cache = block_cache
        self.failsafe = False
        self.broken_blocks = collections.defaultdict(set)

        # Root inode is always open
        self.open_inodes[llfuse.ROOT_INODE] += 1

    def destroy(self):
        eprint('fs.destory is called')
        self.forget(list(self.open_inodes.items()))
        self.inodes.destroy()

    def lookup(self, id_p, name, ctx):
        eprint('fs.lookup is called')
        return self._lookup(id_p, name, ctx).entry_attributes()

    def _lookup(self, id_p, name, ctx):
        ''' This method takes 
                1. parent's inode id
                2. name of the file
                3. context
            and returns the inode id of corresponding file
        '''
        eprint('fs._lookup is called')
        log.debug('started with %d, %r', id_p, name)

        if name == CTRL_NAME:
            inode = self.inodes[CTRL_INODE]

            # Make sure the control file is only writable by the user
            # who mounted the file system (but don't mark inode as dirty)
            object.__setattr__(inode, 'uid', os.getuid())
            object.__setattr__(inode, 'gid', os.getgid())

        elif name == '.':
            inode = self.inodes[id_p]

        elif name == '..':
            id_ = self.db.get_val("SELECT parent_inode FROM contents WHERE inode=%s",
                                  (id_p,))
            inode = self.inodes[id_]

        else:
            try:
                id_ = self.db.get_val("SELECT inode FROM contents_v WHERE name=%s AND parent_inode=%s",
                                      (name, id_p))
            except NoSuchRowError:
                raise llfuse.FUSEError(errno.ENOENT)
            inode = self.inodes[id_]

        self.open_inodes[inode.id] += 1
        return inode

    def getattr(self, id_, ctx):
        # returns an instance of EntryAttributes(defined by llfuse), which
        # contains infomation about thie inode
        eprint('fs.getattr is called')
        log.debug('started with %d', id_)
        if id_ == CTRL_INODE:
            # Make sure the control file is only writable by the user
            # who mounted the file system (but don't mark inode as dirty)
            inode = self.inodes[CTRL_INODE]
            object.__setattr__(inode, 'uid', os.getuid())
            object.__setattr__(inode, 'gid', os.getgid())
            return inode.entry_attributes()

        return self.inodes[id_].entry_attributes()

    def readlink(self, id_, ctx):
        eprint('fs.readlink is called')
        log.debug('started with %d', id_)
        now_ns = time_ns()
        inode = self.inodes[id_]
        if inode.atime_ns < inode.ctime_ns or inode.atime_ns < inode.mtime_ns:
            inode.atime_ns = now_ns
        try:
            return self.db.get_val("SELECT target FROM symlink_targets WHERE inode=%s", (id_,))
        except NoSuchRowError:
            log.warning('Inode does not have symlink target: %d', id_)
            raise FUSEError(errno.EINVAL)

    def opendir(self, id_, ctx):
        eprint('fs.opendir is called')
        log.debug('started with %d', id_)
        return id_

    def readdir(self, id_, off):
        eprint('fs.readdir is called')
        log.debug('started with %d, %d', id_, off)
        if off == 0:
            off = -1

        inode = self.inodes[id_]
        if inode.atime_ns < inode.ctime_ns or inode.atime_ns < inode.mtime_ns:
            inode.atime_ns = time_ns()

        # NFS treats offsets 1 and 2 special, so we have to exclude
        # them.
        with self.db.query("SELECT name_id, name, inode FROM contents_v "
                           'WHERE parent_inode=%s AND name_id > %s ORDER BY name_id',
                           (id_, off-3)) as res:
            for (next_, name, cid_) in res:
                yield (name, self.inodes[cid_].entry_attributes(), next_+3)

    def getxattr(self, id_, name, ctx):
        eprint('fs.getxattr is called')
        log.debug('started with %d, %r', id_, name)
        # Handle S3QL commands
        if id_ == CTRL_INODE:
            if name == b's3ql_pid?':
                return ('%d' % os.getpid()).encode()

            elif name == b's3qlstat':
                return self.extstat()

            raise llfuse.FUSEError(errno.EINVAL)

        # http://code.google.com/p/s3ql/issues/detail?id=385
        elif name in (b'system.posix_acl_access',
                      b'system.posix_acl_default'):
            raise FUSEError(ACL_ERRNO)

        else:
            try:
                value = self.db.get_val('SELECT value FROM ext_attributes_v WHERE inode=%s AND name=%s',
                                          (id_, name))
            except NoSuchRowError:
                raise llfuse.FUSEError(llfuse.ENOATTR)
            return value

    def listxattr(self, id_, ctx):
        eprint('fs.listxattr is called')
        log.debug('started with %d', id_)
        names = list()
        with self.db.query('SELECT name FROM ext_attributes_v WHERE inode=%s', (id_,)) as res:
            for (name,) in res:
                names.append(name)
        return names

    def setxattr(self, id_, name, value, ctx):
        eprint('fs.setxattr is called')
        log.debug('started with %d, %r, %r', id_, name, value)

        # Handle S3QL commands
        if id_ == CTRL_INODE:
            if name == b's3ql_flushcache!':
                self.cache.clear()

            elif name == b'copy':
                try:
                    tup = parse_literal(value, (int, int))
                except ValueError:
                    log.warning('Received malformed command via control inode')
                    raise FUSEError.EINVAL()
                self.copy_tree(*tup)

            elif name == b'upload-meta':
                if self.upload_event is not None:
                    self.upload_event.set()
                else:
                    raise llfuse.FUSEError(errno.ENOTTY)

            elif name == b'lock':
                try:
                    id_ = parse_literal(value, int)
                except ValueError:
                    log.warning('Received malformed command via control inode')
                    raise FUSEError.EINVAL()
                self.lock_tree(id_)

            elif name == b'rmtree':
                try:
                    tup = parse_literal(value, (int, bytes))
                except ValueError:
                    log.warning('Received malformed command via control inode')
                    raise FUSEError.EINVAL()
                self.remove_tree(*tup)

            elif name == b'logging':
                try:
                    (lvl, modules)= parse_literal(value, (int, str))
                except (ValueError, KeyError):
                    log.warning('Received malformed command via control inode')
                    raise FUSEError.EINVAL()
                update_logging(lvl, modules.split(',') if modules else None)

            elif name == b'cachesize':
                try:
                    self.cache.cache.max_size = parse_literal(value, int)
                except ValueError:
                    log.warning('Received malformed command via control inode')
                    raise FUSEError.EINVAL()
                log.debug('updated cache size to %d bytes', self.cache.cache.max_size)

            else:
                log.warning('Received unknown command via control inode')
                raise llfuse.FUSEError(errno.EINVAL)

        # http://code.google.com/p/s3ql/issues/detail?id=385
        elif name in (b'system.posix_acl_access',
                      b'system.posix_acl_default'):
            raise FUSEError(ACL_ERRNO)

        else:
            if self.failsafe or self.inodes[id_].locked:
                raise FUSEError(errno.EPERM)

            if len(value) > deltadump.MAX_BLOB_SIZE:
                raise FUSEError(errno.EINVAL)

            self.db.execute('INSERT OR REPLACE INTO ext_attributes (inode, name_id, value) '
                            'VALUES(%s, %s, %s)', (id_, self._add_name(name), value))
            self.inodes[id_].ctime_ns = time_ns()

    def removexattr(self, id_, name, ctx):
        eprint('fs.removexattr is called')
        log.debug('started with %d, %r', id_, name)

        if self.failsafe or self.inodes[id_].locked:
            raise FUSEError(errno.EPERM)

        try:
            name_id = self._del_name(name)
        except NoSuchRowError:
            raise llfuse.FUSEError(llfuse.ENOATTR)

        changes = self.db.execute('DELETE FROM ext_attributes WHERE inode=%s AND name_id=%s',
                                  (id_, name_id))
        if changes == 0:
            raise llfuse.FUSEError(llfuse.ENOATTR)

        self.inodes[id_].ctime_ns = time_ns()

    def lock_tree(self, id0):
        '''Lock directory tree'''
        eprint('fs.lock_tree is called')
        if self.failsafe:
            raise FUSEError(errno.EPERM)

        log.debug('started with %d', id0)
        queue = [ id0 ]
        self.inodes[id0].locked = True
        processed = 0 # Number of steps since last GIL release
        stamp = time_ns() # Time of last GIL release
        gil_step = 250 # Approx. number of steps between GIL releases
        while True:
            id_p = queue.pop()
            with self.db.query('SELECT inode FROM contents WHERE parent_inode=%s',
                               (id_p,)) as res:
                for (id_,) in res:
                    self.inodes[id_].locked = True
                    processed += 1

                    if self.db.has_val('SELECT 1 FROM contents WHERE parent_inode=%s', (id_,)):
                        queue.append(id_)

            if not queue:
                break

            if processed > gil_step:
                dt = time.time() - stamp
                gil_step = max(int(gil_step * GIL_RELEASE_INTERVAL / dt), 250)
                log.debug('Adjusting gil_step to %d', gil_step)
                processed = 0
                llfuse.lock.yield_(100)
                log.debug('re-acquired lock')
                stamp = time.time()

        log.debug('finished')

    def remove_tree(self, id_p0, name0):
        '''Remove directory tree'''
        eprint('fs.remove_tree is called')
        if self.failsafe:
            raise FUSEError(errno.EPERM)

        log.debug('started with %d, %s', id_p0, name0)

        if self.inodes[id_p0].locked:
            raise FUSEError(errno.EPERM)

        id0 = self._lookup(id_p0, name0, ctx=None).id
        queue = [ id0 ] # Directories that we still need to delete
        processed = 0 # Number of steps since last GIL release
        stamp = time.time() # Time of last GIL release
        gil_step = 250 # Approx. number of steps between GIL releases
        while queue: # For every directory
            found_subdirs = False # Does current directory have subdirectories?
            id_p = queue.pop()
            if id_p in self.open_inodes:
                inval_entry = lambda x: llfuse.invalidate_entry(id_p, x)
            else:
                inval_entry = lambda x: None

            with self.db.query('SELECT name_id, inode FROM contents WHERE '
                               'parent_inode=%s', (id_p,)) as res:
                for (name_id, id_) in res:

                    if self.db.has_val('SELECT 1 FROM contents WHERE parent_inode=%s', (id_,)):
                        if not found_subdirs:
                            # When current directory has subdirectories, we must reinsert
                            # it into queue
                            found_subdirs = True
                            queue.append(id_p)
                        queue.append(id_)

                    else:
                        name = self.db.get_val("SELECT name FROM names WHERE id=%s", (name_id,))
                        inval_entry(name)
                        self._remove(id_p, name, id_, force=True)

                    processed += 1
                    if processed > gil_step:
                        # Also reinsert current directory if we need to yield to other threads
                        if not found_subdirs:
                            queue.append(id_p)
                        break

            if processed > gil_step:
                dt = time.time() - stamp
                gil_step = max(int(gil_step * GIL_RELEASE_INTERVAL / dt), 250)
                log.debug('Adjusting gil_step to %d and yielding', gil_step)
                processed = 0
                llfuse.lock.yield_(100)
                log.debug('re-acquired lock')
                stamp = time.time()


        if id_p0 in self.open_inodes:
            log.debug('invalidate_entry(%d, %r)', id_p0, name0)
            llfuse.invalidate_entry(id_p0, name0)
        self._remove(id_p0, name0, id0, force=True)

        self.forget([(id0, 1)])
        log.debug('finished')


    def copy_tree(self, src_id, target_id):
        '''Efficiently copy directory tree'''
        eprint('fs.copy_tree is called')
        if self.failsafe:
            raise FUSEError(errno.EPERM)

        log.debug('started with %d, %d', src_id, target_id)

        # To avoid lookups and make code tidier
        make_inode = self.inodes.create_inode
        db = self.db

        # First we make sure that all blocks are in the database
        self.cache.commit()
        log.debug('committed cache')

        # Copy target attributes
        # These come from setxattr, so they may have been deleted
        # without being in open_inodes
        try:
            src_inode = self.inodes[src_id]
            target_inode = self.inodes[target_id]
        except KeyError:
            raise FUSEError(errno.ENOENT)
        for attr in ('atime_ns', 'ctime_ns', 'mtime_ns', 'mode', 'uid', 'gid'):
            setattr(target_inode, attr, getattr(src_inode, attr))

        # We first replicate into a dummy inode, so that we
        # need to invalidate only once.
        now_ns = time_ns()
        tmp = make_inode(mtime_ns=now_ns, ctime_ns=now_ns, atime_ns=now_ns,
                         uid=0, gid=0, mode=0, refcount=0)

        queue = [ (src_id, tmp.id, 0) ]
        id_cache = dict()
        processed = 0 # Number of steps since last GIL release
        stamp = time.time() # Time of last GIL release
        gil_step = 250 # Approx. number of steps between GIL releases
        while queue:
            (src_id, target_id, off) = queue.pop()
            log.debug('Processing directory (%d, %d, %d)', src_id, target_id, off)
            with db.query('SELECT name_id, inode FROM contents WHERE parent_inode=%s '
                          'AND name_id > %s ORDER BY name_id', (src_id, off)) as res:
                for (name_id, id_) in res:

                    if id_ not in id_cache:
                        inode = self.inodes[id_]

                        try:
                            inode_new = make_inode(refcount=1, mode=inode.mode, size=inode.size,
                                                   uid=inode.uid, gid=inode.gid,
                                                   mtime_ns=inode.mtime_ns, atime_ns=inode.atime_ns,
                                                   ctime_ns=inode.ctime_ns, rdev=inode.rdev)
                        except OutOfInodesError:
                            log.warning('Could not find a free inode')
                            raise FUSEError(errno.ENOSPC)

                        id_new = inode_new.id

                        if inode.refcount != 1:
                            id_cache[id_] = id_new

                        db.execute('INSERT INTO symlink_targets (inode, target) '
                                   'SELECT %s, target FROM symlink_targets WHERE inode=%s',
                                   (id_new, id_))

                        db.execute('INSERT INTO ext_attributes (inode, name_id, value) '
                                   'SELECT %s, name_id, value FROM ext_attributes WHERE inode=%s',
                                   (id_new, id_))
                        db.execute('UPDATE names SET refcount = refcount + 1 WHERE '
                                   'id IN (SELECT name_id FROM ext_attributes WHERE inode=%s)',
                                   (id_,))

                        processed += db.execute('INSERT INTO inode_blocks (inode, blockno, block_id) '
                                                'SELECT %s, blockno, block_id FROM inode_blocks '
                                                'WHERE inode=%s', (id_new, id_))
                        db.execute('REPLACE INTO blocks (id, hash, refcount, size, obj_id) '
                                   'SELECT id, hash, refcount+COUNT(id), size, obj_id '
                                   'FROM inode_blocks JOIN blocks ON block_id = id '
                                   'WHERE inode = %s GROUP BY id', (id_new,))

                        if db.has_val('SELECT 1 FROM contents WHERE parent_inode=%s', (id_,)):
                            queue.append((id_, id_new, 0))
                    else:
                        id_new = id_cache[id_]
                        self.inodes[id_new].refcount += 1

                    db.execute('INSERT INTO contents (name_id, inode, parent_inode) VALUES(%s, %s, %s)',
                               (name_id, id_new, target_id))
                    db.execute('UPDATE names SET refcount=refcount+1 WHERE id=%s', (name_id,))

                    processed += 1

                    if processed > gil_step:
                        log.debug('Requeueing (%d, %d, %d) to yield lock',
                                  src_id, target_id, name_id)
                        queue.append((src_id, target_id, name_id))
                        break

            if processed > gil_step:
                dt = time.time() - stamp
                gil_step = max(int(gil_step * GIL_RELEASE_INTERVAL / dt), 250)
                log.debug('Adjusting gil_step to %d and yielding', gil_step)
                processed = 0
                llfuse.lock.yield_(100)
                log.debug('re-acquired lock')
                stamp = time.time()

        # Make replication visible
        self.db.execute('UPDATE contents SET parent_inode=%s WHERE parent_inode=%s',
                        (target_inode.id, tmp.id))
        del self.inodes[tmp.id]
        llfuse.invalidate_inode(target_inode.id)

        log.debug('finished')

    def unlink(self, id_p, name, ctx):
        eprint('fs.unlink is called')
        log.debug('started with %d, %r', id_p, name)
        if self.failsafe:
            raise FUSEError(errno.EPERM)

        inode = self._lookup(id_p, name, ctx)

        if stat.S_ISDIR(inode.mode):
            raise llfuse.FUSEError(errno.EISDIR)

        self._remove(id_p, name, inode.id)

        self.forget([(inode.id, 1)])

    def rmdir(self, id_p, name, ctx):
        eprint('fs.rmdir is called')
        log.debug('started with %d, %r', id_p, name)
        if self.failsafe:
            raise FUSEError(errno.EPERM)

        inode = self._lookup(id_p, name, ctx)

        if self.inodes[id_p].locked:
            raise FUSEError(errno.EPERM)

        if not stat.S_ISDIR(inode.mode):
            raise llfuse.FUSEError(errno.ENOTDIR)

        self._remove(id_p, name, inode.id)

        self.forget([(inode.id, 1)])

    def _remove(self, id_p, name, id_, force=False):
        '''Remove entry `name` with parent inode `id_p`

        `id_` must be the inode of `name`. If `force` is True, then
        the `locked` attribute is ignored.

        This method releases the global lock.
        '''
        eprint('fs._remove is called')
        log.debug('started with %d, %r', id_p, name)

        now_ns = time_ns()

        # Check that there are no child entries
        if self.db.has_val("SELECT 1 FROM contents WHERE parent_inode=%s", (id_,)):
            log.debug("Attempted to remove entry with children: %s",
                      get_path(id_p, self.db, name))
            raise llfuse.FUSEError(errno.ENOTEMPTY)

        if self.inodes[id_p].locked and not force:
            raise FUSEError(errno.EPERM)

        name_id = self._del_name(name)
        self.db.execute("DELETE FROM contents WHERE name_id=%s AND parent_inode=%s",
                        (name_id, id_p))

        inode = self.inodes[id_]
        inode.refcount -= 1
        inode.ctime_ns = now_ns

        inode_p = self.inodes[id_p]
        inode_p.mtime_ns = now_ns
        inode_p.ctime_ns = now_ns

        if inode.refcount == 0 and id_ not in self.open_inodes:
            log.debug('removing from cache')
            self.cache.remove(id_, 0, int(math.ceil(inode.size / self.max_obj_size)))
            # Since the inode is not open, it's not possible that new blocks
            # get created at this point and we can safely delete the inode
            self.db.execute('UPDATE names SET refcount = refcount - 1 WHERE '
                            'id IN (SELECT name_id FROM ext_attributes WHERE inode=%s)',
                            (id_,))
            self.db.execute('DELETE FROM names WHERE refcount=0 AND '
                            'id IN (SELECT name_id FROM ext_attributes WHERE inode=%s)',
                            (id_,))
            self.db.execute('DELETE FROM ext_attributes WHERE inode=%s', (id_,))
            self.db.execute('DELETE FROM symlink_targets WHERE inode=%s', (id_,))
            del self.inodes[id_]

        log.debug('finished')

    def symlink(self, id_p, name, target, ctx):
        eprint('fs.symlink is called')
        log.debug('started with %d, %r, %r', id_p, name, target)

        if self.failsafe:
            raise FUSEError(errno.EPERM)

        mode = (stat.S_IFLNK | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR |
                stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP |
                stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH)

        # Unix semantics require the size of a symlink to be the length
        # of its target. Therefore, we create symlink directory entries
        # with this size. If the kernel ever learns to open and read
        # symlinks directly, it will read the corresponding number of \0
        # bytes.
        inode = self._create(id_p, name, mode, ctx, size=len(target))
        self.db.execute('INSERT INTO symlink_targets (inode, target) VALUES(%s,%s)',
                        (inode.id, target))
        self.open_inodes[inode.id] += 1
        return inode.entry_attributes()

    def rename(self, id_p_old, name_old, id_p_new, name_new, ctx):
        eprint('fs.rename is called')
        log.debug('started with %d, %r, %d, %r', id_p_old, name_old, id_p_new, name_new)
        if name_new == CTRL_NAME or name_old == CTRL_NAME:
            log.warning('Attempted to rename s3ql control file (%s -> %s)',
                      get_path(id_p_old, self.db, name_old),
                      get_path(id_p_new, self.db, name_new))
            raise llfuse.FUSEError(errno.EACCES)


        if (self.failsafe or self.inodes[id_p_old].locked
            or self.inodes[id_p_new].locked):
            raise FUSEError(errno.EPERM)

        inode_old = self._lookup(id_p_old, name_old, ctx)

        try:
            inode_new = self._lookup(id_p_new, name_new, ctx)
        except llfuse.FUSEError as exc:
            if exc.errno != errno.ENOENT:
                raise
            else:
                target_exists = False
        else:
            target_exists = True

        if target_exists:
            self._replace(id_p_old, name_old, id_p_new, name_new,
                          inode_old.id, inode_new.id)
            self.forget([(inode_old.id, 1), (inode_new.id, 1)])
        else:
            self._rename(id_p_old, name_old, id_p_new, name_new)
            self.forget([(inode_old.id, 1)])

    def _add_name(self, name):
        '''Get id for *name* and increase refcount

        Name is inserted in table if it does not yet exist.
        '''
        eprint('fs._add_name is called')
        try:
            name_id = self.db.get_val('SELECT id FROM names WHERE name=%s', (name,))
        except NoSuchRowError:
            name_id = self.db.rowid('INSERT INTO names (name, refcount) VALUES(%s,%s)',
                                    (name, 1))
        else:
            self.db.execute('UPDATE names SET refcount=refcount+1 WHERE id=%s', (name_id,))
        return name_id

    def _del_name(self, name):
        '''Decrease refcount for *name*

        Name is removed from table if refcount drops to zero. Returns the
        (possibly former) id of the name.
        '''
        eprint('fs._del_name is called')
        (name_id, refcount) = self.db.get_row('SELECT id, refcount FROM names WHERE name=%s', (name,))

        if refcount > 1:
            self.db.execute('UPDATE names SET refcount=refcount-1 WHERE id=%s', (name_id,))
        else:
            self.db.execute('DELETE FROM names WHERE id=%s', (name_id,))

        return name_id

    def _rename(self, id_p_old, name_old, id_p_new, name_new):
        eprint('fs._rename is called')
        now_ns = time_ns()

        name_id_new = self._add_name(name_new)
        name_id_old = self._del_name(name_old)

        self.db.execute("UPDATE contents SET name_id=%s, parent_inode=%s WHERE name_id=%s "
                        "AND parent_inode=%s", (name_id_new, id_p_new,
                                               name_id_old, id_p_old))

        inode_p_old = self.inodes[id_p_old]
        inode_p_new = self.inodes[id_p_new]
        inode_p_old.mtime_ns = now_ns
        inode_p_new.mtime_ns = now_ns
        inode_p_old.ctime_ns = now_ns
        inode_p_new.ctime_ns = now_ns

    def _replace(self, id_p_old, name_old, id_p_new, name_new,
                 id_old, id_new):
        eprint('fs._replace is called')
        now_ns = time_ns()

        if self.db.has_val("SELECT 1 FROM contents WHERE parent_inode=%s", (id_new,)):
            log.info("Attempted to overwrite entry with children: %s",
                      get_path(id_p_new, self.db, name_new))
            raise llfuse.FUSEError(errno.EINVAL)

        # Replace target
        name_id_new = self.db.get_val('SELECT id FROM names WHERE name=%s', (name_new,))
        self.db.execute("UPDATE contents SET inode=%s WHERE name_id=%s AND parent_inode=%s",
                        (id_old, name_id_new, id_p_new))

        # Delete old name
        name_id_old = self._del_name(name_old)
        self.db.execute('DELETE FROM contents WHERE name_id=%s AND parent_inode=%s',
                        (name_id_old, id_p_old))

        inode_new = self.inodes[id_new]
        inode_new.refcount -= 1
        inode_new.ctime_ns = now_ns

        inode_p_old = self.inodes[id_p_old]
        inode_p_old.ctime_ns = now_ns
        inode_p_old.mtime_ns = now_ns

        inode_p_new = self.inodes[id_p_new]
        inode_p_new.ctime_ns = now_ns
        inode_p_new.mtime_ns = now_ns

        if inode_new.refcount == 0 and id_new not in self.open_inodes:
            self.cache.remove(id_new, 0,
                              int(math.ceil(inode_new.size / self.max_obj_size)))
            # Since the inode is not open, it's not possible that new blocks
            # get created at this point and we can safely delete the inode
            self.db.execute('UPDATE names SET refcount = refcount - 1 WHERE '
                            'id IN (SELECT name_id FROM ext_attributes WHERE inode=%s)',
                            (id_new,))
            self.db.execute('DELETE FROM names WHERE refcount=0')
            self.db.execute('DELETE FROM ext_attributes WHERE inode=%s', (id_new,))
            self.db.execute('DELETE FROM symlink_targets WHERE inode=%s', (id_new,))
            del self.inodes[id_new]


    def link(self, id_, new_id_p, new_name, ctx):
        eprint('fs.link is called')
        log.debug('started with %d, %d, %r', id_, new_id_p, new_name)

        if new_name == CTRL_NAME or id_ == CTRL_INODE:
            log.warning('Attempted to create s3ql control file at %s',
                      get_path(new_id_p, self.db, new_name))
            raise llfuse.FUSEError(errno.EACCES)

        now_ns = time_ns()
        inode_p = self.inodes[new_id_p]

        if inode_p.refcount == 0:
            log.warning('Attempted to create entry %s with unlinked parent %d',
                     new_name, new_id_p)
            raise FUSEError(errno.EINVAL)

        if self.failsafe or inode_p.locked:
            raise FUSEError(errno.EPERM)

        inode_p.ctime_ns = now_ns
        inode_p.mtime_ns = now_ns

        self.db.execute("INSERT INTO contents (name_id, inode, parent_inode) VALUES(%s,%s,%s)",
                        (self._add_name(new_name), id_, new_id_p))
        inode = self.inodes[id_]
        inode.refcount += 1
        inode.ctime_ns = now_ns

        self.open_inodes[inode.id] += 1
        return inode.entry_attributes()

    def setattr(self, id_, attr, fields, fh, ctx):
        """Handles FUSE setattr() requests"""
        eprint('fs.setattr is called')
        inode = self.inodes[id_]
        if fh is not None:
            assert fh == id_
        now_ns = time_ns()

        if self.failsafe or inode.locked:
            raise FUSEError(errno.EPERM)

        if fields.update_mode:
            inode.mode = attr.st_mode

        if fields.update_uid:
            inode.uid = attr.st_uid

        if fields.update_gid:
            inode.gid = attr.st_gid

        if fields.update_atime:
            inode.atime_ns = attr.st_atime_ns

        if fields.update_mtime:
            inode.mtime_ns = attr.st_mtime_ns

        inode.ctime_ns = now_ns

        # This needs to go last, because the call to cache.remove and cache.get
        # will release the global lock and may thus evict the *inode* object
        # from the cache.
        if fields.update_size:
            len_ = attr.st_size

            # Determine blocks to delete
            last_block = len_ // self.max_obj_size
            cutoff = len_ % self.max_obj_size
            total_blocks = int(math.ceil(inode.size / self.max_obj_size))

            # Adjust file size
            inode.size = len_

            # Delete blocks and truncate last one if required
            if cutoff == 0:
                self.cache.remove(id_, last_block, total_blocks)
            else:
                self.cache.remove(id_, last_block + 1, total_blocks)
                try:
                    with self.cache.get(id_, last_block) as fh:
                        fh.truncate(cutoff)
                except NoSuchObject as exc:
                    log.warning('Backend lost block %d of inode %d (id %s)!',
                             last_block, id_, exc.key)
                    raise

                except CorruptedObjectError as exc:
                    log.warning('Backend returned malformed data for block %d of inode %d (%s)',
                                last_block, id_, exc)
                    self.failsafe = True
                    self.broken_blocks[id_].add(last_block)
                    raise FUSEError(errno.EIO)


        return inode.entry_attributes()

    def mknod(self, id_p, name, mode, rdev, ctx):
        eprint('fs.mknod is called')
        log.debug('started with %d, %r', id_p, name)
        if self.failsafe:
            raise FUSEError(errno.EPERM)
        inode = self._create(id_p, name, mode, ctx, rdev=rdev)
        self.open_inodes[inode.id] += 1
        return inode.entry_attributes()

    def mkdir(self, id_p, name, mode, ctx):
        eprint('fs.mkdir is called')
        log.debug('started with %d, %r', id_p, name)
        if self.failsafe:
            raise FUSEError(errno.EPERM)
        inode = self._create(id_p, name, mode, ctx)
        self.open_inodes[inode.id] += 1
        return inode.entry_attributes()

    def extstat(self):
        '''Return extended file system statistics'''
        eprint('fs.extstat is called')
        log.debug('started')

        # Flush inode cache to get better estimate of total fs size
        self.inodes.flush()

        entries = self.db.get_val("SELECT COUNT(rowid) FROM contents")
        blocks = self.db.get_val("SELECT COUNT(id) FROM objects")
        inodes = self.db.get_val("SELECT COUNT(id) FROM inodes")
        fs_size = self.db.get_val('SELECT SUM(size) FROM inodes') or 0
        dedup_size = self.db.get_val('SELECT SUM(size) FROM blocks') or 0

        # Objects that are currently being uploaded/compressed have size == -1
        compr_size = self.db.get_val('SELECT SUM(size) FROM objects '
                                     'WHERE size > 0') or 0

        return struct.pack('QQQQQQQQQQQQ', entries, blocks, inodes, fs_size, dedup_size,
                           compr_size, self.db.get_size(), *self.cache.get_usage())

    def statfs(self, ctx):
        eprint('fs.statfs is called')
        log.debug('started')

        stat_ = llfuse.StatvfsData()

        # Get number of blocks & inodes
        blocks = self.db.get_val("SELECT COUNT(id) FROM objects")
        inodes = self.db.get_val("SELECT COUNT(id) FROM inodes")
        size = self.db.get_val('SELECT SUM(size) FROM blocks')

        if size is None:
            size = 0

        # file system block size, i.e. the minimum amount of space that can
        # be allocated. This doesn't make much sense for S3QL, so we just
        # return the average size of stored blocks.
        stat_.f_frsize = max(4096, size // blocks) if blocks != 0 else 4096

        # This should actually be the "preferred block size for doing IO.  However, `df` incorrectly
        # interprets f_blocks, f_bfree and f_bavail in terms of f_bsize rather than f_frsize as it
        # should (according to statvfs(3)), so the only way to return correct values *and* have df
        # eprint something sensible is to set f_bsize and f_frsize to the same value. (cf.
        # http://bugs.debian.org/671490)
        stat_.f_bsize = stat_.f_frsize

        # size of fs in f_frsize units. Since backend is supposed to be unlimited,
        # always return a half-full filesystem, but at least 1 TB)
        fs_size = max(2 * size, 1024 ** 4)

        stat_.f_blocks = fs_size // stat_.f_frsize
        stat_.f_bfree = (fs_size - size) // stat_.f_frsize
        stat_.f_bavail = stat_.f_bfree # free for non-root

        total_inodes = max(2 * inodes, 1000000)
        stat_.f_files = total_inodes
        stat_.f_ffree = total_inodes - inodes
        stat_.f_favail = total_inodes - inodes # free for non-root

        return stat_

    def open(self, id_, flags, ctx):
        eprint('fs.open is called')
        log.debug('started with %d', id_)
        if ((flags & os.O_RDWR or flags & os.O_WRONLY)
            and (self.failsafe or self.inodes[id_].locked)):
            raise FUSEError(errno.EPERM)

        return id_

    def access(self, id_, mode, ctx):
        '''Check if requesting process has `mode` rights on `inode`.

        This method always returns true, since it should only be called
        when permission checking is disabled (if permission checking is
        enabled, the `default_permissions` FUSE option should be set).
        '''
        # Yeah, could be a function and has unused arguments
        #pylint: disable=R0201,W0613
        eprint('fs.access is called')
        log.debug('started with %d', id_)
        return True

    def create(self, id_p, name, mode, flags, ctx):
        eprint('fs.create is called')
        log.debug('started with id_p=%d, %s', id_p, name)
        if self.failsafe:
            raise FUSEError(errno.EPERM)

        try:
            id_ = self.db.get_val("SELECT inode FROM contents_v WHERE name=%s AND parent_inode=%s",
                                  (name, id_p))
        except NoSuchRowError:
            inode = self._create(id_p, name, mode, ctx)
        else:
            self.open(id_, flags, ctx)
            inode = self.inodes[id_]

        self.open_inodes[inode.id] += 1
        return (inode.id, inode.entry_attributes())

    def _create(self, id_p, name, mode, ctx, rdev=0, size=0):
        eprint('fs._create is called')
        if name == CTRL_NAME:
            log.warning('Attempted to create s3ql control file at %s',
                     get_path(id_p, self.db, name))
            raise FUSEError(errno.EACCES)

        now_ns = time_ns()
        inode_p = self.inodes[id_p]

        if inode_p.locked:
            raise FUSEError(errno.EPERM)

        if inode_p.refcount == 0:
            log.warning('Attempted to create entry %s with unlinked parent %d',
                     name, id_p)
            raise FUSEError(errno.EINVAL)
        inode_p.mtime_ns = now_ns
        inode_p.ctime_ns = now_ns

        if inode_p.mode & stat.S_ISGID:
            gid = inode_p.gid
            if stat.S_ISDIR(mode):
                mode |= stat.S_ISGID
        else:
            gid = ctx.gid
        try:
            inode = self.inodes.create_inode(mtime_ns=now_ns, ctime_ns=now_ns, atime_ns=now_ns,
                                             uid=ctx.uid, gid=gid, mode=mode, refcount=1,
                                             rdev=rdev, size=size)
        except OutOfInodesError:
            log.warning('Could not find a free inode')
            raise FUSEError(errno.ENOSPC)

        self.db.execute("INSERT INTO contents(name_id, inode, parent_inode) VALUES(%s,%s,%s)",
                        (self._add_name(name), inode.id, id_p))

        return inode


    def read(self, fh, offset, length):
        eprint('fs.read is called')
        '''Read `size` bytes from `fh` at position `off`

        Unless EOF is reached, returns exactly `size` bytes.

        This method releases the global lock while it is running.
        '''
        #log.debug('started with %d, %d, %d', fh, offset, length)
        buf = BytesIO()
        inode = self.inodes[fh]

        # Make sure that we don't read beyond the file size. This
        # should not happen unless direct_io is activated, but it's
        # cheap and nice for testing.
        size = inode.size
        length = min(size - offset, length)

        while length > 0:
            tmp = self._readwrite(fh, offset, length=length)
            buf.write(tmp)
            length -= len(tmp)
            offset += len(tmp)

        # Inode may have expired from cache
        inode = self.inodes[fh]

        if inode.atime_ns < inode.ctime_ns or inode.atime_ns < inode.mtime_ns:
            inode.atime_ns = time_ns()

        return buf.getvalue()


    def write(self, fh, offset, buf):
        eprint('fs.write is called')
        '''Handle FUSE write requests.

        This method releases the global lock while it is running.
        '''
        #log.debug('started with %d, %d, datalen=%d', fh, offset, len(buf))

        if self.failsafe or self.inodes[fh].locked:
            raise FUSEError(errno.EPERM)

        total = len(buf)
        minsize = offset + total
        while buf:
            written = self._readwrite(fh, offset, buf=buf)
            offset += written
            buf = buf[written:]

        # Update file size if changed
        # Fuse does not ensure that we do not get concurrent write requests,
        # so we have to be careful not to undo a size extension made by
        # a concurrent write (because _readwrite() releases the global
        # lock).
        now_ns = time_ns()
        inode = self.inodes[fh]
        inode.size = max(inode.size, minsize)
        inode.mtime_ns = now_ns
        inode.ctime_ns = now_ns

        return total

    def _readwrite(self, id_, offset, *, buf=None, length=None):
        eprint('fs._readwrite is called')
        """Read or write as much as we can.

        If *buf* is None, read and return up to *length* bytes.

        If *length* is None, write from *buf* and return the number of
        bytes written.

        This is one method to reduce code duplication.

        This method releases the global lock while it is running.
        """

        # Calculate required block
        blockno = offset // self.max_obj_size
        offset_rel = offset - blockno * self.max_obj_size

        if id_ in self.broken_blocks and blockno in self.broken_blocks[id_]:
            raise FUSEError(errno.EIO)

        if length is None:
            write = True
            length = len(buf)
        elif buf is None:
            write = False
        else:
            raise TypeError("Don't know what to do!")

        # Don't try to write/read into the next block
        if offset_rel + length > self.max_obj_size:
            length = self.max_obj_size - offset_rel

        try:
            with self.cache.get(id_, blockno) as fh:
                fh.seek(offset_rel)
                if write:
                    fh.write(buf[:length])
                else:
                    buf = fh.read(length)

        except NoSuchObject as exc:
            log.error('Backend lost block %d of inode %d (id %s)!',
                      blockno, id_, exc.key)
            self.failsafe = True
            self.broken_blocks[id_].add(blockno)
            raise FUSEError(errno.EIO)

        except CorruptedObjectError as exc:
            log.error('Backend returned malformed data for block %d of inode %d (%s)',
                      blockno, id_, exc)
            self.failsafe = True
            self.broken_blocks[id_].add(blockno)
            raise FUSEError(errno.EIO)

        if write:
            return length
        elif len(buf) == length:
            return buf
        else:
            # If we can't read enough, add null bytes
            return buf + b"\0" * (length - len(buf))

    def fsync(self, fh, datasync):
        eprint('fs.fsync is called')
        log.debug('started with %d, %s', fh, datasync)
        if not datasync:
            self.inodes.flush_id(fh)

        for blockno in range(0, self.inodes[fh].size // self.max_obj_size + 1):
            self.cache.flush(fh, blockno)

    def forget(self, forget_list):
        eprint('fs.forget is called')
        log.debug('started with %s', forget_list)

        for (id_, nlookup) in forget_list:
            self.open_inodes[id_] -= nlookup

            if self.open_inodes[id_] == 0:
                del self.open_inodes[id_]
                if id_ in self.broken_blocks:
                    del self.broken_blocks[id_]

                inode = self.inodes[id_]
                if inode.refcount == 0:
                    log.debug('removing %d from cache', id_)
                    self.cache.remove(id_, 0, inode.size // self.max_obj_size + 1)
                    # Since the inode is not open, it's not possible that new blocks
                    # get created at this point and we can safely delete the inode
                    self.db.execute('UPDATE names SET refcount = refcount - 1 WHERE '
                                    'id IN (SELECT name_id FROM ext_attributes WHERE inode=%s)',
                                    (id_,))
                    self.db.execute('DELETE FROM names WHERE refcount=0 AND '
                                    'id IN (SELECT name_id FROM ext_attributes WHERE inode=%s)',
                                    (id_,))
                    self.db.execute('DELETE FROM ext_attributes WHERE inode=%s', (id_,))
                    self.db.execute('DELETE FROM symlink_targets WHERE inode=%s', (id_,))
                    del self.inodes[id_]


    def fsyncdir(self, fh, datasync):
        eprint('fs.fsyncdir is called')
        log.debug('started with %d, %s', fh, datasync)
        if not datasync:
            self.inodes.flush_id(fh)

    def releasedir(self, fh):
        eprint('fs.releasedir is called')
        log.debug('started with %d', fh)

    def release(self, fh):
        eprint('fs.release is called')
        log.debug('started with %d', fh)

    def flush(self, fh):
        eprint('fs.flush is called')
        log.debug('started with %d', fh)

def eprint(*a, **kw):
    print(*a, file=sys.stderr, **kw)

def update_logging(level, modules):
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    if level == logging.DEBUG:
        logging.disable(logging.NOTSET)
        if 'all' in modules:
            root_logger.setLevel(logging.DEBUG)
        else:
            for module in modules:
                logging.getLogger(module).setLevel(logging.DEBUG)

    else:
        logging.disable(logging.DEBUG)

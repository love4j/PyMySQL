# -*- coding: utf-8 -*-
from __future__ import print_function, absolute_import
import re
import warnings

from ._compat import range_type, text_type, PY2

from . import err


#: Regular expression for :meth:`Cursor.executemany`.
#: executemany only suports simple bulk insert.
#: You can use it to load large dataset.
RE_INSERT_VALUES = re.compile(r"""INSERT\s.+\sVALUES\s+(\(\s*%s\s*(,\s*%s\s*)*\))\s*\Z""",
                              re.IGNORECASE | re.DOTALL)


class Cursor(object):
    '''
    This is the object you use to interact with the database.
    '''

    #: Max stetement size which :meth:`executemany` generates.
    #:
    #: Max size of allowed statement is max_allowed_packet - packet_header_size.
    #: Default value of max_allowed_packet is 1048576.
    max_stmt_length = 1024000

    def __init__(self, connection):
        '''
        Do not create an instance of a Cursor yourself. Call
        connections.Connection.cursor().
        '''
        self.connection = connection
        self.description = None
        self.rownumber = 0
        self.rowcount = -1
        self.arraysize = 1
        self._executed = None
        self._result = None
        self._rows = None

    def __del__(self):
        '''
        When this gets GC'd close it.
        '''
        self.close()

    def close(self):
        '''
        Closing a cursor just exhausts all remaining data.
        '''
        conn = self.connection
        if conn is None:
            return
        try:
            while self.nextset():
                pass
        finally:
            self.connection = None

    def _get_db(self):
        if not self.connection:
            raise err.ProgrammingError("Cursor closed")
        return self.connection

    def _check_executed(self):
        if not self._executed:
            raise err.ProgrammingError("execute() first")

    def _conv_row(self, row):
        return row

    def setinputsizes(self, *args):
        """Does nothing, required by DB API."""

    def setoutputsizes(self, *args):
        """Does nothing, required by DB API."""

    def _nextset(self, unbuffered=False):
        """Get the next query set"""
        conn = self._get_db()
        current_result = self._result
        if current_result is None or current_result is not conn._result:
            return None
        if not current_result.has_next:
            return None
        conn.next_result(unbuffered=unbuffered)
        self._do_get_result()
        return True

    def nextset(self):
        return self._nextset(False)

    def _escape_args(self, args, conn):
        if isinstance(args, (tuple, list)):
            return tuple(conn.escape(arg) for arg in args)
        elif isinstance(args, dict):
            return dict((key, conn.escape(val)) for (key, val) in args.items())
        else:
            #If it's not a dictionary let's try escaping it anyways.
            #Worst case it will throw a Value error
            return conn.escape(args)

    def execute(self, query, args=None):
        '''Execute a query'''
        conn = self._get_db()

        while self.nextset():
            pass

        if PY2:  # Use bytes on Python 2 always
            encoding = conn.encoding

            def ensure_bytes(x):
                if isinstance(x, unicode):
                    x = x.encode(encoding)
                return x

            query = ensure_bytes(query)

            if args is not None:
                if isinstance(args, (tuple, list)):
                    args = tuple(map(ensure_bytes, args))
                elif isinstance(args, dict):
                    args = dict((ensure_bytes(key), ensure_bytes(val)) for (key, val) in args.items())
                else:
                    args = ensure_bytes(args)

        if args is not None:
            query = query % self._escape_args(args, conn)

        result = self._query(query)
        self._executed = query
        return result

    def executemany(self, query, args):
        """Run several data against one query

        PyMySQL can execute bulkinsert for query like 'INSERT ... VALUES (%s)'.
        In other form of queries, just run :meth:`execute` many times.
        """
        if not args:
            return

        m = RE_INSERT_VALUES.match(query)
        if m:
            q_values = m.group(1).rstrip()
            assert q_values[0] == '(' and q_values[-1] == ')'
            q_prefix = query[:m.start(1)]
            return self._do_execute_many(q_prefix, q_values, args,
                                         self.max_stmt_length,
                                         self._get_db().encoding)

        self.rowcount = sum(self.execute(query, arg) for arg in args)
        return self.rowcount

    def _do_execute_many(self, prefix, values, args, max_stmt_length, encoding):
        conn = self._get_db()
        escape = self._escape_args
        if isinstance(prefix, text_type):
            prefix = prefix.encode(encoding)
        sql = bytearray(prefix)
        args = iter(args)
        v = values % escape(next(args), conn)
        if isinstance(v, text_type):
            v = v.encode(encoding)
        sql += v
        rows = 0
        for arg in args:
            v = values % escape(arg, conn)
            if isinstance(v, text_type):
                v = v.encode(encoding)
            if len(sql) + len(v) + 1 > max_stmt_length:
                rows += self.execute(sql)
                sql = bytearray(prefix)
            else:
                sql += b','
            sql += v
        rows += self.execute(sql)
        self.rowcount = rows
        return rows

    def callproc(self, procname, args=()):
        """Execute stored procedure procname with args

        procname -- string, name of procedure to execute on server

        args -- Sequence of parameters to use with procedure

        Returns the original args.

        Compatibility warning: PEP-249 specifies that any modified
        parameters must be returned. This is currently impossible
        as they are only available by storing them in a server
        variable and then retrieved by a query. Since stored
        procedures return zero or more result sets, there is no
        reliable way to get at OUT or INOUT parameters via callproc.
        The server variables are named @_procname_n, where procname
        is the parameter above and n is the position of the parameter
        (from zero). Once all result sets generated by the procedure
        have been fetched, you can issue a SELECT @_procname_0, ...
        query using .execute() to get any OUT or INOUT values.

        Compatibility warning: The act of calling a stored procedure
        itself creates an empty result set. This appears after any
        result sets generated by the procedure. This is non-standard
        behavior with respect to the DB-API. Be sure to use nextset()
        to advance through all result sets; otherwise you may get
        disconnected.
        """
        conn = self._get_db()
        for index, arg in enumerate(args):
            q = "SET @_%s_%d=%s" % (procname, index, conn.escape(arg))
            self._query(q)
            self.nextset()

        q = "CALL %s(%s)" % (procname,
                             ','.join(['@_%s_%d' % (procname, i)
                                       for i in range_type(len(args))]))
        self._query(q)
        self._executed = q
        return args

    def fetchone(self):
        ''' Fetch the next row '''
        self._check_executed()
        if self._rows is None or self.rownumber >= len(self._rows):
            return None
        result = self._rows[self.rownumber]
        self.rownumber += 1
        return result

    def fetchmany(self, size=None):
        ''' Fetch several rows '''
        self._check_executed()
        if self._rows is None:
            return None
        end = self.rownumber + (size or self.arraysize)
        result = self._rows[self.rownumber:end]
        self.rownumber = min(end, len(self._rows))
        return result

    def fetchall(self):
        ''' Fetch all the rows '''
        self._check_executed()
        if self._rows is None:
            return None
        if self.rownumber:
            result = self._rows[self.rownumber:]
        else:
            result = self._rows
        self.rownumber = len(self._rows)
        return result

    def scroll(self, value, mode='relative'):
        self._check_executed()
        if mode == 'relative':
            r = self.rownumber + value
        elif mode == 'absolute':
            r = value
        else:
            raise err.ProgrammingError("unknown scroll mode %s" % mode)

        if not (0 <= r < len(self._rows)):
            raise IndexError("out of range")
        self.rownumber = r

    def _query(self, q):
        conn = self._get_db()
        self._last_executed = q
        conn.query(q)
        self._do_get_result()
        return self.rowcount

    def _do_get_result(self):
        conn = self._get_db()

        self.rownumber = 0
        self._result = result = conn._result

        self.rowcount = result.affected_rows
        self.description = result.description
        self.lastrowid = result.insert_id
        self._rows = result.rows

        if result.warning_count > 0:
            self._show_warnings(conn)

    def _show_warnings(self, conn):
        ws = conn.show_warnings()
        for w in ws:
            warnings.warn(w[-1], err.Warning, 4)

    def __iter__(self):
        return iter(self.fetchone, None)

    Warning = err.Warning
    Error = err.Error
    InterfaceError = err.InterfaceError
    DatabaseError = err.DatabaseError
    DataError = err.DataError
    OperationalError = err.OperationalError
    IntegrityError = err.IntegrityError
    InternalError = err.InternalError
    ProgrammingError = err.ProgrammingError
    NotSupportedError = err.NotSupportedError


class DictCursorMixin(object):
    # You can override this to use OrderedDict or other dict-like types.
    dict_type = dict

    def _do_get_result(self):
        super(DictCursorMixin, self)._do_get_result()
        fields = []
        if self.description:
            for f in self._result.fields:
                name = f.name
                if name in fields:
                    name = f.table_name + '.' + name
                fields.append(name)
            self._fields = fields

        if fields and self._rows:
            self._rows = [self._conv_row(r) for r in self._rows]

    def _conv_row(self, row):
        if row is None:
            return None
        return self.dict_type(zip(self._fields, row))


class DictCursor(DictCursorMixin, Cursor):
    """A cursor which returns results as a dictionary"""


class SSCursor(Cursor):
    """
    Unbuffered Cursor, mainly useful for queries that return a lot of data,
    or for connections to remote servers over a slow network.

    Instead of copying every row of data into a buffer, this will fetch
    rows as needed. The upside of this, is the client uses much less memory,
    and rows are returned much faster when traveling over a slow network,
    or if the result set is very big.

    There are limitations, though. The MySQL protocol doesn't support
    returning the total number of rows, so the only way to tell how many rows
    there are is to iterate over every row returned. Also, it currently isn't
    possible to scroll backwards, as only the current row is held in memory.
    """

    def _conv_row(self, row):
        return row

    def close(self):
        conn = self.connection
        if conn is None:
            return

        if self._result is not None and self._result is conn._result:
            self._result._finish_unbuffered_query()

        try:
            while self.nextset():
                pass
        finally:
            self.connection = None

    def _query(self, q):
        conn = self._get_db()
        self._last_executed = q
        conn.query(q, unbuffered=True)
        self._do_get_result()
        return self.rowcount

    def nextset(self):
        return self._nextset(unbuffered=True)

    def read_next(self):
        """ Read next row """
        return self._conv_row(self._result._read_rowdata_packet_unbuffered())

    def fetchone(self):
        """ Fetch next row """
        self._check_executed()
        row = self.read_next()
        if row is None:
            return None
        self.rownumber += 1
        return row

    def fetchall(self):
        """
        Fetch all, as per MySQLdb. Pretty useless for large queries, as
        it is buffered. See fetchall_unbuffered(), if you want an unbuffered
        generator version of this method.

        """
        return list(self.fetchall_unbuffered())

    def fetchall_unbuffered(self):
        """
        Fetch all, implemented as a generator, which isn't to standard,
        however, it doesn't make sense to return everything in a list, as that
        would use ridiculous memory for large result sets.
        """
        return iter(self.fetchone, None)

    def __iter__(self):
        return self.fetchall_unbuffered()

    def fetchmany(self, size=None):
        """ Fetch many """

        self._check_executed()
        if size is None:
            size = self.arraysize

        rows = []
        for i in range_type(size):
            row = self.read_next()
            if row is None:
                break
            rows.append(row)
            self.rownumber += 1
        return rows

    def scroll(self, value, mode='relative'):
        self._check_executed()

        if mode == 'relative':
            if value < 0:
                raise err.NotSupportedError(
                        "Backwards scrolling not supported by this cursor")

            for _ in range_type(value):
                self.read_next()
            self.rownumber += value
        elif mode == 'absolute':
            if value < self.rownumber:
                raise err.NotSupportedError(
                    "Backwards scrolling not supported by this cursor")

            end = value - self.rownumber
            for _ in range_type(end):
                self.read_next()
            self.rownumber = value
        else:
            raise err.ProgrammingError("unknown scroll mode %s" % mode)


class SSDictCursor(DictCursorMixin, SSCursor):
    """ An unbuffered cursor, which returns results as a dictionary """

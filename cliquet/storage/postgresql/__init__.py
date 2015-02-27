import contextlib
import json
import os
from collections import defaultdict

import psycopg2
import psycopg2.extras
import six
from six.moves.urllib import parse as urlparse

from pyramid import httpexceptions

from cliquet import logger
from cliquet.storage import StorageBase, exceptions, Filter
from cliquet.utils import COMPARISON


psycopg2.extensions.register_type(psycopg2.extensions.UNICODE)
psycopg2.extensions.register_type(psycopg2.extensions.UNICODEARRAY)


DEFAULT_MAX_FETCH_SIZE = 10000


class PostgreSQL(StorageBase):
    """Storage backend using PostgreSQL.

    Recommended in production (*requires PostgreSQL 9.3 or higher*).

    Enable in configuration::

        cliquet.storage_backend = cliquet.storage.postgresql

    Database location URI can be customized::

        cliquet.storage_url = postgres://user:pass@db.server.lan:5432/dbname

    Alternatively, username and password could also rely on system user ident
    or even specified in ``~/.pgpass`` (*see PostgreSQL documentation*).

    :note:

        Using a `connection pool <http://pgpool.net>`_ is highly recommended to
        boost performances and bound memory usage (*work_mem per connection*).
    """
    def __init__(self, *args, **kwargs):
        self._max_fetch_size = kwargs.pop('max_fetch_size')
        self._conn_kwargs = kwargs
        self._init_schema()

    @contextlib.contextmanager
    def connect(self):
        """Connect to the database and instantiates a cursor.
        At exiting the context manager, a COMMIT is performed on the current
        transaction if everything went well. Otherwise transaction is ROLLBACK,
        and everything cleaned up.

        If the database could not be be reached a 503 error is raised.
        """
        conn = None
        cursor = None
        try:
            conn = psycopg2.connect(**self._conn_kwargs)
            options = dict(cursor_factory=psycopg2.extras.DictCursor)
            cursor = conn.cursor(**options)
            yield cursor
            conn.commit()
        except psycopg2.Error as e:
            if cursor:
                logger.debug(cursor.query)
            logger.exception(e)
            if conn and not conn.closed:
                conn.rollback()
            if isinstance(e, psycopg2.OperationalError):
                raise httpexceptions.HTTPServiceUnavailable()
            raise
        finally:
            if cursor:
                cursor.close()
            if conn and not conn.closed:
                conn.close()

    def _init_schema(self):
        """Create PostgreSQL tables, only if not exists.

        :note:
            Relies on JSON fields, available in recent versions of PostgreSQL.
        """
        # Since indices cannot be created with IF NOT EXISTS, inspect.
        query = """
        SELECT *
          FROM pg_tables
         WHERE tablename = 'records';
        """
        with self.connect() as cursor:
            cursor.execute(query)
            exists = cursor.rowcount > 0

        if exists:
            logger.debug('Detected PostgreSQL storage tables')
            return

        # Make sure database is UTF-8
        query = """
        SELECT pg_encoding_to_char(encoding) AS encoding
          FROM pg_database
         WHERE datname =  current_database();
        """
        with self.connect() as cursor:
            cursor.execute(query)
            result = cursor.fetchone()
        encoding = result['encoding'].lower()
        assert encoding == 'utf8', 'Unexpected database encoding %s' % encoding

        # Create schema
        here = os.path.abspath(os.path.dirname(__file__))
        schema = open(os.path.join(here, 'schema.sql')).read()
        with self.connect() as cursor:
            cursor.execute(schema)
        logger.info('Created PostgreSQL storage tables')

    def flush(self):
        """Delete records from tables without destroying schema. Mainly used
        in tests suites.
        """
        query = """
        DELETE FROM deleted;
        DELETE FROM records;
        """
        with self.connect() as cursor:
            cursor.execute(query)
        logger.debug('Flushed PostgreSQL storage tables')

    def ping(self):
        query = """
        UPDATE metadata
           SET value = NOW()::TEXT
         WHERE name = 'last_heartbeat';
        """
        try:
            with self.connect() as cursor:
                cursor.execute(query)
            return True
        except psycopg2.Error:
            return False

    def collection_timestamp(self, resource, user_id):
        query = """
        SELECT resource_timestamp(%(user_id)s, %(resource_name)s)::BIGINT
            AS timestamp;
        """
        placeholders = dict(user_id=user_id, resource_name=resource.name)
        with self.connect() as cursor:
            cursor.execute(query, placeholders)
            result = cursor.fetchone()
        return result['timestamp']

    def create(self, resource, user_id, record):
        query = """
        INSERT INTO records (user_id, resource_name, data)
        VALUES (%(user_id)s, %(resource_name)s, %(data)s::json)
        RETURNING id, last_modified::BIGINT
        """
        placeholders = dict(user_id=user_id,
                            resource_name=resource.name,
                            data=json.dumps(record))

        with self.connect() as cursor:
            self._check_unicity(cursor, resource, user_id, record)

            cursor.execute(query, placeholders)
            inserted = cursor.fetchone()

        record = record.copy()
        record[resource.id_field] = inserted['id']
        record[resource.modified_field] = inserted['last_modified']
        return record

    def get(self, resource, user_id, record_id):
        query = """
        SELECT last_modified::BIGINT, data
          FROM records
         WHERE id = %(record_id)s
        """
        placeholders = dict(record_id=record_id)
        with self.connect() as cursor:
            cursor.execute(query, placeholders)
            if cursor.rowcount == 0:
                raise exceptions.RecordNotFoundError(record_id)
            else:
                result = cursor.fetchone()

        record = result['data']
        record[resource.id_field] = record_id
        record[resource.modified_field] = result['last_modified']
        return record

    def update(self, resource, user_id, record_id, record):
        query_create = """
        INSERT INTO records (id, user_id, resource_name, data)
        VALUES (%(record_id)s, %(user_id)s,
                %(resource_name)s, %(data)s::json)
        RETURNING last_modified::BIGINT
        """

        query_update = """
        UPDATE records SET data=%(data)s::json
        WHERE id = %(record_id)s
        RETURNING last_modified::BIGINT
        """
        placeholders = dict(record_id=record_id,
                            user_id=user_id,
                            resource_name=resource.name,
                            data=json.dumps(record))

        with self.connect() as cursor:
            self._check_unicity(cursor, resource, user_id, record)

            # Create or update ?
            query = "SELECT id FROM records WHERE id = %s;"
            cursor.execute(query, (record_id,))
            query = query_update if cursor.rowcount > 0 else query_create

            cursor.execute(query, placeholders)
            result = cursor.fetchone()

        record = record.copy()
        record[resource.id_field] = record_id
        record[resource.modified_field] = result['last_modified']
        return record

    def delete(self, resource, user_id, record_id):
        query = """
        WITH deleted_record AS (
            DELETE
            FROM records
            WHERE id = %(record_id)s
            RETURNING id
        )
        INSERT INTO deleted (id, user_id, resource_name)
        SELECT id, %(user_id)s, %(resource_name)s
          FROM deleted_record
        RETURNING last_modified::BIGINT;
        """
        placeholders = dict(record_id=record_id,
                            user_id=user_id,
                            resource_name=resource.name)

        with self.connect() as cursor:
            cursor.execute(query, placeholders)
            if cursor.rowcount == 0:
                raise exceptions.RecordNotFoundError(record_id)
            inserted = cursor.fetchone()

        record = {}
        record[resource.modified_field] = inserted['last_modified']
        record[resource.id_field] = record_id

        field, value = resource.deleted_mark
        record[field] = value
        return record

    def delete_all(self, resource, user_id, filters=None):
        query = """
        WITH deleted_records AS (
            DELETE
            FROM records
            WHERE user_id = %%(user_id)s
              AND resource_name = %%(resource_name)s
              %(conditions_filter)s
            RETURNING id
        )
        INSERT INTO deleted (id, user_id, resource_name)
        SELECT id, %%(user_id)s, %%(resource_name)s
          FROM deleted_records
        RETURNING id, last_modified::BIGINT;
        """
        placeholders = dict(user_id=user_id,
                            resource_name=resource.name)
        # Safe strings
        safeholders = defaultdict(six.text_type)

        if filters:
            safe_sql, holders = self._format_conditions(resource, filters)
            safeholders['conditions_filter'] = safe_sql
            placeholders.update(**holders)

        with self.connect() as cursor:
            cursor.execute(query % safeholders, placeholders)
            results = cursor.fetchmany(self._max_fetch_size)

        records = []
        for result in results:
            record = dict([resource.deleted_mark])
            record[resource.id_field] = result['id']
            record[resource.modified_field] = result['last_modified']
            records.append(record)

        return records

    def get_all(self, resource, user_id, filters=None, sorting=None,
                pagination_rules=None, limit=None, include_deleted=False):
        query = """
        WITH collection_filtered AS (
            SELECT id, last_modified::BIGINT, data
              FROM records
             WHERE user_id = %%(user_id)s
               AND resource_name = %%(resource_name)s
               %(conditions_filter)s
        ),
        total_filtered AS (
            SELECT COUNT(*) AS count
              FROM collection_filtered
        ),
        fake_deleted AS (
            SELECT %%(deleted_mark)s::json AS data
        ),
        filtered_deleted AS (
            SELECT id, last_modified::BIGINT, fake_deleted.data AS data
              FROM deleted, fake_deleted
             WHERE user_id = %%(user_id)s
               AND resource_name = %%(resource_name)s
               %(conditions_filter)s
               %(deleted_limit)s
        ),
        all_records AS (
            SELECT * FROM filtered_deleted
             UNION ALL
            SELECT * FROM collection_filtered
        ),
        paginated_records AS (
            SELECT DISTINCT id
              FROM all_records
              %(pagination_rules)s
        )
        SELECT total_filtered.count AS count_total, a.*
          FROM paginated_records AS p JOIN all_records AS a ON (a.id = p.id),
               total_filtered
          %(sorting)s
          %(pagination_limit)s;
        """
        deleted_mark = json.dumps(dict([resource.deleted_mark]))

        # Unsafe strings escaped by PostgreSQL
        placeholders = dict(user_id=user_id,
                            resource_name=resource.name,
                            deleted_mark=deleted_mark)

        # Safe strings
        safeholders = defaultdict(six.text_type)

        if filters:
            safe_sql, holders = self._format_conditions(resource, filters)
            safeholders['conditions_filter'] = 'AND %s' % safe_sql
            placeholders.update(**holders)

        if not include_deleted:
            safeholders['deleted_limit'] = 'LIMIT 0'

        if sorting:
            sql, holders = self._format_sorting(resource, sorting)
            safeholders['sorting'] = sql
            placeholders.update(**holders)

        if pagination_rules:
            sql, holders = self._format_pagination(resource, pagination_rules)
            safeholders['pagination_rules'] = 'WHERE %s' % sql
            placeholders.update(**holders)

        if limit:
            assert isinstance(limit, six.integer_types)  # validated in view
            safeholders['pagination_limit'] = 'LIMIT %s' % limit

        with self.connect() as cursor:
            cursor.execute(query % safeholders, placeholders)
            results = cursor.fetchmany(self._max_fetch_size)

        if not len(results):
            return [], 0

        count_total = results[0]['count_total']

        records = []
        for result in results:
            record = result['data']
            record[resource.id_field] = result['id']
            record[resource.modified_field] = result['last_modified']
            records.append(record)

        return records, count_total

    def _format_conditions(self, resource, filters, prefix='filters'):
        """Format the filters list in SQL, with placeholders for safe escaping.

        :note:
            All conditions are combined using AND.

        :note:
            Field name and value are escaped as they come from HTTP API.

        :returns: A SQL string with placeholders, and a dict mapping
            placeholders to actual values.
        :rtype: tuple
        """
        operators = {
            COMPARISON.EQ: '=',
            COMPARISON.NOT: '<>',
        }

        conditions = []
        holders = {}
        for i, filtr in enumerate(filters):
            value = filtr.value

            if filtr.field == resource.id_field:
                sql_field = 'id'
            elif filtr.field == resource.modified_field:
                sql_field = 'last_modified::BIGINT'
            else:
                # Safely escape field name
                field_holder = '%s_field_%s' % (prefix, i)
                holders[field_holder] = filtr.field
                # JSON operator ->> retrieves values as text.
                # If field is missing, we default to ''.
                sql_field = "coalesce(data->>%%(%s)s, '')" % field_holder
                # JSON-ify the native value (e.g. True -> 'true')
                value = json.dumps(filtr.value).strip('"')

            # Safely escape value
            value_holder = '%s_value_%s' % (prefix, i)
            holders[value_holder] = value

            sql_operator = operators.setdefault(filtr.operator, filtr.operator)
            cond = "%s %s %%(%s)s" % (sql_field, sql_operator, value_holder)
            conditions.append(cond)

        safe_sql = ' AND '.join(conditions)
        return safe_sql, holders

    def _format_pagination(self, resource, pagination_rules):
        """Format the pagination rules in SQL, with placeholders for
        safe escaping.

        :note:
            All rules are combined using OR.

        :note:
            Field names are escaped as they come from HTTP API.

        :returns: A SQL string with placeholders, and a dict mapping
            placeholders to actual values.
        :rtype: tuple
        """
        rules = []
        placeholders = {}

        for i, rule in enumerate(pagination_rules):
            prefix = 'rules_%s' % i
            safe_sql, holders = self._format_conditions(resource, rule,
                                                        prefix=prefix)
            rules.append(safe_sql)
            placeholders.update(**holders)

        safe_sql = ' OR '.join(['(%s)' % r for r in rules])
        return safe_sql, placeholders

    def _format_sorting(self, resource, sorting):
        """Format the sorting in SQL, with placeholders for safe escaping.

        :note:
            Field names are escaped as they come from HTTP API.

        :returns: A SQL string with placeholders, and a dict mapping
            placeholders to actual values.
        :rtype: tuple
        """
        sorts = []
        holders = {}
        for i, sort in enumerate(sorting):

            if sort.field == resource.id_field:
                sql_field = 'id'
            elif sort.field == resource.modified_field:
                sql_field = 'last_modified'
            else:
                field_holder = 'sort_field_%s' % i
                holders[field_holder] = sort.field
                sql_field = 'data->>%%(%s)s' % field_holder

            sql_direction = 'ASC' if sort.direction > 0 else 'DESC'
            sql_sort = "%s %s" % (sql_field, sql_direction)
            sorts.append(sql_sort)

        safe_sql = 'ORDER BY %s' % (', '.join(sorts))
        return safe_sql, holders

    def _check_unicity(self, cursor, resource, user_id, record):
        """Check that no existing record (in the current transaction snapshot)
        violates the resource unicity rules.
        """
        unique_fields = resource.mapping.Options.unique_fields
        if not unique_fields:
            return

        query = """
        SELECT id
          FROM records
         WHERE user_id = %%(user_id)s
           AND resource_name = %%(resource_name)s
           AND (%(conditions_filter)s)
           AND %(condition_record)s
         LIMIT 1;
        """
        safeholders = dict()
        placeholders = dict(user_id=user_id,
                            resource_name=resource.name)

        # Transform each field unicity into a query condition.
        filters = []
        for field in unique_fields:
            value = record.get(field)
            if value is None:
                continue
            sql, holders = self._format_conditions(
                resource,
                [Filter(field, value, COMPARISON.EQ)],
                prefix=field)
            filters.append(sql)
            placeholders.update(**holders)

        # All unique fields are empty in record
        if not filters:
            return

        safeholders['conditions_filter'] = ' OR '.join(filters)

        # If record is in database, then exclude it of unicity check.
        record_id = record.get(resource.id_field)
        if record_id:
            sql, holders = self._format_conditions(
                resource,
                [Filter(resource.id_field, record_id, COMPARISON.NOT)])
            safeholders['condition_record'] = sql
            placeholders.update(**holders)
        else:
            safeholders['condition_record'] = 'TRUE'

        cursor.execute(query % safeholders, placeholders)
        if cursor.rowcount > 0:
            result = cursor.fetchone()
            existing = self.get(resource, user_id, result['id'])
            raise exceptions.UnicityError(field, existing)


def load_from_config(config):
    settings = config.registry.settings

    max_fetch_size = settings.get('storage.max_fetch_size',
                                  DEFAULT_MAX_FETCH_SIZE)

    uri = settings.get('storage.url', '')
    uri = urlparse.urlparse(uri)
    conn_kwargs = dict(host=uri.hostname,
                       port=uri.port,
                       user=uri.username,
                       password=uri.password,
                       database=uri.path[1:] if uri.path else '')
    # Filter specified values only, to preserve PostgreSQL defaults
    conn_kwargs = dict([(k, v) for k, v in conn_kwargs.items() if v])

    return PostgreSQL(max_fetch_size=int(max_fetch_size), **conn_kwargs)
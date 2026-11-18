"""PostgreSQL source connector (psycopg2). Connects to the address that was validated (``hostaddr``), keeping the host name for TLS
verification, in a read-only session with a statement timeout and UTC time zone."""
from __future__ import annotations

from typing import Iterator, List

import config
from connectors import netguard
from connectors.database import ConnectorError, DatabaseConnector, MAX_TABLES, SourceColumn, SourceTable, describe_type


class PostgresConnector(DatabaseConnector):
    name, label, default_port = "postgres", "PostgreSQL", 5432

    def validate_config(self, cfg):
        out = super().validate_config(cfg)
        out.setdefault("schema", "public")
        return out

    def open(self, cfg, secrets):
        import psycopg2
        try:
            ips = netguard.resolve_allowed(cfg["host"], cfg["port"])
        except netguard.HostNotAllowed as exc:
            raise ConnectorError("HOST_NOT_ALLOWED", str(exc)) from None
        except ConnectionError as exc:
            raise ConnectorError("HOST_UNRESOLVED", str(exc), transient=True) from None
        try:
            conn = psycopg2.connect(
                host=cfg["host"], hostaddr=ips[0], port=cfg["port"], dbname=cfg["database"], user=cfg["user"],
                password=(secrets or {}).get("password") or None, sslmode=cfg.get("ssl_mode", "require"),
                connect_timeout=int(config.CONNECTION_TIMEOUT_SECONDS), application_name="querysure",
                options=(f"-c statement_timeout={config.CONNECTION_QUERY_TIMEOUT_SECONDS * 1000} -c lock_timeout=5000 "
                         "-c default_transaction_read_only=on -c TimeZone=UTC"))
            conn.set_session(readonly=True, autocommit=True)
            return conn
        except Exception as exc:  # noqa: BLE001
            raise self.classify(exc) from None

    def server_version(self, conn) -> str:
        return "PostgreSQL " + str(self._fetch(conn, "SHOW server_version")[0][0]).split(" ")[0]

    def tables(self, conn, cfg) -> List[SourceTable]:
        schema = cfg.get("schema") or "public"
        cols = self._fetch(
            conn, "SELECT c.table_name, c.column_name, c.data_type, t.table_type FROM information_schema.columns c "
                  "JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                  "WHERE c.table_schema = %s AND t.table_type IN ('BASE TABLE', 'VIEW') "
                  "ORDER BY c.table_name, c.ordinal_position", (schema,))
        pks = self._fetch(
            conn, "SELECT tc.table_name, kcu.column_name FROM information_schema.table_constraints tc "
                  "JOIN information_schema.key_column_usage kcu ON kcu.constraint_name = tc.constraint_name "
                  "AND kcu.table_schema = tc.table_schema AND kcu.table_name = tc.table_name "
                  "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s "
                  "ORDER BY tc.table_name, kcu.ordinal_position", (schema,))
        out, by_name = [], {}
        for table, column, dtype, ttype in cols:
            t = by_name.get(table)
            if t is None:
                if len(by_name) >= MAX_TABLES:
                    continue
                t = by_name[table] = SourceTable(name=table, is_view=ttype == "VIEW")
                out.append(t)
            sqlite_type, kind = describe_type(dtype)
            t.columns.append(SourceColumn(column, dtype, sqlite_type, kind))
        for table, column in pks:
            if table in by_name:
                by_name[table].primary_key.append(column)
        return out

    def qualified(self, cfg, table) -> str:
        return f"{self.q(cfg.get('schema') or 'public')}.{self.q(table.name)}"

    def _stream(self, conn, sql, batch) -> Iterator[list]:
        cur = conn.cursor(name="qs_" + __import__("uuid").uuid4().hex[:12], withhold=True)   # server-side cursor (works in autocommit)
        try:
            cur.itersize = batch
            cur.execute(sql)
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    return
                yield rows
        finally:
            cur.close()

    def classify(self, exc) -> ConnectorError:
        import psycopg2
        text = str(exc).lower()
        if isinstance(exc, psycopg2.errors.QueryCanceled):
            return ConnectorError("QUERY_TIMEOUT", "The query took too long and was cancelled.")
        if "password authentication failed" in text or "authentication failed" in text or "no password supplied" in text:
            return ConnectorError("AUTH_FAILED", "The server rejected the user name or password.")
        if "permission denied" in text:
            return ConnectorError("PERMISSION_DENIED", "The user is not allowed to read this object.")
        if "does not exist" in text and "database" in text:
            return ConnectorError("DATABASE_NOT_FOUND", "The database does not exist.")
        if "ssl" in text or "tls" in text:
            return ConnectorError("TLS_FAILED", "A secure connection could not be established.")
        if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
            return ConnectorError("CONNECT_FAILED", "The database could not be reached.", transient=True)
        return ConnectorError("QUERY_FAILED", "The database could not run the query.")

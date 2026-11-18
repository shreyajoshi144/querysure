"""MySQL / MariaDB source connector (PyMySQL, pure Python). Connects to the validated address; with ssl_mode verify-full it connects
by host name instead (the certificate must match it), leaving the usual small DNS-rebinding window between check and connect."""
from __future__ import annotations

from typing import Iterator, List

import config
from connectors import netguard
from connectors.database import ConnectorError, DatabaseConnector, MAX_TABLES, SourceColumn, SourceTable, describe_type


class MysqlConnector(DatabaseConnector):
    name, label, default_port, quote_char = "mysql", "MySQL / MariaDB", 3306, "`"

    def open(self, cfg, secrets):
        import pymysql
        try:
            ips = netguard.resolve_allowed(cfg["host"], cfg["port"])
        except netguard.HostNotAllowed as exc:
            raise ConnectorError("HOST_NOT_ALLOWED", str(exc)) from None
        except ConnectionError as exc:
            raise ConnectorError("HOST_UNRESOLVED", str(exc), transient=True) from None
        mode = cfg.get("ssl_mode", "require")
        try:
            conn = pymysql.connect(
                host=cfg["host"] if mode == "verify-full" else ips[0], port=cfg["port"], user=cfg["user"],
                password=(secrets or {}).get("password") or "", database=cfg["database"],
                connect_timeout=int(config.CONNECTION_TIMEOUT_SECONDS), read_timeout=config.CONNECTION_QUERY_TIMEOUT_SECONDS,
                write_timeout=config.CONNECTION_QUERY_TIMEOUT_SECONDS, autocommit=True, charset="utf8mb4",
                ssl_disabled=mode == "disable", ssl_verify_cert=mode == "verify-full", ssl_verify_identity=mode == "verify-full",
                client_flag=0)
            cur = conn.cursor()
            for stmt in ("SET SESSION TRANSACTION READ ONLY", "SET time_zone = '+00:00'",
                         f"SET SESSION max_execution_time = {config.CONNECTION_QUERY_TIMEOUT_SECONDS * 1000}"):
                try:
                    cur.execute(stmt)
                except Exception:  # noqa: BLE001 - e.g. MariaDB has no max_execution_time; the read timeout still applies
                    pass
            cur.close()
            return conn
        except Exception as exc:  # noqa: BLE001
            raise self.classify(exc) from None

    def server_version(self, conn) -> str:
        return "MySQL " + str(self._fetch(conn, "SELECT VERSION()")[0][0])

    def tables(self, conn, cfg) -> List[SourceTable]:
        cols = self._fetch(
            conn, "SELECT c.TABLE_NAME AS t, c.COLUMN_NAME AS c, c.DATA_TYPE AS d, t.TABLE_TYPE AS tt "
                  "FROM information_schema.COLUMNS c JOIN information_schema.TABLES t "
                  "ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME "
                  "WHERE c.TABLE_SCHEMA = DATABASE() AND t.TABLE_TYPE IN ('BASE TABLE', 'VIEW') "
                  "ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION")
        pks = self._fetch(
            conn, "SELECT TABLE_NAME AS t, COLUMN_NAME AS c FROM information_schema.KEY_COLUMN_USAGE "
                  "WHERE TABLE_SCHEMA = DATABASE() AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY TABLE_NAME, ORDINAL_POSITION")
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

    def _stream(self, conn, sql, batch) -> Iterator[list]:
        import pymysql.cursors
        cur = conn.cursor(pymysql.cursors.SSCursor)   # unbuffered: rows are fetched as they are read
        try:
            cur.execute(sql)
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    return
                yield rows
        finally:
            cur.close()

    def classify(self, exc) -> ConnectorError:
        import pymysql
        code = exc.args[0] if getattr(exc, "args", None) and isinstance(exc.args[0], int) else None
        text = str(exc).lower()
        if code in (1045, 1044) or "access denied" in text:
            return ConnectorError("AUTH_FAILED", "The server rejected the user name or password.")
        if code == 1049:
            return ConnectorError("DATABASE_NOT_FOUND", "The database does not exist.")
        if code in (1142, 1143, 1227):
            return ConnectorError("PERMISSION_DENIED", "The user is not allowed to read this object.")
        if code in (3024, 1317) or "max_execution_time" in text or "timed out" in text and "read" in text:
            return ConnectorError("QUERY_TIMEOUT", "The query took too long and was cancelled.")
        if "ssl" in text or "tls" in text or "certificate" in text:
            return ConnectorError("TLS_FAILED", "A secure connection could not be established.")
        if isinstance(exc, (pymysql.err.OperationalError, pymysql.err.InterfaceError, OSError)):
            return ConnectorError("CONNECT_FAILED", "The database could not be reached.", transient=True)
        return ConnectorError("QUERY_FAILED", "The database could not run the query.")

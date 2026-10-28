import socket
import dotenv

dotenv.load_dotenv = lambda *a, **k: False
_real_connect = socket.socket.connect


def restricted_connect(self, address):
    if (
        isinstance(address, tuple)
        and address[0] in ("127.0.0.1", "localhost")
        and address[1] in (55957, 15173, 18080)
    ):
        return _real_connect(self, address)
    if isinstance(address, str) and address.startswith("/tmp/"):
        return _real_connect(self, address)
    raise RuntimeError("External network blocked in company operations fixture")


socket.socket.connect = restricted_connect
import psycopg2

_connect = psycopg2.connect


def restricted_db(*a, **kw):
    from psycopg2.extensions import parse_dsn

    d = parse_dsn(a[0]) if a and a[0] else {}
    d.update(kw)
    if (
        str(d.get("host")) not in ("127.0.0.1", "localhost")
        or str(d.get("port")) != "55957"
    ):
        raise RuntimeError("Only the disposable fixture Postgres is allowed")
    return _connect(*a, **kw)


psycopg2.connect = restricted_db

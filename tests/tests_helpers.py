from conftest import PG_URL, _reset_postgres


def reset_database():
    if PG_URL:
        _reset_postgres()

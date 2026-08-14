import psycopg

from config import DATABASE_URL


def get_conn():
    return psycopg.connect(DATABASE_URL)

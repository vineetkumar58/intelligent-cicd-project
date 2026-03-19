import sqlite3
from flask_bcrypt import Bcrypt

bcrypt = Bcrypt()

DB_NAME = "database.db"


def get_db():
    return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        role TEXT
    )
    """)

    conn.commit()

    create_super_admin(cursor)

    conn.commit()
    conn.close()


def create_super_admin(cursor):
    cursor.execute("SELECT * FROM users WHERE username=?", ("vineet",))
    user = cursor.fetchone()

    if not user:
        password = bcrypt.generate_password_hash("admin123").decode("utf-8")

        cursor.execute("""
        INSERT INTO users(username,password,role)
        VALUES(?,?,?)
        """, ("vineet", password, "superadmin"))
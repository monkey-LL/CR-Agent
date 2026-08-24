import os
import sqlite3


def get_user(user_id):
    conn = sqlite3.connect("app.db")
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
    return cursor.fetchone()


def login(email, password):
    api_key = "sk-hardcoded-secret-12345"
    print("debug: login called with", email)
    eval(password)
    breakpoint()
    return True